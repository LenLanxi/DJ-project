"""无人值守自动联调：不弹窗、不按键，跑完自动存画面与报告。

流程（真机，拆桨叶、不起飞）
    1. 连接飞机，读取真实电量/高度/链路
    2. 开启视频流，等一帧真实画面
    3. 抓帧 → RGB/BGR 校正 → 保存 current_frame_fixed.jpg（附 cv2 读回校验）
    4. 3组风格：申请 follow 控制权 + 下发速度 → 地面应被 [拦截] is_landed
    5. 4组风格：申请 model 控制权 + move_forward(50) → 地面应被 [拦截] is_landed
    6. 急停：stop_motion 必须 50ms 内返回
    7. 限速/限高常量与夹紧逻辑校验（只算不发，不会让飞机动）
    8. 释放控制权 → 停电机 → 关视频 → 断开

用法
    python run_auto_integration.py --self-test         # 离线自检，不连飞机（含色彩断言）
    python run_auto_integration.py --props-removed     # 真机自动联调

产物：current_frame_fixed.jpg / auto_integration_report.json，退出码 = 失败项数量。
"""
import argparse
import json
import os
import sys
import time

import numpy as np

from drone_sdk import _driver as driver
from drone_sdk import ai_bridge, controller

RESULTS = []


def emit(label, text):
    print('%s [%s] %s' % (time.strftime('%H:%M:%S'), label, text), flush=True)


def check(name, ok, detail=''):
    RESULTS.append(dict(name=name, ok=bool(ok), detail=str(detail)))
    emit('PASS' if ok else 'FAIL', '%s%s' % (name, ('  <- ' + detail) if detail and not ok else ''))
    return ok


class FakeReader:
    """模拟 djitellopy 的 BackgroundFrameRead：注意它给的是 RGB（PIL 顺序）。"""

    def __init__(self, rgb):
        self.frame = rgb

    def stop(self):
        pass


def color_probe_image():
    """左半纯红、右半纯蓝（按 RGB 顺序构造），用于验证通道是否被纠正。"""
    image = np.zeros((120, 240, 3), dtype=np.uint8)
    image[:, :120] = (255, 0, 0)      # RGB 红
    image[:, 120:] = (0, 0, 255)      # RGB 蓝
    return image


def save_frame(frame, path):
    import cv2
    ok = cv2.imwrite(path, frame)
    if not ok:
        return False, 'cv2.imwrite 返回 False'
    back = cv2.imread(path)
    if back is None:
        return False, 'cv2.imread 读不回'
    if back.shape != frame.shape:
        return False, '尺寸不一致 %s != %s' % (back.shape, frame.shape)
    diff = float(np.abs(back.astype(np.int16) - frame.astype(np.int16)).mean())
    return True, 'shape=%s 平均差=%.2f' % (frame.shape, diff)


def report_path(args, name):
    return os.path.join(args.output, name)


def step_color_check(args):
    """验证 _driver.get_frame() 把解码器的 RGB 转成 BGR，并且落盘后颜色正确。"""
    reader = FakeReader(color_probe_image())
    driver._video = reader
    try:
        frame = driver.get_frame()
    finally:
        driver._video = None
    ok = frame is not None and frame.shape == (120, 240, 3)
    check('get_frame 返回三通道图像', ok, '' if ok else 'frame=%r' % (None if frame is None else frame.shape))
    if not ok:
        return
    left = frame[60, 30].tolist()      # 源图左上为 RGB 红 → BGR 应为 [0,0,255]
    right = frame[60, 210].tolist()    # 源图右上为 RGB 蓝 → BGR 应为 [255,0,0]
    check('左半区 RGB 红 → BGR [0,0,255]', left == [0, 0, 255], 'pixel=%s' % left)
    check('右半区 RGB 蓝 → BGR [255,0,0]', right == [255, 0, 0], 'pixel=%s' % right)
    path = report_path(args, 'current_frame_fixed.jpg')
    saved, detail = save_frame(frame, path)
    check('保存 current_frame_fixed.jpg', saved, detail)
    if saved:
        import cv2
        back = cv2.imread(path)
        pixel = back[60, 30].tolist()
        check('落盘后左上仍是红色（BGR 约 [0,0,255]，JPEG 有损允许 ±3）',
              all(abs(a - b) <= 3 for a, b in zip(pixel, (0, 0, 255))), 'pixel=%s' % pixel)


def step_speed_intercept():
    """3组风格：地面下发速度必须被 is_landed 拦下。"""
    owner = controller.get_status().control_owner
    if owner != 'none':
        controller.release_control(owner)
    grant = controller.request_control('follow')
    if not grant.ok:
        check('3组：申请 follow 控制权', False, grant.reason)
        return
    started = time.perf_counter()
    result = controller.update_velocity('follow', 20, 10, 0, 0,
                                        control_epoch=grant.control_epoch,
                                        command_epoch=grant.command_epoch)
    elapsed = (time.perf_counter() - started) * 1000
    check('3组速度指令被拦截（未起飞）',
          result.ok is False and result.reason == 'is_landed',
          'ok=%s reason=%s' % (result.ok, result.reason))
    check('拦截在 50ms 内返回', elapsed < 50, '%.2fms' % elapsed)
    controller.release_control('follow', control_epoch=grant.control_epoch)


def step_action_intercept():
    """4组风格：地面下发动作必须被 is_landed 拦下。"""
    drone = ai_bridge.UnifiedDrone()
    grant = drone.request_control('model')
    if not grant.get('ok'):
        check('4组：申请 model 控制权', False, grant.get('message'))
        return
    started = time.perf_counter()
    result = drone.move_forward('model', 50)
    elapsed = (time.perf_counter() - started) * 1000
    check('4组动作指令被拦截（未起飞）',
          result.get('ok') is False and '尚未起飞' in str(result.get('message')),
          'ok=%s message=%s' % (result.get('ok'), result.get('message')))
    check('4组拦截在 50ms 内返回', elapsed < 50, '%.2fms' % elapsed)

    started = time.perf_counter()
    stopped = drone.stop_motion('model')
    elapsed = (time.perf_counter() - started) * 1000
    check('急停 stop_motion 返回成功', stopped.get('ok') is True, str(stopped))
    check('急停 50ms 内返回', elapsed < 50, '%.2fms' % elapsed)
    controller.release_control('model')


def step_limits():
    """只做计算校验，不会给飞控发送任何上升指令。"""
    check('限速常量 = 100cm/s', driver.MAX_SPEED_CM_S == 100, str(driver.MAX_SPEED_CM_S))
    check('限高常量 = 250cm(2.5m)', driver.MAX_HEIGHT_CM == 250, str(driver.MAX_HEIGHT_CM))
    try:
        driver.set_speed(driver.MAX_SPEED_CM_S + 1)
        check('超速请求被拒绝', False, '未抛出 ValueError')
    except ValueError:
        check('超速请求被拒绝', True)
    granted = driver._plan_ascent(500)
    height = driver._current_height()
    room = driver.MAX_HEIGHT_CM - height
    expected = None if room < 20 else min(500, room)
    check('上升请求按剩余空间夹紧（只计算不发送）', granted == expected,
          'granted=%s expected=%s height=%s' % (granted, expected, height))
    channels = driver._sanitise_rc((40, 40, 40, 0))
    check('未起飞时 RC 通道被压成 0', channels == (0, 0, 0, 0), str(channels))


def step_video_and_capture(args):
    """开启视频流，等第一帧**真实**画面（300x400 占位帧不算）并存成 jpg。"""
    if not driver.start_video(backend=args.backend):
        check('开启视频流', False, 'streamon / 解码器启动失败')
        return None
    stats = driver.video_stats()
    emit('视频', 'backend=%s 解码器=%s' % (stats.get('backend'), stats.get('reader')))
    frame, stats = driver.await_video(args.frame_timeout)
    if frame is None:
        check('抓到真实画面帧', False,
              '%.1fs 内没有真实画面，最后状态=%s（占位帧 shape=300x400 全零不算）'
              % (args.frame_timeout, stats))
        errors = stats.get('errors') or []
        if errors:
            emit('解码错误', ' | '.join(errors[-3:]))
        return None
    check('抓到真实画面帧', True)
    check('画面尺寸是真实分辨率（非 300x400 占位）',
          tuple(frame.shape[:2]) != (300, 400), str(frame.shape))
    check('画面不是全黑', float(frame.mean()) > 0.5, 'mean=%.2f' % float(frame.mean()))
    path = report_path(args, 'current_frame_fixed.jpg')
    saved, detail = save_frame(frame, path)
    check('保存 current_frame_fixed.jpg', saved, detail)
    if args.save_raw and saved:
        import cv2
        raw = cv2.imwrite(report_path(args, 'current_frame_raw_rgb.jpg'), frame[:, :, ::-1])
        check('保存对照图 current_frame_raw_rgb.jpg', bool(raw))
    means = [round(float(frame[:, :, i].mean()), 1) for i in range(3)]
    emit('画面', 'shape=%s BGR均值=%s → %s' % (frame.shape, means, path))
    return frame


def steps_offline(args):
    emit('自检', '离线模式：不连接真机，只验证转码与拦截逻辑')
    step_limits()
    step_color_check(args)
    if driver.SIMULATED:
        # 模拟 SDK 起飞后可放行，确认拦截不是因为代码写死
        controller.request_control('model')
        controller.takeoff('model')
        controller.release_control('model')
        grant = controller.request_control('follow')
        flying = controller.update_velocity('follow', 10, 0, 0, 0,
                                            control_epoch=grant.control_epoch,
                                            command_epoch=grant.command_epoch)
        check('飞行状态下速度指令不再被 is_landed 拦截', flying.ok is True,
              'reason=%s' % (flying.reason,))
        controller.release_control('follow', control_epoch=grant.control_epoch)
        controller.request_control('model')
        controller.land('model')
        deadline = time.monotonic() + 2
        while controller.get_status().flight_state != 'landed' and time.monotonic() < deadline:
            time.sleep(0.01)
        controller.release_control('model')
    step_speed_intercept()
    step_action_intercept()


def steps_hardware(args):
    health = driver.health()
    emit('连接', 'mode=%s 电量=%s%% 高度=%scm 链路=%s 速度上限=%scm/s' % (
        health['mode'], health['battery'], health['height_cm'],
        'ok' if health['link_ok'] else 'FAIL', health['max_speed_cm_s']))
    check('连接真机', health['connected'] is True and health['link_ok'] is True,
          'connected=%s link_ok=%s' % (health['connected'], health['link_ok']))
    check('读到真实电量', isinstance(health['battery'], int) and health['battery'] >= 0,
          str(health['battery']))
    step_limits()
    frame = step_video_and_capture(args)
    step_speed_intercept()
    step_action_intercept()
    return frame


def control_port_owner():
    """UDP 8889 是 Tello 控制端口，独占。返回占用它的 PID（没有则 None）。"""
    try:
        import subprocess
        out = subprocess.run(['netstat', '-ano', '-p', 'UDP'], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1].endswith(':8889'):
            return parts[-1]
    return None


def warn_if_port_busy():
    owner = control_port_owner()
    if owner:
        emit('警告', 'UDP 8889 已被 PID %s 占用（多半是上一个地面测试窗口没关）。' % owner)
        emit('警告', '先关闭那个窗口，或执行：Stop-Process -Id %s' % owner)
    return owner


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='无人值守自动联调（不弹窗、不按键）')
    parser.add_argument('--self-test', action='store_true', help='离线自检，不连接真机')
    parser.add_argument('--props-removed', action='store_true', help='确认真机桨叶已拆下')
    parser.add_argument('--address', default=None, help='飞机 IP，默认 192.168.10.1')
    parser.add_argument('--timeout', type=float, default=driver.RESPONSE_TIMEOUT_S,
                        help='单条指令响应超时（秒）')
    parser.add_argument('--frame-timeout', type=float, default=8.0, help='等待首帧的秒数')
    parser.add_argument('--backend', choices=('av', 'djitellopy'), default='av',
                        help='视频解码后端（默认自研 av，失败可试 djitellopy）')
    parser.add_argument('--output', default='.', help='产物输出目录')
    parser.add_argument('--save-raw', action='store_true', help='额外保存未校正的对照图')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    os.makedirs(args.output, exist_ok=True)
    driver.set_simulated_delay(0)
    started = time.time()
    frame = None
    try:
        if args.self_test:
            steps_offline(args)
        else:
            if not args.props_removed:
                print('拒绝连接：地面测试必须先拆下桨叶。', flush=True)
                print('  离线自检：python run_auto_integration.py --self-test', flush=True)
                print('  真机联调：python run_auto_integration.py --props-removed', flush=True)
                return 2
            emit('启动', '自动联调开始：不含任何起飞指令，不弹窗，不按键')
            warn_if_port_busy()
            try:
                driver.connect(args.address, timeout=args.timeout)
            except Exception as exc:
                emit('错误', '连接失败：%s' % exc)
                text = str(exc)
                if '10048' in text or 'only be used once' in text:
                    emit('排查', 'UDP 8889 已被占用（上一个 test_ground_real/run_ground_test 未关闭）。'
                                 '先关掉那个窗口；查询命令：Get-NetUDPEndpoint -LocalPort 8889')
                else:
                    emit('排查', 'ipconfig 看 192.168.10.x / ping 192.168.10.1 / '
                                 '关掉手机 App 与其它占用 8889 的程序')
                RESULTS.append(dict(name='连接真机', ok=False, detail=str(exc)))
                return 1
            frame = steps_hardware(args)
    except KeyboardInterrupt:
        emit('状态', '收到 Ctrl+C，开始清理')
    except Exception as exc:
        emit('错误', '未预期异常 %s: %s' % (type(exc).__name__, exc))
        RESULTS.append(dict(name='未预期异常', ok=False, detail='%s: %s' % (type(exc).__name__, exc)))
    finally:
        try:
            owner = controller.get_status().control_owner
            if owner != 'none':
                controller.release_control(owner)
                emit('清理', '已释放控制权 %s' % owner)
            driver.motors_off()
            driver.stop_video()
            driver.disconnect()
        except Exception as exc:
            emit('错误', '清理阶段异常：%s' % exc)

    failures = [item for item in RESULTS if not item['ok']]
    summary = dict(successful=not failures, started=started, elapsed_s=round(time.time() - started, 2),
                   checks=RESULTS, passed=len(RESULTS) - len(failures), failed=len(failures),
                   mode='self-test' if args.self_test else 'hardware',
                   frame=dict(shape=list(frame.shape)) if frame is not None else None,
                   image=report_path(args, 'current_frame_fixed.jpg') if frame is not None else None,
                   note='软件自测结果；不代表真实飞行、物理急停或落地已验证')
    path = report_path(args, 'auto_integration_report.json')
    with open(path, 'w', encoding='utf8') as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    emit('汇总', '%d/%d 通过，失败 %d，耗时 %.1fs' % (
        summary['passed'], len(RESULTS), len(failures), summary['elapsed_s']))
    emit('汇总', '报告：%s' % path)
    if frame is not None:
        emit('汇总', '画面：%s' % report_path(args, 'current_frame_fixed.jpg'))
    return len(failures)


if __name__ == '__main__':
    sys.exit(main())
