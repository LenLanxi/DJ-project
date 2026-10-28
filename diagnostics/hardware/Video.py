"""真机图传诊断：先检查 UDP 包，再通过唯一 PyAV 解码器读取帧结果。

--self-check 使用合成解码输入，不连接飞机、不绑定 UDP、不打开窗口。
--props-removed 显式启用真机诊断；帧时间是本机解码完成时间，不能证明曝光时延。
"""
import argparse
import os
from pathlib import Path
import socket
import sys
import threading
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from drone_sdk import _driver as driver

VIDEO_PORT = 11111


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


def start_stream():
    """通过公共关闭接口确认旧解码器结束，再启动唯一 PyAV 来源。"""
    if not driver.stop_video():
        emit('错误', '旧视频流未确认关闭，拒绝重启')
        return False
    if not driver.start_video():
        emit('错误', 'streamon / PyAV 解码器启动失败')
        return False
    return True


def loop_frames(args):
    import cv2
    window = 'Tello Live View'
    deadline = time.monotonic() + args.timeout
    printed = 0
    last_show = 0.0
    while time.monotonic() < deadline:
        stats = driver.video_stats()
        packet = driver.get_frame()
        frame = packet.frame
        printed += 1
        if printed <= args.max_lines or printed % 10 == 0:
            emit('帧', '#%d id=%d stale=%s shape=%s mean=%s age=%s 重连=%s' % (
                printed, packet.frame_id, packet.stale, stats.get('shape'), stats.get('mean'),
                stats.get('age'), stats.get('reconnects', 0)))
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
    emit('结论', '期限内没有新鲜解码画面；依据公开统计中的 count/age/errors 排查')
    emit('版本', 'av=%s' % av_version())
    return False, stats


def av_version():
    try:
        import av
        return '%s %s' % (av.__version__, av.library_versions.get('libavcodec'))
    except Exception as exc:
        return 'unavailable: %s' % exc


def self_check():
    """公共视频 API + 真实解码线程；仅替换合成 AV 输入，不启动网络。"""
    from unittest.mock import patch
    if not driver.SIMULATED or driver.is_connected():
        emit('拒绝', '离线自检不能覆盖正在使用的真实设备')
        return 2
    checks = []
    paused, finished, closed = threading.Event(), threading.Event(), threading.Event()
    formats = []

    class Frame:
        def to_ndarray(self, format):
            formats.append(format)
            return np.zeros((300, 400, 3), dtype=np.uint8)

    class Packet:
        def decode(self): return [Frame()]

    class Container:
        def demux(self, video):
            while not finished.is_set():
                if not paused.is_set(): yield Packet()
                time.sleep(.01)
        def close(self): closed.set()

    class Stream:
        def get_udp_video_address(self): return 'synthetic://offline'
        def streamoff(self): return True

    with patch.object(driver, '_tello', Stream()), patch.object(driver, '_video', None), \
         patch.object(driver, 'stream_video_only', return_value=True), \
         patch.object(driver, '_call_real', side_effect=lambda operation, **_kwargs: (operation(), '')), \
         patch('av.open', side_effect=lambda *_args, **_kwargs: Container()):
        missing = driver.get_frame()
        checks.append(('没有解码器时返回无像素的过期帧结果', missing.frame is None and missing.stale))
        try:
            checks.append(('唯一 PyAV 来源启动', driver.start_video()))
            packet, stats = driver.await_video(timeout=2)
            checks.append(('真实解码线程发布帧身份和解码时间',
                           packet.frame is not None and packet.frame_id > 0 and packet.timestamp > 0 and not packet.stale))
            checks.append(('真实黑色画面不按旧占位尺寸过滤',
                           packet.frame is not None and packet.frame.shape == (300, 400, 3) and not packet.frame.any()))
            checks.append(('解码直接请求 BGR 数据', bool(formats) and set(formats) == {'bgr24'}))
            checks.append(('公开统计记录真实解码数量', stats.get('count', 0) > 0))
            paused.set()
            # Wait for an already-yielded conversion to settle, then keep the
            # same cached source idle past the public freshness deadline.
            time.sleep(.05)
            before = driver.get_frame()
            time.sleep(.55)
            stale = driver.get_frame()
            checks.append(('缓存读取不刷新身份或时间且过期隐藏像素',
                           stale.stale and stale.frame is None and stale.frame_id == before.frame_id
                           and stale.timestamp == before.timestamp))
        finally:
            finished.set()
            checks.append(('公共 stop_video 确认解码关闭', driver.stop_video()))
        checks.append(('关闭后没有帧且 AV 容器已关闭',
                       driver.get_frame().frame is None and driver.get_frame().stale and closed.is_set()))
    for name, ok in checks:
        emit('PASS' if ok else 'FAIL', name)
    failures = sum(not ok for _, ok in checks)
    emit('汇总', '%d/%d 通过，av=%s' % (len(checks)-failures, len(checks), av_version()))
    return 1 if failures else 0


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
        print('  离线自检：python diagnostics/hardware/Video.py --self-check', flush=True)
        print('  真机诊断：python diagnostics/hardware/Video.py --props-removed', flush=True)
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
            if not packets_ok and result['packets'] == 0:
                emit('说明', '仍会尝试解码一次，但包都没到基本不会出图')
        if not start_stream(): return 1
        emit('拉流', '唯一 PyAV 来源，最多等待 %.0fs' % args.timeout)
        ok, stats = loop_frames(args)
        errors = stats.get('errors') or []
        if errors: emit('解码错误', ' | '.join(errors[-3:]))
        if not ok:
            emit('失败', '唯一 PyAV 来源没有拿到新鲜画面，最后状态：%s' % stats)
            emit('提示', '把上面的 [探针]/[帧]/[结论] 行整段发我，我据此改 _driver')
            return 1
        emit('完成', '图传正常（PyAV），已保存 %s' % args.output)
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
