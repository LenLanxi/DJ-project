"""Deterministic device fake using the production SDK contract."""
import threading
import time
from drone_sdk.contract import FrameResult, StatusResult, CommandResult, ControlResult, ControlGrant


class FakeDevice:
    def __init__(self, monotonic=time.monotonic):
        self.clock = monotonic
        self.lock = threading.RLock()
        self.snapshot = FrameResult(None, 0, 0, True)
        self.lease = None
        self.epoch = 0
        self.connected = self.airborne = True
        self.velocity = (0, 0, 0, 0)
        self.last_update = None
        self.events = []
        self.simulated = True
        self.height_cm, self.height_fresh = 120., True

    def _record(self, event, **data):
        self.events.append(dict(time=self.clock(), event=event, **data))

    def publish(self, frame, frame_id, timestamp, stale=False):
        self.snapshot = FrameResult(frame, frame_id, timestamp, stale)

    def get_frame(self):
        return self.snapshot

    def get_status(self):
        return StatusResult(100, self.height_cm, self.connected,
            'flying' if self.airborne else 'landed', self.lease.owner if self.lease else 'none',
            time.time(), control_epoch=self.epoch, command_epoch=self.epoch,
            velocity=self.velocity, simulated=self.simulated and self.height_fresh,
            height_timestamp=time.time() if self.height_fresh else None)

    def _zero(self, reason):
        self.velocity = (0, 0, 0, 0)
        self.last_update = None
        self._record('zero', reason=reason)

    def request_control(self, owner, *, exclusive=False):
        with self.lock:
            if self.lease is not None:
                return ControlResult(False, self.lease.owner, 'busy')
            self._zero('control_change')
            self.epoch += 1
            self.lease = ControlGrant(owner,self.epoch,self.epoch)
            self._record('grant', owner=owner, epoch=self.epoch)
            return ControlResult(True, owner, grant=self.lease)

    def stop_motion(self, grant, *, cancelled=None):
        owner, control_epoch = grant.owner, grant.control_epoch
        if not self.lease or owner != self.lease.owner or control_epoch != self.epoch:
            return CommandResult(False, 'failed', 'obsolete_control')
        self._zero('stop_motion')
        return CommandResult(True, 'completed')

    def release_control(self, grant, *, cancelled=None):
        owner, control_epoch = grant.owner, grant.control_epoch
        with self.lock:
            if not self.lease or owner != self.lease.owner or control_epoch != self.epoch:
                return ControlResult(False, reason='obsolete_control')
            self._zero('release')
            self.lease = None
            self._record('release', owner=owner)
            return ControlResult(True, 'none')

    def update_velocity(self, grant, left_right, forward_back, up_down, yaw, *, cancelled=None):
        owner, control_epoch, command_epoch = grant.owner, grant.control_epoch, grant.command_epoch
        with self.lock:
            if not self.lease or owner != self.lease.owner or control_epoch != self.epoch:
                self._record('rejected_old_request')
                return CommandResult(False, 'failed', 'obsolete_control')
            if command_epoch is not None and command_epoch != self.epoch:
                return CommandResult(False, 'failed', 'obsolete_command')
            self.velocity = (left_right, forward_back, up_down, yaw)
            self.last_update = self.clock()
            self._record('velocity', channels=self.velocity, owner=owner)
            return CommandResult(True, 'accepted')

    def revoke_for_test(self, owner='model'):
        self._zero('revoke')
        self.epoch += 1
        self.lease = ControlGrant(owner,self.epoch,self.epoch)
        return self.lease

    def watchdog_step(self):
        if self.last_update is not None and self.clock()-self.last_update > .5:
            self._zero('watchdog_timeout')


def make_controller(*, config=None, **options):
    """Keep algorithm cases concise while configuration has one validator."""
    from dataclasses import replace
    from follow.r3_follow.config import FollowConfig
    from follow.r3_follow.core import FollowController
    config = config or FollowConfig()
    config = replace(config, **{k: options.pop(k) for k in tuple(options)
                               if k in FollowConfig.__dataclass_fields__})
    return FollowController(config=config, **options)
