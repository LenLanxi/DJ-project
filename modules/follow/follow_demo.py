"""Chinese local demo. Synthetic input by default; --camera enables webcam."""
import argparse
import math
from dataclasses import asdict
import json
from pathlib import Path
import threading
import time
import sys

import cv2
import numpy as np

from r3_follow.core import FollowController, State
from r3_follow import __version__ as SOURCE_VERSION
from r3_follow.vision import MarkerTracker, synthetic_frame
from r3_follow.diagnostics import ExperimentSession, follow_hint, recognition_hint

BASE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
APP_VERSION = "0.14-side-view-candidate"


class CameraReader:
    """One camera owner; UI/watchdog never wait for blocking camera.read()."""
    def __init__(self, index):
        self.index = index
        self.lock = threading.Lock()
        self.latest = None
        self.error = "摄像头连接中"
        self.metadata = None
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        cap = None
        try:
            for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY):
                if self.done.is_set():
                    return
                cap = cv2.VideoCapture(self.index, backend)
                if cap.isOpened():
                    break
                cap.release()
            if not cap or not cap.isOpened():
                raise RuntimeError("无法打开摄像头，请检查占用或权限")
            def reported(prop):
                value = float(cap.get(prop))
                return value if math.isfinite(value) and value != 0 else None
            with self.lock:
                self.metadata = {
                    "backend": cap.getBackendName(),
                    "reported_width": reported(cv2.CAP_PROP_FRAME_WIDTH),
                    "reported_height": reported(cv2.CAP_PROP_FRAME_HEIGHT),
                    "reported_fps": reported(cv2.CAP_PROP_FPS),
                    "reported_exposure": reported(cv2.CAP_PROP_EXPOSURE),
                    "reported_auto_exposure": reported(cv2.CAP_PROP_AUTO_EXPOSURE),
                    "note": "驱动报告值；零/不可用记为未知，非零值也不保证支持，未修改相机设置"}
            seq = 0
            failures = 0
            while not self.done.is_set():
                ok, frame = cap.read()
                now = time.monotonic()
                if not ok:
                    failures += 1
                    with self.lock:
                        self.error = "摄像头未返回新画面"
                    if failures >= 100:
                        break
                    self.done.wait(.05)
                    continue
                seq += 1
                failures = 0
                with self.lock:
                    self.latest = (seq, now, frame)
                    self.error = ""
        except Exception as error:
            with self.lock:
                self.error = str(error)
        finally:
            if cap is not None:
                cap.release()

    def snapshot(self):
        with self.lock:
            return self.latest, self.error


def gui(camera_index=None, smoke=False, diagnostics=False, tello_video=False,
        recovery_window=3.0, test_driver=None, detection_budget_ms=None):
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(f"R3地面标记识别 · {SOURCE_VERSION}" if tello_video else
               f"R3跟随模块 · 电脑模拟版 v{APP_VERSION}")
    root.geometry(f"{min(1020, root.winfo_screenwidth()-60)}x{min(760, root.winfo_screenheight()-100)}")
    root.minsize(680, 420)
    # Fixed action bar plus a scrollable workspace. No camera/desktop changes.
    action_bar = ttk.Frame(root, padding=4)
    action_bar.pack(side="bottom", fill="x")
    scroll_host = ttk.Frame(root)
    scroll_host.pack(fill="both", expand=True)
    canvas = tk.Canvas(scroll_host, highlightthickness=0)
    vertical = ttk.Scrollbar(scroll_host, orient="vertical", command=canvas.yview)
    horizontal = ttk.Scrollbar(scroll_host, orient="horizontal", command=canvas.xview)
    vertical.pack(side="right", fill="y")
    horizontal.pack(side="bottom", fill="x")
    canvas.pack(side="left", fill="both", expand=True)
    canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    workspace = ttk.Frame(canvas)
    canvas.create_window((0,0), window=workspace, anchor="nw")
    workspace.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
    controller, tracker = FollowController(
        recovery_window=recovery_window, max_request_rate=.8, require_geometry=True), MarkerTracker(
            processing_budget_ms=detection_budget_ms)
    experiment = ExperimentSession(controller)
    if tello_video:
        from r3_follow.tello_adapter import TelloCameraReader
        reader = TelloCameraReader()
    else:
        reader = CameraReader(camera_index) if camera_index is not None else None
    mode = "Tello视频（仅接收画面）" if tello_video else (
        "电脑摄像头" if reader else "生成的标记画面（非真实相机）")
    heading = "Tello地面标记识别 · 不会发送飞行动作" if tello_video else "仅模拟运动请求 · 未连接无人机"
    ttk.Label(workspace, text=heading, font=("Microsoft YaHei", 17, "bold")).pack(pady=8)
    ttk.Label(workspace, text=f"输入：{mode}  ｜  目标：DICT_4X4_50 编号0").pack()
    if tello_video:
        ttk.Label(workspace, text=f"识别源码：{SOURCE_VERSION} ｜ 预算：{detection_budget_ms} ms ｜ 本轮不测跟随控制").pack()
    content = ttk.Frame(workspace)
    content.pack(fill="both", expand=True, padx=12, pady=8)
    picture = ttk.Label(content)
    picture.grid(row=0, column=0, rowspan=8, sticky="n")
    panel = ttk.Frame(content, padding=12)
    panel.grid(row=0, column=1, sticky="nw")
    state_var, obs_var, req_var, note_var = (tk.StringVar() for _ in range(4))
    ttk.Label(panel, textvariable=state_var, font=("Microsoft YaHei", 14, "bold")).pack(anchor="w", pady=6)
    ttk.Label(panel, textvariable=obs_var, wraplength=285).pack(anchor="w", pady=6)
    ttk.Label(panel, textvariable=req_var, wraplength=285, font=("Microsoft YaHei", 11)).pack(anchor="w", pady=6)
    labels = {State.STOPPED: "已停止", State.PAUSED: "已暂停", State.TRACKING: "模拟跟随中",
              State.RECOVERING: "短暂漏检，零请求等待恢复",
              State.LOST: "目标丢失，需重新开始", State.STALE: "画面过期，需重新开始",
              State.INVALID: "输入无效，需重新开始"}
    action_var = tk.StringVar(value="")
    log_dir = BASE / "logs"
    log_dir.mkdir(exist_ok=True)
    logfile = log_dir / f"demo_{time.time_ns()}.jsonl"
    experiment.context = {"app_version": APP_VERSION, "source": mode,
                          "session_log": logfile.name, "automated_gui_check": test_driver is not None,
                          "control_profile": controller.status()["control_profile"],
                          "max_request_rate": controller.max_request_rate}
    log = logfile.open("w", encoding="utf-8")
    last_log = [0.0, None]
    logged_state = [None]
    camera_logged = [False]
    session_stats = {"automatic_recoveries": 0, "recovery_timeouts": 0,
                     "actions": {}, "nontracking_nonzero_requests": 0}

    def record(event, **extra):
        row = {"time_monotonic": time.monotonic(), "event": event, "source": mode,
               "observation": asdict(controller.observation) if controller.observation else None,
               "request": asdict(controller.last_request), "reference_size": controller.reference_size,
               "detector_details": tracker.last_diagnostics,
               "app_version": APP_VERSION, "source_version": SOURCE_VERSION,
               "detection_budget_ms": detection_budget_ms,
               "automated_gui_check": test_driver is not None,
               "session_log": logfile.name,
               "recovery_window_seconds": controller.recovery_window,
               "max_request_rate": controller.max_request_rate,
               "control_profile": controller.status()["control_profile"],
               "distance_guard": controller.distance_guard.status(),
               "last_stop_reason": controller.last_stop_reason, **extra}
        if logged_state[0] != controller.state:
            previous = logged_state[0]
            if previous == State.RECOVERING and controller.state == State.TRACKING:
                session_stats["automatic_recoveries"] += 1
            if controller.last_stop_reason == "recovery_timeout_requires_start" and previous != controller.state:
                session_stats["recovery_timeouts"] += 1
            log.write(json.dumps({**row, "event": "state_transition",
                                 "previous_state": previous}, ensure_ascii=False) + "\n")
            logged_state[0] = controller.state
        if event in ("start", "stop", "pause", "resume", "calibrate"):
            actions = session_stats["actions"]
            actions[event] = actions.get(event, 0) + 1
        if (controller.state != State.TRACKING and
                (controller.last_request.right or controller.last_request.forward)):
            session_stats["nontracking_nonzero_requests"] += 1
        log.write(json.dumps(row, ensure_ascii=False) + "\n")
        log.flush()

    record("session_start")

    def summarize_log():
        # Reporting only runs with control stopped, after a short test or window close.
        # Failure to write a report must not prevent the window from closing.
        from log_report import write_report
        try:
            return write_report(logfile)
        except Exception as exc:
            print(f"Log report failed: {type(exc).__name__}: {exc}")
            return None

    def command(name):
        now = time.monotonic()
        if not experiment.allowed(name):
            action_var.set("正在进行识别短测；结束后才能操作跟随")
            record("action_blocked", action=name)
            return
        if name == "stop" and experiment.testing:
            report = experiment.finish(now, aborted=True)
            diag_var.set(f"短测已提前结束，未完成：logs/{report.name}")
            record("short_test_aborted", report=report.name)
            summarize_log()
        if name == "calibrate":
            ok, _ = controller.calibrate(now)
            action_var.set("参考大小已记录；它不是实际距离" if ok else "记录失败：请先暂停，把标记完整正对摄像头，再记录")
        elif name in ("start", "resume"):
            ok, _ = getattr(controller, name)(now)
            action_var.set("" if ok else "操作未成功：检查参考大小和目标；丢失后请点开始")
        else:
            getattr(controller, name)()
            action_var.set("")
        record(name)

    action_buttons = {}
    for caption, name in [("1 记录参考大小", "calibrate"), ("2 开始", "start"),
                          ("暂停", "pause"), ("恢复", "resume"), ("停止", "stop")]:
        button = ttk.Button(action_bar, text=caption, command=lambda n=name: command(n))
        button.pack(side="left", expand=True, fill="x", padx=2)
        action_buttons[name] = button
    recovery_var = tk.StringVar(value=f"{recovery_window:g}")
    ttk.Label(panel, text="漏检等待秒数（停止时可改）").pack(anchor="w")
    recovery_choice = ttk.Combobox(panel, textvariable=recovery_var,
                                  values=("3", "1.5"), state="readonly", width=8)
    recovery_choice.pack(anchor="w")
    def change_recovery(_event=None):
        if experiment.set_recovery(float(recovery_var.get())):
            record("recovery_setting_changed")
        recovery_var.set(f"{controller.recovery_window:g}")
    recovery_choice.bind("<<ComboboxSelected>>", change_recovery)
    ttk.Label(panel, textvariable=note_var, wraplength=285).pack(pady=8, anchor="w")
    ttk.Label(panel, textvariable=action_var, wraplength=285).pack(anchor="w")
    ttk.Label(panel, text="参考大小：标记正对镜头，在希望保持的位置记录。\n右移/前进均为示意值，不是速度。\n侧转或变形时暂停前后调整；漏检立即全部归零。", wraplength=285).pack(anchor="w")
    x_var, size_var = tk.DoubleVar(value=.5), tk.DoubleVar(value=.25)
    visible_var, freeze_var, wrong_var = tk.BooleanVar(value=True), tk.BooleanVar(value=False), tk.BooleanVar(value=False)
    if not reader:
        controls = ttk.LabelFrame(workspace, text="模拟输入操作", padding=8)
        controls.pack(fill="x", padx=12, pady=5)
        ttk.Label(controls, text="左右位置").grid(row=0, column=0)
        ttk.Scale(controls, from_=.1, to=.9, variable=x_var, length=280).grid(row=0, column=1)
        ttk.Label(controls, text="标记大小").grid(row=1, column=0)
        ttk.Scale(controls, from_=.10, to=.38, variable=size_var, length=280).grid(row=1, column=1)
        ttk.Checkbutton(controls, text="显示标记（取消=遮挡）", variable=visible_var).grid(row=0, column=2, padx=15)
        ttk.Checkbutton(controls, text="冻结新画面（测试过期）", variable=freeze_var).grid(row=1, column=2)
        ttk.Checkbutton(controls, text="改成编号1（不是跟随对象）", variable=wrong_var).grid(row=2, column=2)
        def brief_loss():
            visible_var.set(False)
            root.after(150, lambda: visible_var.set(True) if not closing[0] else None)
        ttk.Button(controls, text="模拟短暂漏检（0.15秒）", command=brief_loss).grid(row=3, column=2, pady=4)
    ttk.Label(workspace, text="仅电脑实验：短暂漏检立即归零，稳定重识别可恢复；超时/卡帧/停止需手动开始。不保存画面。", wraplength=970).pack(pady=5)
    ttk.Label(workspace, text=f"v{APP_VERSION} · 本窗口日志：{logfile}", wraplength=970).pack()
    current = {"seq": -1, "synthetic_seq": 0, "image": np.full((480, 640, 3), 245, np.uint8)}
    closing = [False]
    diag_var = tk.StringVar(value="短测：5秒准备，站立10秒，再缓慢左右移动10秒。无需点击跟随开始。")
    if diagnostics:
        diag_panel = ttk.LabelFrame(workspace, text="识别诊断 · 不保存画面（下方看不全时拖动右侧滚动条）", padding=6)
        diag_panel.pack(fill="x", padx=12)
        def begin_short_test():
            path = log_dir / f"diagnostic_{time.time_ns()}.jsonl"
            if not experiment.begin(path, time.monotonic()):
                return
            action_var.set("")
            record("short_test_start", diagnostic_log=path.name)
            diag_var.set("准备5秒：退到测试位置，标记正对摄像头。")
        ttk.Button(diag_panel, text="开始20秒识别短测", command=begin_short_test).pack(anchor="w")
        ttk.Label(diag_panel, textvariable=diag_var, wraplength=960).pack(anchor="w")

    def refresh():
        if closing[0]:
            return
        now = time.monotonic()
        frame_diagnostic = None
        sample, error = (reader.snapshot() if reader else (None, ""))
        if reader and not camera_logged[0] and getattr(reader, "metadata", None):
            record("camera_info", camera_info=reader.metadata)
            camera_logged[0] = True
        if not reader and not freeze_var.get():
            current["synthetic_seq"] += 1
            sample = (current["synthetic_seq"], now, synthetic_frame(x_var.get(), size_var.get(),
                      visible_var.get(), 1 if wrong_var.get() else 0))
        if sample is not None and sample[0] != current["seq"]:
            seq, acquired, frame = sample
            current["seq"] = seq
            current["image"] = frame.copy()
            detect_start = time.perf_counter()
            try:
                obs = tracker.detect(frame, seq, acquired,
                                     "tello" if tello_video else "webcam" if reader else "synthetic")
                controller.observe(obs)
                frame_diagnostic = dict(frame_id=seq, found=obs.found,
                    reason=obs.reason or ("detected" if obs.found else "not_found"),
                    size_px=obs.size_ratio*obs.width if obs.found else None,
                    detector_details=tracker.last_diagnostics)
            except Exception as exc:
                controller.observe(None)
                action_var.set(f"识别输入错误：{exc}")
                frame_diagnostic = dict(frame_id=seq, found=False,
                    reason=f"input_error:{type(exc).__name__}", size_px=None,
                    detector_details={})
            frame_diagnostic.update(detection_ms=(time.perf_counter()-detect_start)*1000,
                                    frame_age_ms=(time.monotonic()-acquired)*1000)
            tracker.last_diagnostics.update(detection_ms=frame_diagnostic['detection_ms'],
                frame_age_ms=frame_diagnostic['frame_age_ms'])
        request = controller.tick(time.monotonic())
        if diagnostics and frame_diagnostic is not None:
            record("frame", frame_diagnostic=frame_diagnostic)  # metadata only, includes input errors
        state_var.set("状态：" + labels[controller.state])
        obs = controller.observation
        age = None if obs is None else time.monotonic() - obs.acquired_at
        fresh = obs is not None and 0 <= age <= controller.max_age
        framing_hint = recognition_hint(obs, tracker.last_diagnostics, fresh) if reader else ""
        note_var.set(follow_hint(controller, fresh) + ("\n" + framing_hint if framing_hint else ""))
        if controller.state == State.LOST:
            state_var.set("状态：跟随已停止（此前丢失目标）")
        if diagnostics and experiment.testing:
            test_now = time.monotonic()
            test = experiment.short_test
            if frame_diagnostic is not None:
                test.record(test_now, state=controller.state, **frame_diagnostic)
            phase, remaining = test.phase(test_now)
            diag_var.set(f"{phase}：剩余{remaining}秒。只看绿框，不用按开始或重新校准。")
            if phase == "完成":
                report = experiment.finish(test_now)
                record("short_test_completed", report=report.name)
                summary = summarize_log()
                diag_var.set("短测完成，汇总已保存到logs/reports；双击09可打开。" if summary else
                             f"短测完成：logs/{report.name}；自动汇总失败，原始日志仍保留。")
        for name, button in action_buttons.items():
            button.configure(state="normal" if not tello_video and experiment.allowed(name) else "disabled")
        recovery_choice.configure(state="readonly" if (
            controller.state == State.STOPPED and not experiment.testing) else "disabled")
        obs_var.set(error or ("尚无画面" if obs is None else
                    f"帧 {obs.frame_id} ｜ {'新画面' if fresh else '已过期'}\n" +
                    (f"编号0：中心({obs.center_x:.0f}, {obs.center_y:.0f})\n大小比例 {obs.size_ratio:.3f}，边长约{obs.size_ratio*obs.width:.0f}像素" if obs.found else "当前未找到唯一编号0")))
        req_var.set(f"模拟请求\n左右：{request.right:+.2f}（正值=右）\n前后：{request.forward:+.2f}（正值=前）")
        if tello_video:
            req_var.set(f"本轮仅检测标记\n识别耗时：{tracker.last_diagnostics.get('detection_ms', 0):.1f} ms\n预算：{detection_budget_ms} ms")
        image = current["image"].copy()
        if fresh and obs.found:
            points = np.array(obs.corners, dtype=np.int32)
            cv2.polylines(image, [points], True, (0, 180, 0), 2)
            cv2.circle(image, (round(obs.center_x), round(obs.center_y)), 5, (0, 0, 255), -1)
        cv2.line(image, (image.shape[1]//2, 0), (image.shape[1]//2, image.shape[0]-1), (190, 190, 190), 1)
        image = cv2.resize(image, (640, 480))
        raw = b"P6\n640 480\n255\n" + cv2.cvtColor(image, cv2.COLOR_BGR2RGB).tobytes()
        photo = tk.PhotoImage(data=raw, format="PPM")
        picture.configure(image=photo)
        picture.image = photo
        if now - last_log[0] > .5 or last_log[1] != controller.state:
            record("tick")
            last_log[:] = [now, controller.state]
        root.after(50, refresh)

    def close():
        closing[0] = True
        controller.stop()
        record("window_closed_stop")
        if reader:
            reader.done.set()
        if experiment.testing:
            report = experiment.finish(time.monotonic(), aborted=True)
            record("short_test_aborted", report=report.name)
        record("session_summary", summary=session_stats)
        logfile.with_suffix(".summary.json").write_text(
            json.dumps({"version": APP_VERSION, "log": logfile.name, "source": mode,
                        "automated_gui_check": test_driver is not None, **session_stats},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        log.close()
        summarize_log()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    refresh()
    if test_driver is not None:
        root.after(100, lambda: test_driver(root, controller, experiment, command,
                                          action_buttons, recovery_choice, close,
                                          begin_short_test if diagnostics else None))
    if smoke:
        root.after(700, close)
    root.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--camera", type=int, default=None)
    sources.add_argument("--tello-video", action="store_true",
                         help="Ground preview: connect/streamon only; never send flight commands")
    parser.add_argument("--scenario", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--detection-budget-ms", type=float, default=None,
                        help="Cooperative detection budget; use 120 for current flight-profile recognition")
    parser.add_argument("--recovery-window", type=float, choices=(1.5, 3.0), default=3.0,
                        help="Computer experiment only; core integration default remains manual restart")
    args = parser.parse_args()
    if args.scenario:
        from r3_follow.scenario import run
        dest = BASE / "logs" / "scenario.jsonl"
        rows = run(dest)
        print(f"PASS {len(rows)} scenario checkpoints: {dest}")
    else:
        gui(args.camera, args.smoke, args.diagnostics, args.tello_video, args.recovery_window,
            detection_budget_ms=args.detection_budget_ms)
