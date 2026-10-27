"""Transition-only console notices. Slow output never holds a control lock."""
from queue import Queue, Full, Empty
import threading


class ConsoleNotices:
    def __init__(self, sink=None):
        self.sink = sink if sink is not None else lambda text: print(text, flush=True)
        # State snapshots coalesce; do not replay an obsolete start after stop.
        self.queue = Queue(maxsize=1)
        self.closed = threading.Event()
        self.worker = None
        self.dropped = 0
        self.errors = 0

    def start(self):
        if self.worker is not None or self.closed.is_set():
            return
        self.worker = threading.Thread(target=self._run, daemon=True, name="follow-notices")
        self.worker.start()

    def submit(self, text):
        if self.closed.is_set():
            return
        try:
            self.queue.put_nowait(text)
        except Full:
            try:
                self.queue.get_nowait()
            except Empty:
                pass
            self.dropped += 1
            try:
                self.queue.put_nowait(text)
            except Full:
                self.dropped += 1

    def _run(self):
        while not self.closed.is_set() or not self.queue.empty():
            try:
                text = self.queue.get(timeout=.05)
            except Empty:
                continue
            try:
                self.sink(text)
            except Exception:
                self.errors += 1  # A broken stdout must not affect following.

    def close(self, wait=True):
        self.closed.set()
        if wait and self.worker is not None and self.worker is not threading.current_thread():
            self.worker.join(timeout=.1)


def describe_status(*, state, active, visible, closed, pending_resume,
                    cleanup_pending, reason, budget_wait=False, identity_wait=False):
    if cleanup_pending:
        return "跟随已停用；归零或控制权释放尚未确认，请检查；不能继续启动。"
    if closed:
        return "跟随程序已关闭。"
    if active and state == "tracking":
        return "跟随已开启；目标有效。当前请求为零时也可能是在保持位置。"
    if active and state == "recovering_target":
        if identity_wait:
            return "目标唯一性检查未完成，运动请求已归零；等待连续确认唯一目标后恢复。"
        return ("识别处理超时，运动请求已归零；等待连续稳定识别后恢复。" if budget_wait else
                "暂时没有可靠目标，运动请求已归零；等待连续稳定识别后恢复。")
    if state == "paused":
        return ("跟随已暂停，正在交接；动作完成并满足条件后才恢复。" if pending_resume else
                "跟随已暂停；确认目标有效后，可调用恢复。")
    cause = {
        "frame_stale": "有效识别画面过期",
        "frame_gap": "画面间隔过长",
        "vision_result_expired": "识别完成时画面已过期",
        "frame_unavailable_or_expired": "输入画面缺失或过期",
        "frame_unavailable": "输入画面不可用",
        "recovery_timeout_requires_start": "等待目标恢复超时",
        "ambiguous_duplicate_id": "发现同编号的多个标记",
        "ambiguous_scan_budget_exceeded": "目标唯一性检查未完成",
        "control_lost": "控制权已被其他任务接管",
        "device_unavailable_or_not_airborne": "设备不可用或不在飞行状态",
        "explicit_stop": "已收到停止指令",
        "not_started": "尚未开始",
        "image_size_changed_requires_new_session": "画面分辨率已改变，需关闭并重新建立跟随服务",
    }.get(reason, reason or "尚未开始")
    target = "目标现在可见，但跟随没有运行" if visible else "当前没有新鲜、可靠的目标"
    return f"跟随未运行（{cause}）；{target}。排除原因后需重新开始；不会因绿框出现自行启动。"
