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


def _current_height(tello=None):
    global _height_cm
    with _lock: known, tello = _height_cm, tello or _tello
    if tello is None: return known
    try:
        value = tello.get_height()
    except Exception:
        return known
    if type(value) is not int or value < 0: return known
    with _lock: _height_cm = value
    return value


def _plan_ascent(cm):
    height = _current_height()
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
    height = _current_height()
    if up_down > 0 and height >= MAX_HEIGHT_CM:
        _record('height_limit_clamped', requested=up_down, height_cm=height, channel='up_down')
        up_down = 0
    if up_down < 0 and height <= 0: up_down = 0
    return (left_right, forward_back, up_down, yaw)


def _call_real(fn, *, action, deadline):
    """Run one blocking Tello command on a worker and cap the caller's wait."""
    outcome = {'ok': False, 'error': 'timeout'}
    done = threading.Event()

    def runner():
        if not _command_lock.acquire(timeout=max(1.0, deadline)):
            outcome['error'] = 'command_busy'
            done.set()
            return
        try:
            fn()
            outcome['ok'], outcome['error'] = True, ''
        except Exception as exc:
            outcome['error'] = '%s: %s' % (type(exc).__name__, exc)
        finally:
            _command_lock.release()
            done.set()

    threading.Thread(target=runner, daemon=True, name='tello-command').start()
    if not done.wait(deadline):
        _link_fault('deadline_exceeded:%s' % action)
        return False, 'timeout'
    if outcome['ok']:
        _set_link_ok()
        return True, ''
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
    global _flying, _height_cm
    with _lock:
        tello = _tello
        if tello is None: return False
        if not _link_ok:
            _events.append(dict(event='action_rejected', action=action, reason='link_down',
                                monotonic=time.monotonic(), simulated=False))
            return False
    _record('action_sent', action=action, param=param)
    if action in ('takeoff', 'land'):
        ok, _error = _call_real(getattr(tello, action), action=action,
                                deadline=_move_deadline(action, None))
        if ok:
            with _lock:
                _flying = action == 'takeoff'
                if action == 'land': _height_cm = 0
            if action == 'takeoff': _current_height(tello)
        return ok
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


def set_velocity(channels, reason):
    global _velocity
    with _lock: real = not SIMULATED
    applied = _sanitise_rc(channels) if real else tuple(channels)
    with _lock:
        _velocity = applied
        _events.append(dict(event='velocity', channels=_velocity, reason=reason,
                            monotonic=time.monotonic(), simulated=not real))
        tello = _tello
    if not real or tello is None: return
    try:
        tello.send_rc_control(*applied)
    except Exception as exc:
        _link_fault('rc_control: %s' % exc)


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
        if not _command_lock.acquire(timeout=LAND_DISPATCH_TIMEOUT_S):
            _record('land_deferred', reason='command_busy')
            return
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
        data = dict(mode='simulated' if SIMULATED else 'real', simulated=SIMULATED,
                    connected=connected, link_ok=_link_ok, faults=_faults,
                    last_error=_last_error, flying=_flying, height_cm=_height_cm,
                    speed_cm_s=_speed_cm_s, velocity=_velocity,
                    video=_video is not None, battery=None, flight_time_s=None,
                    ceiling_cm=MAX_HEIGHT_CM, max_speed_cm_s=MAX_SPEED_CM_S)
    if connected and tello is not None:
        for key, getter in (('battery', tello.get_battery), ('flight_time_s', tello.get_flight_time)):
            try:
                value = getter()
            except Exception:
                continue
            if type(value) is int and value >= 0: data[key] = value
        data['height_cm'] = _current_height(tello)
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
    tello = tello_class(str(address) if address else None)
    tello.connect()
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
    while not stop.wait(KEEPALIVE_INTERVAL_S):
        with _lock: flying, linked = _flying, _link_ok
        if not flying or not linked: continue
        try:
            tello.send_keepalive()
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
    if isinstance(reader, _VideoDecoder): return frame
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
