"""终极跨组联调：真机画面 → 3组识别/测速 → 4组离线AI → 底层拦截判定。

真机地面测试（拆桨叶，不起飞），20 秒自动收工：
  1. 连接飞机，启动 av 图传，等真实画面（720x960）
  2. 真帧持续发布进 SDK（controller.publish_frame），3组端口读到的就是真画面
  3. 3组 MarkerTracker 逐帧识别 ArUco：found / 标记 id / size_ratio / 中心像素
  4. 3组 FollowController 用同一帧算出速度（与 service.control_once 同一换算公式），
     再走 SDK 的 update_velocity —— 地面必被 [拦截] 未起飞
  5. 3组 follow_integration.start_follow() 也会尝试一次：地面会被飞行状态门禁挡住
     （connected_airborne_state_required），这是安全设计，不是故障
  6. 4组离线 AI（demo_text.Demo）发一条动作指令：控制权交接 + 动作同样被 [拦截]
  7. 控制台打印 FPS、识别结果、拦截流水；到点释放控制权、关图传、断开

用法
    python run_final_real_sim.py --props-removed
    python run_final_real_sim.py --props-removed --seconds 30 --marker-id 0 --no-window
    python run_final_real_sim.py --self-check          # 离线自检，不连飞机
"""
import argparse
import os
import sys
import threading
import time

import drone_sdk._driver as driver
from drone_sdk import ai_bridge, controller
from drone_sdk.contract import FrameResult

ROOT = os.path.dirname(os.path.abspath(__file__))
for extra in ('modules/ai', 'modules/follow'):
    candidate = os.path.join(ROOT, extra)
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from run_ground_test import (EventTail, StatusPoller, Throttle, close_output,
                             disable_quick_edit, emit, enable_ansi, format_snapshot,
                             install_taps)


class FrameBridge(threading.Thread):
    """真机帧 → SDK：3组/4组读到的就是同一份真画面。"""

    def __init__(self, fps=30):
        super().__init__(daemon=True, name='frame-bridge')
        self.interval = 1 / float(fps)
        self._halt = threading.Event()
        self.published = 0
        self.last_frame = None

    def run(self):
        while not self._halt.wait(self.interval):
            try:
                frame = driver.get_frame()
            except Exception:
                frame = None
            if frame is None:
                continue
            self.published += 1
            self.last_frame = frame
            try:
                controller.publish_frame(FrameResult(frame, self.published, time.time(), False))
            except Exception as exc:
                emit('错误', 'publish_frame 失败：%s' % exc)

    def stop(self):
        self._halt.set()


def clamp30(value):
    return int(max(-30, min(30, value)))


class FollowCalculator:
    """直接用 3组的 FollowController 算速度；不启动服务、不碰飞控。"""

    def __init__(self, marker_id):
        from r3_follow.core import FollowController, State
        self.State = State
        self.controller = FollowController(target_id=marker_id)
        self.reference_ready = False
        self.last_channels = None
        self.last_reason = 'waiting_for_marker'

    def update(self, observation):
        """喂一帧检测结果，返回 (channels 或 None, 说明)。"""
        if observation is None or not observation.found:
            self.last_reason = 'no_marker'
            return None, self.last_reason
        now = time.monotonic()
        self.controller.observe(observation)
        if not self.reference_ready:
            ok, reason = self.controller.calibrate(now)
            if not ok:
                self.last_reason = 'calibrate:%s' % reason
                return None, self.last_reason
            self.reference_ready = True
            emit('3组', '参考尺寸校准完成 reference_size=%.3f' % self.controller.reference_size)
        if self.controller.state != self.State.TRACKING:
            ok, reason = self.controller.start(now)
            if not ok:
                self.last_reason = 'start:%s' % reason
                return None, self.last_reason
        motion = self.controller.tick(now)
        channels = (clamp30(round(motion.right * 100)), clamp30(round(motion.forward * 100)), 0, 0)
        self.last_channels = channels
        self.last_reason = motion.reason or self.controller.state
        return channels, self.last_reason


def send_like_follow(channels, grant):
    """完全按 3组 control_once 的方式把速度送到 SDK 边界（地面会被拦下）。"""
    if grant is None:
        return None
    result = controller.update_velocity('follow', channels[0], channels[1], 0, 0,
                                        control_epoch=grant.control_epoch,
                                        command_epoch=grant.command_epoch)
    return result


FOLLOW_REASON_HINTS = {
    'connected_airborne_state_required': '3组的飞行状态门禁：地面不启动跟随，属预期安全行为',
    'follow_calibration_failed': '校准失败：镜头前需要有完整可见的 ArUco 码',
    'follow_no_fresh_frame': '没有新鲜画面（图传未出帧或帧过旧）',
    'follow_no_frame_source': '没有画面来源',
    'busy': '控制权被占用',
}


def try_start_follow():
    """尝试让 3组的 follow_integration 真正启动一次（地面会被飞行状态门禁挡住）。"""
    emit('跟随', '尝试 controller.start_follow()（3组服务真实入口）')
    result = controller.start_follow(timeout=1.5)
    if result.ok:
        emit('跟随', '跟随已启动（意外：地面竟然通过了门禁）')
        controller.stop_follow()
    else:
        reason = result.reason or 'unknown'
        emit('跟随', '未启动：%s' % reason)
        emit('说明', FOLLOW_REASON_HINTS.get(reason, reason))
    return result


def four_group_action(drone):
    """4组离线 AI：用他们自己的 Demo 处理一句中文指令。"""
    try:
        from demo_text import Demo
    except Exception as exc:
        emit('错误', '导入 4组 demo_text 失败：%s' % exc)
        return
    emit('4组', '离线 AI 解析指令：前进30厘米')
    try:
        reply = Demo(drone).handle('前进30厘米')
    except Exception as exc:
        emit('错误', '4组 Demo 抛异常 %s: %s' % (type(exc).__name__, exc))
        return
    emit('4组', '回复：%s' % reply)


def draw_overlay(frame, observation, fps, channels, reason):
    import cv2
    if observation is not None and observation.found and observation.corners:
        points = [(int(x), int(y)) for x, y in observation.corners]
        for index in range(len(points)):
            cv2.line(frame, points[index], points[(index + 1) % len(points)], (0, 255, 0), 2)
        cv2.circle(frame, (int(observation.center_x), int(observation.center_y)), 5, (0, 0, 255), -1)
    lines = [
        ('FINAL INTEGRATION - NO TAKEOFF', (0, 0, 255)),
        ('fps %.1f   marker %s   size %s' % (
            fps,
            'FOUND id=%s' % observation.target_id if observation is not None and observation.found else 'not found',
            ('%.3f' % observation.size_ratio) if observation is not None and observation.found else '-'),
         (0, 255, 0)),
        ('3rd channels %s  reason %s' % (channels, reason), (0, 255, 255)),
    ]
    for index, (text, color) in enumerate(lines):
        cv2.putText(frame, text, (10, 26 + index * 26), cv2.FONT_HERSHEY_SIMPLEX,
                    0.58, color, 2 if index == 0 else 1, cv2.LINE_AA)
    return frame


def run(args, throttle):
    if not driver.connect(args.address, timeout=args.timeout):
        emit('错误', '连接返回 False')
        return 1
    health = driver.health()
    emit('连接', '电量=%s%% 高度=%scm 链路=%s 图传后端=%s' % (
        health['battery'], health['height_cm'], 'ok' if health['link_ok'] else 'FAIL',
        driver.VIDEO_BACKEND))
    if not driver.start_video(wait=args.frame_timeout):
        stats = driver.video_stats()
        emit('错误', '图传未出真实画面：%s' % stats)
        emit('提示', '先跑 python test_live_view.py --props-removed 定位原因')
        return 1
    emit('图传', '真实画面已就绪（backend=%s）' % driver.VIDEO_BACKEND)

    from r3_follow.vision import MarkerTracker
    tracker = MarkerTracker()
    calculator = FollowCalculator(args.marker_id)
    bridge = FrameBridge()
    bridge.start()
    drone = ai_bridge.UnifiedDrone()
    poller = StatusPoller()
    poller.start()
    grant = None
    started = time.time()
    deadline = started + args.seconds
    next_status = started + 1.0
    last_send = 0.0
    last_channels = None
    sent_count = 0
    window = 'Final Integration (real video)'
    shown = 0
    last_show = 0.0
    started_action = False
    follow_attempted = False

    try:
        while time.time() < deadline:
            packet = controller.get_frame()
            observation = None
            frame = packet.frame
            if frame is not None:
                try:
                    observation = tracker.detect(frame, packet.frame_id, time.monotonic())
                except Exception as exc:
                    emit('错误', '3组识别异常：%s' % exc)
            channels, reason = calculator.update(observation)

            if channels is not None and time.time() - last_send >= .5:
                if grant is None:
                    fresh = controller.request_control('follow')
                    if not fresh.ok:
                        emit('拦截', '申请 follow 控制权失败：%s' % fresh.reason)
                        grant = None
                    else:
                        grant = fresh
                if grant is not None:
                    result = send_like_follow(channels, grant)
                    if result is not None and not result.ok and \
                            result.reason in ('obsolete_control', 'obsolete_command'):
                        grant = controller.request_control('follow')
                        if not grant.ok:
                            grant = None
                        result = send_like_follow(channels, grant) if grant is not None else None
                    sent_count += 1
                    last_send = time.time()
                    last_channels = channels
                    if result is not None and result.ok and any(channels):
                        emit('警告', '非零速度在地面被放行（不应发生）：%s' % (channels,))
                    elif result is not None and result.ok:
                        if throttle.allow(('zero-speed',)):
                            emit('3组', '计算速度 lr=0 fb=0（已居中/稳态）→ 归零放行')

            if not follow_attempted and time.time() - started > 1.0:
                follow_attempted = True
                try_start_follow()

            if not started_action and time.time() - started > 3.0:
                started_action = True
                if grant is not None:
                    controller.release_control('follow', control_epoch=grant.control_epoch)
                    grant = None
                four_group_action(drone)
                fresh = controller.request_control('follow')
                grant = fresh if fresh.ok else None

            now = time.time()
            elapsed = max(1e-6, now - started)
            fps = bridge.published / elapsed
            if now >= next_status:
                next_status = now + args.status_interval
                owner = controller.get_status().control_owner
                emit('状态', 'fps=%.1f 已发布=%d 识别=%s 速度发送=%d 控制权=%s' % (
                    fps, bridge.published,
                    ('id=%s size=%.3f cx=%.0f' % (observation.target_id, observation.size_ratio,
                                                  observation.center_x))
                    if observation is not None and observation.found else 'lost',
                    sent_count, owner))

            if not args.no_window:
                if frame is not None and now - last_show > 1 / 15:
                    import cv2
                    last_show = now
                    shown += 1
                    cv2.imshow(window, draw_overlay(frame.copy(), observation, fps,
                                                    last_channels, reason))
                pressed = cv2.waitKey(15) & 0xFF
                if pressed in (ord('q'), 27):
                    emit('状态', '手动退出')
                    break
                if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                    emit('状态', '窗口被关闭')
                    break
            else:
                time.sleep(.05)
        return 0
    except KeyboardInterrupt:
        emit('状态', '收到 Ctrl+C，开始清理')
        return 0
    finally:
        if not args.no_window:
            import cv2
            cv2.destroyAllWindows()
        bridge.stop()
        poller.stop()
        controller.stop_follow()
        current = controller.get_status().control_owner
        if current != 'none':
            controller.release_control(current)
            emit('清理', '已释放控制权 %s' % current)
        driver.stop_video()
        driver.disconnect()
        emit('清理', '图传已关闭，真机已断开')


def self_check():
    """离线自检：算速公式、拦截判定、ArUco 识别（合成帧），全程不连飞机。"""
    checks = []
    from r3_follow.core import State
    from r3_follow.vision import MarkerTracker, synthetic_frame

    tracker = MarkerTracker()
    fake = synthetic_frame(x=.5, size=.3, visible=True, marker_id=0)
    observation = tracker.detect(fake, 1, time.monotonic())
    checks.append(('合成 ArUco 帧能被识别', bool(observation.found)))
    calculator = FollowCalculator(0)
    calculator.update(observation)
    channels, reason = calculator.update(observation)
    checks.append(('3组算速能给出通道值', channels is not None and -30 <= channels[0] <= 30))
    missing = tracker.detect(synthetic_frame(visible=False), 2, time.monotonic())
    checks.append(('无码时返回 not found', missing.found is False))

    driver.set_simulated_delay(0)
    owner = controller.get_status().control_owner
    if owner != 'none':
        controller.release_control(owner)
    grant = controller.request_control('follow')
    result = controller.update_velocity('follow', 10, 10, 0, 0,
                                        control_epoch=grant.control_epoch,
                                        command_epoch=grant.command_epoch)
    checks.append(('地面速度被 is_landed 拦截', result.ok is False and result.reason == 'is_landed'))
    controller.release_control('follow', control_epoch=grant.control_epoch)

    checks.append(('默认图传后端锁定为 av', driver.VIDEO_BACKEND == 'av'))
    checks.append(('3组状态机含 TRACKING', hasattr(State, 'TRACKING')))

    failures = [name for name, ok in checks if not ok]
    for name, ok in checks:
        emit('PASS' if ok else 'FAIL', name)
    emit('汇总', '%d/%d 通过' % (len(checks) - len(failures), len(checks)))
    return 0 if not failures else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='终极跨组联调（真机画面 + 3组跟随 + 4组AI）')
    parser.add_argument('--self-check', action='store_true', help='离线自检，不连飞机')
    parser.add_argument('--props-removed', action='store_true', help='确认真机桨叶已拆下')
    parser.add_argument('--address', default=None, help='飞机 IP，默认 192.168.10.1')
    parser.add_argument('--timeout', type=float, default=driver.RESPONSE_TIMEOUT_S)
    parser.add_argument('--frame-timeout', type=float, default=12.0, help='等真实画面的秒数')
    parser.add_argument('--seconds', type=float, default=20.0, help='联调时长（默认 20 秒）')
    parser.add_argument('--marker-id', type=int, default=0, help='ArUco 目标 id（DICT_4X4_50）')
    parser.add_argument('--status-interval', type=float, default=2.0)
    parser.add_argument('--no-window', action='store_true', help='不弹窗口')
    parser.add_argument('--color', action='store_true', help='强制 ANSI 颜色')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    enable_ansi(args.color)
    disable_quick_edit()
    if args.self_check:
        try:
            return self_check()
        finally:
            close_output()
    if not args.props_removed:
        print('拒绝连接：地面联调必须先拆下桨叶。', flush=True)
        print('  离线自检：python run_final_real_sim.py --self-check', flush=True)
        print('  真机联调：python run_final_real_sim.py --props-removed', flush=True)
        return 2
    throttle = Throttle(1.0)
    install_taps(throttle)
    tail = EventTail(throttle)
    tail.start()
    emit('启动', '终极跨组联调开始（不含任何起飞指令，%.0fs 后自动收工）' % args.seconds)
    try:
        return run(args, throttle)
    finally:
        tail.stop()
        close_output()


if __name__ == '__main__':
    sys.exit(main())
