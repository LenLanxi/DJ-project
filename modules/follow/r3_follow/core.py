"""No device/network calls. Requests are dimensionless simulation values."""
from dataclasses import dataclass
from enum import StrEnum
import math

from .openpose_control import OpenPoseMarkerControl, PROFILE as OPENPOSE_PROFILE

_DEFAULT_STEERING = object()

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
    def __init__(self, required=False):
        self.enabled = required
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
            if self.last_good is None or obs.acquired_at-self.last_good > .2 + 1e-9:
                self.good_since, self.good_count = obs.acquired_at, 0
            self.last_good = obs.acquired_at
            self.good_count += 1
            self.reason = "waiting_for_stable_shape"
            if self.good_count >= 3 and obs.acquired_at-self.good_since >= .2 - 1e-9:
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
                 control_profile="legacy"):
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
        # Preserve the bare legacy controller's old lateral default. Selecting
        # the open-source profile alone must still use its original yaw routing;
        # lateral/hybrid require explicit selection. Explicit None is invalid.
        if steering_mode is _DEFAULT_STEERING:
            steering_mode = "yaw" if control_profile == "openpose_pid" else "lateral"
        if steering_mode not in ("lateral", "yaw", "hybrid"):
            raise ValueError("steering_mode must be lateral, yaw or hybrid")
        if steering_mode == "hybrid" and control_profile != "openpose_pid":
            raise ValueError("hybrid steering requires openpose_pid")
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
                                               steering_mode=steering_mode)
                          if control_profile == "openpose_pid" else None)
        # Dimensionless OFFLINE candidate, not an experimentally measured yaw
        # speed or a claim that this gain/limit is safe for physical flight.
        self.yaw_limit = yaw_limit
        self.forward_limit = forward_limit
        self.alignment_stop = alignment_stop
        self.alignment_resume = alignment_resume
        self._alignment_blocked = steering_mode == "yaw" and self._openpose is None
        self._motion_at = None
        self._motion_values = (0.0, 0.0, 0.0)
        self._loss_at = None
        self._good_since = None
        self._good_count = 0
        self.state = State.STOPPED
        self.reference_size = None
        self.observation = None
        self._last_frame_id = -1
        self._source = None
        self._invalid = False
        self.last_request = MotionRequest(self.state)
        self.last_stop_reason = ""
        self.distance_guard = ApparentSizeGuard(required=require_geometry)

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
        if not self._valid(obs):
            self._invalid = True
            self.distance_guard.block("invalid_input")
            if self._openpose:
                self._openpose.reset()
            return False
        if self._source is not None and obs.source != self._source:
            self._invalid = True
            return False  # source switch requires a new controller/calibration
        if obs.frame_id <= self._last_frame_id:
            return False  # duplicates never refresh the stored timestamp
        if self.observation is not None and obs.acquired_at <= self.observation.acquired_at:
            self._invalid = True
            self.distance_guard.block("nonadvancing_frame_time")
            if self._openpose:
                self._openpose.reset()
            return False
        if (self.observation is not None
                and self.state in (State.TRACKING, State.RECOVERING)
                and obs.acquired_at-self.observation.acquired_at > self.max_age):
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
                        or obs.acquired_at-previous.acquired_at > .2):
                    self._good_since, self._good_count = obs.acquired_at, 1
                else:
                    self._good_count += 1
            else:
                self._good_since, self._good_count = None, 0
        self._source = obs.source
        self._last_frame_id = obs.frame_id
        self.observation = obs
        self._invalid = False
        if self._openpose and not obs.found:
            self._openpose.reset()
        self.distance_guard.observe(obs)
        return True

    def _clear_recovery(self):
        self._loss_at = self._good_since = None
        self._good_count = 0

    def _issue(self, now):
        if self._invalid or not isinstance(now, (int, float)) or not math.isfinite(now):
            return State.INVALID
        obs = self.observation
        if obs is None or not 0 <= now - obs.acquired_at <= self.max_age:
            return State.STALE
        if not obs.found:
            return State.LOST
        return None

    def _zero(self, reason):
        self._motion_at = None
        self._motion_values = (0.0, 0.0, 0.0)
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
        if not self.distance_guard.calibrate(self.observation):
            return False, "Front-facing marker geometry required for reference"
        self.reference_size = self.observation.size_ratio
        self._zero("reference_updated")
        return True, "Reference size saved"

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
        self._motion_values = (0.0, 0.0, 0.0)
        if self._openpose:
            self._openpose.reset()
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
                "reference_size": self.reference_size, "request": self.last_request,
                "last_stop_reason": self.last_stop_reason,
                "control_profile": (OPENPOSE_PROFILE if self._openpose else
                                    "visible_target_yaw_v1" if self.steering_mode == "yaw"
                                    else "local_deadzone_transition_v1"),
                "openpose_control": self._openpose.status() if self._openpose else None,
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
            if (issue is None and self._good_count >= 3 and self._good_since is not None
                    and self.observation.acquired_at-self._good_since >= .15):
                self.state = State.TRACKING
                self._clear_recovery()
            else:
                return self._zero("waiting_for_stable_target")
        elif (issue == State.LOST and self.recovery_window > 0
              and self.observation.reason in RECOVERABLE_MISS_REASONS):
            self.state = State.RECOVERING
            self._loss_at = now
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
        x_error = 2 * obs.center_x / obs.width - 1
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
            desired = self._openpose.update(
                frame_id=obs.frame_id, acquired_at=obs.acquired_at,
                center_x=obs.center_x, image_width=obs.width,
                size_ratio=obs.size_ratio, reference_size=self.reference_size,
                forward_allowed=not self.distance_guard.blocked)
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
                       else adjustment(scale_error, self.forward_limit), yaw)
        if self.max_request_rate is not None:
            elapsed = 0 if self._motion_at is None else max(0, min(.1, now-self._motion_at))
            step = self.max_request_rate * elapsed
            def ramp(previous, target):
                # No smoothing may prolong a stop or carry motion the wrong way.
                if target == 0 or previous*target < 0:
                    return 0.0
                return previous + max(-step, min(step, target-previous))
            desired = tuple(ramp(old, new) for old, new in zip(self._motion_values, desired))
            self._motion_values, self._motion_at = desired, now
        if self.distance_guard.blocked:
            reason = "distance_guard: " + self.distance_guard.reason
        elif self._alignment_blocked:
            reason = "turn_to_align_before_depth"
        else:
            reason = "current_observation"
        self.last_request = MotionRequest(self.state, right=desired[0], forward=desired[1],
                                          reason=reason, yaw=desired[2])
        return self.last_request
