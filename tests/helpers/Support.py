"""Recording-only device/follow replies for transport tests; no SDK policies."""
import time
from drone_sdk.contract import CommandResult, ControlResult, ControlGrant, StatusResult

class MockBackend:
    simulated = True
    def __init__(self):
        self.owner = None
        self.flying = self.following = False
        self.calls = []
        self.grant = ControlGrant('model', 1, 1)
        self.observation_index = 0

    def get_status(self):
        return StatusResult(80, 80 if self.flying else 0, True,
            'flying' if self.flying else 'landed', self.owner or 'none', time.time(),
            self.grant.control_epoch, self.grant.command_epoch, simulated=True)

    def request_control(self, owner, **options):
        self.owner = owner
        self.grant = ControlGrant(owner, 1, 1)
        return ControlResult(True, owner, grant=self.grant)

    def release_control(self, grant, **options):
        self.owner = None
        return ControlResult(True)

    def action(self, name, *args, grant=None, cancelled=None):
        self.calls.append((name, args))
        return CommandResult(True, 'completed', grant=grant)

    def priority_interrupt(self, land=False):
        self.calls.append(('priority_stop', (land,)))
        return CommandResult(True, 'accepted', grant=self.grant)

    def start(self, *, cancelled=None):
        self.following = True
        return CommandResult(True, 'completed')

    def stop(self, *, cancelled=None):
        self.following = False
        return CommandResult(True, 'completed')

    def start_follow(self):
        return self.start()

    def is_following(self):
        return self.following

    def pause_if_active(self, *, cancelled=None):
        active = self.following
        self.following = False
        return object() if active else None

    def finish_action(self, ticket, success, **options):
        self.calls.append(('finish_action', (success,)))
        return CommandResult(True, 'completed')

    def stop_cancelled_operation(self, event):
        return CommandResult(True, 'completed')

    def get_frame(self):
        raise RuntimeError('recording device has no camera')


def _action(name):
    def call(self, grant, *args, cancelled=None):
        return self.action(name, *args, grant=grant, cancelled=cancelled)
    return call

for _name in ('takeoff', 'land', 'stop_motion', 'rotate', 'update_velocity',
              'move_forward', 'move_back', 'move_left', 'move_right', 'move_up', 'move_down'):
    setattr(MockBackend, _name, _action(_name))
