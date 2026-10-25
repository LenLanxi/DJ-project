"""Offline SDK candidate. This module cannot connect to a drone.

Public owner arguments remain compatible. Reviewed callers additionally send
control_epoch/command_epoch to reject stale same-owner tasks. Legacy callers
without these fields do NOT gain ABA protection merely by upgrading the SDK.
"""
import math
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

def _advance():
    global _flight_state, _landing_deadline
    if not _driver.SIMULATED:
        if _flight_state == 'unknown':
            # 起飞结果未知：只允许真实遥测修正，绝不凭超时改判。
            h = _driver.health()
            if h['flying']:
                _flight_state, _landing_deadline = 'flying', None
            elif not _driver.action_unknown('takeoff') and not _driver.action_pending():
                _flight_state, _landing_deadline = 'landed', None
            return
        if _flight_state == 'landing':
            if not _driver.health()['flying'] and not _driver.action_pending():
                _flight_state, _landing_deadline = 'landed', None
            return
    if _landing_deadline is not None and time.monotonic() >= _landing_deadline:
        _flight_state, _landing_deadline = 'landed', None

def _zero(reason):
    global _last_velocity_time
    _last_velocity_time = 0.
    return _driver.set_velocity((0, 0, 0, 0), reason)

def _check(owner, control_epoch=None, command_epoch=None, *, allow_landing=False):
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
    """Explicit generated-frame injection. Preserve capture identity/time."""
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
            bool(h['connected'] and h['link_ok']) if h else True,
            _flight_state, _current_owner or 'none', time.time(), _control_epoch,
            _command_epoch, _driver.snapshot()['velocity'], _driver.SIMULATED)

def request_control(owner, *, priority=False):
    """priority=True 是紧急保留通道：不被在途动作/落地流程/占用者拦住，并作废在途动作。"""
    global _current_owner, _control_epoch, _command_epoch, _active_action
    with _velocity_lock:
        _advance()
        reason = ('invalid_owner' if owner not in ('follow', 'model') else
                  '' if priority else
                  'landing_in_progress' if _flight_state == 'landing' else
                  'action_result_pending' if _driver.action_pending() else
                  'busy' if _current_owner not in (None, owner) else '')
        if reason: return ControlResult(False, _current_owner or 'none', reason, _control_epoch, _command_epoch)
        if priority:
            # 保留通道：先作废在途动作的 token，再零速度接管，保证随后的 stop/land 一定能下发。
            _driver.record_priority_override('request_control', current_owner=_current_owner)
            _active_action = None
            _command_epoch += 1
            if _current_owner != owner:
                _control_epoch += 1
                _current_owner = owner
            _zero('priority_takeover')
            return ControlResult(True, owner, '', _control_epoch, _command_epoch)
        if _current_owner != owner:
            _zero('owner_changed')
            _control_epoch += 1
            _command_epoch += 1
            _current_owner = owner
        return ControlResult(True, owner, '', _control_epoch, _command_epoch)

def release_control(owner, *, control_epoch=None):
    global _current_owner, _control_epoch, _command_epoch, _active_action
    with _velocity_lock:
        reason = _check(owner, control_epoch)
        if not reason and _driver.action_pending(): reason = 'action_result_pending'
        if reason: return ControlResult(False, _current_owner or 'none', reason, _control_epoch, _command_epoch)
        if _zero('owner_released') is False:
            return ControlResult(False, _current_owner or 'none', 'stop_send_failed', _control_epoch, _command_epoch)
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
        if not reason and any(type(v) is not int or not -100 <= v <= 100 for v in channels): reason = 'invalid_channels'
        if not reason and any(channels) and _flight_state != 'flying': reason = 'is_landed'
        if not reason and _active_action is not None: reason = 'action_busy'
        if reason: return CommandResult(False, 'failed', reason, False)
        if _driver.set_velocity(channels, 'velocity_update') is False:
            return CommandResult(False, 'failed', 'velocity_send_failed', False)
        _last_velocity_time = time.monotonic() if any(channels) else 0.
        return CommandResult(True, 'accepted', '', False)

def _mock_move(owner, action, param=None, *, control_epoch=None, command_epoch=None):
    global _flight_state, _active_action
    with _velocity_lock:
        reason = _check(owner, control_epoch, command_epoch)
        if not reason and _active_action is not None: reason = 'action_busy'
        if not reason and action != 'takeoff' and _flight_state != 'flying': reason = 'is_landed'
        if not reason and action == 'takeoff' and _flight_state != 'landed': reason = 'already_airborne'
        if not reason and action not in ('takeoff', 'rotate') and (type(param) is not int or not 20 <= param <= 500): reason = 'invalid_distance'
        if not reason and action == 'rotate' and (type(param) is not int or not -360 <= param <= 360 or param == 0): reason = 'invalid_degrees'
        if reason: return CommandResult(False, 'failed', reason)
        _zero('discrete_action_started')
        token = (_control_epoch, _command_epoch, object())
        _active_action = token
    try:
        ok = _driver.execute(action, param)
    except Exception:
        ok = False
    with _velocity_lock:
        if _active_action != token or token[:2] != (_control_epoch, _command_epoch):
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
_min_battery = 10

def configure_takeoff(*, authorized=False, min_battery=10):
    """min_battery 默认 10：与 modules/ai/main.py 的 --min-battery 默认值保持一致。

    注意 Tello 固件自己在约 50% 就会回 error 拒飞，这一层只是我们自己的软件闸门。
    """
    global _takeoff_authorized, _min_battery
    if type(min_battery) is not int or not 1 <= min_battery <= 100: raise ValueError('invalid_battery_limit')
    _takeoff_authorized, _min_battery = bool(authorized), min_battery

def takeoff(owner, **grant):
    if not _driver.SIMULATED:
        h = _driver.health(); battery = h['battery']
        if not _takeoff_authorized or type(battery) is not int or not _min_battery <= battery <= 100 or not h['link_ok']:
            return CommandResult(False, 'failed', 'takeoff_gate_rejected')
    return _mock_move(owner, 'takeoff', **grant)

def land(owner, *, control_epoch=None, command_epoch=None, priority=False):
    """priority=True：越过 action_result_pending / 代次校验，保证降落指令必达。"""
    global _flight_state, _landing_deadline, _command_epoch, _active_action, _current_owner
    with _velocity_lock:
        reason = '' if priority else _check(owner, control_epoch, command_epoch, allow_landing=True)
        if reason: return CommandResult(False, 'failed', reason, False)
        if priority:
            _driver.record_priority_override('land', current_owner=_current_owner)
            if _current_owner != owner: _current_owner = owner
        if _flight_state != 'landing':
            _command_epoch += 1
            _active_action = None
            _zero('land')
            _driver.interrupt('land')
            if _flight_state != 'landed':
                _flight_state = 'landing'
                _landing_deadline = time.monotonic() + _landing_delay
        elif priority:
            _driver.interrupt('land')
        return CommandResult(True, 'accepted', '', False)

def stop_motion(owner, *, control_epoch=None, command_epoch=None, priority=False):
    """priority=True：越过 action_result_pending / 代次校验，保证急停指令必达。"""
    global _command_epoch, _active_action, _current_owner
    with _velocity_lock:
        reason = '' if priority else _check(owner, control_epoch, command_epoch)
        if reason: return CommandResult(False, 'failed', reason, False)
        if priority:
            _driver.record_priority_override('stop_motion', current_owner=_current_owner)
            if _current_owner != owner: _current_owner = owner
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
