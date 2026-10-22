"""Headless following service, deliberately restricted to simulation ports.

Vision runs separately from control. Backend control methods MUST be prompt;
this service cannot interrupt a blocking backend or prove physical hovering.
"""
from dataclasses import dataclass, asdict
import math
import threading
import time

from .core import FollowController, State
from .service_ports import FrameSnapshot, Result
from .vision import MarkerTracker


@dataclass(frozen=True)
class HandoffTicket:
    generation: int
    was_following: bool
    frame_id: int


class FollowService:
    def __init__(self, port, *, tracker=None, monotonic=time.monotonic,
                 wall_clock=time.time, recovery_window=0):
        if getattr(port, "simulation_only", None) is not True:
            raise ValueError("This version only accepts simulation ports")
        self.port = port
        self.clock, self.wall = monotonic, wall_clock
        self.tracker = tracker if tracker is not None else MarkerTracker()
        self.controller = FollowController(recovery_window=recovery_window)
        self.lock = threading.RLock()
        self.vision_lock = threading.Lock()
        self.shutdown = threading.Event()
        self.workers = []
        self.closed = False
        self.lease = None
        self.ticket = None
        self.generation = 0
        self.last_frame_id = -1
        self.last_timestamp = None
        self.last_send = -math.inf
        self.last_channels = None
        self.last_reason = "not_started"
        self.events = []

    def _record(self, event, **data):
        self.events.append(dict(time=self.clock(), event=event,
                                simulation_only=True, **data))

    def _cancel_ticket(self):
        self.generation += 1
        self.ticket = None

    def _cleanup(self):
        """Only clear our own lease; never stop a new owner's motion."""
        lease = self.lease
        if lease is None:
            return Result("completed")
        try:
            if self.port.get_status().lease != lease:
                self.lease = None
                return Result("completed", "already_released_or_revoked")
            stopped = self.port.stop_motion(lease)
            released = self.port.release_control(lease)
            if released.status == "completed":
                self.lease = None
            if stopped.status != "completed" or released.status != "completed":
                self.last_reason = "cleanup_unconfirmed"
                self._record("cleanup_failed", stop=stopped.status, release=released.status)
                return Result("failed", self.last_reason)
            self.last_channels = (0, 0, 0, 0)
            return Result("completed")
        except Exception as exc:
            self.last_reason = "cleanup_exception"
            self._record("cleanup_failed", error=str(exc))
            return Result("failed", self.last_reason)

    def _fault(self, reason):
        self.controller.stop()
        self._cancel_ticket()
        self.last_reason = reason
        self._record("fault", reason=reason)
        self._cleanup()
        return Result("failed", reason)

    def capture_once(self):
        """Fetch/detect without holding the control lock. Never opens a camera."""
        with self.vision_lock:
            if self.closed:
                return False
            try:
                packet = self.port.get_frame()
                now, wall = self.clock(), self.wall()
                if (not isinstance(packet, FrameSnapshot)
                        or type(packet.frame_id) is not int or packet.frame_id < 0
                        or type(packet.stale) is not bool
                        or type(packet.timestamp) not in (int, float)
                        or not math.isfinite(packet.timestamp)):
                    raise ValueError("invalid_frame_metadata")
                with self.lock:
                    if self.closed:
                        return False
                    if packet.frame_id < self.last_frame_id:
                        raise ValueError("frame_id_went_backwards")
                    if packet.frame_id == self.last_frame_id:
                        if packet.timestamp != self.last_timestamp:
                            raise ValueError("cached_frame_timestamp_changed")
                        if packet.stale or packet.frame is None:
                            self._fault("frame_unavailable")
                        return False
                age = wall - packet.timestamp
                if packet.stale or packet.frame is None or not 0 <= age <= .5:
                    with self.lock:
                        if not self.closed:
                            self._fault("frame_unavailable_or_expired")
                    return False
                observation = self.tracker.detect(packet.frame, packet.frame_id,
                                                  now - age, source="team2-simulation")
                with self.lock:
                    if self.closed:
                        return False
                    self.last_frame_id, self.last_timestamp = packet.frame_id, packet.timestamp
                    if not self.controller.observe(observation):
                        self._fault("invalid_observation")
                        return False
                    self._record("observation", frame_id=packet.frame_id,
                                 found=observation.found, reason=observation.reason)
                    return True
            except Exception as exc:
                with self.lock:
                    if not self.closed:
                        self._fault("vision_error: " + str(exc))
                return False

    def calibrate_reference(self):
        with self.lock:
            if self.closed:
                return Result("failed", "closed")
            ok, reason = self.controller.calibrate(self.clock())
            return Result("completed" if ok else "failed", reason)

    def _start(self):
        if self.closed:
            return Result("failed", "closed")
        if self.lease is not None:
            return Result("failed", "lease_cleanup_required")
        try:
            status = self.port.get_status()
            if status.connected is not True or status.airborne is not True:
                return Result("failed", "connected_airborne_state_required")
            if status.lease is not None:
                return Result("failed", "busy")
            ok, reason = self.controller.start(self.clock())
            if not ok:
                return Result("failed", reason)
            grant = self.port.request_control("follow")
            if not grant.ok or grant.lease is None or grant.lease.owner != "follow":
                self.controller.stop()
                return Result("failed", grant.reason or "invalid_control_grant")
            self.lease = grant.lease
            self.last_send = -math.inf
            self.last_channels = None
            self.last_reason = "started"
            self.control_once()
            if self.controller.state != State.TRACKING:
                return Result("failed", self.last_reason)
            self._record("start")
            return Result("completed", "simulated_follow_started")
        except Exception as exc:
            return self._fault("start_error: " + str(exc))

    def start_follow(self):
        with self.lock:
            self._cancel_ticket()
            if self.controller.state == State.TRACKING and self.lease is not None:
                self.control_once()
                if self.controller.state == State.TRACKING:
                    return Result("completed", "already_following")
                return Result("failed", self.last_reason)
            return self._start()

    def pause_follow(self):
        with self.lock:
            self._cancel_ticket()
            self.controller.pause()
            self.last_reason = "paused"
            self._record("pause")
            return self._cleanup()

    def resume_follow(self):
        with self.lock:
            if self.controller.state != State.PAUSED:
                return Result("failed", "not_paused_use_start")
            self._cancel_ticket()
            return self._start()

    def stop_follow(self, reason="explicit_stop"):
        with self.lock:
            self._cancel_ticket()
            self.controller.stop()
            self.last_reason = reason
            self._record("stop", reason=reason)
            return self._cleanup()

    def pause_for_action(self):
        with self.lock:
            self.control_once()  # Expire stale follow state before deciding eligibility.
            self._cancel_ticket()
            ticket = HandoffTicket(self.generation,
                                   self.controller.state == State.TRACKING,
                                   self.last_frame_id)
            self.controller.pause()
            cleaned = self._cleanup()
            if not cleaned.ok:
                return cleaned, None
            self.ticket = ticket
            self._record("handoff_pause", ticket=asdict(ticket))
            return Result("completed"), ticket

    def finish_action(self, ticket, status):
        with self.lock:
            if ticket is None or ticket != self.ticket or self.closed:
                return Result("failed", "obsolete_ticket")
            if status == "accepted":
                return Result("accepted", "waiting_for_action_completion")
            self._cancel_ticket()
            if status != "completed":
                return self._fault("action_failed_or_cancelled")
            if not ticket.was_following or self.last_frame_id <= ticket.frame_id:
                return Result("failed", "resume_requires_prior_follow_and_new_frame")
            result = self._start()
            self._record("handoff_finish", status=result.status, reason=result.reason)
            return result

    def control_once(self):
        with self.lock:
            if self.closed:
                return
            try:
                if self.lease is not None:
                    status = self.port.get_status()
                    if status.lease != self.lease:
                        self.lease = None
                        self._fault("control_lost")
                        return
                    if status.connected is not True or status.airborne is not True:
                        self._fault("device_unavailable_or_not_airborne")
                        return
                motion = self.controller.tick(self.clock())
                if self.controller.state not in (State.TRACKING, State.RECOVERING):
                    if self.lease is not None:
                        self.last_reason = motion.reason
                        self._record("tracking_halted", state=str(self.controller.state), reason=motion.reason)
                        self._cleanup()
                    return
                if self.lease is None:
                    self._fault("missing_lease")
                    return
                # Preview conversion only; not a calibrated physical speed.
                channels = (round(motion.right * 100), round(motion.forward * 100), 0, 0)
                channels = tuple(max(-30, min(30, value)) for value in channels)
                now = self.clock()
                urgent_zero = channels == (0, 0, 0, 0) and channels != self.last_channels
                if now - self.last_send < .1 - 1e-9 and not urgent_zero:
                    return
                result = self.port.update_velocity(self.lease, left_right=channels[0],
                    forward_back=channels[1], up_down=0, yaw=0)
                if not result.ok:
                    self._fault("velocity_rejected: " + result.reason)
                    return
                self.last_send, self.last_channels = now, channels
                self._record("motion_request", channels=channels, status=result.status)
            except Exception as exc:
                self._fault("control_error: " + str(exc))

    def is_following(self):
        return self.get_follow_status()["following"]

    def get_follow_status(self):
        with self.lock:
            # Queries also expire stale data when the caller uses manual stepping.
            self.control_once()
            return dict(state=str(self.controller.state),
                        following=self.controller.state == State.TRACKING,
                        reference_size=self.controller.reference_size,
                        lease=asdict(self.lease) if self.lease else None,
                        last_reason=self.last_reason, closed=self.closed,
                        simulation_only=True, hardware_verified=False,
                        background_running=any(t.is_alive() for t in self.workers),
                        frame_id=self.last_frame_id)

    def start_background(self):
        with self.lock:
            if self.closed:
                raise RuntimeError("Service is closed; create a new instance")
            if self.workers:
                return
            def loop(action, interval):
                while not self.shutdown.is_set():
                    action()
                    self.shutdown.wait(interval)
            self.workers = [threading.Thread(target=loop, args=(self.capture_once, .03),
                            daemon=True, name="follow-vision"),
                            threading.Thread(target=loop, args=(self.control_once, .02),
                            daemon=True, name="follow-control")]
            for worker in self.workers:
                worker.start()

    def close(self):
        with self.lock:
            result = self.stop_follow("closed")
            self.closed = True
            self.shutdown.set()
        for worker in self.workers:
            if worker is not threading.current_thread():
                worker.join(timeout=.25)
        return result
