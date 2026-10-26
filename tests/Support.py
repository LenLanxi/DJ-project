"""Test-only device substitutes; production code must never import this module."""
import threading
import time


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

    def _context(self):
        from backends import GRANT
        value = GRANT.get()
        return value if value is not None else self.grant or {}

    def get_status(self):
        with self.lock:
            return {'battery': 80, 'height_cm': 80 if self.flying else 0,
                    'connected': True, 'flight_state': 'flying' if self.flying else 'landed',
                    'control_owner': self.owner or 'none', 'timestamp': time.time(),
                    'control_epoch': self.control_epoch, 'command_epoch': self.command_epoch,
                    'simulated': True}

    def request_control(self, owner):
        with self.lock:
            if self.owner not in (None, owner):
                return {'ok': False, 'reason': 'busy'}
            self.owner = owner
            self.control_epoch += 1
            self.command_epoch += 1
            self.grant = {'control_epoch': self.control_epoch, 'command_epoch': self.command_epoch}
            return dict(ok=True, current_owner=owner, **self.grant)

    def release_control(self, owner):
        with self.lock:
            grant = self._context()
            if (self.owner != owner or grant.get('control_epoch') != self.control_epoch
                    or grant.get('command_epoch') != self.command_epoch):
                return {'ok': False, 'reason': 'obsolete_control'}
            self.owner = None
            self.control_epoch += 1
            self.command_epoch += 1
            return {'ok': True, 'current_owner': None}

    def is_following(self):
        return self.following

    def stop_follow(self):
        with self.lock:
            self.following = False
            self.ticket = None
            if self.owner == 'follow':
                self.owner = None
                self.control_epoch += 1
                self.command_epoch += 1
            return {'ok': True}

    def start_follow(self):
        with self.lock:
            if not self.flying:
                return {'ok': False, 'reason': 'landed'}
            result = self.request_control('follow')
            if result['ok']:
                self.following = True
            return result

    def pause_follow_for_action(self):
        with self.lock:
            was = self.following
            self.stop_follow()
            self.ticket = object() if was else None
            return {'ok': True, 'ticket': self.ticket}

    def finish_follow_action(self, ticket, success):
        with self.lock:
            if ticket is None or ticket is not self.ticket:
                return {'ok': False, 'reason': 'obsolete_ticket'}
            self.ticket = None
            return self.start_follow() if success else {'ok': True}

    def action(self, name, *args):
        with self.lock:
            self.calls.append((name, args))
            grant = self._context()
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

    def priority_stop(self, land=False):
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
