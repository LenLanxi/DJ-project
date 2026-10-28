"""Shared SDK control arbitration. Hardware is connected explicitly by runtime.

Public owner arguments remain compatible. Reviewed callers additionally send
control_epoch/command_epoch to reject stale same-owner tasks. Legacy callers
without these fields do NOT gain ABA protection merely by upgrading the SDK.
"""
import math
import contextvars
from contextlib import contextmanager
import threading
import time
from .contract import CommandResult, ControlResult, StatusResult, FrameResult
from . import _driver, follow_integration

SIMULATED = True
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
_frame_lock = threading.RLock()
_frame = FrameResult(None, 0, 0., True)
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

def _check(owner, control_epoch=None, command_epoch=None, *, allow_landing=False):
    if operation_cancelled(): return 'program_cancelled'
    _advance()
    if _current_owner != owner or owner not in ('follow', 'model'): return 'no_control'
    if control_epoch is not None and control_epoch != _control_epoch: return 'obsolete_control'
    if command_epoch is not None and command_epoch != _command_epoch: return 'obsolete_command'
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

def publish_frame(packet):
    """Publish decoded frames without refreshing identity/time; tests may inject images."""
    global _frame
    if (not isinstance(packet, FrameResult) or type(packet.frame_id) is not int
            or packet.frame_id < 0 or type(packet.timestamp) not in (int, float)
            or not math.isfinite(packet.timestamp) or type(packet.stale) is not bool):
        raise ValueError('invalid_frame_metadata')
    with _frame_lock:
        if packet.frame_id < _frame.frame_id: raise ValueError('frame_id_went_backwards')
        if packet.frame_id == _frame.frame_id:
            if packet.timestamp != _frame.timestamp: raise ValueError('cached_frame_timestamp_changed')
            return
        _frame = FrameResult(None if packet.frame is None else packet.frame.copy(),
                             packet.frame_id, packet.timestamp, packet.stale)

def get_frame():
    with _frame_lock:
        stale = _frame.stale or _frame.frame is None or not 0 <= time.time()-_frame.timestamp <= 1.
        return FrameResult(None if stale else _frame.frame.copy(), _frame.frame_id,
                           _frame.timestamp, stale)

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

def request_control(owner, *, priority=False):
    """priority=True 是紧急保留通道：不被在途动作/落地流程/占用者拦住，并作废在途动作。"""
    global _current_owner, _control_epoch, _command_epoch, _active_action
    with _velocity_lock:
        _advance()
        reason = ('invalid_owner' if owner not in ('follow', 'model') else
                  '' if priority else
                  'program_cancelled' if operation_cancelled() else
                  'landing_in_progress' if _flight_state == 'landing' else
                  'command_channel_ambiguous' if not _driver.command_channel_clear() else
                  'action_result_pending' if _driver.action_pending() else
                  'busy' if _current_owner not in (None, owner) else '')
        if reason: return ControlResult(False, _current_owner or 'none', reason, _control_epoch, _command_epoch)
        if priority:
            if _flight_state == 'landing':
                _preserve_landing(owner, 'request_control')
                return ControlResult(True, owner, 'landing_preserved', _control_epoch, _command_epoch)
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
            return ControlResult(True, owner, '', _control_epoch, _command_epoch)
        # Each successful request grants a new lease, including same-name
        # callers. Identity is an authorization number, not the word model.
        _driver.cancel_pending('owner_granted')
        if _zero('owner_granted') is False:
            return ControlResult(False, _current_owner or 'none', 'stop_send_failed', _control_epoch, _command_epoch)
        _control_epoch += 1
        _command_epoch += 1
        _current_owner = owner
        return ControlResult(True, owner, '', _control_epoch, _command_epoch)

def release_control(owner, *, control_epoch=None, command_epoch=None):
    global _current_owner, _control_epoch, _command_epoch, _active_action
    with _velocity_lock:
        reason = _check(owner, control_epoch, command_epoch, allow_landing=True)
        if not reason and _driver.action_pending(): reason = 'action_result_pending'
        if reason: return ControlResult(False, _current_owner or 'none', reason, _control_epoch, _command_epoch)
        if _zero('owner_released') is False:
            return ControlResult(False, _current_owner or 'none', 'stop_send_failed', _control_epoch, _command_epoch)
        _driver.cancel_pending('owner_released')
        _current_owner = None
        _control_epoch += 1
        _command_epoch += 1
        _active_action = None
        return ControlResult(True, 'none', '', _control_epoch, _command_epoch)

def update_velocity(owner, left_right, forward_back, up_down, yaw, *, control_epoch=None, command_epoch=None):
    global _last_velocity_time
    channels = (left_right, forward_back, up_down, yaw)
    with _velocity_lock:
        reason = _check(owner, control_epoch, command_epoch)
        if not reason and any(channels) and not _driver.command_channel_clear():
            reason = 'command_channel_ambiguous'
        if not reason and not _driver.SIMULATED and not _driver.health().get('telemetry_fresh') and any(channels):
            reason = 'telemetry_stale'
        if not reason and any(type(v) is not int or not -100 <= v <= 100 for v in channels): reason = 'invalid_channels'
        if not reason and any(channels) and _flight_state != 'flying': reason = 'is_landed'
        if not reason and _active_action is not None: reason = 'action_busy'
        if reason: return CommandResult(False, 'failed', reason, False)
        options = {} if operation_event() is None else {'guard': operation_event()}
        if _driver.set_velocity(channels, 'velocity_update', **options) is False:
            return CommandResult(False, 'failed', 'velocity_send_failed', False)
        _last_velocity_time = time.monotonic() if any(channels) else 0.
        return CommandResult(True, 'accepted', '', False)

def _mock_move(owner, action, param=None, *, control_epoch=None, command_epoch=None):
    global _flight_state, _active_action
    with _velocity_lock:
        reason = _check(owner, control_epoch, command_epoch)
        if not reason and not _driver.command_channel_clear(): reason = 'command_channel_ambiguous'
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
    try:
        options = {} if driver_guard is None else {'guard': driver_guard}
        ok = _driver.execute(action, param, epoch=driver_epoch, **options)
    except Exception:
        ok = False
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
                return CommandResult(False, 'unknown', 'takeoff_result_unknown')
            return CommandResult(False, 'failed', 'driver_failed')
        if action == 'takeoff': _flight_state = 'flying'
        return CommandResult(True, 'completed')

_takeoff_authorized = False
_min_battery = 30

def configure_takeoff(*, authorized=False, min_battery=30):
    """Software authorization; the threshold is not a firmware guarantee."""
    global _takeoff_authorized, _min_battery
    if type(min_battery) is not int or not 0 <= min_battery <= 100: raise ValueError('invalid_battery_limit')
    _takeoff_authorized, _min_battery = bool(authorized), min_battery

def takeoff(owner, **grant):
    if not _driver.SIMULATED:
        h = _driver.health(); battery = h['battery']
        if (not _takeoff_authorized or type(battery) is not int
                or not _min_battery <= battery <= 100 or not h['link_ok']
                or not h.get('telemetry_fresh')
                or type(h.get('temperature_c')) not in (int, float)
                or h['temperature_c'] >= 95):
            return CommandResult(False, 'failed', 'takeoff_gate_rejected')
    return _mock_move(owner, 'takeoff', **grant)

def land(owner, *, control_epoch=None, command_epoch=None, priority=False):
    """priority=True：越过 action_result_pending / 代次校验，保证降落指令必达。"""
    global _flight_state, _landing_deadline, _command_epoch, _control_epoch, _active_action, _current_owner
    with _velocity_lock:
        reason = ('invalid_owner' if owner not in ('model', 'follow') else
                  '' if priority else _check(owner, control_epoch, command_epoch, allow_landing=True))
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
                return CommandResult(False, 'failed', 'stop_send_failed', False)
            return CommandResult(True, 'completed', 'already_landed', False)
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
        return CommandResult(True, 'accepted', '', False)

def stop_motion(owner, *, control_epoch=None, command_epoch=None, priority=False):
    """priority=True：越过 action_result_pending / 代次校验，保证急停指令必达。"""
    global _command_epoch, _control_epoch, _active_action, _current_owner
    with _velocity_lock:
        if priority:
            _advance()
        reason = ('invalid_owner' if owner not in ('model', 'follow') else
                  '' if priority else _check(owner, control_epoch, command_epoch))
        if reason: return CommandResult(False, 'failed', reason, False)
        if priority:
            if _flight_state == 'landing':
                _preserve_landing(owner, 'stop_motion')
                return CommandResult(True, 'accepted', 'landing_preserved', False)
            _driver.record_priority_override('stop_motion', current_owner=_current_owner)
            _control_epoch += 1
            _current_owner = owner
        _driver.cancel_pending('stop_motion')
        _command_epoch += 1
        _active_action = None
        if _zero('stop_motion') is False: return CommandResult(False,'failed','stop_send_failed',False)
        return CommandResult(True, 'accepted', '', False)

def move_forward(owner, cm, **grant): return _mock_move(owner, 'forward', cm, **grant)
def move_back(owner, cm, **grant): return _mock_move(owner, 'back', cm, **grant)
def move_left(owner, cm, **grant): return _mock_move(owner, 'left', cm, **grant)
def move_right(owner, cm, **grant): return _mock_move(owner, 'right', cm, **grant)
def move_up(owner, cm, **grant): return _mock_move(owner, 'up', cm, **grant)
def move_down(owner, cm, **grant): return _mock_move(owner, 'down', cm, **grant)
def rotate(owner, degrees, **grant): return _mock_move(owner, 'rotate', degrees, **grant)

_follow = follow_integration.FollowIntegration(lambda: _current_owner or 'none')
def start_follow(timeout=follow_integration.FIRST_FRAME_TIMEOUT): return _follow.start(timeout)
def stop_follow(): return _follow.stop()
def is_following(): return _follow.is_following()
def set_frame_provider(provider): _follow.set_frame_provider(provider)
