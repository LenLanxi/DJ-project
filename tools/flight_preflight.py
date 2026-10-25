"""飞行前检查：跟随配置 / 离线语音链路 / 飞机连通与电量。

给 start_flight_test.ps1 用，也可单独跑：
    python -X utf8 -B tools/flight_preflight.py            # 完整检查（需要连 Tello）
    python -X utf8 -B tools/flight_preflight.py --no-drone # 不碰飞机，只查本机
退出码：0=可以起飞  2=连不上飞机  3=电量不足  4=配置有问题
输出全部是 ASCII 的 `KEY=VALUE`，方便 PowerShell / 批处理解析。
"""
import argparse
import os
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOLLOW = os.path.join(ROOT, 'modules', 'follow')
AI = os.path.join(ROOT, 'modules', 'ai')
TELLO_HOST, TELLO_PORT = '192.168.10.1', 8889


def emit(key, value):
    print('%s=%s' % (key, value), flush=True)


def probe_config():
    sys.path.insert(0, FOLLOW)
    from r3_follow.config import load_config
    config = load_config()
    emit('CONFIG_FILE', os.environ.get('R3_FOLLOW_CONFIG') or '(default)')
    emit('CONTROL_PROFILE', config.control_profile)
    emit('STEERING_MODE', config.steering_mode)
    emit('YAW_LIMIT', config.yaw_limit)
    emit('FORWARD_LIMIT', config.forward_limit)
    emit('MAX_REQUEST_RATE', config.max_request_rate)
    emit('TARGET_ID', config.target_id)
    if config.control_profile != 'openpose_pid':
        emit('VERDICT', 'block:control_profile_not_openpose_pid')
        return 4
    return 0


def probe_voice():
    sys.path.insert(0, AI)
    try:
        import local_stt
        emit('STT_INSTALLED', local_stt.installed())
        emit('STT_CACHED', local_stt.model_cached())
    except Exception as exc:
        emit('STT_INSTALLED', False)
        emit('STT_ERROR', '%s: %s' % (type(exc).__name__, exc))
    try:
        import offline_tts
        emit('SAPI_AVAILABLE', offline_tts.available())
    except Exception as exc:
        emit('SAPI_AVAILABLE', False)
        emit('SAPI_ERROR', '%s: %s' % (type(exc).__name__, exc))
    return 0


def probe_drone(timeout=3.0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)

    def ask(command):
        sock.sendto(command.encode('ascii'), (TELLO_HOST, TELLO_PORT))
        try:
            return sock.recvfrom(1024)[0].decode(errors='replace').strip()
        except socket.timeout:
            return None

    try:
        if ask('command') is None:
            emit('DRONE', 'no_drone')
            return 2, None
        emit('DRONE', 'ok')
        battery = ask('battery?')
        if battery and battery.isdigit():
            emit('BATTERY', battery)
            return 0, int(battery)
        emit('BATTERY', 'unknown')
        return 0, None
    except Exception as exc:
        emit('DRONE', 'error:%s:%s' % (type(exc).__name__, exc))
        return 2, None
    finally:
        sock.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--min-battery', type=int, default=60)
    parser.add_argument('--no-drone', action='store_true', help='不连接飞机（离线自检用）')
    args = parser.parse_args(argv)

    code = probe_config()
    if code:
        return code
    probe_voice()
    if args.no_drone:
        emit('DRONE', 'skipped')
        emit('VERDICT', 'ok')
        return 0
    code, battery = probe_drone()
    if code:
        emit('VERDICT', 'block:drone_unreachable' if code == 2 else 'block:drone_error')
        return code
    if battery is not None and battery < args.min_battery:
        emit('VERDICT', 'block:battery_low')
        return 3
    emit('VERDICT', 'ok')
    return 0


if __name__ == '__main__':
    sys.exit(main())
