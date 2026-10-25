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
import time

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


def parse_state(text):
    """把 Tello 的状态串（pitch:0;bat:46;templ:81;...）解析成字典。"""
    values = {}
    for chunk in (text or '').split(';'):
        if ':' in chunk:
            key, _, value = chunk.partition(':')
            values[key.strip()] = value.strip()
    return values


def probe_state(timeout=4.0):
    """只监听 8890 的状态广播：不碰 8889 命令口，不会抢走飞机的 UDP 会话。

    飞机进入 SDK 模式后会持续往客户端 8890 端口推状态，里面有电量、温度、高度，
    够用来判断"能不能飞"，而且完全只读。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(('', 8890))
    except OSError as exc:
        emit('STATE', 'port_busy:%s' % exc)
        sock.close()
        return None
    sock.settimeout(0.5)
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            try:
                data, _addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            state = parse_state(data.decode(errors='replace'))
            if 'bat' in state:
                return state
        return None
    finally:
        sock.close()


def probe_drone(timeout=2.5):
    # 必须用 8889 当源端口：Tello 会把"第一个跟它说话的客户端端口"记住并只往那里回包，
    # 用临时端口探测会把锁抢走，随后 djitellopy（固定 8889）就再也收不到回复。
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(('', TELLO_PORT))
    except OSError as exc:
        emit('DRONE', 'port_busy:%s' % exc)
        emit('VERDICT', 'block:tello_port_busy')
        sock.close()
        return 6, None
    sock.settimeout(timeout)

    def ask(command):
        sock.sendto(command.encode('ascii'), (TELLO_HOST, TELLO_PORT))
        try:
            return sock.recvfrom(1024)[0].decode(errors='replace').strip()
        except socket.timeout:
            return None

    try:
        if ask('command') is None:
            # 'command' 不回：可能是飞机在但 SDK 命令服务卡住（断电重启可恢复），
            # 也可能已经处在 SDK 模式里。再问一次电量把两种情况区分开。
            battery = ask('battery?')
            if battery is None:
                emit('DRONE', 'no_response')
                return 2, None
            emit('DRONE', 'sdk_mode_stuck')
            emit('BATTERY', battery if battery.isdigit() else 'unknown')
            return 5, int(battery) if battery.isdigit() else None
        emit('DRONE', 'ok')
        battery = ask('battery?') or ask('battery?')
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


def battery_verdict(battery, min_battery, firmware_battery):
    """电量结论：硬门限才拦，固件拒飞线只警告（警告让操作者自己判断，别假装能飞）。"""
    if battery is None:
        return None, None
    if battery < min_battery:
        return 'block:battery_low', None
    if battery < 20:
        return None, 'battery_very_low'
    if battery < firmware_battery:
        return None, 'battery_below_firmware_line'
    return None, None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--min-battery', type=int, default=10,
                        help='起飞硬门限（低于此值直接拦下；默认 10，与 main.py --min-battery 一致）')
    parser.add_argument('--firmware-battery', type=int, default=50,
                        help='Tello 固件的实际拒飞线，低于此值只警告不拦截')
    parser.add_argument('--temp-max', type=int, default=95,
                        help='板/芯片温度硬上限（℃）：templ/temph 超过就禁止起飞')
    parser.add_argument('--temp-warn', type=int, default=85,
                        help='温度警告线（℃）：超过只提示不拦截')
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

    # 优先用只读的状态广播（8890）判断电量/温度：不碰命令口，不会抢走 UDP 会话
    state = probe_state()
    if state:
        emit('DRONE', 'ok_state_stream')
        battery = state.get('bat')
        if battery and battery.isdigit():
            emit('BATTERY', battery)
            battery = int(battery)
        else:
            battery = None
        emit('TEMP_LOW', state.get('templ', '?'))
        emit('TEMP_HIGH', state.get('temph', '?'))
        emit('HEIGHT_CM', state.get('h', '?'))
        temps = [int(t) for t in (state.get('templ'), state.get('temph')) if t and t.isdigit()]
        hottest = max(temps) if temps else None
        if hottest is not None and hottest >= args.temp_max:
            emit('VERDICT', 'block:battery_too_hot')
            return 7
        if hottest is not None and hottest >= args.temp_warn:
            emit('WARN', 'temperature_high')
        block, warn = battery_verdict(battery, args.min_battery, args.firmware_battery)
        if warn:
            emit('WARN', warn)
        if block:
            emit('VERDICT', block)
            return 3
        emit('VERDICT', 'ok')
        return 0

    code, battery = probe_drone()
    if code == 5:
        emit('VERDICT', 'block:drone_sdk_stuck')
        return code
    if code:
        emit('VERDICT', 'block:drone_unreachable' if code == 2 else 'block:drone_error')
        return code
    block, warn = battery_verdict(battery, args.min_battery, args.firmware_battery)
    if warn:
        emit('WARN', warn)
    if block:
        emit('VERDICT', block)
        return 3
    emit('VERDICT', 'ok')
    return 0


if __name__ == '__main__':
    sys.exit(main())
