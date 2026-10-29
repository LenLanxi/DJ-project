"""Following lifecycle and algorithm workers using the shared device contract.

Vision runs separately from control. Backend control methods MUST be prompt;
this service cannot interrupt a blocking backend or prove physical hovering.
"""
from dataclasses import dataclass, asdict
import math
from collections import deque
import threading
import time
import uuid
from pathlib import Path

from .core import FollowController, State, FRAME_TIME_EPSILON
from drone_sdk.contract import FrameResult, CommandResult
from .config import FollowConfig
from .vision import MarkerTracker
from .session_log import SessionLog
from .notices import ConsoleNotices, describe_status


@dataclass(frozen=True, eq=False)
class _Handoff:
    frame_id: int


class FollowService:
    """Own the SDK and internal tracker/controller contracts for one session.

    Injected trackers use detect(..., processing_budget_ms=...) and preserve
    the supplied frame ID and capture time.
    """

    def __init__(self, device, *, config: FollowConfig, frame_provider=None, tracker=None, monotonic=time.perf_counter,
                 wall_clock=time.time, auto_recover=True, log_directory=None,
                 detection_budget_ms=120., console_notices=False, notice_sink=None, log_sink=None):
        self.device = device
        self.simulated = device.get_status().simulated
        self.frame_provider = frame_provider
        self._start_gate = threading.Lock()
        self._operation_source = None
        self._last_start_failure = ""
        if (type(detection_budget_ms) not in (int, float)
                or not math.isfinite(detection_budget_ms) or not 0 < detection_budget_ms <= 250):
            raise ValueError("detection_budget_ms must be in (0, 250]")
        self.detection_budget_ms = float(detection_budget_ms)
        self.config = config
        target_id = self.config.target_id
        vertical_follow = self.config.vertical_follow
        distance_mode = self.config.distance_mode
        horizontal_limit_rc, forward_limit_rc = self.config.horizontal_limit_rc, self.config.forward_limit_rc
        self.vertical_guard_reason = "disabled" if not vertical_follow else "awaiting_height"
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
        self.controller = FollowController(config=self.config,
                                           auto_recover=auto_recover,
                                           require_geometry=True)
        # RC clamp is derived from the SAME limits the law uses, so raising the
        # budget in one place cannot be silently re-clamped here. Vertical is opt-in. Hard ceiling is the SDK's RC range.
        self.rc_caps = (min(100, round(horizontal_limit_rc)),
                        min(100, round(self.controller.forward_limit_rc)),
                        20 if vertical_follow else 0,
                        min(100, round(self.controller.horizontal_limit_rc)))
        self.lock = threading.RLock()
        self.vision_lock = threading.Lock()
        self.shutdown = threading.Event()
        self.workers = []
        self.closed = False
        self.grant = None
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
        self.last_regular_send = -math.inf
        self.last_channels = None
        self.last_reason = "not_started"
        self.last_halt_reason = ""
        self.last_halt_at = None
        self._notice_key = None
        self.notice_seq = 0
        self.notifier = ConsoleNotices(notice_sink) if console_notices else None
        self._events = deque(maxlen=4096)
        self.last_cleanup = -math.inf
        self.cleanup_unconfirmed = False
        self.log_directory = log_directory
        self.log_sink = log_sink
        self.session_log = None
        if (log_directory is not None or log_sink is not None) and log_directory is not False:
            self._start_log()

    def _start_log(self):
        if self.session_log is None and self.log_directory is not False:
            directory = self.log_directory if self.log_directory is not None else (
                Path(__file__).resolve().parents[1] / "logs" / "background")
            self.session_log = SessionLog(directory, dict(version="r3-motion-follow1",
                service_session_id=self.service_session_id,
                instance_id=self.config.instance_id, target_id=self.config.target_id,
                dictionary=self.config.dictionary,
                configured_control_profile=self.config.control_profile,
                pid_tuning=self.config.pid_tuning,
                vertical_follow=self.config.vertical_follow,
                vertical_limit_rc=20 if self.config.vertical_follow else 0,
                distance_mode=self.config.distance_mode,
                height_guard_cm=[self.config.vertical_min_height_cm,self.config.vertical_max_height_cm],
                simulated=self.simulated,
                mode="simulated_device" if self.simulated else "real_device",
                hardware_verified=False, steering_mode=self.controller.steering_mode,
                auto_recover=self.controller.auto_recover,
                detection_budget_ms=self.detection_budget_ms,
                horizontal_limit_rc=self.controller.horizontal_limit_rc,
                forward_limit_rc=self.controller.forward_limit_rc,
                rc_caps=list(self.rc_caps),
                rc_caps_unit="percentage_of_max_rc_100_not_metres_per_second",
                detection_budget_policy="cooperative_between_native_calls",
                motion_unit="RC_channel_value_not_physical_velocity",
                direction_frame="aircraft_camera_view",
                source="shared_sdk_frame_provider"),
                **({'sink': self.log_sink} if self.log_sink is not None else {}))

    def _record(self, event, **data):
        record = dict(time=self.clock(), unix_time=self.wall(), event=event,
                      service_session_id=self.service_session_id,
                      instance_id=self.config.instance_id,
                      target_id=self.config.target_id,
                      simulated=self.simulated,
                      hardware_verified=False, **data)
        self._events.append(record)
        if self.session_log is not None:
            self.session_log.submit(record)

    def _cancel_ticket(self):
        self.generation += 1
        self.ticket = None

    def _owns_control(self, status):
        return (self.grant is not None and status.control_owner == "follow"
                and status.control_epoch == self.grant.control_epoch)

    def _height_fresh(self, status):
        stamp = status.height_timestamp
        return (status.connected is True and status.flight_state == "flying"
                and type(status.height_cm) in (int, float) and math.isfinite(status.height_cm)
                and 0 <= status.height_cm <= 1000
                and (status.simulated or type(stamp) in (int, float)
                     and math.isfinite(stamp) and 0 <= self.wall()-stamp <= 1.0))

    def _cleanup(self, *, retry=False):
        """SDK release atomically zeros and retires this captured authorization."""
        if self.grant is None:
            return CommandResult(True, "completed")
        if retry and self.clock()-self.last_cleanup < .1-1e-9:
            return CommandResult(False, "failed", "cleanup_pending")
        self.last_cleanup = self.clock()
        try:
            result = self.device.release_control(self.grant)
            if result.ok or result.reason in ("obsolete_control", "no_control"):
                self.grant = None
                self.cleanup_unconfirmed = False
                self.last_channels = (0, 0, 0, 0)
                self._record("cleanup_completed", physical_hover_confirmed=False)
                return CommandResult(True, "completed")
            # A priority owner may already have retired our grant. Never zero it.
            if not self._owns_control(self.device.get_status()):
                self.grant = None
                self.cleanup_unconfirmed = False
                return CommandResult(True, "completed", "already_released_or_revoked")
            self.last_reason = "cleanup_unconfirmed"
        except Exception as exc:
            self.last_reason = "cleanup_exception"
            self._record("cleanup_failed", error=str(exc))
        self.cleanup_unconfirmed = True
        return CommandResult(False, "failed", self.last_reason)

    def retry_cleanup(self):
        """Retry a failed zero/release while retaining ownership evidence."""
        with self.lock:
            return self._cleanup()

    def _fault(self, reason, *, invalid_frame=False, state=None):
        if invalid_frame and reason != 'image_size_changed_requires_new_session':
            self.frame_ready = False
            self.controller._invalid = True
            if self.controller.state in (State.TRACKING, State.RECOVERING):
                self.controller.state = State.RECOVERING
                self.controller._clear_recovery()
            self.controller._zero(reason)
            if self.grant is not None:
                stopped = self.device.update_velocity(self.grant, 0, 0, 0, 0)
                if not stopped.ok:
                    return self._fault('frame_wait_zero_failed')
                self.last_channels = (0, 0, 0, 0)
            self.last_reason = reason
            self._record('frame_wait', reason=reason)
            self._publish_status()
            return CommandResult(False, 'failed', reason)
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
        return CommandResult(False, "failed", reason)

    def capture_once(self):
        """Fetch/detect without holding the control lock. Never opens a camera."""
        with self.vision_lock:
            with self.lock:
                if self.closed:
                    return False
                generation = self.generation
            try:
                packet = self.frame_provider() if self.frame_provider is not None else self.device.get_frame()
                now, wall = self.clock(), self.wall()
                if (not isinstance(packet, FrameResult)
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
                if packet.stale or packet.frame is None or not -FRAME_TIME_EPSILON <= age <= 1.0+FRAME_TIME_EPSILON:
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
                acquired_at = now-max(0., age)
                # Leave scheduling room; this is a cooperative budget, not an
                # interrupt of an OpenCV call. Age is checked again on return.
                budget_ms = min(self.detection_budget_ms, max(.001, (1.0-age-.02)*1000))
                with self.lock:
                    if self.closed:
                        return False
                    if generation != self.generation:
                        if packet.frame_id > self.last_received_frame_id:
                            self.last_received_frame_id, self.last_received_timestamp = packet.frame_id, packet.timestamp
                        self._record("observation_discarded", reason="lifecycle_changed_before_detection",
                                     frame_id=packet.frame_id)
                        return False
                    previous = self.controller.observation
                    if previous is not None and acquired_at <= previous.acquired_at:
                        # Raw IDs AND raw wall capture stamps already advanced.
                        # A coarse/jittery local clock mapping can still reverse
                        # their monotonic values. Discard this sample, never
                        # invent a newer capture time or reset freshness. The
                        # control worker still expires the previous sample.
                        self.last_received_frame_id = packet.frame_id
                        self.last_received_timestamp = packet.timestamp
                        self._record("observation_discarded", reason="nonadvancing_clock_mapping",
                            frame_id=packet.frame_id, frame_timestamp=packet.timestamp,
                            acquired_at_monotonic=acquired_at,
                            previous_frame_id=previous.frame_id,
                            previous_acquired_at=previous.acquired_at)
                        return False
                    work_now = self.clock()
                    input_age = work_now-acquired_at
                    if not -FRAME_TIME_EPSILON <= input_age <= self.controller.max_age+FRAME_TIME_EPSILON:
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
                observation = self.tracker.detect(packet.frame, packet.frame_id,
                    acquired_at, source="team2-simulation", processing_budget_ms=budget_ms)
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
                    if not -FRAME_TIME_EPSILON <= completed_at-acquired_at <= self.controller.max_age+FRAME_TIME_EPSILON:
                        self._record("observation_rejected", reason="vision_result_expired", **self.last_vision)
                        self._fault("vision_result_expired", invalid_frame=True, state=State.STALE)
                        return False
                    self.last_frame_id, self.last_timestamp = packet.frame_id, packet.timestamp
                    if not self.controller.observe(observation):
                        previous = self.controller.observation
                        self._record("observation_rejected",
                            reason=self.controller.last_observation_rejection,
                            rejected_observation=repr(observation)[:1600],
                            previous_frame_id=previous.frame_id if previous else None,
                            previous_acquired_at=previous.acquired_at if previous else None,
                            frame_timestamp=packet.timestamp,
                            acquired_at_monotonic=acquired_at, **self.last_vision)
                        reason = (self.controller.last_stop_reason
                            if self.controller.last_stop_reason == "image_size_changed_requires_new_session"
                            else "invalid_observation")
                        self._fault(reason, invalid_frame=True)
                        return False
                    # The SDK waits for frame_ready before its first calibration.
                    # A timed-out identity scan supplies no calibratable target:
                    # keep waiting within the SDK's existing startup deadline.
                    # Once calibrated, preserve the normal loss/zero/recovery path.
                    self.frame_ready = not (
                        self.controller.reference_size is None
                        and not observation.found
                        and observation.reason in ("detection_budget_exceeded",
                                                   "ambiguous_scan_budget_exceeded"))
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
                                 identity_check=diagnostics.get("identity_check"),
                                 recognition_attempts=diagnostics.get("attempts", [])[:32],
                                 detected_markers=diagnostics.get("detected_markers", [])[:16],
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

    @staticmethod
    def _cancelled(cancelled):
        return bool(cancelled is not None and cancelled.is_set())

    def set_frame_provider(self, provider):
        self.frame_provider = provider

    def start(self, timeout=2., *, cancelled=None):
        if not self._start_gate.acquire(blocking=False):
            return CommandResult(False, "failed", 'follow_start_or_close_busy')
        try:
            if self._cancelled(cancelled):
                return CommandResult(False, "failed", 'program_cancelled')
            with self.lock:
                if self.closed:
                    return CommandResult(False, "failed", 'closed')
                if self._cancelled(cancelled):
                    return CommandResult(False, 'failed', 'program_cancelled')
                if self.grant is not None and self.status()['active']:
                    self._operation_source = cancelled
                    return self._start_tracking()
                if self.grant is not None:
                    cleaned = self.retry_cleanup()
                    if not cleaned.ok:
                        return CommandResult(False, "failed", 'previous_cleanup_unconfirmed')
                generation = self.generation
            self.start_background()
            if self.frame_provider is None and self.device.get_frame().frame is None:
                self._last_start_failure = 'follow_no_frame_source'
                return CommandResult(False, "failed", self._last_start_failure)
            deadline = time.monotonic() + max(0., float(timeout))
            reason = 'follow_no_fresh_frame'
            while True:
                with self.lock:
                    if self.closed or self.generation != generation:
                        return CommandResult(False, "failed", 'follow_start_cancelled')
                    if self._cancelled(cancelled):
                        return CommandResult(False, "failed", 'follow_start_cancelled')
                    observation = self.controller.observation
                    fresh = (observation is not None and self.frame_ready
                        and 0 <= self.clock() - observation.acquired_at <= self.controller.max_age)
                    if observation is not None and not observation.found:
                        reason = ('follow_recognition_budget_exceeded' if 'budget_exceeded' in observation.reason
                                  else 'follow_target_not_found')
                        if observation.reason == 'ambiguous_duplicate_id':
                            break
                    elif not fresh:
                        reason = 'follow_no_fresh_frame'
                    if fresh and observation.found:
                        calibrated = (self.calibrate_reference() if self.controller.reference_size is None else None)
                        if calibrated is None or calibrated.ok:
                            self._operation_source = cancelled
                            result = self._start_tracking()
                            if result.ok:
                                if self._cancelled(cancelled):
                                    self.stop()
                                    return CommandResult(False, "failed", 'program_cancelled')
                                self._last_start_failure = ''
                            return result
                        reason = calibrated.reason or 'follow_calibration_failed'
                if time.monotonic() >= deadline:
                    break
                time.sleep(.01)
            self._last_start_failure = reason
            return CommandResult(False, "failed", reason)
        finally:
            self._start_gate.release()

    def calibrate_reference(self):
        with self.lock:
            if self.closed:
                return CommandResult(False, "failed", "closed")
            if not self.frame_ready:
                return CommandResult(False, "failed", "new_valid_frame_required")
            ok, reason = self.controller.calibrate(self.clock())
            self._record("calibration", ok=ok, reason=reason,
                         reference_size=self.controller.reference_size)
            return CommandResult(ok, "completed" if ok else "failed", reason)

    def _start(self):
        if self.closed:
            return CommandResult(False, "failed", "closed")
        if not self.frame_ready:
            return CommandResult(False, "failed", "new_valid_frame_required")
        if self.grant is not None:
            return CommandResult(False, "failed", "lease_cleanup_required")
        try:
            status = self.device.get_status()
            if status.connected is not True or status.flight_state != "flying":
                return CommandResult(False, "failed", "connected_airborne_state_required")
            if status.control_owner != "none":
                return CommandResult(False, "failed", "busy")
            ok, reason = self.controller.start(self.clock())
            if not ok:
                return CommandResult(False, "failed", reason)
            grant = self.device.request_control("follow", exclusive=True)
            if not grant.ok:
                self.controller.stop()
                return CommandResult(False, "failed", grant.reason or "invalid_control_grant")
            self.grant = grant.grant
            self.cleanup_unconfirmed = False
            self.last_send = -math.inf
            self.last_regular_send = -math.inf
            self.last_channels = None
            self.last_reason = "started"
            self.last_halt_reason, self.last_halt_at = "", None
            self.control_once()
            if self.controller.state != State.TRACKING:
                return CommandResult(False, "failed", self.last_reason)
            self._record("start")
            return CommandResult(True, "completed", "follow_started")
        except Exception as exc:
            return self._fault("start_error: " + str(exc))

    def _start_tracking(self):
        with self.lock:
            self._cancel_ticket()
            if self.controller.state in (State.TRACKING, State.RECOVERING) and self.grant is not None:
                self.control_once()
                if self.controller.state in (State.TRACKING, State.RECOVERING):
                    return CommandResult(True, "completed", "already_following" if self.controller.state == State.TRACKING
                                  else "waiting_for_target")
                return CommandResult(False, "failed", self.last_reason)
            return self._start()

    def _stop_tracking(self, reason="explicit_stop"):
        with self.lock:
            self._cancel_ticket()
            self.controller.stop()
            self.last_reason = reason
            self.last_halt_reason, self.last_halt_at = reason, self.clock()
            self._record("stop", reason=reason)
            result = self._cleanup()
            self._publish_status()
            return result

    def stop(self, *, cancelled=None):
        """Explicit user stop ends the calibration session, keeping workers.

        Retain the last received source identity: a cached pre-stop frame may
        never become fresh calibration evidence for the next start.
        """
        with self.lock:
            if self._cancelled(cancelled):
                return CommandResult(False, "failed", "program_cancelled")
            result = self._stop_tracking()
            if not result.ok:
                return result
            self._operation_source = None
            previous = self.controller
            self.controller = FollowController(config=self.config,
                auto_recover=previous.auto_recover, require_geometry=True)
            self.frame_ready = False
            self.last_frame_id, self.last_timestamp = -1, None
            self.last_observation_published_at = None
            self.vision_in_progress = None
            self._publish_status()
            return result

    def pause_if_active(self, *, cancelled=None):
        """Atomically yield an active tracking/recovery grant; token stays local."""
        with self.lock:
            if self.closed or self._cancelled(cancelled):
                raise RuntimeError("closed" if self.closed else "program_cancelled")
            self.control_once()
            active = self.grant is not None and self.controller.state in (State.TRACKING, State.RECOVERING)
            if not active:
                if self.grant is not None or self.cleanup_unconfirmed:
                    self._operation_source = cancelled
                    cleaned = self._cleanup()
                    if not cleaned.ok:
                        raise RuntimeError(cleaned.reason)
                return None
            self._operation_source = cancelled
            self._cancel_ticket()
            token = _Handoff(self.last_frame_id)
            self.controller.pause()
            cleaned = self._cleanup()
            if not cleaned.ok:
                raise RuntimeError(cleaned.reason)
            self.ticket = token
            self.last_reason = "handoff_paused"
            self._record("handoff_pause", frame_id=token.frame_id)
            self._publish_status()
            return token

    def finish_action(self, ticket, success: bool, *, adjust_axis=None, not_before=None,
                      timeout=.5, cancelled=None):
        deadline = time.monotonic() + max(0., timeout)
        while success:
            with self.lock:
                if (ticket is None or ticket is not self.ticket or self.closed):
                    return CommandResult(False, 'failed', 'obsolete_ticket')
                if cancelled is not None and cancelled.is_set():
                    return self._fault('action_failed_or_cancelled')
                new_frame = self.last_frame_id > ticket.frame_id
                new_stamp = (adjust_axis is None or type(not_before) not in (int, float)
                             or self.last_timestamp is not None and self.last_timestamp > not_before)
                if new_frame and new_stamp or time.monotonic() >= deadline:
                    break
            # Vision keeps running while a model owns control. Waiting belongs
            # here so callers neither unpack tickets nor inspect frame counters.
            time.sleep(.01)
        with self.lock:
            if (ticket is None or ticket is not self.ticket or self.closed):
                return CommandResult(False, "failed", "obsolete_ticket")
            if cancelled is not None and cancelled.is_set():
                return self._fault('action_failed_or_cancelled')
            if success and adjust_axis is not None:
                if (adjust_axis not in ('horizontal', 'vertical', 'depth') or
                        type(not_before) not in (int, float) or not math.isfinite(not_before)):
                    self._cancel_ticket()
                    return CommandResult(False, 'failed', 'invalid_adjustment')
                if (self.last_timestamp is None or self.last_timestamp <= not_before
                        or self.last_frame_id <= ticket.frame_id):
                    return CommandResult(False, 'failed', 'awaiting_post_action_frame')
                device = self.device.get_status()
                if adjust_axis == 'vertical' and not self._height_fresh(device):
                    self._cancel_ticket()
                    return CommandResult(False, 'failed', 'height_unknown_or_stale')
                ok, reason = self.controller.rebase_axis(adjust_axis, self.clock())
                if not ok:
                    self._cancel_ticket()
                    return CommandResult(False, 'failed', reason)
                self._record('reference_adjusted', axis=adjust_axis,
                             target_x_fraction=self.controller.target_x_fraction,
                             target_y_fraction=self.controller.target_y_fraction,
                             reference_size=self.controller.reference_size,
                             post_action_frame_id=self.last_frame_id)
            self._cancel_ticket()
            if not success:
                return self._fault("action_failed_or_cancelled")
            if self.last_frame_id <= ticket.frame_id:
                return CommandResult(False, "failed", "resume_requires_prior_follow_and_new_frame")
            result = self._start()
            if not result.ok:
                self.last_reason = "handoff_resume_failed: " + result.reason
            self._record("handoff_finish", status=result.status, reason=result.reason)
            self._publish_status()
            return result

    def _guard_vertical(self, channels, status):
        """Block stale/unknown altitude and outward motion near height bounds.

        The 20 cm margin is a provisional command guard, not a braking-distance
        guarantee. No automatic climb/descent/landing is invented here.
        """
        self.vertical_guard_reason = "disabled" if not self.config.vertical_follow else "clear"
        if not self.config.vertical_follow:
            return channels
        height = status.height_cm
        fresh = self._height_fresh(status)
        up = channels[2]
        if not fresh:
            up, self.vertical_guard_reason = 0, "height_unknown_or_stale"
        elif up > 0 and height >= self.config.vertical_max_height_cm-20:
            up, self.vertical_guard_reason = 0, "upper_height_margin"
        elif up < 0 and height <= self.config.vertical_min_height_cm+20:
            up, self.vertical_guard_reason = 0, "lower_height_margin"
        return channels[0], channels[1], up, channels[3]

    def control_once(self):
        with self.lock:
            if self.closed:
                return
            try:
                if self.grant is not None:
                    status = self.device.get_status()
                    if not self._owns_control(status):
                        self.grant = None
                        # The old lease is no longer ours. Never retain a
                        # cleanup warning (or issue stop) for a new owner.
                        self.cleanup_unconfirmed = False
                        self._fault("control_lost")
                        return
                    if status.connected is not True or status.flight_state != "flying":
                        self._fault("device_unavailable_or_not_airborne")
                        return
                motion = self.controller.tick(self.clock())
                if self.controller.state not in (State.TRACKING, State.RECOVERING):
                    if self.grant is not None:
                        self.last_reason = motion.reason
                        if self.last_halt_reason != motion.reason:
                            self.last_halt_reason, self.last_halt_at = motion.reason, self.clock()
                        self._cancel_ticket()
                        self._record("tracking_halted", state=str(self.controller.state), reason=motion.reason)
                        self._cleanup(retry=True)
                    return
                if self.grant is None:
                    self._fault("missing_lease")
                    return
                # Preview conversion only; not a calibrated physical speed.
                channels = tuple(round(value) for value in motion.channels)
                channels = tuple(max(-cap, min(cap, value))
                                 for value, cap in zip(channels, self.rc_caps))
                channels = self._guard_vertical(channels, status)
                now = self.clock()
                urgent_zero = channels == (0, 0, 0, 0) and channels != self.last_channels
                urgent_depth_stop = (self.last_channels is not None
                                     and self.last_channels[1] != 0 and channels[1] == 0)
                reductions = None if self.last_channels is None else tuple(
                    new if (old and new == 0) or (index == 1 and old*new > 0
                           and abs(new) < abs(old)) else old
                    for index,(old,new) in enumerate(zip(self.last_channels,channels)))
                regular_due = now-self.last_regular_send >= .1-1e-9
                if not regular_due and not urgent_zero:
                    if reductions is None or reductions == self.last_channels:
                        return
                    # Safety braking may bypass the 10 Hz deadline. No other
                    # axis may increase or reverse during this early send.
                    channels = reductions
                result = self.device.update_velocity(self.grant, *channels)
                if not result.ok:
                    self._fault("velocity_rejected: " + result.reason)
                    return
                self.last_send, self.last_channels = now, channels
                if regular_due:
                    self.last_regular_send = now
                snapshot = self._snapshot()
                self._record("motion_request", channels=channels, status=result.status,
                             state=str(self.controller.state), reason=motion.reason,
                             reference_size=self.controller.reference_size,
                             observation_frame_id=self.controller.observation.frame_id,
                             observation_age_ms=(now-self.controller.observation.acquired_at)*1000,
                             openpose_control=snapshot["algorithm"]["openpose_control"],
                             depth_brake=snapshot["algorithm"]["depth_brake"],
                             distance_guard=snapshot["distance_guard"],
                             distance_measurement=snapshot["distance_measurement"],
                             vertical_guard_reason=self.vertical_guard_reason,
                             height_cm=status.height_cm,
                             height_fresh=self._height_fresh(status),
                             requested_up_rc=round(motion.up),
                             reference_size_limit=self.config.reference_size_limit,
                             urgent_depth_stop=urgent_depth_stop)
            except Exception as exc:
                self._fault("control_error: " + str(exc))
            finally:
                self._publish_status()

    def _snapshot(self):
        """Read under lock, without I/O or recursive control calls."""
        now = self.clock()
        obs = self.controller.observation
        age = None if obs is None else now-obs.acquired_at
        fresh = self.frame_ready and age is not None and 0 <= age <= self.controller.max_age
        visible = bool(fresh and obs.found)
        active = (self.grant is not None and not self.closed
                  and self.controller.state in (State.TRACKING, State.RECOVERING))
        pending_cleanup = self.cleanup_unconfirmed or (self.grant is not None and not active)
        reason = self.last_halt_reason or self.last_reason
        state = str(self.controller.state)
        restart = (not active and not self.closed and state != "paused"
                   and bool(self.last_halt_reason))
        raw_age = None if self.last_received_acquired_at is None else now-self.last_received_acquired_at
        # During work, the input's actual monotonic acquired_at is authoritative.
        if self.vision_in_progress is not None:
            raw_age = now-self.vision_in_progress["acquired_at"]
        budget_wait = bool(obs is not None and obs.reason == "detection_budget_exceeded")
        message = describe_status(state=state, active=active, visible=visible,
            closed=self.closed, pending_resume=self.ticket is not None,
            cleanup_pending=pending_cleanup, reason=reason, budget_wait=budget_wait,
            identity_wait=bool(obs is not None and obs.reason == "ambiguous_scan_budget_exceeded"))
        distance = self.controller.distance_status()
        if active and visible and not distance['forward_allowed']:
            message += "；前后暂停：距离信息不可靠（" + distance['reason'] + "）"
        brake = self.controller._depth_brake.status()
        if active and self.controller.last_request.reason == 'at_or_inside_reference_no_forward':
            message += '；已达到跟随距离，阻止预测补偿继续前进'
        if active and self.config.vertical_follow:
            extra = {"height_unknown_or_stale": "高度数据未知或过期，升降暂停",
                     "upper_height_margin": "接近高度上限，上升暂停",
                     "lower_height_margin": "接近高度下限，下降暂停"}.get(self.vertical_guard_reason)
            if extra:
                message += "；" + extra
        algorithm = self.controller.status()
        return dict(observation=asdict(obs) if obs is not None else None,
            state=state, following=bool(active and visible and state == "tracking"),
            active=active, recovering=state == "recovering_target", closed=self.closed,
            pending_resume=self.ticket is not None, cleanup_pending=pending_cleanup,
            cleanup_unconfirmed=self.cleanup_unconfirmed, cleanup_confirmed=not pending_cleanup,
            reference_size=self.controller.reference_size, last_reason=self.last_reason,
            frame_ready=self.frame_ready, frame_id=self.last_frame_id,
            recognition_running=any(t.name == "follow-vision" and t.is_alive() for t in self.workers),
            background_running=any(t.is_alive() for t in self.workers),
            instance_id=self.config.instance_id, target_id=self.config.target_id,
            service_session_id=self.service_session_id, simulated=self.simulated,
            last_start_failure=self._last_start_failure,
            algorithm=algorithm, distance_guard=algorithm["distance_guard"],
            log=self.session_log.status() if self.session_log else None,
            target_visible=visible, observation_fresh=bool(fresh),
            observation_age_ms=None if age is None else age*1000,
            raw_frame_age_ms=None if raw_age is None else raw_age*1000,
            detection_in_progress=self.vision_in_progress is not None,
            detection_elapsed_ms=(None if self.vision_in_progress is None else
                                   max(0., (now-self.vision_in_progress["started_at"])*1000)),
            last_vision=dict(self.last_vision),
            restart_required=restart,
            last_halt_reason=self.last_halt_reason, last_halt_at=self.last_halt_at,
            requested_channels=self.last_channels if active and self.last_channels is not None else (0,0,0,0),
            physical_hover_confirmed=False, status_message=message,
            vertical_guard_reason=self.vertical_guard_reason,
            reference_size_limit=self.config.reference_size_limit,
            distance_measurement=distance,
            depth_brake=brake,
            status_notice_seq=self.notice_seq)

    def _publish_status(self):
        presentation = self._snapshot()
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
            self.notifier.submit(f"[跟随 {self.config.instance_id}/ID{self.config.target_id} "
                                 f"状态#{self.notice_seq} t={self.clock():.3f}s] "
                                 + presentation["status_message"])

    def stop_cancelled_operation(self, operation):
        with self.lock:
            if operation is None or not operation.is_set() or self._operation_source is not operation:
                return CommandResult(True, "completed")
            return self.stop()

    def events(self):
        with self.lock:
            return list(self._events)

    def is_following(self):
        return self.status()["following"]

    def status(self):
        """One dynamic snapshot; fixed tuning is available separately as config."""
        with self.lock:
            return self._snapshot()

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
            result = self._stop_tracking("closed")
            self.closed = True
            self.shutdown.set()
            self._publish_status()
        if wait:
            for worker in self.workers:
                if worker is not threading.current_thread():
                    worker.join(timeout=1.)
        if self.session_log is not None:
            self.session_log.close(wait=wait)
        if self.notifier is not None:
            self.notifier.close(wait=wait)
        # Diagnostics may finish draining after control has stopped. They do
        # not retain aircraft authority and must not prevent device cleanup.
        pending = [worker.name for worker in self.workers if worker.is_alive()]
        if result.ok and pending:
            return CommandResult(False, 'failed', 'follow_workers_still_running: ' + ', '.join(pending))
        if result.ok:
            with self.lock:
                self._operation_source = None
        return result
