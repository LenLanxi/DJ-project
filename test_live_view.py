"""真机图传诊断：判定「视频包没到」还是「到了但解不出来」。

为什么需要它
    djitellopy 在收到第一帧真实画面之前，会一直返回 300x400 的全零占位数组
    (BackgroundFrameRead.frame 的初始值)。只看「frame 不为空」会把黑图当成画面。
    本脚本先把占位帧判掉，再用裸 UDP 抓包确认视频包到底有没有到达 11111 端口。

跑法
    python test_live_view.py --self-check            # 离线自检（不连飞机）
    python test_live_view.py --props-removed         # 真机：抓包 + 持续拉流 + 窗口

判定逻辑
    PASS = 画面尺寸不是 (300,400,3) 且 均值 > 0.5 且 峰值 > 10 → 存 live_test.jpg
    失败时按探针结果给出明确结论：
      0 包    → 网络/防火墙/端口占用（不是解码问题）
      有包无图 → 解码线程异常（PyAV/关键帧），脚本会打印线程存活与 av 版本
"""
import argparse
import os
import socket
import sys
import threading
import time

import numpy as np

from drone_sdk import _driver as driver

VIDEO_PORT = 11111
PLACEHOLDER = (300, 400, 3)


def emit(label, text):
    print('%s [%s] %s' % (time.strftime('%H:%M:%S'), label, text), flush=True)


def probe_video_packets(seconds=4.0):
    """独占绑定 11111 端口，数一数无人机的视频包到底有没有来。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(('0.0.0.0', VIDEO_PORT))
    except OSError as exc:
        sock.close()
        return dict(bound=False, error=str(exc), packets=0, total_bytes=0, source=None)
    sock.settimeout(0.2)
    packets = total = 0
    source = None
    first_at = None
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError as exc:
            sock.close()
            return dict(bound=True, error=str(exc), packets=packets, total_bytes=total,
                        source=source)
        packets += 1
        total += len(data)
        if source is None:
            source, first_at = addr[0], time.monotonic() - started
    sock.close()
    kbps = (total * 8 / 1000.0 / max(1e-6, seconds))
    return dict(bound=True, error='', packets=packets, total_bytes=total, source=source,
                first_packet_s=None if first_at is None else round(first_at, 3),
                kbps=round(kbps, 1), seconds=seconds)


def report_probe(result):
    if not result['bound']:
        emit('探针', 'UDP %d 已被占用，无法抓包：%s' % (VIDEO_PORT, result['error']))
        emit('结论', '有别的进程占着视频端口（常见：上一个地面测试窗口没关）。'
                     '先关掉它，或用 Get-NetUDPEndpoint -LocalPort %d 查 PID。' % VIDEO_PORT)
        return False
    emit('探针', '%d 秒内收到 %d 个视频包，共 %.1f KB，约 %.1f kbps，来源=%s，首包=%.3fs' % (
        result['seconds'], result['packets'], result['total_bytes'] / 1024.0,
        result.get('kbps', 0), result['source'], result['first_packet_s'] or -1))
    if result['packets'] == 0:
        emit('结论', '视频包一个都没到 → 问题在网络/防火墙，不是解码。')
        emit('建议', '1) 以管理员身份执行下面这条放行 Python 的入站 UDP：')
        emit('建议', '   New-NetFirewallRule -DisplayName "Tello Video UDP" -Direction Inbound '
                     '-Protocol UDP -Profile Private -Program "%s" -Action Allow' % sys.executable)
        emit('建议', '2) 确认连的是 TELLO-XXXXXX 热点（Tello EDU 组网模式下不推流）')
        emit('建议', '3) 关掉手机 Tello App（它会抢视频流）')
        return False
    emit('结论', '视频包已到达 → 解码环节问题，继续看下面的解帧日志')
    return True


def start_stream(backend='av'):
    """重启一次视频流，逼无人机立刻发一个新的关键帧。"""
    try:
        driver.stop_video()
    except Exception:
        pass
    if not driver.start_video(backend=backend):
        emit('错误', 'streamon / 解码器启动失败（backend=%s）' % backend)
        return False
    return True


def decoder_alive():
    reader = getattr(driver, '_video', None)
    if reader is None:
        return None
    if isinstance(reader, threading.Thread):
        return reader.is_alive()
    worker = getattr(reader, 'worker', None)
    return None if worker is None else worker.is_alive()


def loop_frames(args):
    import cv2
    window = 'Tello Live View'
    deadline = time.monotonic() + args.timeout
    reader = getattr(driver, '_video', None)
    printed = 0
    seen_placeholder = False
    last_show = 0.0
    while time.monotonic() < deadline:
        stats = driver.video_stats()
        alive = decoder_alive()
        frame = driver.get_frame()          # 占位帧会被过滤成 None
        printed += 1
        if printed <= args.max_lines or printed % 10 == 0:
            emit('帧', '#%d backend=%s shape=%s mean=%s max=%s placeholder=%s '
                       '解码线程=%s 重连=%s' % (
                printed, stats.get('backend'), stats['shape'], stats['mean'], stats['max'],
                stats['placeholder'], 'alive' if alive else ('dead' if alive is False else '?'),
                stats.get('reconnects', 0)))
        if stats['placeholder']:
            seen_placeholder = True
        if frame is not None:
            emit('成功', '拿到真实画面 shape=%s mean=%.2f' % (frame.shape, float(frame.mean())))
            path = args.output
            ok = cv2.imwrite(path, frame)
            emit('保存', '%s → %s' % (path, 'ok' if ok else '失败'))
            if not args.no_window:
                cv2.imshow(window, frame)
                cv2.waitKey(200)
            return True, stats
        if not args.no_window:
            now = time.time()
            if now - last_show > 0.2:
                last_show = now
                canvas = np.zeros((360, 640, 3), dtype=np.uint8)
                cv2.putText(canvas, 'NO SIGNAL - waiting for real frame', (12, 40),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2, cv2.LINE_AA)
                cv2.putText(canvas, 'shape=%s mean=%s' % (stats['shape'], stats['mean']),
                            (12, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 220, 220), 1, cv2.LINE_AA)
                cv2.imshow(window, canvas)
            pressed = cv2.waitKey(30) & 0xFF
            if pressed in (ord('q'), 27):
                emit('状态', '手动退出')
                return False, stats
            if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                emit('状态', '窗口被关闭')
                return False, stats
        else:
            time.sleep(0.03)
    stats = driver.video_stats()
    alive = decoder_alive()
    if seen_placeholder:
        emit('结论', '始终是 300x400 占位帧：没有解出任何一帧真实画面')
    if alive is False:
        emit('结论', '解码线程已退出（PyAV 抛异常后线程结束）→ 关键帧/解码器问题')
        emit('建议', '把下面版本信息发我：av=%s' % av_version())
    return False, stats


def av_version():
    try:
        import av
        return '%s %s' % (av.__version__, av.library_versions.get('libavcodec'))
    except Exception as exc:
        return 'unavailable: %s' % exc


def self_check():
    """离线验证占位帧过滤与探针逻辑，不连接飞机。"""
    checks = []

    class Reader:
        def __init__(self, frame): self.frame = frame

    driver._video = Reader(np.zeros(PLACEHOLDER, dtype=np.uint8))
    checks.append(('300x400 全零占位帧被判为 None', driver.get_frame() is None))
    checks.append(('video_stats 标记 placeholder',
                   driver.video_stats()['placeholder'] is True))
    real = np.zeros((720, 960, 3), dtype=np.uint8)
    real[:, :, 2] = 200                      # RGB 红 → BGR 应为蓝通道 200
    driver._video = Reader(real)
    frame = driver.get_frame()
    checks.append(('真实帧正常返回且尺寸正确', frame is not None and frame.shape == (720, 960, 3)))
    checks.append(('RGB→BGR 转换生效', frame is not None and int(frame[10, 10, 0]) == 200))
    frame, stats = driver.await_video(timeout=0.2)
    checks.append(('await_video 能立刻拿到真实帧', frame is not None and not stats['placeholder']))
    driver._video = None
    checks.append(('无解码器时 get_frame 为 None', driver.get_frame() is None))

    probe = probe_video_packets(seconds=1.0)
    checks.append(('探针能绑定 11111 或明确报告占用',
                   probe['bound'] or 'in use' in probe['error'].lower()
                   or 'only be used' in probe['error'].lower()))
    emit('自检', '探针 1 秒收到 %d 个包（离线应为 0）' % probe['packets'])

    failures = [name for name, ok in checks if not ok]
    for name, ok in checks:
        emit('PASS' if ok else 'FAIL', name)
    emit('汇总', '%d/%d 通过，av=%s' % (len(checks) - len(failures), len(checks), av_version()))
    return 0 if not failures else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='真机图传诊断与持续拉流')
    parser.add_argument('--self-check', action='store_true', help='离线自检，不连飞机')
    parser.add_argument('--props-removed', action='store_true', help='确认真机桨叶已拆下')
    parser.add_argument('--address', default=None, help='飞机 IP，默认 192.168.10.1')
    parser.add_argument('--timeout', type=float, default=10.0, help='等真实画面的秒数')
    parser.add_argument('--probe-seconds', type=float, default=4.0, help='抓包观察秒数')
    parser.add_argument('--no-probe', action='store_true', help='跳过抓包探针')
    parser.add_argument('--no-window', action='store_true', help='不弹窗口')
    parser.add_argument('--max-lines', type=int, default=40, help='前 N 帧逐帧打印')
    parser.add_argument('--output', default='live_test.jpg', help='成功时保存的文件名')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.self_check:
        return self_check()
    if not args.props_removed:
        print('拒绝连接：地面测试必须先拆下桨叶。', flush=True)
        print('  离线自检：python test_live_view.py --self-check', flush=True)
        print('  真机诊断：python test_live_view.py --props-removed', flush=True)
        return 2
    emit('启动', '图传诊断开始；av=%s' % av_version())
    try:
        driver.connect(args.address, timeout=driver.RESPONSE_TIMEOUT_S)
    except Exception as exc:
        emit('错误', '连接失败：%s' % exc)
        return 1
    try:
        if not args.no_probe:
            # 先 streamon 让无人机开始推流，再用裸 socket 抓包
            driver.stream_video_only()
            result = probe_video_packets(args.probe_seconds)
            packets_ok = report_probe(result)
            driver.stop_video()
            time.sleep(0.3)
            if not packets_ok and result['packets'] == 0:
                emit('说明', '仍会尝试解码一次，但包都没到基本不会出图')
        ok = False
        stats = {}
        for backend in ('av', 'djitellopy'):
            if not start_stream(backend):
                continue
            emit('拉流', 'backend=%s，开始持续取帧，最多等 %.0fs（逐帧打印 shape/mean）'
                 % (backend, args.timeout))
            ok, stats = loop_frames(args)
            if ok:
                break
            errors = stats.get('errors') or []
            if errors:
                emit('解码错误', ' | '.join(errors[-3:]))
        if not ok:
            emit('失败', '两个后端都没有拿到真实画面，最后状态：%s' % stats)
            emit('提示', '把上面的 [探针]/[帧]/[结论] 行整段发我，我据此改 _driver')
            return 1
        emit('完成', '图传正常（backend=%s），已保存 %s' % (stats.get('backend'), args.output))
        return 0
    finally:
        import cv2
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass
        try:
            driver.motors_off()
            driver.stop_video()
            driver.disconnect()
        except Exception as exc:
            emit('警告', '清理异常：%s' % exc)


if __name__ == '__main__':
    sys.exit(main())
