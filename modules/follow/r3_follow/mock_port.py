"""Minimal recording test double. No SDK, camera, network or real motion.

watchdog_step is deterministic; the test explicitly advances it. This does not
claim to verify team 2's future independent watchdog implementation.
"""
import threading
import time
from .service_ports import DeviceStatus, FrameSnapshot, Lease, Result


class MockPort:
    simulation_only = True

    def __init__(self, monotonic=time.monotonic):
        self.clock = monotonic
        self.lock = threading.RLock()
        self.snapshot = FrameSnapshot(None, 0, 0, True)
        self.lease = None
        self.epoch = 0
        self.connected = True
        self.airborne = True  # explicitly simulated, never a real flight claim
        self.velocity = (0, 0, 0, 0)
        self.last_update = None
        self.events = []

    def _record(self, event, **data):
        self.events.append(dict(time=self.clock(), event=event, simulation_only=True, **data))

    def publish(self, frame, frame_id, timestamp, stale=False):
        with self.lock:
            self.snapshot = FrameSnapshot(frame, frame_id, timestamp, stale)

    def get_frame(self):
        with self.lock:
            return self.snapshot

    def get_status(self):
        with self.lock:
            return DeviceStatus(self.connected, self.airborne, self.lease,
                                getattr(self, "height_cm", 120.),
                                getattr(self, "height_fresh", True))

    def _zero(self, reason):
        self.velocity = (0, 0, 0, 0)
        self.last_update = None
        self._record("zero", reason=reason)

    def request_control(self, owner):
        with self.lock:
            if self.lease is not None:
                return Result("failed", "busy")
            self._zero("control_change")
            self.epoch += 1
            self.lease = Lease(owner, self.epoch)
            self._record("grant", owner=owner, epoch=self.epoch)
            return Result("completed", lease=self.lease)

    def stop_motion(self, lease):
        with self.lock:
            if lease != self.lease or lease is None:
                return Result("failed", "not_owner")
            self._zero("stop_motion")
            return Result("completed")

    def release_control(self, lease):
        with self.lock:
            if lease != self.lease or lease is None:
                return Result("failed", "not_owner")
            self._zero("release")
            self.lease = None
            self._record("release", owner=lease.owner)
            return Result("completed")

    def update_velocity(self, lease, *, left_right, forward_back, up_down, yaw):
        with self.lock:
            if lease != self.lease or lease is None:
                self._record("rejected_old_request")
                return Result("failed", "not_owner")
            values = (left_right, forward_back, up_down, yaw)
            if any(type(v) is not int or abs(v) > 100 for v in values):
                return Result("failed", "invalid_channels")
            self.velocity = values
            self.last_update = self.clock()
            self._record("velocity", channels=values, owner=lease.owner)
            return Result("accepted")

    def revoke_for_test(self, owner="model"):
        with self.lock:
            self._zero("revoke")
            self.epoch += 1
            self.lease = Lease(owner, self.epoch)
            return self.lease

    def watchdog_step(self):
        with self.lock:
            if self.last_update is not None and self.clock()-self.last_update > .5:
                self._zero("watchdog_timeout")
