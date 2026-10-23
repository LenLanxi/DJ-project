"""现场联调监控：真机画面 + 电量/高度 + 3组/4组指令流水（拦截醒目标注）。

两种模式
    python run_ground_test.py --demo              # 假数据跑通全流程，不连飞机
    python run_ground_test.py --props-removed     # 真机地面测试（拆桨叶，不起飞）

监控原理
  * 在 drone_sdk.controller 层装探针：谁调用 update_velocity（3组）、move_*（4组）、
    request_control（控制权）、start_follow（跟随），都会被打印出来；
  * 同时尾随 drone_sdk._driver 的事件流：显示驱动真正下发的 RC、限高夹紧、链路故障；
  * 任何返回 ok=False 的调用统一用 [拦截] 前缀 + 中文原因打印。

真机模式按键
    v 3组速度指令演示    c 4组动作指令演示    d 释放控制权
    w 电机怠速开         s 电机停            b 立即打印状态        q/ESC 退出
"""
import argparse
import os
import sys
import threading
import time

from drone_sdk import _driver, ai_bridge, controller
from drone_sdk.contract import FrameResult

REASONS = {
    'is_landed': '未起飞（地面状态）',
    'no_control': '没有控制权',
    'busy': '控制权被其它 owner 占用',
    'landing_in_progress': '正在降落',
    'action_busy': '上一条动作还没结束',
    'obsolete_control': '控制权授权已过期',
    'obsolete_command': '指令授权已过期',
    'invalid_channels': '速度通道越界（-100~100）',
    'invalid_distance': '距离不在 20~500cm',
    'invalid_degrees': '角度不在 -360~360 或为 0',
    'invalid_owner': 'owner 只能是 follow/model',
    'height_limit_rejected': '触及 2.5m 限高',
    'height_limit_clamped': '限高夹紧',
    'link_down': '链路已断开',
    'timeout': '指令超时',
    'driver_failed': '驱动执行失败',
    'action_cancelled': '动作被打断后返回',
    'program_cancelled': '本轮任务已取消',
    'not_owner': '不是当前 owner',
}
TAG_CODES = {'拦截': '91;1', '3组': '96', '4组': '95', '急停': '93;1', '链路': '91',
             '状态': '92', '控制权': '94', '跟随': '96', '电机': '93', '错误': '91;1', '演示': '90'}
ACTIONS = ('takeoff', 'move_forward', 'move_back', 'move_left', 'move_right',
           'move_up', 'move_down', 'rotate')
TAG_OF = {'update_velocity': '3组', 'takeoff': '4组', 'rotate': '4组', 'land': '急停',
          'stop_motion': '急停', 'request_control': '控制权', 'release_control': '控制权',
          'start_follow': '跟随', 'stop_follow': '跟随'}
TAG_OF.update({name: '4组' for name in ACTIONS})

_ANSI = False


def enable_ansi(force=False):
    global _ANSI
    if force:
        _ANSI = True
        return
    if not sys.stdout.isatty():
        return
    if os.name != 'nt':
        _ANSI = True
        return
    try:
        import ctypes
        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        _ANSI = bool(ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        _ANSI = False


def stamp():
    now = time.time()
    return time.strftime('%H:%M:%S', time.localtime(now)) + '.%03d' % int((now % 1) * 1000)


def tag(name):
    return '\x1b[%sm[%s]\x1b[0m' % (TAG_CODES.get(name, '0'), name) if _ANSI else '[%s]' % name


def emit(name, text):
    print('%s %s %s' % (stamp(), tag(name), text), flush=True)


def reason_text(reason):
    return REASONS.get(reason, reason or '未知原因')


class Throttle:
    """同一条重复消息 0.7 秒内只打一行，避免跟随线程 30Hz 刷屏。"""

    def __init__(self, window):
        self.window = window
        self._last = {}

    def allow(self, key):
        now = time.monotonic()
        previous = self._last.get(key)
        self._last[key] = now
        return previous is None or now - previous >= self.window


def describe(name, args, kwargs):
    if name == 'update_velocity' and len(args) >= 5:
        return 'lr=%s fb=%s ud=%s yaw=%s' % args[1:5]
    if name in ACTIONS and name != 'takeoff' and len(args) >= 2:
        return '%scm' % (args[1],) if name != 'rotate' else '%s度' % (args[1],)
    if name in ('request_control', 'release_control') and args:
        return 'owner=%s' % (args[0],)
    return ''


def describe_result(result):
    status = getattr(result, 'status', None)
    owner = getattr(result, 'current_owner', None)
    if status: return status
    if owner: return 'owner=%s' % owner
    return 'ok'


def install_taps(throttle):
    """把 controller 的公开入口包一层，谁调用、结果如何都会打印。"""
    for name in ('takeoff', 'land', 'stop_motion', 'move_forward', 'move_back', 'move_left',
                 'move_right', 'move_up', 'move_down', 'rotate', 'update_velocity',
                 'request_control', 'release_control', 'start_follow', 'stop_follow'):
        original = getattr(controller, name)

        def wrapper(*args, _name=name, _original=original, **kwargs):
            detail = describe(_name, args, kwargs)
            suffix = (' ' + detail) if detail else ''
            label = TAG_OF.get(_name, '状态')
            if _name in ACTIONS:
                emit(label, '%s%s 执行中…' % (_name, suffix))
            try:
                result = _original(*args, **kwargs)
            except Exception as exc:
                emit('错误', '%s%s 抛出异常 %s: %s' % (_name, suffix, type(exc).__name__, exc))
                raise
            ok = getattr(result, 'ok', None)
            reason = getattr(result, 'reason', '') or ''
            if ok is False:
                if throttle.allow(('blocked', _name, detail, reason)):
                    emit('拦截', '%s%s 被拦下：%s' % (_name, suffix, reason_text(reason)))
            elif _name in ACTIONS or _name in ('update_velocity', 'start_follow', 'stop_follow',
                                               'land', 'stop_motion', 'request_control',
                                               'release_control'):
                if throttle.allow(('pass', _name, detail)):
                    emit(label, '%s%s → %s' % (_name, suffix, describe_result(result)))
            return result

        wrapper.__wrapped__ = original
        setattr(controller, name, wrapper)


INTERESTING = {'action_rejected', 'height_limit_rejected', 'height_limit_clamped', 'priority_sent',
               'link_fault', 'motor_test', 'connected', 'disconnected', 'video_started',
               'land_sent', 'land_deferred', 'action_unknown', 'disconnect_failed'}
DECISIVE_ZERO = {'stop_motion', 'land', 'watchdog_timeout', 'owner_released', 'owner_changed'}


class EventTail(threading.Thread):
    """尾随 _driver 事件流，打印驱动真正下发的动作。

    游标用事件对象身份而不是 monotonic 时间戳：Windows 上 time.monotonic()
    只有约 15ms 分辨率，同一 tick 内的多条事件会互相覆盖。
    """

    def __init__(self, throttle, interval=0.1):
        super().__init__(daemon=True, name='driver-events')
        self.throttle = throttle
        self.interval = interval
        self._last = self._tail() or None
        self._halt = threading.Event()

    @staticmethod
    def _tail():
        events = _driver.snapshot()['events']
        return events[-1] if events else None

    def run(self):
        while not self._halt.wait(self.interval):
            self.poll()

    def poll(self):
        try:
            events = _driver.snapshot()['events']
        except Exception:
            return
        start = 0
        if self._last is not None:
            for index in range(len(events) - 1, -1, -1):
                if events[index] is self._last:
                    start = index + 1
                    break
            else:
                start = max(0, len(events) - 1)
        for event in events[start:]:
            self.report(event)
            self._last = event

    def stop(self):
        self._halt.set()
        if self.is_alive():
            self.join(0.5)
        self.poll()

    def report(self, event):
        try:
            self._report(event)
        except Exception as exc:
            emit('错误', '事件解析失败 %s: %s' % (event.get('event'), exc))

    def _report(self, event):
        kind = event['event']
        if kind == 'velocity':
            channels, reason = event['channels'], event.get('reason', '')
            if any(channels):
                if self.throttle.allow(('rc', channels)):
                    emit('3组', 'RC 下发 lr=%s fb=%s ud=%s yaw=%s（%s）' % (*channels, reason))
            elif reason in DECISIVE_ZERO and self.throttle.allow(('zero', reason)):
                emit('急停', 'RC 归零（%s）' % reason)
            return
        if kind == 'action_sent':
            if self.throttle.allow(('sent', event.get('action'))):
                param = event.get('param')
                emit('4组', '驱动已下发 %s%s' % (event.get('action'),
                                               '' if param is None else ' %s' % (param,)))
            return
        if kind == 'action_rejected':
            if self.throttle.allow(('rejected', event.get('action'), event.get('reason'))):
                emit('拦截', '驱动拒绝 %s：%s' % (event.get('action'), reason_text(event.get('reason'))))
            return
        if kind in ('height_limit_rejected', 'height_limit_clamped'):
            emit('拦截', '%s 请求 %s，当前 %scm / 上限 %scm' % (
                '限高拒绝' if kind.endswith('rejected') else '限高夹紧',
                event.get('requested'), event.get('height_cm'), event.get('ceiling_cm')))
            return
        if kind == 'priority_sent':
            emit('急停', '优先指令 %s 已发送（不阻塞）' % event.get('action'))
            return
        if kind == 'link_fault':
            emit('链路', '%s（累计 %s 次）' % (event.get('reason'), event.get('faults')))
            return
        if kind == 'motor_test':
            emit('电机', '电机测试 %s' % ('怠速开' if event.get('state') == 'on' else '停'))
            return
        if kind in INTERESTING:
            emit('状态', '%s %s' % (kind, {k: v for k, v in event.items()
                                          if k not in ('event', 'monotonic', 'simulated')}))


def status_line(source):
    info = source()
    emit('状态', '电量 %s%%  高度 %scm  链路 %s  控制权 %s  视频 %s' % (
        info['battery'], info['height_cm'], 'ok' if info['link_ok'] else 'FAIL',
        info['owner'], 'on' if info['video'] else 'off'))
    return info


def real_status():
    health = _driver.health()
    return dict(battery=health['battery'], height_cm=health['height_cm'],
                link_ok=health['link_ok'], owner=controller.get_status().control_owner,
                video=health['video'])


def demo_status():
    status = controller.get_status()
    return dict(battery=status.battery, height_cm=status.height_cm, link_ok=True,
                owner=status.control_owner, video=controller.get_frame().frame is not None)


def demo_frames(stop):
    """给监控窗口喂假画面，顺便验证解码后的显示链路。"""
    import numpy as np
    frame_id, timestamp = 0, time.time()
    while not stop.wait(1 / 30):
        canvas = np.zeros((360, 640, 3), dtype=np.uint8)
        canvas[:, :, 1] = 30
        left = 40 + int((time.time() * 60) % 480)
        canvas[150:210, left:left + 120] = (0, 140, 255)
        frame_id += 1
        timestamp = time.time()
        controller.publish_frame(FrameResult(canvas, frame_id, timestamp, False))


def demo(args, throttle):
    _driver.set_simulated_delay(0)
    emit('演示', 'DEMO 模式：不连接真机，全程使用模拟 SDK 的假数据')
    status_line(demo_status)

    emit('演示', '1) 3组：地面状态下发速度指令')
    grant = controller.request_control('follow')
    controller.update_velocity('follow', 20, 10, 0, 0,
                               control_epoch=grant.control_epoch, command_epoch=grant.command_epoch)
    controller.release_control('follow', control_epoch=grant.control_epoch)

    emit('演示', '2) 4组：语音“向前飞” → 地面状态下发动作')
    drone = ai_bridge.UnifiedDrone()
    drone.request_control('model')
    drone.move_forward('model', 50)

    emit('演示', '3) 4组：先起飞，再执行同一个动作（这次应放行）')
    drone.takeoff('model')
    drone.move_forward('model', 50)

    emit('演示', '4) 3组：飞行中下发速度（应放行并看到 RC 流水）')
    _release(drone, 'model')
    follow_grant = controller.request_control('follow')
    controller.update_velocity('follow', 20, 10, 0, 0,
                               control_epoch=follow_grant.control_epoch,
                               command_epoch=follow_grant.command_epoch)
    controller.update_velocity('follow', 0, 0, 0, 0,
                               control_epoch=follow_grant.control_epoch,
                               command_epoch=follow_grant.command_epoch)
    controller.release_control('follow', control_epoch=follow_grant.control_epoch)

    emit('演示', '5) 4组：一条 1 秒阻塞动作进行中，另一线程急停必须立即返回')
    drone.request_control('model')
    _driver.set_simulated_delay(1.0)
    timing = {}

    def stop_from_other_thread():
        started = time.perf_counter()
        controller.stop_motion('model')
        timing['ms'] = (time.perf_counter() - started) * 1000

    timer = threading.Timer(0.2, stop_from_other_thread)
    timer.start()
    drone.move_forward('model', 50)
    timer.join(3)
    _driver.set_simulated_delay(0)
    emit('急停', 'stop_motion 在阻塞动作中返回耗时 %.2fms' % timing.get('ms', float('nan')))

    emit('演示', '6) 4组：重新拿授权 → 降落 → 等待 landed → 释放')
    drone.request_control('model')
    drone.land('model')
    deadline = time.monotonic() + 2
    while controller.get_status().flight_state != 'landed' and time.monotonic() < deadline:
        time.sleep(0.01)
    _release(drone, 'model')
    status_line(demo_status)

    emit('演示', '7) 真机专属动作在模拟模式下应当失败（不会误发到硬件）')
    _driver.motors_on()
    time.sleep(0.2)
    return 0


def run_demo_window(args, throttle):
    if args.no_video:
        return demo(args, throttle)
    import cv2
    stop = threading.Event()
    threading.Thread(target=demo_frames, args=(stop,), daemon=True, name='demo-frames').start()
    result = [None]

    def body():
        result[0] = demo(args, throttle)

    worker = threading.Thread(target=body, daemon=True)
    worker.start()
    deadline = time.time() + (args.seconds or 4)
    while time.time() < deadline and worker.is_alive():
        packet = controller.get_frame()
        frame = packet.frame
        if frame is None:
            time.sleep(0.05)
            continue
        cv2.putText(frame, 'DEMO MODE - FAKE FRAMES', (12, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (0, 0, 255), 2, cv2.LINE_AA)
        cv2.putText(frame, 'q/ESC to quit', (12, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 255, 0), 2, cv2.LINE_AA)
        cv2.imshow('Ground Test Monitor (demo)', frame)
        if cv2.waitKey(1) & 0xFF in (ord('q'), 27):
            break
    stop.set()
    worker.join(2)
    cv2.destroyAllWindows()
    return result[0] or 0


def _terminal_key():
    """无窗口（--no-video）时用 Windows 终端按键，避免现场视频失败就失去交互。"""
    if os.name != 'nt':
        return -1
    import msvcrt
    if not msvcrt.kbhit():
        return -1
    raw = msvcrt.getch()
    if raw in (b'\x00', b'\xe0'):
        msvcrt.getch()
        return -1
    try:
        return raw.decode('latin1').lower()[0]
    except Exception:
        return -1


def run_real(args, throttle):
    from drone_sdk import _driver as driver
    driver.set_simulated_delay(0)
    try:
        driver.connect(args.address, timeout=args.timeout)
    except Exception as exc:
        emit('错误', '连接失败：%s' % exc)
        emit('演示', '排查：ipconfig 看是否拿到 192.168.10.x，ping 192.168.10.1，'
                     '关掉手机 Tello App 与其它占用 8889 端口的程序')
        return 1
    if not args.no_video and not driver.start_video():
        emit('错误', '视频流开启失败，降级到状态模式（视频端口 11111 可能被占用）')
        args.no_video = True
    status_line(real_status)
    emit('演示', '监控已开启。v=3组速度演示  c=4组动作演示  d=释放控制权  '
                 'w/s=电机怠速开关  b=状态  q=退出')

    drone = ai_bridge.UnifiedDrone()
    owner = None
    motors = False
    deadline = time.time() + args.seconds if args.seconds else None
    next_status = time.time() + args.status_interval
    while True:
        now = time.time()
        if deadline and now >= deadline:
            break
        if now >= next_status:
            status_line(real_status)
            next_status = now + args.status_interval
        key = -1
        if args.no_video:
            key = _terminal_key()
            time.sleep(0.05)
        else:
            import cv2
            frame = driver.get_frame()
            if frame is None:
                time.sleep(0.1)
                continue
            health = driver.health()
            cv2.putText(frame, 'GROUND MONITOR - NO TAKEOFF', (10, 26), cv2.FONT_HERSHEY_SIMPLEX,
                        0.62, (0, 0, 255), 2, cv2.LINE_AA)
            cv2.putText(frame, 'battery %s%%  height %scm  link %s  owner %s' % (
                health['battery'], health['height_cm'],
                'ok' if health['link_ok'] else 'FAIL', controller.get_status().control_owner),
                (10, 54), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 1, cv2.LINE_AA)
            cv2.putText(frame, 'v=3rd speed  c=4th action  d=release  w/s=motor  q=quit',
                        (10, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
            cv2.imshow('Ground Test Monitor', frame)
            key = cv2.waitKey(1) & 0xFF
        if key in (ord('q'), 27):
            break
        if key == ord('b'):
            status_line(real_status)
        elif key == ord('v'):
            owner = _demo_speed(drone, owner)
        elif key == ord('c'):
            owner = _demo_action(drone, owner)
        elif key == ord('d'):
            owner = _release(drone, owner)
        elif key == ord('w'):
            motors = driver.motors_on()
            emit('电机', '怠速开：%s' % ('已确认' if motors else '指令未确认'))
        elif key == ord('s'):
            driver.motors_off()
            motors = False
            emit('电机', '已停止')
    _release(drone, owner)
    if not args.no_video:
        import cv2
        cv2.destroyAllWindows()
    driver.motors_off()
    driver.stop_video()
    driver.disconnect()
    emit('状态', '已断开并清理完成')
    return 0


def _demo_speed(drone, owner):
    """3组风格：申请 follow 控制权后下发速度。"""
    if controller.get_status().control_owner == 'none':
        grant = controller.request_control('follow')
        if not grant.ok:
            emit('拦截', '申请 follow 控制权失败：%s' % reason_text(grant.reason))
            return owner
        controller.update_velocity('follow', 20, 10, 0, 0,
                                   control_epoch=grant.control_epoch,
                                   command_epoch=grant.command_epoch)
        return 'follow'
    controller.update_velocity(controller.get_status().control_owner, 20, 10, 0, 0)
    return owner


def _demo_action(drone, owner):
    """4组风格：申请 model 控制权后下发动作。"""
    if controller.get_status().control_owner != 'model':
        if controller.get_status().control_owner != 'none':
            _release(drone, owner)
        grant = drone.request_control('model')
        if not grant.get('ok'):
            emit('拦截', '申请 model 控制权失败：%s' % grant.get('message'))
            return owner
        owner = 'model'
    drone.move_forward('model', 50)
    return owner


def _release(drone, owner):
    current = controller.get_status().control_owner
    if current == 'none':
        return None
    result = controller.release_control(current)
    return None if result.ok else owner


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='现场联调监控：真机画面 + 3组/4组指令流水')
    parser.add_argument('--demo', action='store_true', help='假数据模式，不连接真机')
    parser.add_argument('--props-removed', action='store_true', help='确认真机桨叶已拆下')
    parser.add_argument('--address', default=None, help='飞机 IP，默认 192.168.10.1')
    parser.add_argument('--timeout', type=float, default=_driver.RESPONSE_TIMEOUT_S,
                        help='单条指令响应超时（秒）')
    parser.add_argument('--no-video', action='store_true', help='不开摄像头窗口')
    parser.add_argument('--seconds', type=float, default=0, help='运行 N 秒后自动退出')
    parser.add_argument('--status-interval', type=float, default=2.0, help='状态打印间隔（秒）')
    parser.add_argument('--throttle', type=float, default=0.7, help='重复指令打印的抑制窗口（秒）')
    parser.add_argument('--color', action='store_true', help='强制开启 ANSI 颜色')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    enable_ansi(args.color)
    if args.demo and args.props_removed:
        print('--demo 与 --props-removed 不能同时使用')
        return 2
    throttle = Throttle(args.throttle)
    install_taps(throttle)
    tail = EventTail(throttle)
    tail.start()
    try:
        if args.demo:
            return run_demo_window(args, throttle)
        if not args.props_removed:
            print('拒绝连接：地面测试必须先拆下桨叶。')
            print('  先跑假数据验证监控逻辑：python run_ground_test.py --demo')
            print('  确认桨叶已拆下后：      python run_ground_test.py --props-removed')
            return 2
        print('地面联调监控启动：本脚本不含任何起飞指令。')
        return run_real(args, throttle)
    finally:
        tail.stop()


if __name__ == '__main__':
    sys.exit(main())
