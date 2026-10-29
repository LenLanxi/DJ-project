"""Bottom communication layer: offline simulator by default, real Tello on opt-in.

Simulation stays the default so every offline suite keeps running with no
hardware; `connect()` is the only door into real mode and has to be called
explicitly (diagnostics/hardware/Ground.py does that). Records prove software ordering,
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
import math
import re
import threading
import time
import traceback

from .contract import FrameResult

SIMULATED = True
replay_capture = None

MAX_SPEED_CM_S = 100
MIN_SPEED_CM_S = 10
DEFAULT_SPEED_CM_S = 50
MAX_HEIGHT_CM = 250
RC_LIMIT = 100

RESPONSE_TIMEOUT_S = 5.0
COMMAND_DEADLINE_S = {'takeoff': 25.0, 'land': 20.0}
LAND_DISPATCH_TIMEOUT_S = 6.0
KEEPALIVE_INTERVAL_S = 4.0
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
# Only the final UDP send holds this lock, never a network response wait.
# Cancellation and sending have a single ordering point: once cancellation
# returns, an old command cannot subsequently reach socket.sendto().
_send_gate = threading.RLock()
_command_context = threading.local()
_delay = 0.0
_velocity = (0, 0, 0, 0)
_events = deque(maxlen=2000)
_event_sequence = 0
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
_landing_acknowledged = False
_response_quarantine = None
_last_wire_send = 0.
TAKEOFF_CONFIRM_CM = 10
RECONCILE_SETTLE_S = 2.0
TELEMETRY_MAX_AGE_S = 1.0


def capture_cancel_epoch():
    with _lock:
        return _cancel_epoch


def command_channel_clear():
    with _lock:
        return _response_quarantine is None


def command_warning():
    with _lock:
        if _response_quarantine is None:
            return {}
        return {'command_channel_ambiguous': True,
                'command_uncertainty': dict(_response_quarantine),
                'reply_association_verified': False,
                'warning': 'Earlier command outcome is unknown. Further commands are allowed; '
                           'a reply may belong to an earlier command. Observe before deciding the next action; '
                           'do not assume the earlier action did not execute or blindly repeat it.'}


def _mark_response_unknown(action, reason):
    """UDP replies carry no command IDs. A late reply cannot safely answer B.

    Diagnostic only: keep the uncertainty visible without blocking later commands.
    A later reply cannot prove which command the aircraft acknowledged.
    """
    global _response_quarantine
    with _lock:
        if _response_quarantine is None:
            _response_quarantine = {'action':action, 'reason':reason, 'timestamp':time.time()}
        _unknown_actions.add(action)
    _record('command_channel_ambiguous', action=action, reason=reason)

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
    with _send_gate:
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
    global _event_sequence
    with _lock:
        _event_sequence += 1
        _events.append(dict(event=event, monotonic=time.monotonic(),
                            recorded_at=time.time(), sequence=_event_sequence,
                            thread=threading.current_thread().name, simulated=SIMULATED, **fields))


def events_since(sequence):
    """Copy the bounded journal; never perform file I/O on SDK/UDP threads."""
    with _lock:
        rows = [dict(row) for row in _events if row['sequence'] > sequence]
        missed = max(0, rows[0]['sequence'] - sequence - 1) if rows else 0
        return rows, _event_sequence, missed


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


def _telemetry(tello=None):
    """Return the last received packet and its receipt age, never a poll time."""
    with _lock:
        target = tello or _tello
    if target is None:
        return {}, None, None, False
    try:
        packet = target.get_current_state()
        stamp = packet.get('_received_at')
        mono = packet.get('_received_monotonic')
        age = time.monotonic() - mono if type(mono) in (int, float) else None
        fresh = bool(type(stamp) in (int, float) and age is not None
                     and math.isfinite(stamp) and math.isfinite(age)
                     and 0 <= age <= TELEMETRY_MAX_AGE_S)
        return packet, stamp, age, fresh
    except Exception:
        return {}, None, None, False


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
    packet, _stamp, _age, fresh = _telemetry(tello)
    if not fresh:
        return (None, 'stale_telemetry')
    height = None
    try:
        height = tello.get_height()
    except Exception:
        height = None
    if type(height) is int and height > 0:
        return (height, 'baro')
    # A software flag is not a measurement: after a timed-out takeoff it can
    # still say False while the vehicle is already airborne. Always inspect ToF.
    tof = None
    try:
        tof = tello.get_distance_tof()
    except Exception:
        tof = None
    if type(tof) is int and TOF_MIN_CM <= tof <= TOF_MAX_TRUSTED_CM:
        return (tof, 'tof')
    if not flying and type(height) is int and height == 0 and type(tof) is int and 0 <= tof < TOF_MIN_CM:
        return (0, 'ground_telemetry')
    return (None, 'unknown')


def _read_battery(tello=None):
    """读一次真实电量；读不到就返回 None（绝不猜数字）。"""
    if tello is None:
        with _lock: tello = _tello
    if tello is None: return None
    try: value = tello.get_battery()
    except Exception: return None
    return value if type(value) is int else None


def _current_height(tello=None):
    global _height_cm
    value, _source = _height_probe(tello)
    if value is None:
        with _lock: return _height_cm     # 保持旧返回值语义；上升决策走 _height_probe
    with _lock: _height_cm = value
    return value


def _plan_ascent(cm, details=None):
    height, source = _height_probe()
    if height is None:
        if details is not None: details.update(stage='height_check', reason='height_unknown', source=source)
        _record('height_limit_rejected', requested=cm, height_cm=None,
                ceiling_cm=MAX_HEIGHT_CM, reason='height_unknown', source=source)
        return None
    room = MAX_HEIGHT_CM - height
    if cm <= room: return cm
    if room < 20:
        if details is not None:
            details.update(stage='height_check', reason='height_limit', height_cm=height, ceiling_cm=MAX_HEIGHT_CM)
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


def _call_real(fn, *, action, deadline, epoch=None, on_success=None, guard=None, details=None):
    """Run one blocking Tello command on a worker and cap the caller's wait.

    取消语义：调用时绑定当时的取消编号；worker 真正拿到命令锁后、调用 fn 之前
    再比对一次。若期间发生过 cancel_pending（急停/降落/停止），命令直接丢弃，
    绝不发给飞机。
    """
    started = time.monotonic()
    outcome = {'ok': False, 'error': 'timeout', 'wire_sent': False, 'stage': 'command_lock'}
    def finish(ok, error):
        detail = dict(outcome, action=action, elapsed_seconds=round(time.monotonic()-started, 4),
                      caller_deadline_seconds=deadline, reason=error,
                      outcome_unknown=bool(not ok and not outcome.get('reply_received') and
                                           (outcome['wire_sent'] or error.startswith('caller_deadline_'))))
        detail.pop('ok', None)
        detail.pop('error', None)
        if details is not None: details.update(detail)
        if not ok: _record('action_error', **detail)
        return ok, error
    with _lock:
        bound_epoch = _cancel_epoch if epoch is None else epoch
    done = threading.Event()

    with _lock: _pending_commands.add(done)
    def runner():
        if not _command_lock.acquire(timeout=max(1.0, deadline)):
            outcome['error'] = 'command_busy'
            with _lock: _pending_commands.discard(done)
            done.set()
            return
        try:
            _command_context.epoch = bound_epoch
            _command_context.action = action
            _command_context.outcome = outcome
            _command_context.guard = guard
            _command_context.deadline = started + deadline
            with _lock: cancelled = _cancel_epoch != bound_epoch
            if cancelled:
                outcome['error'] = 'cancelled_before_send'
                _record('command_dropped', action=action, reason='cancelled_before_send',
                        bound_epoch=bound_epoch, cancel_epoch=_cancel_epoch)
            elif guard is not None and guard.is_set() and action not in ('land', 'streamoff'):
                outcome['error'] = 'operation_cancelled_before_send'
                _record('command_dropped', action=action, reason='operation_cancelled_before_send')
            else:
                outcome['stage'] = 'send'
                fn()
                outcome['stage'] = 'completed'
                if on_success is not None:
                    on_success()
                outcome['ok'], outcome['error'] = True, ''
        except Exception as exc:
            outcome['error_type'] = type(exc).__name__
            _record('command_exception', action=action, error=str(exc), traceback=traceback.format_exc())
            outcome['error'] = (str(exc) if str(exc) in ('cancelled_before_send', 'cancelled_after_send',
                                                        'response_timeout_after_send','command_channel_ambiguous',
                                                        'operation_cancelled_before_send','caller_deadline_before_send')
                                else '%s: %s' % (type(exc).__name__, exc))
        finally:
            _command_context.epoch = None
            _command_context.action = None
            _command_context.outcome = None
            _command_context.guard = None
            _command_context.deadline = None
            _command_lock.release()
            with _lock: _pending_commands.discard(done)
            done.set()

    threading.Thread(target=runner, daemon=True, name='tello-command').start()
    if not done.wait(deadline):
        if outcome['wire_sent']:
            _mark_response_unknown(action, 'caller_deadline_after_send')
        _link_fault('deadline_exceeded:%s' % action)
        return finish(False, 'caller_deadline_after_send' if outcome['wire_sent'] else 'caller_deadline_before_send')
    if outcome['ok']:
        _set_link_ok()
        return finish(True, '')
    if outcome['error'] in ('cancelled_before_send', 'cancelled_after_send', 'command_channel_ambiguous',
                            'operation_cancelled_before_send'):
        return finish(False, outcome['error'])          # 主动丢弃不是链路故障
    _link_fault(outcome['error'])
    return finish(False, outcome['error'])


def _move_deadline(action, param):
    if action in COMMAND_DEADLINE_S: return COMMAND_DEADLINE_S[action]
    magnitude = abs(param or 0)
    with _lock: speed = max(MIN_SPEED_CM_S, _speed_cm_s)
    return (12.0 + magnitude / 45.0) if action == 'rotate' else (8.0 + 2 * magnitude / speed)


def _execute_move(tello, action, cm):
    if action == 'rotate':
        if cm >= 0: tello.rotate_clockwise(cm)
        else: tello.rotate_counter_clockwise(-cm)
    else:
        getattr(tello, 'move_%s' % action)(cm)


def _execute_real(action, param=None, *, epoch=None, guard=None, details=None):
    global _flying, _height_cm, _last_error
    with _lock:
        tello = _tello
        if tello is None:
            if details is not None: details.update(stage='connection', reason='not_connected')
            return False
    _record('action_queued', action=action, param=param)
    if action in ('takeoff', 'land'):
        def acknowledged():
            global _flying, _height_cm, _landing_acknowledged
            with _lock:
                if _tello is not tello:
                    return
                _flying = action == 'takeoff'
                tello._motor_stop_reported = False
                _landing_acknowledged = action == 'land'
                if action == 'land': _height_cm = 0
            _record('action_acknowledged', action=action)
        ok, error = _call_real(getattr(tello, action), action=action,
                               deadline=_move_deadline(action, None), epoch=epoch,
                               on_success=acknowledged, guard=guard, details=details)
        if ok:
            if action == 'takeoff': _current_height(tello)
            return True
        if action == 'takeoff' and error in ('timeout', 'caller_deadline_after_send', 'caller_deadline_before_send',
                                                     'cancelled_after_send', 'response_timeout_after_send'):
            # 超时/被丢弃都不等于没起飞：标记结果未知，交给真实遥测修正，绝不擅自改回 landed。
            token = object()
            with _lock:
                _unknown_actions.add('takeoff')
                _unknown_since['takeoff'] = (time.monotonic(), token)
            _record('action_unknown', action='takeoff', error=error)
            threading.Thread(target=_reconcile_later, args=(tello, token), daemon=True,
                             name='tello-reconcile').start()
        elif action == 'takeoff' and error not in ('command_busy', 'link_down','cancelled_before_send',
                                                 'command_channel_ambiguous','operation_cancelled_before_send'):
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
        cm = _plan_ascent(param, details=details) if action == 'up' else (
             _plan_descent(param) if action == 'down' else param)
        if cm is None: return False
        ok, _error = _call_real(lambda: _execute_move(tello, action, cm), action=action,
                                deadline=_move_deadline(action, cm), epoch=epoch, guard=guard, details=details)
        if ok:
            if action in ('up', 'down'):
                with _lock:
                    _height_cm = max(0, _height_cm + (cm if action == 'up' else -cm))
                _current_height(tello)
        return ok
    if action == 'rotate':
        ok, _error = _call_real(lambda: _execute_move(tello, action, param), action=action,
                                deadline=_move_deadline(action, param), epoch=epoch, guard=guard, details=details)
        return ok
    if details is not None: details.update(stage='dispatch', reason='unknown_action')
    _record('action_rejected', action=action, reason='unknown_action', param=param)
    return False


def execute(action, param=None, *, epoch=None, guard=None, details=None):
    with _lock:
        real, delay = not SIMULATED, _delay
    if not real:
        if guard is not None and guard.is_set() and action != 'land':
            _record('command_dropped', action=action, reason='operation_cancelled_before_send')
            return False
        if epoch is not None and epoch != capture_cancel_epoch():
            _record('command_dropped', action=action, reason='cancelled_before_send')
            return False
        _record('action_sent', action=action, param=param)
        if delay: time.sleep(delay)
        return True
    return _execute_real(action, param, epoch=epoch, guard=guard, details=details)


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
    height, source = _height_probe(target)
    with _lock:
        resolved = False
        if 'takeoff' in unknown:
            since = _unknown_since.get('takeoff', (0.0, None))[0]
            settled = time.monotonic() - since >= RECONCILE_SETTLE_S
            if _flying or (source == 'baro' and height is not None and height >= TAKEOFF_CONFIRM_CM):
                _flying, resolved = True, True
                _unknown_actions.discard('takeoff')
                _unknown_since.pop('takeoff', None)
                _record('action_reconciled', action='takeoff', result='flying', height_cm=height)
            elif (settled and not _pending_commands and height == 0 and source == 'ground_telemetry'
                    and _response_quarantine is None):
                resolved = True                       # 只有过了稳定期且遥测为地面才敢下结论
                _unknown_actions.discard('takeoff')
                _unknown_since.pop('takeoff', None)
                _record('action_reconciled', action='takeoff', result='not_flying', height_cm=height)
        return resolved


def set_velocity(channels, reason, *, guard=None):
    global _velocity
    with _lock: real = not SIMULATED
    applied = _sanitise_rc(channels) if real else tuple(channels)
    if any(applied) and guard is not None and guard.is_set():
        _record('velocity_rejected', reason='operation_cancelled_before_send')
        return False
    with _lock:
        _velocity = applied
        _record('velocity', channels=_velocity, reason=reason)
        tello = _tello
    if not real: return True
    if tello is None: return False
    previous_guard = getattr(_command_context, 'guard', None)
    previous_origin = getattr(_command_context, 'origin', None)
    _command_context.guard = guard
    _command_context.origin = reason
    try:
        tello.send_rc_control(*applied)
        _set_link_ok()
        return True
    except Exception as exc:
        if str(exc) == 'operation_cancelled_before_send':
            return False  # An expired local evidence/cancellation guard is not a link fault.
        _link_fault('rc_control: %s' % exc)
        return False
    finally:
        _command_context.guard = previous_guard
        _command_context.origin = previous_origin


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
        _record('priority_sent', action=action)
        tello, real = _tello, not SIMULATED
    if real and tello is not None and action == 'land': _dispatch_land(tello)


def _dispatch_land(tello):
    """Landing is fire-and-forget here: the caller only needs the command out."""
    global _landing_acknowledged
    pending = threading.Event()
    with _lock:
        _landing_acknowledged = False
        _pending_commands.add(pending)
    def worker():
        global _flying, _height_cm, _landing_acknowledged
        while not _command_lock.acquire(timeout=LAND_DISPATCH_TIMEOUT_S):
            _record('land_deferred', reason='command_busy')
            with _lock:
                if _tello is not tello:
                    _pending_commands.discard(pending)
                    pending.set()
                    return
        try:
            with _lock:
                if _tello is not tello: return
            tello.land()
            with _lock:
                _flying, _height_cm = False, 0
                _landing_acknowledged = True
            _set_link_ok()
            _record('land_sent', action='land', blocking=False)
        except Exception as exc:
            _link_fault('land: %s' % exc)
        finally:
            _command_lock.release()
            with _lock: _pending_commands.discard(pending)
            pending.set()

    threading.Thread(target=worker, daemon=True, name='tello-land').start()


def snapshot():
    with _lock:
        return dict(velocity=_velocity, events=list(_events), simulated=SIMULATED,
                    connected=_tello is not None, link_ok=_link_ok, flying=_flying,
                    height_cm=_height_cm, battery=None,
                    speed_cm_s=_speed_cm_s, last_error=_last_error)


def health():
    global _flying, _height_cm
    with _lock:
        tello, connected = _tello, _tello is not None
        unknown = sorted(_unknown_actions) if not SIMULATED else []
        data = dict(mode='simulated' if SIMULATED else 'real', simulated=SIMULATED,
                    connected=connected, link_ok=_link_ok, faults=_faults,
                    last_error=_last_error, flying=_flying, height_cm=_height_cm,
                    speed_cm_s=_speed_cm_s, velocity=_velocity,
                    video=_video is not None, battery=None, flight_time_s=None,
                    ceiling_cm=MAX_HEIGHT_CM, max_speed_cm_s=MAX_SPEED_CM_S,
                    action_unknown=unknown, landing_acknowledged=_landing_acknowledged,
                    command_channel_ambiguous=_response_quarantine is not None,
                    command_uncertainty=dict(_response_quarantine) if _response_quarantine else None,
                    telemetry_timestamp=None,
                    telemetry_age_s=None, telemetry_fresh=False,
                    temperature_c=None, height_source='unavailable')
    if connected:
        packet, stamp, age, fresh = _telemetry(tello)
        data.update(telemetry_timestamp=stamp, telemetry_age_s=age,
                    telemetry_fresh=fresh)
        data['last_wire_send_age_s'] = time.monotonic() - _last_wire_send
        last_keepalive = getattr(tello, '_last_keepalive_sent', None)
        data['last_keepalive_age_s'] = None if last_keepalive is None else time.monotonic() - last_keepalive
        # A stopped-motor reply plus fresh ground telemetry contradicts the
        # cached takeoff flag. Low height alone can be an obstacle below us.
        with _lock:
            if (getattr(tello, '_motor_stop_reported', False) and fresh
                    and packet.get('h') == 0
                    and type(packet.get('tof')) is int and 0 <= packet['tof'] < TOF_MIN_CM
                    and not _pending_commands and not _unknown_actions):
                if _flying:
                    _record('flight_state_reconciled', reason='motor_stop_and_ground_telemetry')
                _flying, _height_cm = False, 0
                tello.is_flying = False
                data['flying'] = False
        temperatures = [packet.get(k) for k in ('templ', 'temph')]
        temperatures = [v for v in temperatures if type(v) in (int, float) and -20 <= v <= 150]
        if fresh and temperatures:
            data['temperature_c'] = max(temperatures)
        for key, getter in (('battery', tello.get_battery), ('flight_time_s', tello.get_flight_time)):
            try:
                value = getter()
            except Exception:
                continue
            if fresh and type(value) is int and value >= 0:
                data[key] = value
        height, source = _height_probe(tello)
        data['height_cm'], data['height_source'] = height, source
        if unknown:
            # 有“结果未知”的动作时，用这一轮真实遥测修正状态。
            reconcile_state(tello)
            with _lock: data['flying'], data['action_unknown'] = _flying, sorted(_unknown_actions)
    return data


def _wire_send(command, send):
    """The cancellation check immediately adjacent to the actual UDP write.

    The gate is deliberately released before waiting for a reply. A command
    already sent cannot be unsent; an accepted stop prevents later old sends.
    """
    global _last_wire_send
    with _send_gate:
        bound = getattr(_command_context, 'epoch', None)
        with _lock:
            if bound is not None and bound != _cancel_epoch:
                _record('command_dropped', command=command, reason='cancelled_before_wire_send')
                raise RuntimeError('cancelled_before_send')
            safe = command in ('land','streamoff') or command.split() == ['rc','0','0','0','0']
            guard = getattr(_command_context, 'guard', None)
            if not safe and guard is not None and guard.is_set():
                _record('command_dropped', command=command, reason='operation_cancelled_before_send')
                raise RuntimeError('operation_cancelled_before_send')
        try:
            send(command)
        except Exception as exc:
            _record('wire_send_failed', command=command, error=repr(exc))
            raise
        with _lock:
            gap = time.monotonic() - _last_wire_send if _last_wire_send else None
            _last_wire_send = time.monotonic()
            outcome = getattr(_command_context, 'outcome', None)
            if outcome is not None and not command.startswith('rc '):
                outcome['wire_sent'] = True
                outcome['stage'] = 'response_wait'
        _record('wire_sent', command=command, previous_send_gap_s=gap,
                origin=getattr(_command_context, 'origin', None),
                action=getattr(_command_context, 'action', None), cancel_epoch=_cancel_epoch,
                receipt_verified=False)


def _install_telemetry_clock(tello_type):
    """djitellopy 2.5 replaces its state dict per UDP packet but timestamps none.

    Stamp in its parser, which is invoked by the actual UDP receiver. Polling
    getters must never refresh this stamp. No additional socket is created.
    """
    if getattr(tello_type, '_companion_receipt_clock', False):
        return
    original = tello_type.parse_state
    def stamped(raw):
        state = original(raw)
        if state:
            state['_received_at'] = time.time()
            state['_received_monotonic'] = time.monotonic()
            with _lock:
                _record('telemetry_received', state=dict(state), cached_flying=_flying,
                        pending_commands=len(_pending_commands),
                        last_wire_send_age_s=(time.monotonic()-_last_wire_send if _last_wire_send else None))
        return state
    tello_type.parse_state = staticmethod(stamped)
    tello_type._companion_receipt_clock = True


def _tello_class(timeout):
    import djitellopy
    import importlib
    wire = importlib.import_module('djitellopy.tello')
    _install_telemetry_clock(djitellopy.Tello)

    # djitellopy 的 send_control_command 被 @enforce_types 校验，timeout 必须是 int
    timeout = max(1, int(round(float(timeout))))

    class _Tello(djitellopy.Tello):
        RESPONSE_TIMEOUT = timeout
        FRAME_GRAB_TIMEOUT = timeout
        def __init__(self, host=None):
            # 基类 __init__ 的 host 默认值在定义时就绑死成 TELLO_IP，必须显式传
            super().__init__(**({} if host is None else {'host': host}))
            self.retry_count = 1
        def send_command_with_return(self, command, timeout=timeout):
            outcome = getattr(_command_context, 'outcome', None)
            # Copy the narrow 2.5 protocol wait, replacing only its final send
            # with the cancellation gate. Do not hold a lock during a reply.
            elapsed = time.time() - self.last_received_command_timestamp
            if elapsed < self.TIME_BTW_COMMANDS:
                time.sleep(self.TIME_BTW_COMMANDS - elapsed)
            deadline = getattr(_command_context, 'deadline', None)
            if deadline is not None:
                # The action already owns a distance/speed budget. A second
                # fixed five-second wait used to stop valid long moves early.
                timeout = deadline - time.monotonic() - .05
                if timeout <= 0:
                    raise RuntimeError('caller_deadline_before_send')
            if outcome is not None: outcome['response_timeout_seconds'] = timeout
            responses = self.get_own_udp_object()['responses']
            if responses:
                _record('command_replies_discarded', next_command=command,
                        replies_hex=[reply[:256].hex() for reply in responses])
            responses.clear()  # completed previous commands cannot answer this one
            _wire_send(command, lambda value: wire.client_socket.sendto(value.encode('utf-8'), self.address))
            started = time.monotonic()
            while not responses:
                bound = getattr(_command_context, 'epoch', None)
                if bound is not None and bound != capture_cancel_epoch():
                    _mark_response_unknown(getattr(_command_context, 'action', None) or command.split()[0], 'cancelled_after_send')
                    raise RuntimeError('cancelled_after_send')
                if time.monotonic() - started >= timeout:
                    _record('command_reply_timeout', command=command, timeout_s=timeout)
                    _mark_response_unknown(getattr(_command_context, 'action', None) or command.split()[0], 'response_timeout_after_send')
                    raise RuntimeError('response_timeout_after_send')
                time.sleep(.01)
            reply = responses.pop(0)
            _record('command_reply', command=command, reply_hex=reply[:256].hex(),
                    reply_text=reply.decode('utf-8', errors='replace')[:500],
                    elapsed_s=time.monotonic()-started, association='awaited_command_no_protocol_id')
            try:
                response = reply.decode('utf-8').strip()
            except UnicodeDecodeError:
                _record('invalid_command_reply', command=command.split()[0],
                        reply_hex=reply[:256].hex(), reply_bytes=len(reply))
                raise
            outcome = getattr(_command_context, 'outcome', None)
            if outcome is not None:
                outcome.update(reply_received=True, reply=response, stage='response_received')
            self.last_received_command_timestamp = time.time()
            if response.lower() == 'error motor stop':
                self._motor_stop_reported = True
            return response
        def send_command_without_return(self, command):
            _wire_send(command, lambda value: wire.client_socket.sendto(value.encode('utf-8'), self.address))
        def send_rc_control(self, left_right_velocity, forward_backward_velocity, up_down_velocity, yaw_velocity):
            channels = (left_right_velocity, forward_backward_velocity, up_down_velocity, yaw_velocity)
            if not any(channels):
                # A priority zero must reach the final send gate even within
                # the vendor's 1ms rate limit for ordinary RC updates.
                self.send_command_without_return('rc 0 0 0 0')
            else:
                super().send_rc_control(*channels)
        def send_control_command(self, command, timeout=timeout):
            response = self.send_command_with_return(command, timeout=timeout)
            if response.lower() != 'ok':
                self.raise_result_error(command, response)
            return True

    return _Tello


def connect(address=None, *, timeout=RESPONSE_TIMEOUT_S):
    global _tello, SIMULATED, _link_ok, _faults, _last_error, _height_cm, _flying, _keepalive_stop, _landing_acknowledged, _response_quarantine
    with _lock:
        if _tello is not None: return health()
        _response_quarantine = None  # explicit new connection, never a timer
    if address and not re.match(r'^\d{1,3}(\.\d{1,3}){3}$', str(address)):
        raise ValueError('invalid_address:%s' % (address,))
    try:
        tello_class = _tello_class(timeout)
    except Exception as exc:
        raise RuntimeError('djitellopy_unavailable: %s' % exc)
    tello, ready_error = None, ''
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        # No motion has been admitted during bootstrap. Repeating the idempotent
        # SDK handshake may discard a failed attempt's uncorrelated ACK barrier.
        with _lock:
            _response_quarantine = None
            _unknown_actions.clear()
            _unknown_since.clear()
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
            # Dispose the failed object's destructor before a replacement
            # registers the same host in djitellopy's shared drones mapping.
            candidate = None
            if attempt < CONNECT_ATTEMPTS: time.sleep(CONNECT_RETRY_DELAY_S)
    if tello is None:
        raise RuntimeError('tello_not_ready: %s' % ready_error)
    with _lock:
        _tello, SIMULATED = tello, False
        _link_ok, _faults, _last_error = True, 0, ''
        _flying, _height_cm = False, 0
        _landing_acknowledged = False
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
    # Poll separately from the idle threshold: two 8-second waits previously
    # allowed almost 16 seconds between sends, beyond firmware's 15-second limit.
    while not stop.wait(min(.5, KEEPALIVE_INTERVAL_S / 4)):
        with _lock: flying, last_send = _flying, _last_wire_send
        if not flying: continue
        if action_pending(): continue        # 有在途命令时不插队
        if time.monotonic() - last_send < KEEPALIVE_INTERVAL_S: continue
        try:
            _command_context.origin = 'keepalive'
            tello.send_rc_control(0, 0, 0, 0)
            tello._last_keepalive_sent = time.monotonic()
            _record('keepalive_sent', mode='rc_zero')
        except Exception as exc:
            _link_fault('keepalive: %s' % exc)
        finally:
            _command_context.origin = None


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


def start_video(wait=0.0):
    """Start the single bounded PyAV source; it owns capture identity and time."""
    global _video
    if not stream_video_only(): return False
    with _lock: tello = _tello
    try:
        reader = _VideoDecoder(tello.get_udp_video_address())
        reader.start()
    except Exception as exc:
        _link_fault('frame_read: %s' % exc)
        return False
    with _lock: _video = reader
    _record('video_started', origin='tello_stream', backend='av')
    if wait:
        packet, stats = await_video(wait)
        if packet.frame is None:
            _record('video_no_frame', backend='av', stats=stats)
            return False
    return True


def stop_video():
    global _video
    with _lock:
        tello, reader, _video = _tello, _video, None
    decoder_stopped = True
    if reader is not None:
        try: reader.stop()
        except Exception as exc:
            decoder_stopped = False
            _record('video_stop_failed', error=str(exc))
            # Keep the reader reachable so a later stop can retry cleanup.
            with _lock:
                if _video is None: _video = reader
    if tello is None: return False
    ok, _error = _call_real(tello.streamoff, action='streamoff',
                            deadline=COMMAND_DEADLINE_S['land'])
    time.sleep(.2)
    return ok and decoder_stopped


# FFmpeg fifo_size uses 188-byte units: 2048 is 385,024 bytes, compared with
# its 5,390,336-byte default. Keep a separate 64KiB socket receive request.
# Loopback 720p H264 keyframe bursts rejected 512 units and intermittently
# overran 1024; 2048 leaves room for the tested 241KB compressed keyframe.
# These are requested local buffers, not a camera-to-host age bound.
# A full queue is fatal; probing is also bounded because it buffers separately.
# Official options: ffmpeg.org/ffmpeg-protocols.html#udp and ffmpeg-formats.html.
VIDEO_URL_OPTIONS = '?overrun_nonfatal=0&fifo_size=2048&buffer_size=65536&timeout=1000000'
VIDEO_DECODE_OPTIONS = {'probesize': '32768', 'analyzeduration': '0'}
VIDEO_MAX_DECODE_GAP_S = 1.0
VIDEO_MAX_CONVERSION_S = .05
# PyAV restarts open_timeout for opening the input AND probing stream info.
# Bound each native phase so both can finish within stop()'s three-second
# default budget, including UDP cleanup. A failed probe retries normally.
VIDEO_IO_PHASE_TIMEOUT_S = 1.0


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
        self.invalid_packets = 0
        self.processing_rejections = 0
        self.gap_rejections = 0
        self.last_frame_at = 0.0
        self.last_frame_timestamp = 0.0

    def _note(self, text):
        with self._lock:
            self.errors.append(text)
            del self.errors[:-6]

    def _decode_packets(self, container):
        import av
        if self._halt.is_set():
            return
        for packet in container.demux(video=0):
            if replay_capture is not None and packet.size:
                replay_capture.submit('video_packet', bytes(packet), stream_open=self.opened)
            if self._halt.is_set():
                return
            try:
                frames = packet.decode()
            except av.error.InvalidDataError as exc:
                self.invalid_packets += 1
                # Joining H264 between keyframes can lack references/SPS.
                # Keep consuming the bounded stream rather than reopening at
                # another arbitrary byte; never expose a previous cached frame.
                self._note('invalid_packet: %s' % exc)
                with self._lock:
                    self._frame = None
                    self.last_frame_at = self.last_frame_timestamp = 0.
                continue
            yield from frames

    def run(self):
        import av
        while not self._halt.is_set():
            container = None
            try:
                container = av.open(self.url + VIDEO_URL_OPTIONS,
                                    format='h264', options=dict(VIDEO_DECODE_OPTIONS),
                                    timeout=(min(self.open_timeout, VIDEO_IO_PHASE_TIMEOUT_S),
                                             min(self.read_timeout, VIDEO_IO_PHASE_TIMEOUT_S)))
            except Exception as exc:
                # A plain-URL fallback would silently bypass the chosen queue
                # and overflow policy. Retry the same explicit policy instead.
                self._note('open(bounded_udp): %s' % exc)
            if container is None:
                self._halt.wait(1.0)
                self.reconnects += 1
                continue
            with self._lock:
                self.opened += 1
            last_decoded_at = None
            try:
                for packet in self._decode_packets(container):
                    if self._halt.is_set():
                        break
                    conversion_started = time.monotonic()
                    try:
                        frame = packet.to_ndarray(format='bgr24')
                    except Exception as exc:
                        self._note('to_ndarray: %s' % exc)
                        continue
                    decoded_at = time.monotonic()
                    if last_decoded_at is None:
                        # Opening mid-GOP needs a keyframe, not a .5s flight
                        # deadline. Discard the first frame; only subsequent
                        # continuous decoding may establish a published frame.
                        last_decoded_at = decoded_at
                        continue
                    if decoded_at-conversion_started > VIDEO_MAX_CONVERSION_S:
                        self.processing_rejections += 1
                        # Detect measured local conversion/scheduling overload
                        # before assigning this frame a new publication time.
                        # This does not time FFmpeg's internal decode or prove
                        # camera exposure/network freshness.
                        raise RuntimeError('frame_processing_exceeded: %.3fs; discard queued frames' %
                                           (decoded_at-conversion_started))
                    if decoded_at-last_decoded_at > VIDEO_MAX_DECODE_GAP_S:
                        self.gap_rejections += 1
                        # A stalled decoder may now be consuming an old local
                        # queue. Reject this frame BEFORE giving it a new time,
                        # then reopen to discard that queue. This conservative
                        # local check cannot detect camera/Wi-Fi upstream delay.
                        raise RuntimeError('decode_gap_exceeded: %.3fs; discard queued frames' %
                                           (decoded_at-last_decoded_at))
                    last_decoded_at = decoded_at
                    with self._lock:
                        self._frame = frame
                        self.count += 1
                        self.last_frame_at = decoded_at
                        # Host decode-completion time, NOT camera exposure or
                        # network reception. Fresh decode age cannot prove
                        # sensor-to-controller latency when upstream is queued.
                        self.last_frame_timestamp = time.time()
            except Exception as exc:
                self._note('decode: %s' % exc)
            finally:
                with self._lock:
                    self._frame = None
                    self.last_frame_at = self.last_frame_timestamp = 0.
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
                        invalid_packets=self.invalid_packets,processing_rejections=self.processing_rejections,
                        gap_rejections=self.gap_rejections,
                        opened=self.opened, errors=list(self.errors),
                        age=round(time.monotonic() - self.last_frame_at, 2) if self.last_frame_at else None,
                        shape=list(frame.shape) if frame is not None else None,
                        mean=round(float(frame.mean()), 2) if frame is not None else None)

    def stop(self, timeout=3.0):
        """停止解码并等到线程退出。

        绝不从外部线程 close 容器：FFmpeg 正在 decode 时跨线程关闭会直接崩溃
        (0xC0000005)。打开、探测和读取均限制单阶段等待；由解码线程关闭容器。
        """
        self._halt.set()
        if self.is_alive():
            self.join(timeout)
        if self.is_alive():
            raise TimeoutError('video decoder did not stop within %.3fs' % timeout)


def get_frame():
    """Read image, identity and decode time atomically from the only frame source."""
    with _lock: reader = _video
    if reader is None:
        return FrameResult(None, 0, 0., True)
    with reader._lock:
        stale = (reader._frame is None or not reader.count or
                 not 0 <= time.monotonic() - reader.last_frame_at <= .5)
        return FrameResult(None if stale else reader._frame.copy(), reader.count,
                           reader.last_frame_timestamp, stale)


def video_stats():
    with _lock: reader = _video
    return reader.stats() if reader is not None else dict(reader=False, backend='none', shape=None)


def await_video(timeout=8.0, interval=.05):
    deadline = time.monotonic() + max(0., float(timeout))
    while True:
        packet = get_frame()
        if not packet.stale: return packet, video_stats()
        if time.monotonic() >= deadline: return packet, video_stats()
        time.sleep(interval)


def disconnect(*, safe=True):
    global _tello, _video, _keepalive_stop, SIMULATED, _flying, _height_cm, _response_quarantine
    cancel_pending('disconnect')
    with _lock:
        tello, reader, stop = _tello, _video, _keepalive_stop
    if stop is not None: stop.set()
    ok = decoder_stopped = True
    if reader is not None:
        try:
            reader.stop()
        except Exception as exc:
            ok = decoder_stopped = False
            _record('video_stop_failed', error=str(exc))
    if tello is None:
        with _lock:
            if decoder_stopped:
                if _video is reader: _video = None
                SIMULATED = True
        return False
    if safe:
        try:
            tello.send_rc_control(0, 0, 0, 0)
        except Exception as exc:
            ok = False
            _record('disconnect_rc_failed', error=str(exc))
    ended = False
    try:
        tello.end()
        ended = True
    except Exception as exc:
        ok = False
        _record('disconnect_failed', error=str(exc))
    with _lock:
        if decoder_stopped and _video is reader: _video = None
        if ended and _tello is tello: _tello = None
        if _keepalive_stop is stop: _keepalive_stop = None
        # A failed end keeps the real handle and uncertainty for a retry.
        # Only complete cleanup resets the disconnected simulator state.
        if ok:
            SIMULATED, _flying, _height_cm = True, False, 0
            _unknown_actions.clear()
            _unknown_since.clear()
            _response_quarantine = None
            _pending_commands.clear()
    _record('disconnected' if ok else 'disconnect_unconfirmed')
    return ok


def diagnostics():
    """Stable operator diagnostics, without exposing decoder or telemetry internals."""
    telemetry, _, _, _ = _telemetry()
    return dict(video=video_stats(), health=health(),
                raw_telemetry={key: telemetry.get(key) for key in
                               ('h', 'tof', 'bat', 'time', 'templ', 'temph', 'vgx', 'vgy', 'vgz',
                                'pitch', 'roll', 'yaw', 'baro', 'agx', 'agy', 'agz')})
