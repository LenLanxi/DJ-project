"""Headless following service, deliberately restricted to simulation ports.

Vision runs separately from control. Backend control methods MUST be prompt;
this service cannot interrupt a blocking backend or prove physical hovering.
"""
from dataclasses import dataclass, asdict
import math
import threading
import time
from pathlib import Path

from .core import FollowController, State
from .service_ports import FrameSnapshot, Result
from .vision import MarkerTracker
from .session_log import SessionLog


@dataclass(frozen=True)
class HandoffTicket:
    generation: int
    was_following: bool
    frame_id: int


class FollowService:
    def __init__(self, port, *, tracker=None, monotonic=time.monotonic,
                 wall_clock=time.time, recovery_window=0, steering_mode="yaw",
                 log_directory=None):
        if getattr(port, "simulation_only", None) is not True:
            raise ValueError("This version only accepts simulation ports")
        self.port = port
        self.clock, self.wall = monotonic, wall_clock
        self.tracker = tracker if tracker is not None else MarkerTracker()
        # Same gradual request start as the desktop demo. Faults/loss/stop still
        # bypass the ramp and issue zero immediately; no old target prediction.
        self.controller = FollowController(recovery_window=recovery_window,
                                           max_request_rate=.8, require_geometry=True,
                                           steering_mode=steering_mode)
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
        self.frame_ready = False
        self.last_send = -math.inf
        self.last_channels = None
        self.last_reason = "not_started"
        self.events = []
        self.last_cleanup = -math.inf
        self.cleanup_unconfirmed = False
        self.log_directory = log_directory
        self.session_log = None
        if log_directory is not None and log_directory is not False:
            self._start_log()

    def _start_log(self):
        if self.session_log is None and self.log_directory is not False:
            directory = self.log_directory if self.log_directory is not None else (
                Path(__file__).resolve().parents[1] / "logs" / "background")
            self.session_log = SessionLog(directory, dict(version="r3-b313eb3-fix1",
                port_declared_simulation_only=self.port.simulation_only,
                mode="backend_declared_simulation_unverified",
                hardware_verified=False, steering_mode=self.controller.steering_mode,
                recovery_window=self.controller.recovery_window,
                source="shared_sdk_frame_provider"))

    def _record(self, event, **data):
        record = dict(time=self.clock(), unix_time=self.wall(), event=event,
                      simulation_only=True, simulation_flag_scope="port_contract_only",
                      hardware_verified=False, **data)
        self.events.append(record)
        if len(self.events) > 4096:
            del self.events[:len(self.events)-4096]
        if self.session_log is not None:
            self.session_log.submit(record)

    def _cancel_ticket(self):
        self.generation += 1
        self.ticket = None

    def _cleanup(self, *, retry=False):
        """Only clear our own lease; never stop a new owner's motion."""
        lease = self.lease
        if lease is None:
            return Result("completed")
        if retry and self.clock() - self.last_cleanup < .1 - 1e-9:
            return Result("failed", "cleanup_pending")
        self.last_cleanup = self.clock()
        try:
            if self.port.get_status().lease != lease:
                self.lease = None
                self.cleanup_unconfirmed = False
                return Result("completed", "already_released_or_revoked")
            stopped = self.port.stop_motion(lease)
            released = self.port.release_control(lease)
            if released.status == "completed":
                self.lease = None
            # stop is a send acknowledgement, not physical hover confirmation.
            if not stopped.ok or released.status != "completed":
                self.cleanup_unconfirmed = True
                self.last_reason = "cleanup_unconfirmed"
                self._record("cleanup_failed", stop=stopped.status, release=released.status)
                return Result("failed", self.last_reason)
            self.last_channels = (0, 0, 0, 0)
            self.cleanup_unconfirmed = False
            self._record("cleanup_completed", physical_hover_confirmed=False)
            return Result("completed")
        except Exception as exc:
            try: self.port.stop_motion(lease)
            except Exception: pass
            self.cleanup_unconfirmed = True
            self.last_reason = "cleanup_exception"
            self._record("cleanup_failed", error=str(exc))
            return Result("failed", self.last_reason)

    def _fault(self, reason, *, invalid_frame=False):
        # A recent old detection must not authorize restart after a bad/missing
        # frame. Only a newly accepted frame can clear this latch.
        if invalid_frame:
            self.frame_ready = False
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
                            self._fault("frame_unavailable", invalid_frame=True)
                        return False
                age = wall - packet.timestamp
                if packet.stale or packet.frame is None or not 0 <= age <= .5:
                    with self.lock:
                        if not self.closed:
                            self._fault("frame_unavailable_or_expired", invalid_frame=True)
                    return False
                detection_start = time.perf_counter()
                observation = self.tracker.detect(packet.frame, packet.frame_id,
                                                  now - age, source="team2-simulation")
                detection_ms = (time.perf_counter() - detection_start) * 1000
                with self.lock:
                    if self.closed:
                        return False
                    self.last_frame_id, self.last_timestamp = packet.frame_id, packet.timestamp
                    if not self.controller.observe(observation):
                        self._fault("invalid_observation", invalid_frame=True)
                        return False
                    self.frame_ready = True
                    diagnostics = getattr(self.tracker, "last_diagnostics", {})
                    self._record("observation", frame_id=packet.frame_id,
                                 timestamp=packet.timestamp, found=observation.found,
                                 reason=observation.reason, center_x=observation.center_x,
                                 center_y=observation.center_y, size_ratio=observation.size_ratio,
                                 detection_ms=detection_ms,
                                 detector_method=diagnostics.get("detector_method"),
                                 recognition_profile=diagnostics.get("recognition_profile"))
                    return True
            except Exception as exc:
                with self.lock:
                    if not self.closed:
                        self._fault("vision_error: " + str(exc), invalid_frame=True)
                return False

    def calibrate_reference(self):
        with self.lock:
            if self.closed:
                return Result("failed", "closed")
            if not self.frame_ready:
                return Result("failed", "new_valid_frame_required")
            ok, reason = self.controller.calibrate(self.clock())
            self._record("calibration", ok=ok, reason=reason,
                         reference_size=self.controller.reference_size)
            return Result("completed" if ok else "failed", reason)

    def _start(self):
        if self.closed:
            return Result("failed", "closed")
        if not self.frame_ready:
            return Result("failed", "new_valid_frame_required")
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
            if self.controller.state in (State.TRACKING, State.RECOVERING) and self.lease is not None:
                self.control_once()
                if self.controller.state in (State.TRACKING, State.RECOVERING):
                    return Result("completed", "already_following" if self.controller.state == State.TRACKING
                                  else "waiting_for_target")
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
                        self._cleanup(retry=True)
                    return
                if self.lease is None:
                    self._fault("missing_lease")
                    return
                # Preview conversion only; not a calibrated physical speed.
                channels = (round(motion.right * 100), round(motion.forward * 100), 0,
                            round(motion.yaw * 100))
                channels = tuple(max(-cap, min(cap, value))
                                 for value, cap in zip(channels, (30, 30, 0, 20)))
                now = self.clock()
                urgent_zero = channels == (0, 0, 0, 0) and channels != self.last_channels
                urgent_depth_stop = (self.last_channels is not None
                                     and self.last_channels[1] != 0 and channels[1] == 0)
                reductions = None if self.last_channels is None else tuple(
                    0 if old and new == 0 else old
                    for old, new in zip(self.last_channels, channels))
                if now - self.last_send < .1 - 1e-9 and not urgent_zero:
                    if reductions is None or reductions == self.last_channels:
                        return
                    # Safety braking may bypass the 10 Hz deadline. No other
                    # axis may increase or reverse during this early send.
                    channels = reductions
                result = self.port.update_velocity(self.lease, left_right=channels[0],
                    forward_back=channels[1], up_down=channels[2], yaw=channels[3])
                if not result.ok:
                    self._fault("velocity_rejected: " + result.reason)
                    return
                self.last_send, self.last_channels = now, channels
                self._record("motion_request", channels=channels, status=result.status,
                             state=str(self.controller.state), reason=motion.reason,
                             reference_size=self.controller.reference_size,
                             distance_guard=self.controller.distance_guard.status(),
                             urgent_depth_stop=urgent_depth_stop)
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
                        active=(self.lease is not None and not self.closed and
                                self.controller.state in (State.TRACKING, State.RECOVERING)),
                        recovering=self.controller.state == State.RECOVERING,
                        cleanup_pending=(self.lease is not None and
                                         self.controller.state not in (State.TRACKING, State.RECOVERING)),
                        cleanup_unconfirmed=self.cleanup_unconfirmed,
                        pending_resume=self.ticket is not None,
                        reference_size=self.controller.reference_size,
                        lease=asdict(self.lease) if self.lease else None,
                        last_reason=self.last_reason, closed=self.closed,
                        control_profile=self.controller.status()["control_profile"],
                        steering=self.controller.status()["steering"],
                        distance_guard=self.controller.distance_guard.status(),
                        max_request_rate=self.controller.max_request_rate,
                        simulation_only=True, hardware_verified=False,
                        background_running=any(t.is_alive() for t in self.workers),
                        log=self.session_log.status() if self.session_log else None,
                        frame_ready=self.frame_ready,
                        frame_id=self.last_frame_id)

    def start_background(self):
        with self.lock:
            if self.closed:
                raise RuntimeError("Service is closed; create a new instance")
            if self.workers:
                return
            self._start_log()
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

    def close(self, wait=True):
        with self.lock:
            result = self.stop_follow("closed")
            self.closed = True
            self.shutdown.set()
        if wait:
            for worker in self.workers:
                if worker is not threading.current_thread():
                    worker.join(timeout=.25)
        if self.session_log is not None:
            self.session_log.close(wait=wait)
        return result
