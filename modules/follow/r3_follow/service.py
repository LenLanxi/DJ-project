"""Headless following service, deliberately restricted to simulation ports.

Vision runs separately from control. Backend control methods MUST be prompt;
this service cannot interrupt a blocking backend or prove physical hovering.
"""
from dataclasses import dataclass, asdict
import math
import threading
import time
import uuid
from pathlib import Path

from .core import FollowController, State
from .service_ports import FrameSnapshot, Result
from .vision import MarkerTracker
from .session_log import SessionLog
from .config import FollowConfig
from .notices import ConsoleNotices, describe_status


@dataclass(frozen=True)
class HandoffTicket:
    generation: int
    was_following: bool
    frame_id: int
    service_session_id: str = ""
    nonce: str = ""


class FollowService:
    def __init__(self, port, *, tracker=None, monotonic=time.monotonic,
                 wall_clock=time.time, recovery_window=0, steering_mode="yaw",
                 log_directory=None, target_id=0, instance_id="r3-default",
                 dictionary="DICT_4X4_50", control_profile="legacy",
                 physical_distance_m=None, detection_budget_ms=120.,
                 console_notices=False, notice_sink=None):
        if getattr(port, "simulation_only", None) is not True:
            raise ValueError("This version only accepts simulation ports")
        self.port = port
        if (type(detection_budget_ms) not in (int, float)
                or not math.isfinite(detection_budget_ms) or not 0 < detection_budget_ms <= 250):
            raise ValueError("detection_budget_ms must be in (0, 250]")
        self.detection_budget_ms = float(detection_budget_ms)
        self.config = FollowConfig(instance_id, target_id, dictionary,
                                   control_profile, physical_distance_m, steering_mode)
        # Display labels may repeat; the session identifier must never repeat,
        # including separate services whose frame IDs and generations coincide.
        self.service_session_id = uuid.uuid4().hex
        self.clock, self.wall = monotonic, wall_clock
        if tracker is not None and hasattr(tracker, "target_id"):
            if type(tracker.target_id) is not int or tracker.target_id != target_id:
                raise ValueError("tracker target_id does not match follow configuration")
        self.tracker = tracker if tracker is not None else MarkerTracker(
            target_id=target_id, processing_budget_ms=self.detection_budget_ms)
        # Same gradual request start as the desktop demo. Faults/loss/stop still
        # bypass the ramp and issue zero immediately; no old target prediction.
        self.controller = FollowController(target_id=target_id,
                                           recovery_window=recovery_window,
                                           max_request_rate=.8, require_geometry=True,
                                           steering_mode=steering_mode,
                                           control_profile=control_profile)
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
        self.last_received_frame_id = -1
        self.last_received_timestamp = None
        self.last_received_at = None
        self.last_received_acquired_at = None
        self.last_observation_published_at = None
        self.vision_in_progress = None
        self.last_vision = {}
        self.frame_ready = False
        self.last_send = -math.inf
        self.last_channels = None
        self.last_reason = "not_started"
        self.last_halt_reason = ""
        self.last_halt_at = None
        self._notice_key = None
        self.notice_seq = 0
        self.notifier = ConsoleNotices(notice_sink) if console_notices else None
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
            self.session_log = SessionLog(directory, dict(version="r3-9889b47-closeout1",
                service_session_id=self.service_session_id,
                instance_id=self.config.instance_id, target_id=self.config.target_id,
                dictionary=self.config.dictionary,
                configured_control_profile=self.config.control_profile,
                physical_distance_m=self.config.physical_distance_m,
                physical_distance_source="manual_note_not_estimated",
                port_declared_simulation_only=self.port.simulation_only,
                mode="backend_declared_simulation_unverified",
                hardware_verified=False, steering_mode=self.controller.steering_mode,
                recovery_window=self.controller.recovery_window,
                detection_budget_ms=self.detection_budget_ms,
                detection_budget_policy="cooperative_between_native_calls",
                motion_unit="RC_channel_value_not_physical_velocity",
                direction_frame="aircraft_camera_view",
                source="shared_sdk_frame_provider"))

    def _record(self, event, **data):
        record = dict(time=self.clock(), unix_time=self.wall(), event=event,
                      service_session_id=self.service_session_id,
                      instance_id=self.config.instance_id,
                      target_id=self.config.target_id,
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

    def _fault(self, reason, *, invalid_frame=False, state=None):
        # A recent old detection must not authorize restart after a bad/missing
        # frame. Only a newly accepted frame can clear this latch.
        if invalid_frame:
            self.frame_ready = False
        self.controller.stop()
        if state is not None:
            self.controller.state = state
        self.controller.last_stop_reason = reason
        self._cancel_ticket()
        self.last_reason = reason
        self.last_halt_reason, self.last_halt_at = reason, self.clock()
        self._record("fault", reason=reason)
        self._cleanup()
        self._publish_status()
        return Result("failed", reason)

    def capture_once(self):
        """Fetch/detect without holding the control lock. Never opens a camera."""
        with self.vision_lock:
            with self.lock:
                if self.closed:
                    return False
                generation = self.generation
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
                    if generation != self.generation:
                        # The frame was fetched in an older lifecycle. Consume
                        # its ID even though it never reached the detector, so
                        # a cached re-read cannot become a post-handoff frame.
                        if packet.frame_id > self.last_received_frame_id:
                            self.last_received_frame_id = packet.frame_id
                            self.last_received_timestamp = packet.timestamp
                        self._record("observation_discarded", reason="lifecycle_changed_before_detection",
                                     frame_id=packet.frame_id)
                        return False
                    if packet.frame_id < self.last_received_frame_id:
                        raise ValueError("frame_id_went_backwards")
                    if (packet.frame_id == self.last_received_frame_id
                            and packet.timestamp != self.last_received_timestamp):
                        raise ValueError("cached_frame_timestamp_changed")
                    if packet.frame_id == self.last_received_frame_id:
                        if packet.stale or packet.frame is None:
                            self._record("input_frame_rejected", phase="cached_frame",
                                reason="source_stale" if packet.stale else "missing_pixels",
                                frame_id=packet.frame_id, frame_timestamp=packet.timestamp,
                                checked_at_unix=wall, frame_age_ms=(wall-packet.timestamp)*1000,
                                source_stale=packet.stale, pixels_missing=packet.frame is None)
                            self._fault("frame_unavailable", invalid_frame=True)
                        return False
                    if (self.last_received_timestamp is not None
                            and packet.timestamp <= self.last_received_timestamp):
                        raise ValueError("new_frame_timestamp_not_advancing")
                age = wall - packet.timestamp
                if packet.stale or packet.frame is None or not 0 <= age <= .5:
                    with self.lock:
                        if not self.closed and generation == self.generation:
                            self._record("input_frame_rejected", phase="snapshot_validation",
                                reason=("source_stale" if packet.stale else
                                        "missing_pixels" if packet.frame is None else
                                        "future_timestamp" if age < 0 else "expired_timestamp"),
                                frame_id=packet.frame_id, frame_timestamp=packet.timestamp,
                                checked_at_unix=wall, frame_age_ms=age*1000,
                                source_stale=packet.stale, pixels_missing=packet.frame is None)
                            self._fault("frame_unavailable_or_expired", invalid_frame=True, state=State.STALE)
                        elif not self.closed:
                            if packet.frame_id > self.last_received_frame_id:
                                self.last_received_frame_id, self.last_received_timestamp = packet.frame_id, packet.timestamp
                            self._record("observation_discarded", reason="late_invalid_packet",
                                         frame_id=packet.frame_id)
                    return False
                acquired_at = now-age
                # Leave scheduling room; this is a cooperative budget, not an
                # interrupt of an OpenCV call. Age is checked again on return.
                budget_ms = min(self.detection_budget_ms, max(.001, (.5-age-.02)*1000))
                with self.lock:
                    if self.closed:
                        return False
                    if generation != self.generation:
                        if packet.frame_id > self.last_received_frame_id:
                            self.last_received_frame_id, self.last_received_timestamp = packet.frame_id, packet.timestamp
                        self._record("observation_discarded", reason="lifecycle_changed_before_detection",
                                     frame_id=packet.frame_id)
                        return False
                    work_now = self.clock()
                    input_age = work_now-acquired_at
                    if not 0 <= input_age <= self.controller.max_age:
                        self._record("input_frame_rejected", phase="before_detection",
                            reason="future_acquired_at" if input_age < 0 else "expired_before_detection",
                            frame_id=packet.frame_id, frame_timestamp=packet.timestamp,
                            frame_age_ms=input_age*1000, acquired_at_monotonic=acquired_at,
                            checked_at_monotonic=work_now)
                        self._fault("frame_unavailable_or_expired", invalid_frame=True, state=State.STALE)
                        return False
                    budget_ms = min(budget_ms, max(.001, (self.controller.max_age-input_age-.02)*1000))
                    previous = self.controller.observation
                    if (previous is not None and self.controller.state in (State.TRACKING, State.RECOVERING)):
                        previous_left = self.controller.max_age-(work_now-previous.acquired_at)-.02
                        budget_ms = min(budget_ms, max(.001, previous_left*1000))
                    self.last_received_frame_id, self.last_received_timestamp = packet.frame_id, packet.timestamp
                    self.last_received_at = now
                    self.last_received_acquired_at = acquired_at
                    self.vision_in_progress = dict(frame_id=packet.frame_id, acquired_at=acquired_at,
                        started_at=work_now, budget_ms=budget_ms, generation=generation)
                detection_start = time.perf_counter()
                options = {"processing_budget_ms": budget_ms} if isinstance(self.tracker, MarkerTracker) else {}
                observation = self.tracker.detect(packet.frame, packet.frame_id,
                                                  acquired_at, source="team2-simulation", **options)
                detection_ms = (time.perf_counter() - detection_start) * 1000
                with self.lock:
                    completed_at = self.clock()
                    self.vision_in_progress = None
                    self.last_vision = dict(frame_id=packet.frame_id, capture_age_at_read_ms=age*1000,
                        detection_ms=detection_ms, budget_ms=budget_ms,
                        result_age_ms=(completed_at-acquired_at)*1000,
                        completed_at=completed_at)
                    if self.closed:
                        return False
                    if generation != self.generation:
                        self._record("observation_discarded", reason="lifecycle_changed_during_detection",
                                     **self.last_vision)
                        self._publish_status()
                        return False
                    if not 0 <= completed_at-acquired_at <= self.controller.max_age:
                        self._record("observation_rejected", reason="vision_result_expired", **self.last_vision)
                        self._fault("vision_result_expired", invalid_frame=True, state=State.STALE)
                        return False
                    if (observation.frame_id != packet.frame_id
                            or observation.acquired_at != acquired_at):
                        raise ValueError("detector_changed_frame_identity_or_time")
                    self.last_frame_id, self.last_timestamp = packet.frame_id, packet.timestamp
                    if not self.controller.observe(observation):
                        self._fault("invalid_observation", invalid_frame=True)
                        return False
                    self.frame_ready = True
                    self.last_observation_published_at = completed_at
                    diagnostics = getattr(self.tracker, "last_diagnostics", {})
                    self._record("observation", frame_id=packet.frame_id,
                                 observation_target_id=observation.target_id,
                                 timestamp=packet.timestamp, found=observation.found,
                                 reason=observation.reason, center_x=observation.center_x,
                                 center_y=observation.center_y, size_ratio=observation.size_ratio,
                                 detection_ms=detection_ms,
                                 image_width=observation.width, image_height=observation.height,
                                 corners=observation.corners,
                                 capture_age_at_read_ms=age*1000,
                                 result_age_ms=(completed_at-acquired_at)*1000,
                                 processing_budget=diagnostics.get("processing_budget"),
                                 duplicate_check=diagnostics.get("duplicate_check"),
                                 identity_check=diagnostics.get("identity_check"),
                                 recognition_attempts=diagnostics.get("attempts", [])[:32],
                                 rejected_quads_sample=diagnostics.get("rejected_quads_sample", [])[:6],
                                 detector_method=diagnostics.get("detector_method"),
                                 recognition_profile=diagnostics.get("recognition_profile"))
                    # Control remains on its independent 20 ms loop. Inactive
                    # state can already report 'visible but not following'.
                    if self.controller.state not in (State.TRACKING, State.RECOVERING):
                        self._publish_status()
                    return True
            except Exception as exc:
                with self.lock:
                    self.vision_in_progress = None
                    if not self.closed:
                        if generation == self.generation:
                            self._fault("vision_error: " + str(exc), invalid_frame=True)
                        else:
                            self._record("observation_discarded", reason="late_detection_exception",
                                         error=str(exc))
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
            self.cleanup_unconfirmed = False
            self.last_send = -math.inf
            self.last_channels = None
            self.last_reason = "started"
            self.last_halt_reason, self.last_halt_at = "", None
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
            result = self._cleanup()
            self._publish_status()
            return result

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
            self.last_halt_reason, self.last_halt_at = reason, self.clock()
            self._record("stop", reason=reason)
            result = self._cleanup()
            self._publish_status()
            return result

    def pause_for_action(self):
        with self.lock:
            self.control_once()  # Expire stale follow state before deciding eligibility.
            self._cancel_ticket()
            ticket = HandoffTicket(self.generation,
                                   self.controller.state == State.TRACKING,
                                   self.last_frame_id, self.service_session_id,
                                   uuid.uuid4().hex)
            self.controller.pause()
            cleaned = self._cleanup()
            if not cleaned.ok:
                return cleaned, None
            self.ticket = ticket
            self.last_reason = "handoff_paused"
            self._record("handoff_pause", ticket=asdict(ticket))
            self._publish_status()
            return Result("completed"), ticket

    def finish_action(self, ticket, status):
        with self.lock:
            if (not isinstance(ticket, HandoffTicket) or ticket != self.ticket or self.closed
                    or ticket.service_session_id != self.service_session_id):
                return Result("failed", "obsolete_ticket")
            if status == "accepted":
                return Result("accepted", "waiting_for_action_completion")
            self._cancel_ticket()
            if status != "completed":
                return self._fault("action_failed_or_cancelled")
            if not ticket.was_following or self.last_frame_id <= ticket.frame_id:
                return Result("failed", "resume_requires_prior_follow_and_new_frame")
            result = self._start()
            if not result.ok:
                self.last_reason = "handoff_resume_failed: " + result.reason
            self._record("handoff_finish", status=result.status, reason=result.reason)
            self._publish_status()
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
                        # The old lease is no longer ours. Never retain a
                        # cleanup warning (or issue stop) for a new owner.
                        self.cleanup_unconfirmed = False
                        self._fault("control_lost")
                        return
                    if status.connected is not True or status.airborne is not True:
                        self._fault("device_unavailable_or_not_airborne")
                        return
                motion = self.controller.tick(self.clock())
                if self.controller.state not in (State.TRACKING, State.RECOVERING):
                    if self.lease is not None:
                        self.last_reason = motion.reason
                        if self.last_halt_reason != motion.reason:
                            self.last_halt_reason, self.last_halt_at = motion.reason, self.clock()
                        self._cancel_ticket()
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
                             observation_frame_id=self.controller.observation.frame_id,
                             observation_age_ms=(now-self.controller.observation.acquired_at)*1000,
                             openpose_control=self.controller.status()["openpose_control"],
                             distance_guard=self.controller.distance_guard.status(),
                             urgent_depth_stop=urgent_depth_stop)
            except Exception as exc:
                self._fault("control_error: " + str(exc))
            finally:
                self._publish_status()

    def _presentation(self):
        """Read under lock, without I/O or recursive control calls."""
        now = self.clock()
        obs = self.controller.observation
        age = None if obs is None else now-obs.acquired_at
        fresh = self.frame_ready and age is not None and 0 <= age <= self.controller.max_age
        visible = bool(fresh and obs.found)
        active = (self.lease is not None and not self.closed
                  and self.controller.state in (State.TRACKING, State.RECOVERING))
        pending_cleanup = self.cleanup_unconfirmed or (self.lease is not None and not active)
        reason = self.last_halt_reason or self.last_reason
        state = str(self.controller.state)
        restart = (not active and not self.closed and state != "paused"
                   and bool(self.last_halt_reason))
        remaining = (max(0., self.controller.recovery_window-(now-self.controller._loss_at))
                     if state == "recovering_target" and self.controller._loss_at is not None else None)
        raw_age = None if self.last_received_acquired_at is None else now-self.last_received_acquired_at
        # During work, the input's actual monotonic acquired_at is authoritative.
        if self.vision_in_progress is not None:
            raw_age = now-self.vision_in_progress["acquired_at"]
        budget_wait = bool(obs is not None and obs.reason == "detection_budget_exceeded")
        message = describe_status(state=state, active=active, visible=visible,
            closed=self.closed, pending_resume=self.ticket is not None,
            cleanup_pending=pending_cleanup, reason=reason, budget_wait=budget_wait,
            identity_wait=bool(obs is not None and obs.reason == "ambiguous_scan_budget_exceeded"))
        return dict(target_visible=visible, observation_fresh=bool(fresh),
            observation_age_ms=None if age is None else age*1000,
            raw_frame_age_ms=None if raw_age is None else raw_age*1000,
            detection_in_progress=self.vision_in_progress is not None,
            detection_elapsed_ms=(None if self.vision_in_progress is None else
                                   max(0., (now-self.vision_in_progress["started_at"])*1000)),
            last_vision=dict(self.last_vision),
            restart_required=restart, recovery_remaining_s=remaining,
            last_halt_reason=self.last_halt_reason, last_halt_at=self.last_halt_at,
            requested_channels=self.last_channels if active and self.last_channels is not None else (0,0,0,0),
            physical_hover_confirmed=False, status_message=message,
            status_notice_seq=self.notice_seq)

    def _publish_status(self):
        presentation = self._presentation()
        key = (str(self.controller.state), presentation["status_message"])
        if key == self._notice_key:
            return
        self._notice_key = key
        self.notice_seq += 1
        self._record("status_notice", state=str(self.controller.state),
                     message=presentation["status_message"], notice_seq=self.notice_seq,
                     target_visible=presentation["target_visible"],
                     restart_required=presentation["restart_required"],
                     last_halt_reason=self.last_halt_reason)
        if self.notifier is not None:
            self.notifier.submit(f"[三组 {self.config.instance_id}/ID{self.config.target_id} "
                                 f"状态#{self.notice_seq} t={self.clock():.3f}s] "
                                 + presentation["status_message"])

    def is_following(self):
        return self.get_follow_status()["following"]

    def get_follow_status(self):
        with self.lock:
            # Queries also expire stale data when the caller uses manual stepping.
            self.control_once()
            return dict(state=str(self.controller.state),
                        instance_id=self.config.instance_id,
                        service_session_id=self.service_session_id,
                        target_id=self.config.target_id,
                        dictionary=self.config.dictionary,
                        configured_control_profile=self.config.control_profile,
                        configured_steering_mode=self.config.steering_mode,
                        motion_unit="RC_channel_value_not_physical_velocity",
                        actual_velocity_m_s=None,
                        actual_velocity_source="not_measured",
                        physical_distance_m=self.config.physical_distance_m,
                        physical_distance_source="manual_note_not_estimated",
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
                        frame_id=self.last_frame_id, **self._presentation())

    def start_background(self):
        with self.lock:
            if self.closed:
                raise RuntimeError("Service is closed; create a new instance")
            if self.workers:
                return
            self._start_log()
            if self.notifier is not None:
                self.notifier.start()
            def loop(action, interval):
                while not self.shutdown.is_set():
                    started = time.monotonic()
                    action()
                    # Read the newest upstream snapshot immediately after slow
                    # processing. There is no local queue of old images.
                    self.shutdown.wait(max(.002, interval-(time.monotonic()-started)))
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
            self._publish_status()
        if wait:
            for worker in self.workers:
                if worker is not threading.current_thread():
                    worker.join(timeout=.25)
        if self.session_log is not None:
            self.session_log.close(wait=wait)
        if self.notifier is not None:
            self.notifier.close(wait=wait)
        return result
