"""Bottom communication layer: offline simulator by default, real Tello on opt-in.

Simulation stays the default so every offline suite keeps running with no
hardware; `connect()` is the only door into real mode and has to be called
explicitly (test_ground_real.py does that). Records prove software ordering,
never physical stopping, landing or motor state.

Real-mode safety contract
  * speed hard ceiling 100 cm/s (MAX_SPEED_CM_S), identical to the firmware
    'speed' range 10..100; anything faster is rejected instead of clamped
  * 2.5 m ceiling (MAX_HEIGHT_CM): an ascent is clamped to the remaining room
    and rejected once less than 20 cm is left
  * RC channels are clamped to +/-100 and forced to 0 while on the ground
  * every blocking command runs with a deadline and try/except; a fault is
    reported through health() instead of raising inside the caller
  * interrupt() never blocks: it sends RC zero immediately and dispatches the
    landing command to a background thread, so a stuck link or a long action
    can never delay an emergency stop
"""
from collections import deque
import re
import threading
import time

SIMULATED = True

MAX_SPEED_CM_S = 100
MIN_SPEED_CM_S = 10
DEFAULT_SPEED_CM_S = 50
MAX_HEIGHT_CM = 250
MAX_DISTANCE_CM = 500
MAX_ROTATION_DEG = 360
RC_LIMIT = 100

RESPONSE_TIMEOUT_S = 5.0
COMMAND_DEADLINE_S = {'takeoff': 25.0, 'land': 20.0}
LAND_DISPATCH_TIMEOUT_S = 6.0
KEEPALIVE_INTERVAL_S = 8.0
LINK_FAULT_LIMIT = 3
# 飞控刚上电时 Wi-Fi 已广播但飞控没就绪，`command` 会收到 0xcc 之类的半包，
# djitellopy 把它当 UTF-8 解码错误抛出（现场复现 3 次）。等待 + 重试可消除。
CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY_S = 2.0
# 实测：标准 Tello 固件在电量 50% 就对本机 takeoff 直接回 error（不发电机）。
# 这个值只用于失败诊断文案，不参与起飞授权判断。
TAKEOFF_FIRMWARE_BATTERY = 50

_lock = threading.RLock()
_command_lock = threading.Lock()
_delay = 0.0
_velocity = (0, 0, 0, 0)
_events = deque(maxlen=2000)
_tello = None
_video = None
_keepalive_stop = None
_height_cm = 0
_flying = False
_link_ok = True
_faults = 0
_last_error = ''
_speed_cm_s = DEFAULT_SPEED_CM_S
_pending_commands = set()
_cancel_epoch = 0
_unknown_actions = set()
_unknown_since = {}
TAKEOFF_CONFIRM_CM = 10
RECONCILE_SETTLE_S = 2.0

def action_pending():
    with _lock: return bool(_pending_commands) if not SIMULATED else False


def action_unknown(action=None):
    """True 表示该动作已发出但结果未知（超时/被取消），必须靠遥测修正。"""
    with _lock:
        if SIMULATED: return False
        return (action in _unknown_actions) if action else bool(_unknown_actions)


def cancel_pending(reason='cancelled'):
    """作废所有尚未真正发给飞机的命令（拿到锁后还会二次校验编号）。"""
    global _cancel_epoch
    with _lock:
        _cancel_epoch += 1
        epoch = _cancel_epoch
    _record('commands_cancelled', reason=reason, epoch=epoch)
    return epoch


def record_priority_override(action, **fields):
    """紧急保留通道的审计记录：说明这次为什么越过常规排队。"""
    _record('priority_override', action=action, **fields)

_MOVES = ('forward', 'back', 'left', 'right', 'up', 'down')


def _record(event, **fields):
    with _lock:
        _events.append(dict(event=event, monotonic=time.monotonic(),
                            simulated=SIMULATED, **fields))


def _set_link_ok():
    global _faults, _link_ok
    with _lock:
        _faults, _link_ok = 0, True


def _link_fault(reason):
    global _faults, _link_ok, _last_error
    with _lock:
        _faults += 1
        _last_error = reason
        if _faults >= LINK_FAULT_LIMIT: _link_ok = False
        faults = _faults
    _record('link_fault', reason=reason, faults=faults)


def set_simulated_delay(seconds):
    global _delay
    with _lock: _delay = max(0., float(seconds))

def simulated_delay():
    with _lock: return _delay


def is_connected():
    with _lock: return _tello is not None


TOF_MIN_CM = 20          # 低于此值视为贴地或不可信
TOF_MAX_TRUSTED_CM = 300


def _height_probe(tello=None):
    """返回 (高度cm 或 None, 来源)。None = 读数不可信，调用方必须保守处理。

    Tello（非 EDU）飞行中气压高度字段 ``h`` 可能不更新（实测悬停时仍为 0）。
    此时用 ToF 交叉验证；两个都不信就明确返回 None，绝不假装高度是 0，
    否则「限高 2.5m」会在一架已经 1.2m 高的飞机上失效。
    """
    with _lock:
        tello = tello or _tello
        flying, cached = _flying, _height_cm
    if tello is None:
        return (cached, 'cached')
    height = None
    try:
        height = tello.get_height()
    except Exception:
        height = None
    if type(height) is int and height > 0:
        return (height, 'baro')
    if not flying:
        return (0, 'ground')
    tof = None
    try:
        tof = tello.get_distance_tof()
    except Exception:
        tof = None
    if type(tof) is int and TOF_MIN_CM <= tof <= TOF_MAX_TRUSTED_CM:
        return (tof, 'tof')
    return (None, 'unknown')


def _read_battery(tello=None):
    """读一次真实电量；读不到就返回 None（绝不猜数字）。"""
    if tello is None:
        with _lock: tello = _tello
    getter = getattr(tello, 'get_battery', None)
    if tello is None or not callable(getter): return None
    try: value = getter()
    except Exception: return None
    return value if type(value) is int else None


def _current_height(tello=None):
    global _height_cm
    value, _source = _height_probe(tello)
    if value is None:
        with _lock: return _height_cm     # 保持旧返回值语义；上升决策走 _height_probe
    with _lock: _height_cm = value
    return value


def _plan_ascent(cm):
    height, source = _height_probe()
    if height is None:
        _record('height_limit_rejected', requested=cm, height_cm=None,
                ceiling_cm=MAX_HEIGHT_CM, reason='height_unknown', source=source)
        return None
    room = MAX_HEIGHT_CM - height
    if cm <= room: return cm
    if room < 20:
        _record('height_limit_rejected', requested=cm, height_cm=height, ceiling_cm=MAX_HEIGHT_CM)
        return None
    _record('height_limit_clamped', requested=cm, granted=int(room), height_cm=height, ceiling_cm=MAX_HEIGHT_CM)
    return int(room)


def _plan_descent(cm):
    height = _current_height()
    if height <= 0: return cm
    granted = min(cm, int(height))
    return granted if granted >= 20 else cm


def _sanitise_rc(channels):
    with _lock: flying = _flying
    if not flying: return (0, 0, 0, 0)
    left_right, forward_back, up_down, yaw = (
        max(-RC_LIMIT, min(RC_LIMIT, int(v))) for v in channels)
    if up_down != 0:
        height, source = _height_probe()
        if height is None:
            _record('height_limit_clamped', requested=up_down, height_cm=None,
                    channel='up_down', reason='height_unknown', source=source)
            up_down = 0
        elif up_down > 0 and height >= MAX_HEIGHT_CM:
            _record('height_limit_clamped', requested=up_down, height_cm=height, channel='up_down')
            up_down = 0
        elif up_down < 0 and height <= 0:
            up_down = 0
    return (left_right, forward_back, up_down, yaw)


def _call_real(fn, *, action, deadline, epoch=None):
    """Run one blocking Tello command on a worker and cap the caller's wait.

    取消语义：调用时绑定当时的取消编号；worker 真正拿到命令锁后、调用 fn 之前
    再比对一次。若期间发生过 cancel_pending（急停/降落/停止），命令直接丢弃，
    绝不发给飞机。
    """
    with _lock:
        bound_epoch = _cancel_epoch if epoch is None else epoch
    outcome = {'ok': False, 'error': 'timeout'}
    done = threading.Event()

    with _lock: _pending_commands.add(done)
    def runner():
        if not _command_lock.acquire(timeout=max(1.0, deadline)):
            outcome['error'] = 'command_busy'
            with _lock: _pending_commands.discard(done)
            done.set()
            return
        try:
            with _lock: cancelled = _cancel_epoch != bound_epoch
            if cancelled:
                outcome['error'] = 'cancelled_before_send'
                _record('command_dropped', action=action, reason='cancelled_before_send',
                        bound_epoch=bound_epoch, cancel_epoch=_cancel_epoch)
            else:
                fn()
                outcome['ok'], outcome['error'] = True, ''
        except Exception as exc:
            outcome['error'] = '%s: %s' % (type(exc).__name__, exc)
        finally:
            _command_lock.release()
            with _lock: _pending_commands.discard(done)
            done.set()

    threading.Thread(target=runner, daemon=True, name='tello-command').start()
    if not done.wait(deadline):
        _link_fault('deadline_exceeded:%s' % action)
        return False, 'timeout'
    if outcome['ok']:
        _set_link_ok()
        return True, ''
    if outcome['error'] == 'cancelled_before_send':
        return False, outcome['error']          # 主动丢弃不是链路故障
    _link_fault(outcome['error'])
    return False, outcome['error']


def _move_deadline(action, param):
    if action in COMMAND_DEADLINE_S: return COMMAND_DEADLINE_S[action]
    magnitude = abs(param or 0)
    return (12.0 + magnitude / 45.0) if action == 'rotate' else (8.0 + magnitude / 20.0)


def _execute_move(tello, action, cm):
    if action == 'rotate':
        if cm >= 0: tello.rotate_clockwise(cm)
        else: tello.rotate_counter_clockwise(-cm)
    else:
        getattr(tello, 'move_%s' % action)(cm)


def _execute_real(action, param=None):
    global _flying, _height_cm, _last_error
    with _lock:
        tello = _tello
        if tello is None: return False
        if not _link_ok:
            _events.append(dict(event='action_rejected', action=action, reason='link_down',
                                monotonic=time.monotonic(), simulated=False))
            return False
    _record('action_sent', action=action, param=param)
    if action in ('takeoff', 'land'):
        ok, error = _call_real(getattr(tello, action), action=action,
                               deadline=_move_deadline(action, None))
        if ok:
            with _lock:
                _flying = action == 'takeoff'
                if action == 'land': _height_cm = 0
            if action == 'takeoff': _current_height(tello)
            return True
        if action == 'takeoff' and error in ('timeout', 'cancelled_before_send'):
            # 超时/被丢弃都不等于没起飞：标记结果未知，交给真实遥测修正，绝不擅自改回 landed。
            token = object()
            with _lock:
                _unknown_actions.add('takeoff')
                _unknown_since['takeoff'] = (time.monotonic(), token)
            _record('action_unknown', action='takeoff', error=error)
            threading.Thread(target=_reconcile_later, args=(tello, token), daemon=True,
                             name='tello-reconcile').start()
        elif action == 'takeoff' and error not in ('command_busy', 'link_down'):
            # 飞控明确回 error（区别于超时）。现场原因基本只有低电量一条：
            # 电量 50% 时 takeoff 会在 0.1s 内被拒。记下可操作的诊断，
            # 别让上层只看到 "takeoff was unsuccessful for 2 tries"。
            battery = _read_battery(tello)
            low = type(battery) is int and battery <= TAKEOFF_FIRMWARE_BATTERY
            advice = 'battery_low' if low else 'flight_controller_rejected'
            with _lock:
                _last_error = 'takeoff_rejected:%s battery=%s%%' % (advice, battery)
            _record('takeoff_rejected', error=error, battery=battery, advice=advice,
                    firmware_threshold=TAKEOFF_FIRMWARE_BATTERY)
        return False
    if action in _MOVES:
        if type(param) is not int or not 20 <= param <= MAX_DISTANCE_CM:
            _record('action_rejected', action=action, reason='invalid_distance', param=param)
            return False
        cm = _plan_ascent(param) if action == 'up' else (
             _plan_descent(param) if action == 'down' else param)
        if cm is None: return False
        ok, _error = _call_real(lambda: _execute_move(tello, action, cm), action=action,
                                deadline=_move_deadline(action, cm))
        if ok:
            if action in ('up', 'down'):
                with _lock:
                    _height_cm = max(0, _height_cm + (cm if action == 'up' else -cm))
                _current_height(tello)
        return ok
    if action == 'rotate':
        if type(param) is not int or param == 0 or abs(param) > MAX_ROTATION_DEG:
            _record('action_rejected', action=action, reason='invalid_degrees', param=param)
            return False
        ok, _error = _call_real(lambda: _execute_move(tello, action, param), action=action,
                                deadline=_move_deadline(action, param))
        return ok
    _record('action_rejected', action=action, reason='unknown_action', param=param)
    return False


def execute(action, param=None):
    with _lock:
        real, delay = not SIMULATED, _delay
    if not real:
        _record('action_sent', action=action, param=param)
        if delay: time.sleep(delay)
        return True
    return _execute_real(action, param)


def _reconcile_later(tello, token=None, delay=1.5):
    """超时后不要立刻下结论：等一轮遥测再判定。"""
    time.sleep(delay)
    reconcile_state(tello, token=token)


def reconcile_state(tello=None, token=None):
    """用真实遥测（高度/飞行状态）修正“结果未知”的动作。返回是否已确定。"""
    global _flying
    with _lock:
        target, unknown = (tello or _tello), set(_unknown_actions)
        if SIMULATED or target is None: return False
        if not unknown: return True          # 没有未决动作，状态已是可信的
        if token is not None and _unknown_since.get('takeoff', (0, None))[1] is not token:
            return True                      # 这条是更早那次超时的收尾线程，别动新状态
    height = _current_height(target)
    with _lock:
        resolved = False
        if 'takeoff' in unknown:
            since = _unknown_since.get('takeoff', (0.0, None))[0]
            settled = time.monotonic() - since >= RECONCILE_SETTLE_S
            if _flying or height >= TAKEOFF_CONFIRM_CM:
                _flying, resolved = True, True
                _unknown_actions.discard('takeoff')
                _unknown_since.pop('takeoff', None)
                _record('action_reconciled', action='takeoff', result='flying', height_cm=height)
            elif settled and not _pending_commands:
                resolved = True                       # 只有过了稳定期且遥测为地面才敢下结论
                _unknown_actions.discard('takeoff')
                _unknown_since.pop('takeoff', None)
                _record('action_reconciled', action='takeoff', result='not_flying', height_cm=height)
        return resolved


def set_velocity(channels, reason):
    global _velocity
    with _lock: real = not SIMULATED
    applied = _sanitise_rc(channels) if real else tuple(channels)
    with _lock:
        _velocity = applied
        _events.append(dict(event='velocity', channels=_velocity, reason=reason,
                            monotonic=time.monotonic(), simulated=not real))
        tello = _tello
    if not real: return True
    if tello is None: return False
    try:
        tello.send_rc_control(*applied)
        return True
    except Exception as exc:
        _link_fault('rc_control: %s' % exc)
        return False


def set_speed(cm_s):
    global _speed_cm_s
    if type(cm_s) is not int or not MIN_SPEED_CM_S <= cm_s <= MAX_SPEED_CM_S:
        raise ValueError('speed_out_of_range:%s' % (cm_s,))
    with _lock: _speed_cm_s, tello = cm_s, _tello
    if tello is None: return cm_s
    ok, _error = _call_real(lambda: tello.set_speed(cm_s),
                            action='speed', deadline=12.0)
    return ok


def interrupt(action):
    # 先把尚未发出的排队命令作废，再零速度 + 派发降落，任何一步都不等网络。
    cancel_pending('interrupt:%s' % action)
    set_velocity((0, 0, 0, 0), action)
    with _lock:
        _events.append(dict(event='priority_sent', action=action,
                            monotonic=time.monotonic(), simulated=SIMULATED))
        tello, real = _tello, not SIMULATED
    if real and tello is not None and action == 'land': _dispatch_land(tello)


def _dispatch_land(tello):
    """Landing is fire-and-forget here: the caller only needs the command out."""
    def worker():
        global _flying, _height_cm
        while not _command_lock.acquire(timeout=LAND_DISPATCH_TIMEOUT_S):
            _record('land_deferred', reason='command_busy')
            with _lock:
                if _tello is not tello: return
        try:
            tello.land()
            with _lock:
                _flying, _height_cm = False, 0
            _set_link_ok()
            _record('land_sent', action='land', blocking=False)
        except Exception as exc:
            _link_fault('land: %s' % exc)
        finally:
            _command_lock.release()

    threading.Thread(target=worker, daemon=True, name='tello-land').start()


def snapshot():
    with _lock:
        return dict(velocity=_velocity, events=list(_events), simulated=SIMULATED,
                    connected=_tello is not None, link_ok=_link_ok, flying=_flying,
                    height_cm=_height_cm, battery=None,
                    speed_cm_s=_speed_cm_s, last_error=_last_error)


def health():
    with _lock:
        tello, connected = _tello, _tello is not None
        unknown = sorted(_unknown_actions) if not SIMULATED else []
        data = dict(mode='simulated' if SIMULATED else 'real', simulated=SIMULATED,
                    connected=connected, link_ok=_link_ok, faults=_faults,
                    last_error=_last_error, flying=_flying, height_cm=_height_cm,
                    speed_cm_s=_speed_cm_s, velocity=_velocity,
                    video=_video is not None, battery=None, flight_time_s=None,
                    ceiling_cm=MAX_HEIGHT_CM, max_speed_cm_s=MAX_SPEED_CM_S,
                    action_unknown=unknown)
    if connected and tello is not None:
        for key, name in (('battery', 'get_battery'), ('flight_time_s', 'get_flight_time')):
            getter = getattr(tello, name, None)     # 驱动替身可能没有这些方法
            if not callable(getter): continue
            try:
                value = getter()
            except Exception:
                continue
            if type(value) is int and value >= 0: data[key] = value
        data['height_cm'] = _current_height(tello)
        if unknown:
            # 有“结果未知”的动作时，用这一轮真实遥测修正状态。
            reconcile_state(tello)
            with _lock: data['flying'], data['action_unknown'] = _flying, sorted(_unknown_actions)
    return data


def _tello_class(timeout):
    import djitellopy

    # djitellopy 的 send_control_command 被 @enforce_types 校验，timeout 必须是 int
    timeout = max(1, int(round(float(timeout))))

    class _Tello(djitellopy.Tello):
        RESPONSE_TIMEOUT = timeout
        FRAME_GRAB_TIMEOUT = timeout
        def __init__(self, host=None):
            # 基类 __init__ 的 host 默认值在定义时就绑死成 TELLO_IP，必须显式传
            super().__init__(**({} if host is None else {'host': host}))
            self.retry_count = 1
        def send_control_command(self, command, timeout=timeout):
            return djitellopy.Tello.send_control_command(self, command, timeout=timeout)

    return _Tello


def connect(address=None, *, timeout=RESPONSE_TIMEOUT_S):
    global _tello, SIMULATED, _link_ok, _faults, _last_error, _height_cm, _flying, _keepalive_stop
    with _lock:
        if _tello is not None: return health()
    if address and not re.match(r'^\d{1,3}(\.\d{1,3}){3}$', str(address)):
        raise ValueError('invalid_address:%s' % (address,))
    try:
        tello_class = _tello_class(timeout)
    except Exception as exc:
        raise RuntimeError('djitellopy_unavailable: %s' % exc)
    tello, ready_error = None, ''
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        candidate = tello_class(str(address) if address else None)
        try:
            candidate.connect()
            tello = candidate
            break
        except Exception as exc:
            ready_error = '%s: %s' % (type(exc).__name__, exc)
            _record('connect_retry', attempt=attempt, error=ready_error[:200],
                    max_attempts=CONNECT_ATTEMPTS)
            try: candidate.end()
            except Exception: pass
            if attempt < CONNECT_ATTEMPTS: time.sleep(CONNECT_RETRY_DELAY_S)
    if tello is None:
        raise RuntimeError('tello_not_ready: %s' % ready_error)
    with _lock:
        _tello, SIMULATED = tello, False
        _link_ok, _faults, _last_error = True, 0, ''
        _flying, _height_cm = False, 0
        stop = _keepalive_stop = threading.Event()
    _record('connected', address=getattr(tello, 'address', (address or 'unknown', 0))[0])
    set_speed(DEFAULT_SPEED_CM_S)
    threading.Thread(target=_keepalive_loop, args=(tello, stop), daemon=True,
                     name='tello-keepalive').start()
    return health()


def _keepalive_loop(tello, stop):
    """保活：用**无响应**的 ``rc 0 0 0 0``，不用 ``keepalive`` 命令。

    实测（Tello 非 EDU）：``keepalive`` 返回 ``unknown command: keepalive``，
    djitellopy 会把它当失败抛异常；而且这条响应会污染紧接着的 ``land``
    响应队列，导致 land 第一次返回 ``error``（现场复现两次）。
    ``rc`` 是不需要响应的指令，既能重置 15 秒自动降落计时器，又不占响应队列。
    """
    while not stop.wait(KEEPALIVE_INTERVAL_S):
        with _lock: flying, linked = _flying, _link_ok
        if not flying or not linked: continue
        if action_pending(): continue        # 有在途命令时不插队
        try:
            tello.send_rc_control(0, 0, 0, 0)
            _record('keepalive_sent', mode='rc_zero')
        except Exception as exc:
            _link_fault('keepalive: %s' % exc)


def motors_on():
    """Spin the motors in idle (cooling) mode; never lifts off by itself.

    Standard Tello firmware does not implement Motor On and answers ``error``;
    use ``takeoff`` to verify the motors on real hardware.
    """
    with _lock: tello = _tello
    _record('motor_test', state='on')
    if tello is None: return False
    ok, _error = _call_real(tello.turn_motor_on, action='motoron', deadline=12.0)
    return ok


def motors_off():
    with _lock: tello = _tello
    _record('motor_test', state='off')
    if tello is None: return False
    ok, _error = _call_real(tello.turn_motor_off, action='motoroff', deadline=12.0)
    return ok


def stream_video_only():
    """Send `streamon` without starting a decoder (lets a raw probe bind 11111 first)."""
    with _lock: tello = _tello
    if tello is None: return False
    ok, _error = _call_real(tello.streamon, action='streamon',
                            deadline=COMMAND_DEADLINE_S['land'])
    if ok: _record('video_stream_on', origin='streamon_only')
    return ok


def start_video(backend=None, wait=0.0):
    """Start the video pipeline.

    backend 默认取 VIDEO_BACKEND='av'（现场已用真机验证出图）；
    backend='djitellopy' 仅作对照。wait>0 时等第一帧真实画面，超时未拿到返回 False。
    """
    global _video
    backend = VIDEO_BACKEND if backend is None else backend
    if backend not in VIDEO_BACKENDS:
        raise ValueError('invalid_video_backend:%s' % (backend,))
    with _lock: tello = _tello
    if tello is None: return False
    if not stream_video_only(): return False
    try:
        if backend == 'djitellopy':
            reader = tello.get_frame_read()
        else:
            reader = _VideoDecoder(tello.get_udp_video_address())
            reader.start()
    except Exception as exc:
        _link_fault('frame_read: %s' % exc)
        return False
    with _lock: _video = reader
    _record('video_started', origin='tello_stream', backend=backend)
    if wait:
        frame, stats = await_video(wait)
        if frame is None:
            _record('video_no_frame', backend=backend, stats=stats)
            return False
    return True


def stop_video():
    global _video
    with _lock:
        tello, reader, _video = _tello, _video, None
    stop = getattr(reader, 'stop', None)
    if callable(stop):
        try: stop()
        except Exception as exc: _record('video_stop_failed', error=str(exc))
    if tello is None: return False
    ok, _error = _call_real(tello.streamoff, action='streamoff',
                            deadline=COMMAND_DEADLINE_S['land'])
    time.sleep(.2)  # 让解码线程彻底释放 11111 端口
    return ok


_PLACEHOLDER_SHAPE = (300, 400, 3)
VIDEO_BACKEND = 'av'          # 现场已验证：自研 av 后端（真机实测 720x960 出图）
VIDEO_BACKENDS = ('av', 'djitellopy')
VIDEO_URL_OPTIONS = '?overrun_nonfatal=1&fifo_size=50000000'


class _VideoDecoder(threading.Thread):
    """自研 PyAV 解码线程：读超时 + 自动重连 + 错误可见。

    djitellopy 自带的 BackgroundFrameRead 只捕获 av.error.ExitError，其它解码
    异常会让线程静默退出，之后永远返回 300x400 全零占位帧——现场表现为"黑屏"。
    这里直接 decode 到 bgr24，既省掉 PIL 的 RGB 中间层，也能把失败原因暴露出来。
    """

    def __init__(self, url, open_timeout=5, read_timeout=2):
        super().__init__(daemon=True, name='video-decoder')
        self.url = url
        self.open_timeout = open_timeout
        self.read_timeout = read_timeout
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._frame = None
        self.count = 0
        self.reconnects = 0
        self.errors = []
        self.opened = 0
        self.last_frame_at = 0.0
        self._container = None

    def _note(self, text):
        with self._lock:
            self.errors.append(text)
            del self.errors[:-6]

    def run(self):
        import av
        while not self._halt.is_set():
            container = None
            for url in (self.url + VIDEO_URL_OPTIONS, self.url):
                try:
                    container = av.open(url, timeout=(self.open_timeout, self.read_timeout))
                    break
                except Exception as exc:
                    self._note('open(%s): %s' % ('opts' if url.endswith('50') else 'plain', exc))
            if container is None:
                self._halt.wait(1.0)
                self.reconnects += 1
                continue
            with self._lock:
                self.opened += 1
                self._container = container
            try:
                for packet in container.decode(video=0):
                    if self._halt.is_set():
                        break
                    try:
                        frame = packet.to_ndarray(format='bgr24')
                    except Exception as exc:
                        self._note('to_ndarray: %s' % exc)
                        continue
                    with self._lock:
                        self._frame = frame
                        self.count += 1
                        self.last_frame_at = time.monotonic()
                        self.last_frame_timestamp = time.time()
            except Exception as exc:
                self._note('decode: %s' % exc)
            finally:
                with self._lock:
                    self._container = None
                try:
                    container.close()
                except Exception:
                    pass
            if self._halt.is_set():
                break
            self.reconnects += 1
            self._halt.wait(.3)

    @property
    def frame(self):
        with self._lock:
            return self._frame

    def stats(self):
        with self._lock:
            frame = self._frame
            return dict(backend='av', count=self.count, reconnects=self.reconnects,
                        opened=self.opened, errors=list(self.errors),
                        age=round(time.monotonic() - self.last_frame_at, 2) if self.count else None,
                        shape=list(frame.shape) if frame is not None else None,
                        mean=round(float(frame.mean()), 2) if frame is not None else None)

    def stop(self, timeout=3.0):
        """停止解码并等到线程退出。

        绝不从外部线程 close 容器：FFmpeg 正在 decode 时跨线程关闭会直接崩溃
        (0xC0000005)。这里靠 read_timeout 让 decode 自己返回，再 join。
        """
        self._halt.set()
        if self.is_alive():
            self.join(timeout)


def _is_placeholder(frame):
    """djitellopy 在收到真实帧之前会一直给出 300x400 的全零数组。"""
    return tuple(frame.shape) == _PLACEHOLDER_SHAPE and not frame.any()


def get_frame():
    """Latest frame from the drone camera as BGR, or None when unavailable.

    The av backend decodes straight to BGR; djitellopy's PIL path yields RGB and
    is swapped here once. Its pre-stream placeholder (300x400 zeros) is reported
    as None, so callers can wait for a real picture instead of saving a black one.
    """
    with _lock: reader = _video
    if reader is None: return None
    frame = reader.frame
    if frame is None: return None
    if isinstance(reader, _VideoDecoder):
        return frame if reader.count and 0 <= time.monotonic()-reader.last_frame_at <= .5 else None
    if _is_placeholder(frame): return None
    import numpy
    if frame.ndim == 2:
        frame = numpy.repeat(frame[:, :, None], 3, axis=2)
    return numpy.ascontiguousarray(frame[:, :, ::-1])


def video_stats():
    """Diagnostics for the live stream (backend, counters, errors)."""
    with _lock: reader = _video
    if reader is None:
        return dict(reader=False, backend='none', placeholder=False, shape=None,
                    mean=None, max=None)
    if isinstance(reader, _VideoDecoder):
        stats = reader.stats()
        stats.update(reader=True, placeholder=False, max=None)
        return stats
    frame = reader.frame
    if frame is None:
        return dict(reader=True, backend='djitellopy', placeholder=False, shape=None,
                    mean=None, max=None)
    import numpy
    values = numpy.asarray(frame)
    return dict(reader=True, backend='djitellopy', placeholder=_is_placeholder(frame),
                shape=list(values.shape), mean=round(float(values.mean()), 2),
                max=int(values.max()))


def await_video(timeout=8.0, interval=.05):
    """Wait for the first real frame; the zero placeholder never counts."""
    deadline = time.monotonic() + max(0., float(timeout))
    while True:
        frame = get_frame()
        if frame is not None: return frame, video_stats()
        if time.monotonic() >= deadline: return None, video_stats()
        time.sleep(interval)


def disconnect(*, safe=True):
    global _tello, _video, _keepalive_stop, SIMULATED, _flying, _height_cm
    with _lock:
        tello, _tello = _tello, None
        reader, _video = _video, None
        stop, _keepalive_stop = _keepalive_stop, None
        _unknown_actions.clear()
        _unknown_since.clear()
        _pending_commands.clear()
    if stop is not None: stop.set()
    stopper = getattr(reader, 'stop', None)
    if callable(stopper):
        try: stopper()
        except Exception as exc: _record('video_stop_failed', error=str(exc))
    if tello is None:
        with _lock: SIMULATED = True
        return False
    if safe:
        try:
            tello.send_rc_control(0, 0, 0, 0)
        except Exception as exc:
            _record('disconnect_rc_failed', error=str(exc))
    try:
        tello.end()
    except Exception as exc:
        _record('disconnect_failed', error=str(exc))
    with _lock:
        SIMULATED, _flying, _height_cm = True, False, 0
    _record('disconnected')
    return True


def capture_metadata(frame):
    """Return decoder acquisition metadata atomically; never turn polling into capture."""
    with _lock: reader=_video
    if not isinstance(reader,_VideoDecoder): return None
    with reader._lock:
        if reader._frame is not frame: return False
        return reader.count, getattr(reader,'last_frame_timestamp',0.)
