"""Opt-in, metadata-only short test; never stores images or moves a device."""
import json
import statistics
from collections import Counter
from pathlib import Path

from .core import State


STOP_REASONS = {
    "recovery_timeout_requires_start": "超过恢复等待时间",
    "ambiguous_duplicate_id": "出现多个编号0，无法确定跟随对象",
    "frame_gap": "新画面中断超过0.5秒",
    "frame_stale": "没有及时收到有效新画面",
    "invalid_input": "画面或识别输入无效",
    "stopped": "手动停止或测试结束",
    "target_lost": "启动时没有有效目标",
    "not_found": "未找到目标",
    "paused_or_inactive": "手动暂停",
}


class ExperimentSession:
    """Shared guard for GUI actions and tests, separate from device control."""
    def __init__(self, controller, context=None):
        self.controller = controller
        self.context = context or {}
        self.short_test = None

    @property
    def testing(self):
        return self.short_test is not None and not self.short_test.closed

    def allowed(self, action):
        return not self.testing or action == "stop"

    def begin(self, path, now):
        if self.testing:
            return False
        self.controller.stop()
        self.short_test = ShortTest(path, now, {
            **self.context, "recovery_window_seconds": self.controller.recovery_window})
        return True

    def finish(self, now, aborted=False):
        if not self.testing:
            return None
        self.controller.stop()
        return self.short_test.finish(now, aborted)

    def set_recovery(self, seconds):
        if (self.testing or self.controller.state != State.STOPPED
                or seconds not in (1.5, 3.0)):
            return False
        self.controller.recovery_window = float(seconds)
        return True


def recognition_hint(observation, details, fresh):
    """Current-frame advice only; never infer a physical cause from a miss."""
    if not fresh or observation is None:
        return ""
    if observation.reason == "ambiguous_duplicate_id":
        return "画面里出现多个编号0，请只保留要跟随的一个。"
    if not observation.found:
        return "当前没认出标记。先把黑框和白边完整放在画面中间，固定一下；原因还不能确定。"
    if details.get("target_quality", {}).get("near_image_edge"):
        return "标记太靠近画面边缘，请向中间移动，并留出完整白边。"
    return ""


def follow_hint(controller, fresh):
    obs = controller.observation
    visible = fresh and obs is not None and obs.found
    if controller.state == State.INVALID:
        return "输入无效；请检查画面，恢复后点击开始"
    if controller.state == State.TRACKING:
        if controller.distance_guard.blocked:
            if controller.distance_guard.reason == "waiting_for_stable_shape":
                return "标记已正对镜头，正在确认稳定；前后请求暂时为零，左右仍按当前目标计算。"
            return ("图案倾斜、变形或角点不完整：前后请求保持零，左右仍按当前目标计算。"
                    "请正对摄像头并保持片刻，前后调整才会恢复。")
        return "正在计算模拟运动请求；未控制无人机"
    if controller.state == State.RECOVERING:
        return (f"请求保持零：{controller.recovery_window:g}秒内同一标记连续3帧"
                "且稳定至少0.15秒才恢复")
    if controller.state == State.PAUSED:
        return "跟随已暂停；目标有效时可点击恢复"
    if controller.state in (State.LOST, State.STALE):
        cause = STOP_REASONS.get(controller.last_stop_reason, controller.last_stop_reason)
        return (("已重新识别编号0；跟随仍停止，请点击开始" if visible else
                 "跟随已停止；等待新画面中的编号0，再点击开始")
                + (f"\n停止原因：{cause}" if cause else ""))
    if controller.reference_size is None:
        return "跟随未启动；展示标记后点击记录参考大小"
    return "跟随已停止；点击开始才会重新跟随"


class ShortTest:
    def __init__(self, path, now, context=None):
        self.path = Path(path)
        self.stream = self.path.open("w", encoding="utf-8")
        self.start = now + 5.0
        self.end = self.start + 20.0
        self.rows = []
        self.closed = False
        self.previous_seq = None
        self.context = context or {}

    def phase(self, now):
        if now < self.start:
            return "准备", max(1, int(self.start - now + .999))
        if now < self.start + 10:
            return "站立不动", max(1, int(self.start + 10 - now + .999))
        if now < self.end:
            return "缓慢左右移动", max(1, int(self.end - now + .999))
        return "完成", 0

    def record(self, now, *, frame_id, found, reason, size_px, detection_ms,
               frame_age_ms, state, detector_details=None):
        if self.closed or not self.start <= now < self.end:
            return
        if self.previous_seq is not None and frame_id <= self.previous_seq:
            return
        skipped = None if self.previous_seq is None else frame_id-self.previous_seq-1
        row = dict(elapsed_seconds=now-self.start, phase=self.phase(now)[0],
                   frame_id=frame_id, found=bool(found), reason=reason,
                   size_px=size_px, detection_ms=detection_ms,
                   frame_age_ms=frame_age_ms, skipped_capture_frames=skipped,
                   control_state=str(state))
        row["detector_details"] = detector_details or {}
        row["test_context"] = self.context
        self.previous_seq = frame_id
        self.rows.append(row)
        self.stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.stream.flush()

    def finish(self, now, aborted=False):
        if self.closed:
            return self.path.with_suffix(".txt")
        self.closed = True
        self.stream.close()
        lines = ["20秒人体佩戴标记短测", "提前结束" if aborted else "测试结束",
                 "只记录实际处理画面的检测信息，不保存图像；未控制无人机。",
                 "统计不包含程序未处理的摄像头帧；成功比例不等于跟随成功率。"]
        if self.context:
            lines.append("测试来源：" + json.dumps(self.context, ensure_ascii=False))
        for name in ("站立不动", "缓慢左右移动"):
            rows = [r for r in self.rows if r['phase'] == name]
            n = len(rows)
            found = sum(r['found'] for r in rows)
            lines.append(f"{name}：处理{n}帧，识别到{found}帧，漏检{n-found}帧" +
                         (f"，检出比例{found/n:.1%}" if n else "；无可用画面，不能判断"))
            if n:
                runs, began = [], None
                for row in rows:
                    if not row["found"] and began is None:
                        began = row["elapsed_seconds"]
                    elif row["found"] and began is not None:
                        runs.append(row["elapsed_seconds"] - began)
                        began = None
                phase_end = min(now-self.start, 10 if name == "站立不动" else 20)
                if began is not None:
                    runs.append(max(0, phase_end-began))
                duration = max(0, min(10, now-self.start-(0 if name == "站立不动" else 10)))
                lines.append(f"  漏检段数{len(runs)}，最长约{max(runs, default=0):.2f}秒；"
                             f"处理速率约{n/duration:.1f}帧/秒" if duration else "  无有效测试时长")
                methods = Counter(r["detector_details"].get("detector_method", "unknown")
                                  for r in rows if r["found"])
                sizes = [r["size_px"] for r in rows if r["found"] and r["size_px"] is not None]
                others = sum(bool(r["detector_details"].get("ignored_other_markers")) for r in rows)
                lines.append(f"  出现其他编号的处理帧数：{others}；成功识别路径：{dict(methods)}")
                if sizes:
                    lines.append(f"  成功识别时边长中位数{statistics.median(sizes):.1f}像素"
                                 f"（范围{min(sizes):.1f}～{max(sizes):.1f}）")
                lines.append(f"  最高识别耗时{max(r['detection_ms'] for r in rows):.1f}毫秒；"
                             f"最高本机画面年龄{max(r['frame_age_ms'] for r in rows):.1f}毫秒")
        lines.append("阶段名称按计时分配，不证明实际动作；漏检时段按采样估算。")
        if aborted:
            lines.append("本次未完成，不作通过判定。")
        lines.extend([f"原始诊断记录：{self.path.name}",
                      "画面年龄从本机读取完成计算，不能测出摄像头内部缓冲延迟。"])
        report = self.path.with_suffix(".txt")
        report.write_text("\n".join(lines), encoding="utf-8-sig")
        return report
