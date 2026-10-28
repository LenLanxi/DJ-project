"""Test-only device substitutes; production code must never import this module."""
import threading
import time
from drone_sdk.contract import CommandResult, ControlResult, StatusResult


class MockBackend:
    simulated = True

    def __init__(self):
        self.owner = None
        self.flying = self.following = False
        self.calls = []
        self.observation_index = 0
        self.lock = threading.RLock()
        self.control_epoch = self.command_epoch = 0
        self.grant = None
        self.ticket = None

    def get_status(self):
        with self.lock:
            return StatusResult(80, 80 if self.flying else 0, True,
                'flying' if self.flying else 'landed', self.owner or 'none', time.time(),
                self.control_epoch, self.command_epoch, simulated=self.simulated)

    def request_control(self, owner, *, grant=None, cancelled=None):
        with self.lock:
            if self.owner not in (None, owner):
                return ControlResult(False, self.owner, 'busy')
            self.owner = owner
            self.control_epoch += 1
            self.command_epoch += 1
            self.grant = {'control_epoch': self.control_epoch, 'command_epoch': self.command_epoch}
            return ControlResult(True, owner, control_epoch=self.control_epoch, command_epoch=self.command_epoch)

    def release_control(self, owner, *, control_epoch=None, command_epoch=None, cancelled=None):
        grant = dict(control_epoch=control_epoch, command_epoch=command_epoch)
        with self.lock:
            if (self.owner != owner or grant.get('control_epoch') != self.control_epoch
                    or grant.get('command_epoch') != self.command_epoch):
                return ControlResult(False, self.owner, 'obsolete_control')
            self.owner = None
            self.control_epoch += 1
            self.command_epoch += 1
            return ControlResult(True)

    def is_following(self):
        return self.following

    def stop_follow(self, *, grant=None, cancelled=None):
        with self.lock:
            self.following = False
            self.ticket = None
            if self.owner == 'follow':
                self.owner = None
                self.control_epoch += 1
                self.command_epoch += 1
            return {'ok': True}

    def start_follow(self, *, grant=None, cancelled=None):
        with self.lock:
            if not self.flying:
                return {'ok': False, 'reason': 'landed'}
            result = self.request_control('follow')
            if result.ok:
                self.following = True
            return result

    def pause_follow_for_action(self, *, grant=None, cancelled=None):
        with self.lock:
            was = self.following
            self.stop_follow()
            self.ticket = object() if was else None
            return {'ok': True, 'ticket': self.ticket}

    def finish_follow_action(self, ticket, success, adjust_axis=None, not_before=None, *, grant=None, cancelled=None):
        with self.lock:
            if ticket is None or ticket is not self.ticket:
                return {'ok': False, 'reason': 'obsolete_ticket'}
            self.ticket = None
            return self.start_follow() if success else {'ok': True}

    def action(self, name, *args, grant, cancelled=None):
        with self.lock:
            self.calls.append((name, args))
            if (self.owner != 'model' or grant.get('control_epoch') != self.control_epoch
                    or grant.get('command_epoch') != self.command_epoch):
                return {'ok': False, 'status': 'failed', 'reason': 'obsolete_control'}
            if name == 'takeoff':
                self.flying = True
            elif name == 'land':
                self.flying = self.following = False
            elif name != 'stop_motion' and not self.flying:
                return {'ok': False, 'status': 'failed', 'reason': 'landed'}
            result = {'ok': True, 'status': 'accepted' if name in ('land', 'stop_motion', 'update_velocity') else 'completed',
                      'is_blocking': name not in ('land', 'stop_motion', 'update_velocity'), 'simulated': True}
            if name in ('land', 'stop_motion'):
                self.command_epoch += 1
                self.grant = {'control_epoch': self.control_epoch, 'command_epoch': self.command_epoch}
                result['grant'] = dict(self.grant)
            return result

    def stop_cancelled_follow(self, event):
        return {'ok': True}

    def priority_interrupt(self, land=False):
        with self.lock:
            self.calls.append(('priority_stop', (land,)))
            self.owner = 'model'
            self.control_epoch += 1
            self.command_epoch += 1
            if land:
                self.flying = False
            self.grant = {'control_epoch': self.control_epoch, 'command_epoch': self.command_epoch}
            return {'ok': True, 'status': 'accepted', 'priority': True, 'grant': dict(self.grant)}

    def get_frame(self):
        raise RuntimeError('test substitute has no camera')

    def action_with_grant(self, name, grant, *args, cancelled=None):
        if cancelled is not None and cancelled.is_set():
            return {'ok': False, 'status': 'failed', 'reason': 'program_cancelled'}
        if name in ('move_forward','move_back','move_left','move_right','move_up','move_down'):
            if len(args) != 1 or type(args[0]) is not int or not 20 <= args[0] <= 500:
                return {'ok': False, 'status': 'failed', 'reason': 'invalid_distance'}
        elif name == 'rotate':
            if len(args) != 1 or type(args[0]) is not int or not -360 <= args[0] <= 360 or args[0] == 0:
                return {'ok': False, 'status': 'failed', 'reason': 'invalid_degrees'}
        return self.action(name, *args, grant=grant, cancelled=cancelled)

    def start(self, *, cancelled=None):
        self._operation_source = cancelled
        result = self.start_follow()
        return CommandResult(result.ok, 'completed' if result.ok else 'failed', result.reason)

    def stop(self, *, cancelled=None):
        result = self.stop_follow()
        return CommandResult(result['ok'], 'completed')

    def pause_if_active(self, *, cancelled=None):
        if not self.following:
            return None
        return self.pause_follow_for_action()['ticket']

    def finish_action(self, ticket, success, **options):
        self._operation_source = options.get('cancelled')
        result = self.finish_follow_action(ticket, success)
        if isinstance(result, ControlResult):
            return CommandResult(result.ok, 'completed' if result.ok else 'failed', result.reason)
        return CommandResult(result['ok'], 'completed' if result['ok'] else 'failed', result.get('reason',''))

    def stop_cancelled_operation(self, event):
        if getattr(self, '_operation_source', None) is event:
            return self.stop()
        return CommandResult(True, 'completed')
