"""真机地面测试（拆桨叶、不起飞）：连真机、看真实画面、点动电机。

安全约定（本脚本强制遵守）
  * 从不发送 takeoff / throwfly，起飞相关动作一律不接线；
  * 必须显式加 --props-removed 才会连接真机，防止误连接带桨叶的飞机；
  * 标准 Tello 固件不支持 Motor On（`motoron` 回 error），'w' 只打印提示；
    电机验证请走 test_ground_real 之外的 takeoff 流程（main.py --allow-real-takeoff）。
  * 退出时自动 motoroff + streamoff + 断开，任何异常都会走到清理分支。

用法
    python test_ground_real.py --self-check            # 离线校验安全限制，不连飞机
    python test_ground_real.py --props-removed         # 真机地面测试
    python test_ground_real.py --props-removed --no-video --seconds 20

按键：w 提示（不支持 motoron） / s 急停归零 / b 打印状态 / r 重启视频流 / q 或 ESC 退出
"""
import argparse
import os
import sys
import time

import drone_sdk._driver as driver

KEY_HELP = "w=提示  s=急停归零  b=打印状态  r=重启视频流  q/ESC=退出"


def disable_quick_edit():
    """Windows 控制台 QuickEdit：点一下终端就会冻结输出，表现得像程序卡死。"""
    if os.name != 'nt':
        return False
    try:
        import ctypes
        handle = ctypes.windll.kernel32.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(ctypes.windll.kernel32.SetConsoleMode(
            handle, (mode.value | 0x0080) & ~0x0040))
    except Exception:
        return False


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Tello 地面测试：拆桨叶，不起飞')
    parser.add_argument('--address', default=None, help='飞机 IP，默认 192.168.10.1')
    parser.add_argument('--timeout', type=float, default=driver.RESPONSE_TIMEOUT_S,
                        help='单条指令响应超时（秒），默认 %d' % driver.RESPONSE_TIMEOUT_S)
    parser.add_argument('--no-video', action='store_true', help='只看状态不开摄像头')
    parser.add_argument('--seconds', type=float, default=0, help='运行 N 秒后自动退出（0=手动退出）')
    parser.add_argument('--props-removed', action='store_true', help='确认桨叶已拆下')
    parser.add_argument('--self-check', action='store_true', help='离线校验安全限制，不连接飞机')
    return parser.parse_args(argv)


def self_check():
    """离线校验油门/限高/RC 夹紧逻辑，全程不接触硬件。"""
    checks = []

    def check(name, ok, detail=''):
        checks.append((name, bool(ok), detail))

    def restore():
        driver._height_cm, driver._flying, driver._speed_cm_s = 0, False, driver.DEFAULT_SPEED_CM_S

    try:
        driver.set_speed(driver.MAX_SPEED_CM_S + 1)
        check('速度超过 %d cm/s 被拒绝' % driver.MAX_SPEED_CM_S, False, '未抛出 ValueError')
    except ValueError:
        check('速度超过 %d cm/s 被拒绝' % driver.MAX_SPEED_CM_S, True)
    check('速度上限 %d cm/s 与固件一致' % driver.MAX_SPEED_CM_S,
          driver.MAX_SPEED_CM_S == 100 and driver.set_speed(driver.MAX_SPEED_CM_S) == 100)

    driver._height_cm = 200
    granted = driver._plan_ascent(80)
    check('上升 80cm 在 2.5m 限高内被夹到剩余空间 50cm',
          granted == 50, 'granted=%s' % (granted,))
    driver._height_cm = 240
    check('距限高只剩 10cm 时拒绝继续上升', driver._plan_ascent(50) is None)
    driver._height_cm = 0
    check('地面附近下降指令不被夹紧', driver._plan_descent(50) == 50)

    driver._flying, driver._height_cm = False, 0
    check('未起飞时 RC 通道被压成全 0', driver._sanitise_rc((40, 40, 40, 0)) == (0, 0, 0, 0))
    driver._flying, driver._height_cm = True, 250
    check('到顶后向上的 RC 通道被压成 0', driver._sanitise_rc((10, 10, 60, 0))[2] == 0)
    driver._flying, driver._height_cm = True, 0
    check('RC 通道绝对值不超过 100', driver._sanitise_rc((300, -300, 0, 0)) == (100, -100, 0, 0))

    driver.set_simulated_delay(0)
    check('默认仍是模拟模式（未接真机）', driver.SIMULATED is True and driver.is_connected() is False)
    check('模拟模式不会真的发 motoron', driver.motors_on() is False and driver.motors_off() is False)
    check('未开视频时 get_frame() 返回 None', driver.get_frame() is None)

    started = time.perf_counter()
    driver.interrupt('stop_motion')
    elapsed_ms = (time.perf_counter() - started) * 1000
    check('interrupt() 在模拟模式下立即返回（<50ms）', elapsed_ms < 50, '%.1fms' % elapsed_ms)
    events = driver.snapshot()['events']
    check('急停记录 0 速度并标记优先发送',
          any(e['event'] == 'velocity' and e['channels'] == (0, 0, 0, 0) for e in events[-2:])
          and events[-1]['event'] == 'priority_sent')

    restore()
    failures = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print('[%s] %s%s' % ('PASS' if ok else 'FAIL', name, ('  <- ' + detail) if detail else ''))
    print('\n%d/%d 项通过' % (len(checks) - len(failures), len(checks)))
    return 0 if not failures else 1


def print_health(info):
    print('模式=%s 连接=%s 链路=%s 电量=%s%% 高度=%scm 速度=%scm/s 飞行=%s 视频=%s' % (
        info['mode'], info['connected'], 'ok' if info['link_ok'] else 'FAULT',
        info['battery'], info['height_cm'], info['speed_cm_s'], info['flying'], info['video']))
    if info['last_error']:
        print('最近错误：%s' % info['last_error'])


def overlay(frame, info, fps):
    import cv2
    lines = [
        'GROUND TEST - NO TAKEOFF',
        'battery %s%%  height %scm  speed %scm/s' % (
            info['battery'], info['height_cm'], info['speed_cm_s']),
        'link %s  fps %.1f  (motoron 不支持)' % (
            'ok' if info['link_ok'] else 'FAULT', fps),
        KEY_HELP,
    ]
    for index, text in enumerate(lines):
        cv2.putText(frame, text, (12, 28 + index * 26), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 0, 255) if index == 0 else (0, 255, 0), 2, cv2.LINE_AA)
    return frame


def run(args):
    driver.set_simulated_delay(0)
    try:
        driver.connect(args.address, timeout=args.timeout)
    except Exception as exc:
        print('连接失败：%s' % exc)
        print('检查：飞机已开机 / PC 已连 Tello WiFi / 无其它程序占用 8889 端口')
        return 1
    info = driver.health()
    print_health(info)
    if info['battery'] is not None and info['battery'] < 30:
        print('警告：电量 %s%%，地面测试可以继续，但真飞行前必须充电' % info['battery'])

    video = False
    if not args.no_video:
        video = driver.start_video()
        print('视频流：%s' % ('已开启' if video else '开启失败（继续跑状态模式）'))
    if not video:
        args.no_video = True

    deadline = time.time() + args.seconds if args.seconds else None
    last_frame = time.time() - 1.0
    misses = 0
    try:
        while True:
            if args.no_video:
                print_health(driver.health())
                time.sleep(1.0)
                if deadline and time.time() >= deadline:
                    break
                continue
            import cv2
            now = time.time()
            # 25fps 上限：无节制渲染会把 CPU 打满，窗口随即失去响应
            if now - last_frame < 1 / 25:
                if cv2.waitKey(10) & 0xFF in (ord('q'), 27):
                    break
                if deadline and now >= deadline:
                    break
                continue
            fps = 1.0 / max(1e-6, now - last_frame)
            last_frame = now
            info = driver.health()
            frame = driver.get_frame()
            if frame is None:
                misses += 1
                if misses >= 40:
                    print('连续 20 秒没有画面，已降级到状态模式（按 Ctrl+C 退出）。')
                    args.no_video = True
                    continue
                print('等待视频帧……（检查 WiFi / 是否被其它程序占用视频端口）')
                time.sleep(0.5)
                continue
            misses = 0
            cv2.imshow('Tello Ground Test (no takeoff)', overlay(frame, info, fps))
            key = cv2.waitKey(15) & 0xFF
            if deadline and now >= deadline:
                break
            if key in (ord('q'), 27):
                break
            if key == ord('w'):
                print('标准 Tello 固件不支持 motoron（turn_motor_on 会回 error）。')
                print('电机验证请走 takeoff：main.py --real-drone --allow-real-takeoff')
            elif key == ord('s'):
                driver.set_velocity((0, 0, 0, 0), 'manual_stop')
                print('已发送 rc 0 0 0 0（急停归零）。')
            elif key == ord('b'):
                print_health(driver.health())
            elif key == ord('r'):
                driver.stop_video()
                print('视频流：%s' % ('已重启' if driver.start_video() else '重启失败'))
    except KeyboardInterrupt:
        print('\n收到 Ctrl+C，开始清理')
    finally:
        if not args.no_video:
            import cv2
            cv2.destroyAllWindows()
        driver.motors_off()
        driver.stop_video()
        driver.disconnect()
        print('已断开。真机任何物理动作都需要人工复核后另行授权。')
    return 0


def main(argv=None):
    args = parse_args(argv)
    if args.self_check:
        return self_check()
    if not args.props_removed:
        print('拒绝连接：地面测试必须先拆下桨叶。')
        print('确认桨叶已拆下后重新运行：python test_ground_real.py --props-removed')
        return 2
    disable_quick_edit()
    print('地面测试启动：本脚本不含任何起飞指令；标准 Tello 不支持 motoron，电机验证请走 takeoff。')
    return run(args)


if __name__ == '__main__':
    sys.exit(main())
