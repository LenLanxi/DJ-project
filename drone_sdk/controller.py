"""One mandatory control grant for every ordinary SDK action."""
import contextvars
from contextlib import contextmanager, nullcontext
import threading
import time
from .contract import CommandResult, ControlGrant, ControlResult, StatusResult, FrameResult
from . import _driver

_velocity_lock = threading.RLock()
_current_owner = None
_control_epoch = _command_epoch = 0
_flight_state = 'landed'
_landing_deadline = None
_landing_delay = .05
_active_action = None
_last_velocity_time = 0.
# 速度看门狗：超过这个时间没有新的非零速度就强制归零。
# 满杆约 2.5 m/s ⇒ 0.3s 的最坏失控位移约 0.75m（原 0.5s 是 1.25m）。
VELOCITY_WATCHDOG_S = 0.3
_operation = contextvars.ContextVar('sdk_operation_cancellation', default=None)

@contextmanager
def operation_guard(cancelled):
    """Host-only per-program cancellation; never exposed through sandbox RPC."""
    token = _operation.set(cancelled)
    try:
        yield
    finally:
        _operation.reset(token)

def operation_event():
    return _operation.get()

def operation_cancelled():
    event = operation_event()
    return bool(event is not None and event.is_set())

def _advance():
    global _flight_state, _landing_deadline
    if not _driver.SIMULATED:
        health = _driver.health()
        if (_flight_state == 'flying' and not health['flying']
                and health.get('telemetry_fresh') and health.get('height_source') == 'ground_telemetry'
                and not _driver.action_pending() and not _driver.action_unknown()):
            _flight_state = 'landed'
        if _flight_state == 'landed' and _driver.health()['flying']:
            _flight_state = 'flying'
        if _flight_state == 'unknown':
            # 起飞结果未知：只允许真实遥测修正，绝不凭超时改判。
            h = _driver.health()
            if h['flying']:
                _flight_state, _landing_deadline = 'flying', None
            elif not _driver.action_unknown('takeoff') and not _driver.action_pending():
                _flight_state, _landing_deadline = 'landed', None
            return
        if _flight_state == 'landing':
            h = _driver.health()
            if (h.get('landing_acknowledged') and h.get('telemetry_fresh')
                    and not _driver.action_unknown('takeoff')
                    and h.get('height_cm') == 0 and not _driver.action_pending()):
                _flight_state, _landing_deadline = 'landed', None
            return
    if _landing_deadline is not None and time.monotonic() >= _landing_deadline:
        _flight_state, _landing_deadline = 'landed', None

def _zero(reason):
    global _last_velocity_time
    ok = _driver.set_velocity((0, 0, 0, 0), reason)
    # An unsuccessful zero must be retried by the watchdog, not forgotten.
    _last_velocity_time = 0. if ok is not False else time.monotonic()
    return ok

def _check(grant, *, allow_landing=False):
    if operation_cancelled(): return 'program_cancelled'
    _advance()
    if _current_owner != grant.owner or grant.owner not in ('follow', 'model'): return 'no_control'
    if grant.control_epoch != _control_epoch: return 'obsolete_control'
    if grant.command_epoch != _command_epoch: return 'obsolete_command'
    if _flight_state == 'landing' and not allow_landing: return 'landing_in_progress'
    return ''

def _watchdog_tick():
    with _velocity_lock:
        _advance()
        if _last_velocity_time and time.monotonic() - _last_velocity_time > VELOCITY_WATCHDOG_S:
            _zero('watchdog_timeout')

def _watchdog_thread():
    while True:
        time.sleep(.02)
        _watchdog_tick()

_watchdog = threading.Thread(target=_watchdog_thread, daemon=True, name='sdk-simulator-watchdog')
_watchdog.start()

# The decoder is the sole source of image identity, time and freshness.
get_frame = _driver.get_frame


def get_status():
    with _velocity_lock:
        _advance()
        h = _driver.health() if not _driver.SIMULATED else None
        return StatusResult(h['battery'] if h else 88, h['height_cm'] if h else (0 if _flight_state == 'landed' else 120),
            bool(h['connected'] and h['link_ok'] and h.get('telemetry_fresh')) if h else True,
            _flight_state, _current_owner or 'none', time.time(), _control_epoch,
            _command_epoch, _driver.snapshot()['velocity'], _driver.SIMULATED,
            h.get('command_channel_ambiguous', False) if h else False,
            h.get('command_uncertainty') if h else None,
            h.get('telemetry_timestamp') if h else None)

def _preserve_landing(owner, action):
    """Called with _velocity_lock held: a takeover cannot become a hover.

    Keep the existing landing worker. Only retry after a failed/no-ack dispatch
    has finished; repeated priority requests must not queue extra land workers.
    An acknowledgement without fresh ground telemetry still means landing.
    """
    global _current_owner, _control_epoch, _command_epoch, _active_action
    _driver.record_priority_override(action, current_owner=_current_owner, landing_preserved=True)
    _control_epoch += 1
    _command_epoch += 1
    _current_owner = owner
    _active_action = None
    if not _driver.action_pending() and not _driver.health().get('landing_acknowledged'):
        _driver.interrupt('land')

def _grant():
    return ControlGrant(_current_owner, _control_epoch, _command_epoch)


def _control_result(ok, reason=''):
    return ControlResult(ok, _current_owner or 'none', reason,
                         _grant() if ok and _current_owner is not None else None)


def request_control(owner, *, priority=False, cancelled=None, exclusive=False):
    """Grant atomically; exclusive rejects even an existing same-name owner.

    priority=True 是紧急保留通道：不被在途动作/落地流程/占用者拦住，并作废在途动作。
    """
    global _current_owner, _control_epoch, _command_epoch, _active_action
    with operation_guard(cancelled) if cancelled is not None else nullcontext(), _velocity_lock:
        _advance()
        reason = ('invalid_owner' if owner not in ('follow', 'model') else
                  '' if priority else
                  'program_cancelled' if operation_cancelled() else
                  'landing_in_progress' if _flight_state == 'landing' else
                  'action_result_pending' if _driver.action_pending() else
                  'busy' if _current_owner is not None and (exclusive or _current_owner != owner) else '')
        if reason: return _control_result(False, reason)
        if priority:
            if _flight_state == 'landing':
                _preserve_landing(owner, 'request_control')
                return _control_result(True, 'landing_preserved')
            # 保留通道：先作废在途动作的 token，再零速度接管，保证随后的 stop/land 一定能下发。
            _driver.record_priority_override('request_control', current_owner=_current_owner)
            _driver.cancel_pending('priority_takeover')
            _active_action = None
            _command_epoch += 1
            # A new emergency lease is new even if its textual owner is model.
            # Old same-owner cleanup must not stop the new controller.
            _control_epoch += 1
            _current_owner = owner
            _zero('priority_takeover')
            return _control_result(True)
        # Each successful request grants a new lease, including same-name
        # callers. Identity is an authorization number, not the word model.
        _driver.cancel_pending('owner_granted')
        if _zero('owner_granted') is False:
            return _control_result(False, 'stop_send_failed')
        _control_epoch += 1
        _command_epoch += 1
        _current_owner = owner
        return _control_result(True)

def release_control(grant, *, cancelled=None):
    global _current_owner, _control_epoch, _command_epoch, _active_action
    with operation_guard(cancelled) if cancelled is not None else nullcontext(), _velocity_lock:
        reason = _check(grant, allow_landing=True)
        if not reason and _driver.action_pending(): reason = 'action_result_pending'
        if reason: return _control_result(False, reason)
        if _zero('owner_released') is False:
            return _control_result(False, 'stop_send_failed')
        _driver.cancel_pending('owner_released')
        _current_owner = None
        _control_epoch += 1
        _command_epoch += 1
        _active_action = None
        return _control_result(True)

def update_velocity(grant, left_right, forward_back, up_down, yaw, *, cancelled=None):
    global _last_velocity_time
    channels = (left_right, forward_back, up_down, yaw)
    with operation_guard(cancelled) if cancelled is not None else nullcontext(), _velocity_lock:
        reason = _check(grant)
        if not reason and not _driver.SIMULATED and not _driver.health().get('telemetry_fresh') and any(channels):
            reason = 'telemetry_stale'
        if not reason and any(type(v) is not int or not -100 <= v <= 100 for v in channels): reason = 'invalid_channels'
        if not reason and any(channels) and _flight_state != 'flying': reason = 'is_landed'
        if not reason and _active_action is not None: reason = 'action_busy'
        if reason: return CommandResult(False, 'failed', reason, False)
        options = {} if operation_event() is None else {'guard': operation_event()}
        details = {}
        if _driver.set_velocity(channels, 'velocity_update', details=details, **options) is False:
            return CommandResult(False, 'failed', 'velocity_send_failed', False, details=details)
        _last_velocity_time = time.monotonic() if any(channels) else 0.
        return CommandResult(True, 'accepted', '', False,
                             details={**details, **_driver.command_warning(), 'command_sent': details.get('wire_sent', False), 'motion_verified': False,
                                      'meaning': '发送情况见wire_sent，裁剪后参数见applied_channels；不证明已移动、上升或悬停，也不表示位移厘米数'})

def _move(grant, action, param=None):
    global _flight_state, _active_action
    with _velocity_lock:
        reason = _check(grant)
        if not reason and _active_action is not None: reason = 'action_busy'
        if not reason and _driver.action_pending(): reason = 'action_result_pending'
        if not reason and not _driver.SIMULATED and not _driver.health().get('telemetry_fresh'):
            reason = 'telemetry_stale'
        if not reason and action != 'takeoff' and _flight_state != 'flying': reason = 'is_landed'
        if not reason and action == 'takeoff' and _flight_state != 'landed': reason = 'already_airborne'
        if not reason and action not in ('takeoff', 'rotate') and (type(param) is not int or not 20 <= param <= 500): reason = 'invalid_distance'
        if not reason and action == 'rotate' and (type(param) is not int or not -360 <= param <= 360 or param == 0): reason = 'invalid_degrees'
        if reason: return CommandResult(False, 'failed', reason)
        if _zero('discrete_action_started') is False:
            return CommandResult(False, 'failed', 'stop_send_failed')
        token = (_control_epoch, _command_epoch, object())
        driver_epoch = _driver.capture_cancel_epoch()
        driver_guard = operation_event()
        _active_action = token
    details = {'action': action, 'param': param, 'stage': 'driver', 'wire_sent': False}
    try:
        options = {} if driver_guard is None else {'guard': driver_guard}
        ok = _driver.execute(action, param, epoch=driver_epoch, details=details, **options)
    except Exception as exc:
        details.update(reason=str(exc), error_type=type(exc).__name__)
        ok = False
    details.update(_driver.command_warning())
    with _velocity_lock:
        if _active_action != token or token[:2] != (_control_epoch, _command_epoch):
            if action == 'takeoff' and _flight_state != 'landing':
                # Cancellation cannot promise that an already-sent takeoff did
                # not happen. Hold the motion gate until outcome is reconciled.
                if _driver.action_pending() or _driver.action_unknown('takeoff'):
                    _flight_state = 'unknown'
                elif _driver.health()['flying']:
                    _flight_state = 'flying'
            return CommandResult(False, 'failed', 'action_cancelled')
        _active_action = None
        if ok is not True:
            _zero('action_failed')
            if action == 'takeoff' and _driver.action_unknown('takeoff'):
                _flight_state = 'unknown'
                return CommandResult(False, 'unknown', 'takeoff_result_unknown', details=details)
            return CommandResult(False, 'unknown' if details.get('outcome_unknown') else 'failed',
                                 details.get('reason') or 'driver_failed', details=details)
        if action == 'takeoff': _flight_state = 'flying'
        details.update(motion_verified=False)
        return CommandResult(True, 'completed', details=details)

_takeoff_authorized = False
_min_battery = 30
_temp_max = 95

def configure_takeoff(*, authorized=False, min_battery=30, temp_max=95):
    """Software authorization; the threshold is not a firmware guarantee."""
    global _takeoff_authorized, _min_battery, _temp_max
    if type(min_battery) is not int or not 0 <= min_battery <= 100: raise ValueError('invalid_battery_limit')
    if type(temp_max) not in (int, float) or not 0 < temp_max <= 100: raise ValueError('invalid_temperature_limit')
    _takeoff_authorized, _min_battery = bool(authorized), min_battery
    _temp_max = temp_max

def takeoff(grant, *, cancelled=None):
    if not _driver.SIMULATED:
        h = _driver.health()
        battery, temperature = h['battery'], h.get('temperature_c')
        failed = []
        if not _takeoff_authorized: failed.append('takeoff_not_authorized')
        if not h['link_ok']: failed.append('link_unavailable')
        if not h.get('telemetry_fresh'): failed.append('telemetry_stale')
        if type(battery) is not int or not 0 <= battery <= 100:
            failed.append('battery_unavailable')
        elif battery < _min_battery:
            failed.append('battery_below_limit')
        if type(temperature) not in (int, float):
            failed.append('temperature_unavailable')
        elif temperature >= _temp_max:
            failed.append('temperature_limit_reached')
        if failed:
            return CommandResult(False, 'failed', failed[0], details={
                'stage': 'local_preflight', 'wire_sent': False,
                'failed_checks': failed, 'takeoff_authorized': _takeoff_authorized,
                'battery': battery, 'min_battery': _min_battery,
                'temperature_c': temperature, 'temp_max': _temp_max,
                'link_ok': h['link_ok'], 'telemetry_fresh': h.get('telemetry_fresh'),
                'telemetry_age_s': h.get('telemetry_age_s'),
                'telemetry_timestamp': h.get('telemetry_timestamp'),
            })
    with operation_guard(cancelled) if cancelled is not None else nullcontext():
        return _move(grant, 'takeoff')

def _land(grant, *, priority=False):
    """priority=True：越过 action_result_pending / 代次校验，保证降落指令必达。"""
    global _flight_state, _landing_deadline, _command_epoch, _control_epoch, _active_action, _current_owner
    with _velocity_lock:
        owner = grant.owner
        reason = ('invalid_owner' if owner not in ('model', 'follow') else
                  '' if priority else _check(grant, allow_landing=True))
        if reason: return CommandResult(False, 'failed', reason, False)
        if priority:
            _driver.record_priority_override('land', current_owner=_current_owner)
            _control_epoch += 1
            _current_owner = owner
        known_ground = (_flight_state == 'landed' and _active_action is None
                        and not _driver.action_pending() and not _driver.action_unknown())
        if known_ground and not _driver.SIMULATED:
            health = _driver.health()
            known_ground = (health.get('telemetry_fresh') is True
                            and health.get('height_cm') == 0 and health.get('flying') is False)
        if known_ground:
            _driver.cancel_pending('land_already_grounded')
            _command_epoch += 1
            _active_action = None
            if _zero('land_already_grounded') is False:
                return CommandResult(False, 'failed', 'stop_send_failed', False, _grant())
            return CommandResult(True, 'completed', 'already_landed', False, _grant())
        if _flight_state != 'landing':
            _command_epoch += 1
            _active_action = None
            _zero('land')
            _driver.interrupt('land')
            if _flight_state != 'landed' or _driver.action_pending() or _driver.action_unknown():
                _flight_state = 'landing'
                _landing_deadline = time.monotonic() + _landing_delay
        elif priority:
            _driver.interrupt('land')
        return CommandResult(True, 'accepted', '', False, _grant())

def _stop_motion(grant, *, priority=False):
    """priority=True：越过 action_result_pending / 代次校验，保证急停指令必达。"""
    global _command_epoch, _control_epoch, _active_action, _current_owner
    with _velocity_lock:
        if priority:
            _advance()
        owner = grant.owner
        reason = ('invalid_owner' if owner not in ('model', 'follow') else
                  '' if priority else _check(grant))
        if reason: return CommandResult(False, 'failed', reason, False)
        if priority:
            if _flight_state == 'landing':
                _preserve_landing(owner, 'stop_motion')
                return CommandResult(True, 'accepted', 'landing_preserved', False, _grant())
            _driver.record_priority_override('stop_motion', current_owner=_current_owner)
            _control_epoch += 1
            _current_owner = owner
        _driver.cancel_pending('stop_motion')
        _command_epoch += 1
        _active_action = None
        if _zero('stop_motion') is False: return CommandResult(False,'failed','stop_send_failed',False, _grant())
        return CommandResult(True, 'accepted', '', False, _grant())

def land(grant, *, cancelled=None):
    with operation_guard(cancelled) if cancelled is not None else nullcontext():
        return _land(grant)


def stop_motion(grant, *, cancelled=None):
    with operation_guard(cancelled) if cancelled is not None else nullcontext():
        return _stop_motion(grant)


def move_forward(grant, cm, *, cancelled=None):
    with operation_guard(cancelled) if cancelled is not None else nullcontext():
        return _move(grant, 'forward', cm)

def move_back(grant, cm, *, cancelled=None):
    with operation_guard(cancelled) if cancelled is not None else nullcontext():
        return _move(grant, 'back', cm)

def move_left(grant, cm, *, cancelled=None):
    with operation_guard(cancelled) if cancelled is not None else nullcontext():
        return _move(grant, 'left', cm)

def move_right(grant, cm, *, cancelled=None):
    with operation_guard(cancelled) if cancelled is not None else nullcontext():
        return _move(grant, 'right', cm)

def move_up(grant, cm, *, cancelled=None):
    with operation_guard(cancelled) if cancelled is not None else nullcontext():
        return _move(grant, 'up', cm)

def move_down(grant, cm, *, cancelled=None):
    with operation_guard(cancelled) if cancelled is not None else nullcontext():
        return _move(grant, 'down', cm)

def rotate(grant, degrees, *, cancelled=None):
    with operation_guard(cancelled) if cancelled is not None else nullcontext():
        return _move(grant, 'rotate', degrees)

def priority_interrupt(land=False):
    """Revoke ordinary grants and send a priority stop/land atomically."""
    with _velocity_lock:
        return (_land if land else _stop_motion)(_grant_for_priority(), priority=True)


def _grant_for_priority():
    return ControlGrant('model', _control_epoch, _command_epoch)
