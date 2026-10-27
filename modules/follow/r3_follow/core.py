"""No device/network calls. Requests are dimensionless simulation values."""
from dataclasses import dataclass
from enum import StrEnum
import math

from .openpose_control import OpenPoseMarkerControl, PROFILE as OPENPOSE_PROFILE
from .depth_brake import DepthBrake

_DEFAULT_STEERING = object()
# Unix double timestamps around 2026 have ~0.24 microsecond spacing. Mapping
# them to monotonic time can put an exact 0.5-second boundary a few ulps over.
# One microsecond is numeric tolerance only, not a longer image lifetime.
FRAME_TIME_EPSILON = 1e-6

# Fresh images with no trustworthy observation may wait at zero. Invalid IDs,
# ambiguous duplicates and stale images are deliberately not recoverable here.
# An unfinished identity scan supplies NO position, but does not prove that
# two copies exist. Retry within the same bounded zero-command recovery window.
# Confirmed ambiguous_duplicate_id deliberately remains a latched stop.
RECOVERABLE_MISS_REASONS = ("", "not_found", "detection_budget_exceeded",
                          "ambiguous_scan_budget_exceeded")


class State(StrEnum):
    STOPPED = "stopped"
    PAUSED = "paused"
    TRACKING = "tracking"
    RECOVERING = "recovering_target"
    LOST = "target_lost"
    STALE = "frame_stale"
    INVALID = "invalid_input"


@dataclass(frozen=True)
class Observation:
    frame_id: int
    acquired_at: float  # frame acquisition in local monotonic time, never detection completion
    source: str
    width: int
    height: int
    target_id: int
    found: bool
    center_x: float | None = None  # pixels, origin top left
    center_y: float | None = None
    size_ratio: float | None = None  # mean side length / image width
    corners: tuple = ()
    reason: str = ""


@dataclass(frozen=True)
class MotionRequest:
    state: str
    right: float = 0.0
    forward: float = 0.0
    reason: str = ""
    simulation_only: bool = True
    yaw: float = 0.0  # appended for positional compatibility; negative left, positive right
    up: float = 0.0  # appended for positional compatibility; positive = aircraft up


def marker_shape(corners):
    """Image geometry only: no camera calibration, physical angle or distance.

    Ratios and adjacent-edge cosines are invariant to image rotation and uniform
    scaling. They conservatively reject visibly foreshortened/sheared quads;
    a passing quad is NOT proof of front-facing pose or accurate depth.
    """
    try:
        if len(corners) != 4 or any(len(p) != 2 for p in corners):
            return None
        pts = [(float(x), float(y)) for x, y in corners]
        if not all(math.isfinite(v) for p in pts for v in p):
            return None
        edges = [(pts[(i+1) % 4][0]-pts[i][0],
                  pts[(i+1) % 4][1]-pts[i][1]) for i in range(4)]
        lengths = [math.hypot(*e) for e in edges]
        if min(lengths) < 1:
            return None
        crosses = [edges[i][0]*edges[(i+1) % 4][1]
                   - edges[i][1]*edges[(i+1) % 4][0] for i in range(4)]
        if not (all(c > 0 for c in crosses) or all(c < 0 for c in crosses)):
            return None
        a, b = (lengths[0]+lengths[2])/2, (lengths[1]+lengths[3])/2
        return {"axis_ratio": min(a, b)/max(a, b),
                "opposite_ratio": min(min(lengths[i], lengths[i+2]) /
                                      max(lengths[i], lengths[i+2]) for i in (0, 1)),
                "max_corner_cosine": max(abs(sum(edges[i][j]*edges[(i+1) % 4][j]
                                                   for j in (0, 1))) /
                                         (lengths[i]*lengths[(i+1) % 4]) for i in range(4))}
    except (TypeError, ValueError, OverflowError):
        return None


class ApparentSizeGuard:
    """Suppress both forward/back requests when apparent size is ambiguous.

    Strict geometry is required by camera GUI and background service. Scalar
    mathematical simulations may omit corners; once geometry is used it cannot
    silently fall back to scalar-only input. Thresholds are conservative image
    heuristics, not calibrated angle limits.
    """
    def __init__(self, required=False, confirmation_max_gap=.2):
        self.enabled = required
        self.confirmation_max_gap = confirmation_max_gap
        self.confirmation_min_s = .2
        self.shape = None
        self.blocked = required
        self.reason = "geometry_missing" if required else "scalar_simulation_only"
        self.good_since = self.last_good = None
        self.good_count = 0

    @staticmethod
    def suitable(shape, strict=False):
        return (shape is not None and shape["axis_ratio"] >= (.90 if strict else .85)
                and shape["opposite_ratio"] >= (.90 if strict else .85)
                and shape["max_corner_cosine"] <= (.12 if strict else .20))

    def block(self, reason):
        if self.enabled:
            self.blocked, self.reason = True, reason
            self.good_since = self.last_good = None
            self.good_count = 0

    def observe(self, obs):
        # Called only for accepted NEW observations; ticks/duplicates do not
        # count as repeated confirmation.
        try:
            has_corners = len(obs.corners) > 0
        except TypeError:
            has_corners = obs.corners is not None
        if has_corners:
            if not self.enabled:
                self.enabled = True
                self.block("geometry_confirmation_pending")
        if not self.enabled:
            return
        self.shape = marker_shape(obs.corners) if obs.found else None
        if not obs.found:
            self.block("target_unavailable")
        elif self.shape is None:
            self.block("geometry_missing_or_invalid")
        elif not self.suitable(self.shape):
            self.block("shape_ambiguous")
        elif self.blocked:
            if not self.suitable(self.shape, strict=True):
                self.block("waiting_for_front_facing_target")
                return
            if self.last_good is None or obs.acquired_at-self.last_good > self.confirmation_max_gap + FRAME_TIME_EPSILON:
                self.good_since, self.good_count = obs.acquired_at, 0
            self.last_good = obs.acquired_at
            self.good_count += 1
            self.reason = "waiting_for_stable_shape"
            if self.good_count >= 3 and obs.acquired_at-self.good_since >= self.confirmation_min_s - FRAME_TIME_EPSILON:
                self.blocked, self.reason = False, "clear"

    def calibrate(self, obs):
        # The controller has already accepted this observation. A button press
        # must not count the same frame twice toward automatic re-entry.
        if self.enabled and not self.suitable(self.shape, strict=True):
            return False
        if self.enabled:
            self.blocked, self.reason = False, "clear"
            self.good_since = self.last_good = None
            self.good_count = 0
        return True

    def status(self):
        return {"profile": "apparent_size_shape_guard_v1", "enabled": self.enabled,
                "forward_allowed": not self.blocked, "reason": self.reason,
                "shape": self.shape}


class FollowController:
    def __init__(self, target_id=0, max_age=0.5, recovery_window=0.0,
                 max_request_rate=None, require_geometry=False,
                 steering_mode=_DEFAULT_STEERING, yaw_limit=0.20,
                 forward_limit=0.30,
                 alignment_stop=0.30, alignment_resume=0.18,
                 control_profile="legacy", vertical_follow=False,
                 distance_mode="apparent_pixels", pose_estimator=None, pid_tuning="marker_v2",
                 reference_size_limit=None):
        if reference_size_limit is not None and (
                type(reference_size_limit) not in (int, float) or not math.isfinite(reference_size_limit)
                or not .05 <= reference_size_limit <= .30 or distance_mode != 'relative_size'):
            raise ValueError('invalid relative reference_size_limit')
        self.reference_size_limit = reference_size_limit
        if max_age <= 0 or not math.isfinite(max_age):
            raise ValueError("max_age must be finite and positive")
        self.target_id = target_id
        self.max_age = max_age
        if not math.isfinite(recovery_window) or recovery_window < 0:
            raise ValueError("recovery_window must be finite and nonnegative")
        self.recovery_window = recovery_window
        if max_request_rate is not None and (
                not isinstance(max_request_rate, (int, float))
                or isinstance(max_request_rate, bool)
                or not math.isfinite(max_request_rate) or max_request_rate <= 0):
            raise ValueError("max_request_rate must be None or finite and positive")
        self.max_request_rate = max_request_rate
        if control_profile not in ("legacy", "openpose_pid"):
            raise ValueError("control_profile must be legacy or openpose_pid")
        if pid_tuning not in ("marker_v2", "responsive_v1"):
            raise ValueError("unsupported pid_tuning")
        if pid_tuning != "marker_v2" and control_profile != "openpose_pid":
            raise ValueError("responsive tuning requires openpose_pid")
        if distance_mode not in ("apparent_pixels", "relative_size", "pose"):
            raise ValueError("unsupported distance_mode")
        if distance_mode != "apparent_pixels" and control_profile != "openpose_pid":
            raise ValueError("relative/pose distance requires openpose_pid")
        if distance_mode == "pose" and pose_estimator is None:
            raise ValueError("pose mode requires calibrated pose_estimator")
        self.distance_mode, self.pose_estimator = distance_mode, pose_estimator
        self.pose = None
        self.reference_depth_m = None
        self._image_size = None
        if type(vertical_follow) is not bool:
            raise ValueError("vertical_follow must be a boolean")
        if vertical_follow and control_profile != "openpose_pid":
            raise ValueError("vertical_follow requires openpose_pid")
        # Preserve the bare legacy controller's old lateral default. Selecting
        # the open-source profile alone must still use its original yaw routing;
        # lateral/hybrid require explicit selection. Explicit None is invalid.
        if steering_mode is _DEFAULT_STEERING:
            steering_mode = "yaw" if control_profile == "openpose_pid" else "lateral"
        if steering_mode not in ("lateral", "yaw", "hybrid", "adaptive"):
            raise ValueError("steering_mode must be lateral, yaw, hybrid or adaptive")
        if steering_mode in ("hybrid", "adaptive") and control_profile != "openpose_pid":
            raise ValueError("combined steering requires openpose_pid")
        for name, value in (("yaw_limit", yaw_limit),
                            ("forward_limit", forward_limit),
                            ("alignment_stop", alignment_stop),
                            ("alignment_resume", alignment_resume)):
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or not 0 < value <= 1):
                raise ValueError(f"{name} must be finite and in (0, 1]")
        if alignment_resume >= alignment_stop:
            raise ValueError("alignment_resume must be smaller than alignment_stop")
        self._steering_mode = steering_mode
        self.control_profile = control_profile
        self._openpose = (OpenPoseMarkerControl(yaw_limit=yaw_limit,
                                               forward_limit=forward_limit,
                                               max_dt=max_age,
                                               tuning=pid_tuning,
                                               steering_mode=steering_mode,
                                               vertical_follow=vertical_follow)
                          if control_profile == "openpose_pid" else None)
        # Dimensionless OFFLINE candidate, not an experimentally measured yaw
        # speed or a claim that this gain/limit is safe for physical flight.
        self.yaw_limit = yaw_limit
        self.forward_limit = forward_limit
        self.alignment_stop = alignment_stop
        self.alignment_resume = alignment_resume
        self._alignment_blocked = steering_mode == "yaw" and self._openpose is None
        self._depth_brake = DepthBrake(forward_limit, proportional_scale=(
            self._openpose.pitch_gains[0]*128/100 if self._openpose else .64),
            braking_allowance=.40 if pid_tuning == 'responsive_v1' else .25)
        self._motion_at = None
        self._motion_values = (0.0, 0.0, 0.0, 0.0)
        self._loss_at = None
        self._last_found = None
        self._recovery_anchor = None
        self._quick_recovery = False
        self.fast_reacquire = pid_tuning == 'responsive_v1'
        self._good_since = None
        self._good_count = 0
        self.state = State.STOPPED
        self.reference_size = None
        self.target_x_fraction = self.target_y_fraction = .5
        self.observation = None
        self._last_frame_id = -1
        self._source = None
        self._invalid = False
        self.last_request = MotionRequest(self.state)
        self.last_stop_reason = ""
        self.last_observation_rejection = ""
        self.confirmation_max_gap = max_age if distance_mode != "apparent_pixels" else .2
        self.distance_guard = ApparentSizeGuard(required=require_geometry,
                                               confirmation_max_gap=self.confirmation_max_gap)

    @property
    def steering_mode(self):
        """Read-only for this instance; switching requires a newly built controller."""
        return self._steering_mode

    def _valid(self, obs):
        if not isinstance(obs, Observation):
            return False
        if (type(obs.frame_id) is not int or obs.frame_id < 0
                or not isinstance(obs.source, str) or not obs.source
                or type(obs.found) is not bool
                or type(obs.width) is not int or type(obs.height) is not int
                or obs.width <= 0 or obs.height <= 0
                or type(obs.acquired_at) not in (int, float)
                or not math.isfinite(obs.acquired_at)):
            return False
        if type(obs.target_id) is not int or obs.target_id != self.target_id:
            return False
        if obs.found:
            values = (obs.center_x, obs.center_y, obs.size_ratio)
            if any(type(x) not in (int, float) or not math.isfinite(x) for x in values):
                return False
            if not (0 <= obs.center_x < obs.width and 0 <= obs.center_y < obs.height
                    and 0 < obs.size_ratio <= 2):
                return False
        return True

    def observe(self, obs):
        """Only newer frames from the same source may replace current data."""
        self.last_observation_rejection = ""
        if not self._valid(obs):
            self.last_observation_rejection = "invalid_fields_or_target"
            self._depth_brake.reset("invalid_input")
            self._invalid = True
            self.distance_guard.block("invalid_input")
            if self._openpose:
                self._openpose.reset()
            return False
        if self._source is not None and obs.source != self._source:
            self.last_observation_rejection = "source_changed"
            self._invalid = True
            return False  # source switch requires a new controller/calibration
        if self._image_size is not None and self._image_size != (obs.width, obs.height):
            self.last_observation_rejection = "image_size_changed"
            self._invalid = True
            self.last_stop_reason = "image_size_changed_requires_new_session"
            return False
        if obs.frame_id <= self._last_frame_id:
            self.last_observation_rejection = "frame_id_not_advancing"
            return False  # duplicates never refresh the stored timestamp
        if self.observation is not None and obs.acquired_at <= self.observation.acquired_at:
            self.last_observation_rejection = "capture_time_not_advancing"
            self._invalid = True
            self.distance_guard.block("nonadvancing_frame_time")
            if self._openpose:
                self._openpose.reset()
            return False
        if (self.observation is not None
                and self.state in (State.TRACKING, State.RECOVERING)
                and obs.acquired_at-self.observation.acquired_at > self.max_age + FRAME_TIME_EPSILON):
            self.state = State.STALE
            self.last_stop_reason = "frame_gap"
            self._clear_recovery()
            self.distance_guard.block("frame_gap")
        if self.state == State.RECOVERING:
            if not obs.found and obs.reason not in RECOVERABLE_MISS_REASONS:
                self.state = State.LOST
                self.last_stop_reason = obs.reason
                self._clear_recovery()
            elif obs.found:
                previous = self.observation
                if (self._good_since is None or previous is None or not previous.found
                        or obs.acquired_at-previous.acquired_at > self.confirmation_max_gap + FRAME_TIME_EPSILON):
                    self._good_since, self._good_count = obs.acquired_at, 1
                else:
                    self._good_count += 1
                anchor = self._recovery_anchor
                candidate_fast = bool(self.fast_reacquire and anchor is not None
                    and self._good_since-anchor.acquired_at <= .15
                    and abs(obs.center_x/obs.width-anchor.center_x/anchor.width) <= .08
                    and abs(obs.center_y/obs.height-anchor.center_y/anchor.height) <= .08
                    and abs(obs.size_ratio/anchor.size_ratio-1) <= .12
                    and ApparentSizeGuard.suitable(marker_shape(obs.corners), strict=True))
                self._quick_recovery = candidate_fast and (self._good_count == 1 or self._quick_recovery)
            else:
                self._good_since, self._good_count = None, 0
                self._quick_recovery = False
        self._source = obs.source
        self._image_size = (obs.width, obs.height)
        self._last_frame_id = obs.frame_id
        self.observation = obs
        if obs.found:
            self._last_found = obs
        self._invalid = False
        if self._openpose and not obs.found:
            self._openpose.reset()
            self._depth_brake.reset("target_unavailable")
        self.distance_guard.confirmation_min_s = .05 if self._quick_recovery else .2
        self.distance_guard.observe(obs)
        if self.pose_estimator is not None:
            self.pose = self.pose_estimator.estimate(obs)
        return True

    def _clear_recovery(self):
        self._loss_at = self._good_since = None
        self._good_count = 0
        self._recovery_anchor = None
        self._quick_recovery = False

    def _issue(self, now):
        if self._invalid or not isinstance(now, (int, float)) or not math.isfinite(now):
            return State.INVALID
        obs = self.observation
        if obs is None or not -FRAME_TIME_EPSILON <= now - obs.acquired_at <= self.max_age + FRAME_TIME_EPSILON:
            return State.STALE
        if not obs.found:
            return State.LOST
        return None

    def _zero(self, reason):
        self._depth_brake.reset(reason)
        self._motion_at = None
        self._motion_values = (0.0, 0.0, 0.0, 0.0)
        self._alignment_blocked = self.steering_mode == "yaw" and self._openpose is None
        if self._openpose:
            self._openpose.reset()
        if self.state not in (State.TRACKING, State.RECOVERING) and reason not in (
                "inactive", "reference_updated", "not_calibrated"):
            self.last_stop_reason = reason
        self.last_request = MotionRequest(self.state, reason=reason)
        return self.last_request

    def calibrate(self, now):
        """Use the current apparent size as a demonstration reference, not metres."""
        if self.state in (State.TRACKING, State.RECOVERING):
            return False, "Pause or stop before calibrating"
        if self._issue(now):
            return False, "A fresh valid target is required"
        if self.distance_mode == "pose":
            if self.pose is None or not self.pose.valid:
                return False, "Reliable calibrated pose required for reference"
            self.reference_depth_m = self.pose.depth_m
        elif not self.distance_guard.calibrate(self.observation):
            return False, "Front-facing marker geometry required for reference"
        self.reference_size = self.observation.size_ratio
        # A close startup/rebase must not teach the controller to chase to that
        # same close point. This is an image-size cap, NOT calibrated metres.
        if self.reference_size_limit is not None:
            self.reference_size = min(self.reference_size, self.reference_size_limit)
        self._zero("reference_updated")
        return True, "Reference size saved"

    def rebase_axis(self, axis, now):
        """Preserve only the manually adjusted axis using a fresh observation."""
        if self.state != State.PAUSED or self._issue(now):
            return False, 'fresh_paused_target_required'
        if axis == 'depth':
            return self.calibrate(now)
        if axis not in ('horizontal', 'vertical'):
            return False, 'invalid_adjustment_axis'
        if axis == 'vertical' and (self._openpose is None or not self._openpose.vertical_follow):
            return False, 'vertical_follow_disabled'
        obs = self.observation
        value = obs.center_x/obs.width if axis == 'horizontal' else obs.center_y/obs.height
        if not .15 <= value <= .85:
            return False, 'target_too_close_to_image_edge'
        if axis == 'horizontal':
            self.target_x_fraction = value
        else:
            self.target_y_fraction = value
        self._zero('reference_updated')
        return True, 'adjustment_reference_saved'

    def start(self, now):
        self._clear_recovery()
        issue = self._issue(now)
        if issue:
            self.state = issue
            self._zero(str(issue))
            return False, "Fresh valid target required"
        if self.reference_size is None:
            self._zero("not_calibrated")
            return False, "Calibrate reference size first"
        self.state = State.TRACKING
        self.last_stop_reason = ""
        self._motion_at = None
        self._motion_values = (0.0, 0.0, 0.0, 0.0)
        if self._openpose:
            self._openpose.reset()
        self._depth_brake.reset("start")
        self.tick(now)
        return True, "Simulation started"

    def pause(self):
        self._clear_recovery()
        self.distance_guard.block("paused")
        if self.state in (State.TRACKING, State.RECOVERING):
            self.state = State.PAUSED
        return self._zero("paused_or_inactive")

    def resume(self, now):
        if self.state != State.PAUSED:
            return False, "Resume requires paused state; use Start after loss/stop"
        return self.start(now)

    def stop(self):
        self._clear_recovery()
        self.distance_guard.block("stopped")
        self.state = State.STOPPED
        return self._zero("stopped")

    def status(self):
        return {"state": str(self.state), "target_id": self.target_id,
                "reference_size": self.reference_size, "reference_size_limit": self.reference_size_limit,
                "request": self.last_request,
                "last_stop_reason": self.last_stop_reason,
                "control_profile": (OPENPOSE_PROFILE if self._openpose else
                                    "visible_target_yaw_v1" if self.steering_mode == "yaw"
                                    else "local_deadzone_transition_v1"),
                "openpose_control": self._openpose.status() if self._openpose else None,
                "depth_brake": self._depth_brake.status(),
                "vertical_follow": bool(self._openpose and self._openpose.vertical_follow),
                "distance_measurement": self.distance_status(),
                "distance_guard": self.distance_guard.status(),
                "steering": {"mode": self.steering_mode, "yaw_limit": self.yaw_limit,
                             "forward_limit": self.forward_limit,
                             "horizontal_limit": self.yaw_limit if self._openpose else None,
                             "constructor_only": True,
                             "alignment_stop": self.alignment_stop if not self._openpose else None,
                             "alignment_resume": self.alignment_resume if not self._openpose else None,
                             "alignment_blocked": self._alignment_blocked,
                             "physical_tuning_verified": False},
                "max_request_rate": self.max_request_rate}

    def distance_status(self):
        pose_valid = bool(self.pose is not None and self.pose.valid)
        return {"mode": self.distance_mode,
                "forward_allowed": pose_valid if self.distance_mode == "pose" else not self.distance_guard.blocked,
                "reason": (self.pose.reason if self.pose else "pose_unavailable") if self.distance_mode == "pose" else self.distance_guard.reason,
                "reference_depth_m": self.reference_depth_m,
                "depth_m": self.pose.depth_m if pose_valid else None,
                "pose": self.pose.status() if self.pose else None,
                "physical_accuracy_verified": False}

    def tick(self, now):
        """Call periodically even when video produces no frames."""
        if self.state not in (State.TRACKING, State.RECOVERING):
            return self._zero(self.last_stop_reason or "inactive")
        issue = self._issue(now)
        if self.state == State.RECOVERING:
            if issue in (State.STALE, State.INVALID):
                self.state = issue
                self._clear_recovery()
                self.distance_guard.block(str(issue))
                return self._zero(str(issue))
            if now-self._loss_at >= self.recovery_window:
                self.state = State.LOST
                self._clear_recovery()
                return self._zero("recovery_timeout_requires_start")
            confirmation = .05 if self._quick_recovery else .15
            if (issue is None and self._good_count >= 3 and self._good_since is not None
                    and self.observation.acquired_at-self._good_since >= confirmation-FRAME_TIME_EPSILON):
                self.state = State.TRACKING
                self._clear_recovery()
            else:
                return self._zero("waiting_for_stable_target")
        elif (issue == State.LOST and self.recovery_window > 0
              and self.observation.reason in RECOVERABLE_MISS_REASONS):
            self.state = State.RECOVERING
            self._loss_at = now
            self._recovery_anchor = self._last_found
            self._quick_recovery = False
            self._good_since, self._good_count = None, 0
            return self._zero("brief_loss_zero_request")
        if issue:
            self.state = issue  # latched; a fresh detection does not auto-start
            self._clear_recovery()
            self.distance_guard.block(str(issue))
            reason = (self.observation.reason if issue == State.LOST and self.observation
                      and self.observation.reason else str(issue))
            return self._zero(reason)
        obs = self.observation
        x_error = 2 * (obs.center_x / obs.width - self.target_x_fraction)
        scale_error = (self.reference_size - obs.size_ratio) / self.reference_size

        def adjustment(error, limit=0.3):
            # Pixel normalization can place equal +/- boundary values a few
            # floating-point ulps apart. Keep the deadzone symmetric.
            if abs(error) <= 0.12 + 1e-12:
                return 0.0
            # Smooth only |error| in (0.12, 0.16), where the old deadzone
            # jumped immediately to 0.072. Outside that narrow transition,
            # retain the original 0.6 gain and 0.3 cap. No temporal filtering
            # or stored target positions; loss and stale frames still stop.
            weight = min(1.0, (abs(error) - 0.12) / 0.04)
            magnitude = min(limit, 0.6 * abs(error)) * weight
            return math.copysign(magnitude, error)

        # Legacy yaw/lateral remain separate. The open-source adapter can route
        # one horizontal PID value to one axis or explicitly split it, without
        # adding a second full-strength correction. None is a target search.
        if self._openpose:
            # Reused open-source equations calculate both axes in the same
            # frame. The old image-offset gate is specific to the legacy law.
            # The existing shape guard, rate limiter and lifecycle still apply.
            depth_status = self.distance_status()
            depth_error = None
            if self.distance_mode == "relative_size":
                depth_error = self.reference_size/obs.size_ratio - 1
            elif self.distance_mode == "pose" and depth_status["forward_allowed"]:
                depth_error = self.pose.depth_m/self.reference_depth_m - 1
            desired = self._openpose.update(
                frame_id=obs.frame_id, acquired_at=obs.acquired_at,
                center_x=obs.center_x, image_width=obs.width,
                size_ratio=obs.size_ratio, reference_size=self.reference_size,
                forward_allowed=depth_status["forward_allowed"],
                center_y=obs.center_y, image_height=obs.height, depth_error=depth_error,
                target_x_fraction=self.target_x_fraction, target_y_fraction=self.target_y_fraction)
            desired += (self._openpose.vertical_request,)
            if depth_status["forward_allowed"] and depth_error is not None:
                forward = self._depth_brake.limit(desired[1],frame_id=obs.frame_id,
                    captured_at=obs.acquired_at,now=now,error=depth_error)
                desired = (desired[0],forward,desired[2],desired[3])
            else:
                self._depth_brake.reset("geometry_blocked_or_legacy_distance")
            self._alignment_blocked = False
        elif self.steering_mode == "yaw":
            if abs(x_error) >= self.alignment_stop - 1e-12:
                self._alignment_blocked = True
            elif abs(x_error) <= self.alignment_resume + 1e-12:
                self._alignment_blocked = False
            right, yaw = 0.0, adjustment(x_error, self.yaw_limit)
        else:
            right, yaw = adjustment(x_error, self.forward_limit), 0.0
        # Ambiguous size or a poorly aligned target must clear OLD forward/back
        # requests before the ramp, including backward requests.
        if not self._openpose:
            forward_blocked = self.distance_guard.blocked or self._alignment_blocked
            desired = (right, 0.0 if forward_blocked
                       else adjustment(scale_error, self.forward_limit), yaw, 0.0)
        # Trend lead and filtered PID history cannot request forward motion
        # when the latest valid measurement is already at/inside the setpoint.
        # Keep normal far-target chase and early approach braking unchanged.
        at_or_inside = (depth_error is not None and depth_error <= 0
                        if self._openpose and self.distance_mode == 'pose'
                        else obs.size_ratio >= self.reference_size)
        near_forward_blocked = at_or_inside and desired[1] > 0
        if near_forward_blocked:
            desired = (desired[0], 0., desired[2], desired[3])
        if (self._openpose and self.distance_mode != 'apparent_pixels'
                and self.last_request.forward*desired[1] < 0):
            # Zero-crossing is required even when acceleration smoothing is
            # explicitly disabled (max_request_rate=None).
            desired = (desired[0],0.,desired[2],desired[3])
        if self.max_request_rate is not None:
            elapsed = 0 if self._motion_at is None else max(0, min(.1, now-self._motion_at))
            step = self.max_request_rate * elapsed
            def ramp(previous, target, index):
                # No smoothing may prolong a stop or carry motion the wrong way.
                if target == 0 or previous*target < 0:
                    return 0.0
                # Forward/back deceleration is immediate; acceleration and
                # other axes keep their existing rate limits and caps.
                if self._openpose and index == 1 and abs(target) < abs(previous):
                    return target
                return previous + max(-step, min(step, target-previous))
            desired = tuple(ramp(old,new,index) for index,(old,new) in enumerate(zip(self._motion_values,desired)))
            self._motion_values, self._motion_at = desired, now
        if near_forward_blocked:
            reason = 'at_or_inside_reference_no_forward'
        elif not self.distance_status()["forward_allowed"]:
            reason = "distance_guard: " + self.distance_status()["reason"]
        elif self._alignment_blocked:
            reason = "turn_to_align_before_depth"
        elif self._depth_brake.reason in ("approach_braking","small_reversal_zero"):
            reason = self._depth_brake.reason
        else:
            reason = "current_observation"
        self.last_request = MotionRequest(self.state, right=desired[0], forward=desired[1],
                                          reason=reason, yaw=desired[2], up=desired[3])
        return self.last_request
