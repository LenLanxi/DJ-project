"""Marker adapter for tello-openpose's yaw, pitch and throttle PID axes.

Control equations are reused from geaxgx/tello-openpose at
a76c5e2dcd7d444207e79962e93fc1b4956442aa, lines 722, 726 and 739:
    yaw = int(-pid_yaw(xoff))
    pitch = int(pid_pitch(shoulders_width - keep_distance))
    throttle = int(-pid_throttle(ref_y - target_y))
The pinned source and its MIT license are included under _vendor/tello_openpose.
The actual PID implementation is the unchanged simple-pid 2.0.1 pid.py.

Necessary adaptations: an ArUco center/mean-side replaces the body keypoint/
shoulder width; ratios are mapped to the original program's 640-pixel frame
(its line 145); the RC cap is set per instance by the follow configuration
(yaw_limit / forward_limit; the reviewed default is 20/30 RC, aircraft_A_ID0
raises it to 50/40) instead of the upstream 100/50;
opt-in throttle retains upstream P=.4, I=D=0 with a provisional +/-20 RC cap
instead of upstream -80..100. Marker midheight replaces the body reference.
This vertical addition is not flight calibrated and has no altitude telemetry.
RC requests are divided by 100 for the existing R3 normalized request contract.
There is no new off-axis depth gate or PID law. Frame-time bookkeeping and
reset/shape protection surround the reused equations; no aircraft I/O is here.

The default marker_v2 parameter set adapts measurement bandwidth to variable
camera samples. Its depth input has a time-based 80 ms low-pass; each new frame
updates the PID using actual elapsed capture time, with no sample holding. D is reduced
from .3 to .06 and I from .04 to .01 (P stays .5). The original upstream_reference setting is
retained for exact equation comparisons. Neither is flight-calibrated.
Marker mode pauses integration while the previous depth output is saturated
and the new error pushes farther in that direction. Within one equivalent
640-pixel unit of the reference, depth becomes zero and its history is cleared.
An explicit reference change clears all history and waits for a new frame.

Optional relative depth uses 128 equivalent input units per unit depth error,
matching the local small-error slope at a 20%-wide reference marker. This
removes dependence on its arbitrary reference pixel size, but changes the
large-error response and still requires physical response/braking validation.
Calibrated pose and apparent-size ratios are measurements supplied by the R3
controller, not a change to the vendored PID implementation.

Horizontal routing is an explicit R3 adaptation, not an upstream flight result.
The same horizontal PID value feeds yaw (default), lateral, or an equal RC
number split for hybrid. RC translation and RC rotation are different physical
quantities: this numerical budget does not claim equal thrust, speed or effect.
The opt-in adaptive mode sends 80% of that budget to lateral near image center,
smoothly transferring it to yaw at offsets of 15%..55% of image half-width.
responsive_v1 raises depth P from .5 to .75; the filter, I, D and caps stay the
same. FollowController couples this to stronger, earlier approach braking.
Both additions require flight validation and do not provide obstacle avoidance.

Horizontal gains: upstream P=.25 with no damping saturated the +/-50 RC budget
as soon as the marker was ~120 px off-centre on a 960-wide frame, which made
the loop behave like bang-bang. The upstream integration now uses P=.16 and
a small D=.03 damps the approach. The D term deliberately never uses a frame
interval shorter than YAW_DERIVATIVE_MIN_DT, because a 1 ms interval would
multiply pixel jitter into a full-scale spike. Neither tuning is flight-verified.
"""
import math

from ._vendor.simple_pid import PID


EQUIVALENT_FRAME_WIDTH = 640.0
YAW_GAINS = (0.16, 0.0, 0.03)      # P 0.25→0.16（减饱和）+ 少量阻尼；见模块说明
YAW_DERIVATIVE_MIN_DT = .02        # D 项不使用的短间隔（秒），防帧抖动放大
YAW_DERIVATIVE_LIMIT = .10         # D 项最多贡献 10 RC，防止大幅跳变时反向打满
THROTTLE_GAINS = (0.4, 0.0, 0.0)
VERTICAL_PROFILE = "openpose_throttle_marker_v1"
UPSTREAM_PITCH_GAINS = (0.5, 0.04, 0.3)
PITCH_GAINS = (0.5, 0.01, 0.06)
# Opt-in field candidate: increase proportional response without increasing
# caps, integral accumulation, or motion through missing observations.
RESPONSIVE_PITCH_GAINS = (0.75, 0.01, 0.06)
DEPTH_FILTER_TAU = .08
DEPTH_REFERENCE_DEADBAND_PX = 1.0
RELATIVE_DEPTH_EQUIVALENT_SPAN = 128.0  # 100% relative depth error -> 128 input units
PROFILE = "openpose_pid_marker_v2"


class OpenPoseMarkerControl:
    """New frames advance PID; repeated controller ticks reuse its last result."""

    def __init__(self, yaw_limit=.2, forward_limit=.3, max_dt=.5,
                 *, tuning="marker_v2", steering_mode="yaw", vertical_follow=False,
                 vertical_limit=.2):
        for name, value in (("yaw_limit", yaw_limit), ("forward_limit", forward_limit),
                            ("vertical_limit", vertical_limit)):
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not 0 < value <= 1):
                raise ValueError(f"{name} must be finite and in (0, 1]")
        if type(max_dt) not in (int, float) or not math.isfinite(max_dt) or max_dt <= 0:
            raise ValueError("max_dt must be finite and positive")
        if tuning not in ("marker_v2", "responsive_v1", "upstream_reference"):
            raise ValueError("unsupported PID parameter set")
        if steering_mode not in ("yaw", "lateral", "hybrid", "adaptive"):
            raise ValueError("steering_mode must be yaw, lateral, hybrid or adaptive")
        if type(vertical_follow) is not bool:
            raise ValueError("vertical_follow must be a boolean")
        self._vertical_follow = vertical_follow
        self.vertical_limit = vertical_limit
        self._steering_mode = steering_mode
        self.horizontal_limit = yaw_limit
        self.tuning = tuning
        self.marker_tuning = tuning != "upstream_reference"
        self.max_dt = max_dt
        self.depth_filter_tau = DEPTH_FILTER_TAU if self.marker_tuning else 0.
        self.pitch_gains = (RESPONSIVE_PITCH_GAINS if tuning == "responsive_v1" else
                            PITCH_GAINS if self.marker_tuning else UPSTREAM_PITCH_GAINS)
        self.yaw_pid = PID(*YAW_GAINS, setpoint=0, sample_time=None,
                           output_limits=(-yaw_limit*100, yaw_limit*100))
        self.pitch_pid = PID(*self.pitch_gains, setpoint=0, sample_time=None,
                             output_limits=(-forward_limit*100, forward_limit*100))
        self.throttle_pid = PID(*THROTTLE_GAINS, setpoint=0, sample_time=None,
                                output_limits=(-vertical_limit*100, vertical_limit*100))
        self.reset()

    @property
    def vertical_follow(self):
        return self._vertical_follow

    @property
    def steering_mode(self):
        """Constructor-only routing; stop and construct a new instance to change."""
        return self._steering_mode

    def _route_horizontal(self, value, offset=0.):
        if self.steering_mode == "yaw":
            return 0, value
        if self.steering_mode == "lateral":
            return value, 0
        if self.steering_mode == "adaptive":
            # Offset is a fraction of image half-width. Near the center,
            # translate primarily; approaching an edge, preserve full yaw.
            # Smooth spatial blend, no alternating mode switch or blind search.
            # A single RC-number budget is shared; not equal physical effects.
            blend = max(0., min(1., (abs(offset)-.15)/.40))
            blend = blend*blend*(3.-2.*blend)
            right = int(value*.8*(1.-blend))
            return right, value-right
        # Split integer RC first. Rounding each half later could double an odd
        # unit. Give the remainder to yaw so alignment retains the first unit.
        right = int(value / 2)
        return right, value - right

    def reset(self):
        self.yaw_pid.reset()
        self.pitch_pid.reset()
        self.throttle_pid.reset()
        self.vertical_request = 0.0
        self._last_frame = None
        self._last_at = None
        self._pitch_at = None
        self._filtered_size_delta = None
        self._reference_size = None
        self._pitch_output = None
        self.integral_paused = False
        self._cached = (0.0, 0.0, 0.0)
        self.last_dt = None
        self.last_pitch_dt = None
        self.depth_update_reason = "reset"
        self.relative_depth_error = None
        self.reason = "reset"

    def _clear_depth(self, reason):
        """Public library reset, never private PID component assignment."""
        self.pitch_pid.reset()
        self._pitch_at = None
        self._pitch_output = None
        self._filtered_size_delta = None
        self.last_pitch_dt = None
        self.integral_paused = False
        self.depth_update_reason = reason
        self._cached = (self._cached[0], 0.0, self._cached[2])

    def update(self, *, frame_id, acquired_at, center_x, image_width,
               size_ratio, reference_size, forward_allowed=True,
               center_y=None, image_height=None, depth_error=None,
               target_x_fraction=.5, target_y_fraction=.5):
        if any(type(v) not in (int, float) or not math.isfinite(v) or not .15 <= v <= .85
               for v in (target_x_fraction, target_y_fraction)):
            raise ValueError('aim fractions must be within the central 70 percent of the image')
        self.target_x_fraction, self.target_y_fraction = target_x_fraction, target_y_fraction
        values = (acquired_at, center_x, image_width, size_ratio, reference_size)
        if (type(frame_id) is not int or frame_id < 0
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
                or image_width <= 0 or not 0 <= center_x < image_width
                or not 0 < size_ratio <= 2 or not 0 < reference_size <= 2
                or type(forward_allowed) is not bool):
            self.reset()
            self.reason = "invalid_input"
            raise ValueError("PID requires finite, valid marker geometry and frame time")
        if depth_error is not None and (
                type(depth_error) not in (int, float) or not math.isfinite(depth_error)):
            self.reset()
            raise ValueError("depth_error must be finite relative camera-axis depth error")
        if self.vertical_follow and (
                any(type(v) not in (int, float) or not math.isfinite(v)
                    for v in (center_y, image_height))
                or image_height <= 0 or not 0 <= center_y < image_height):
            self.reset()
            self.reason = "invalid_vertical_input"
            raise ValueError("Vertical PID requires a valid current-frame center_y and height")
        # Never keep depth history/output while shape validity is lost, even
        # if an outer caller is checking the same frame again.
        if not forward_allowed:
            self._clear_depth("geometry_blocked")
        # A stale/older call cannot replace the reference of newer observations.
        if ((self._last_frame is not None and frame_id < self._last_frame)
                or (self._last_at is not None and acquired_at < self._last_at)):
            self.reason = "older_frame_or_time"
            return self._cached
        if (self.marker_tuning and self._reference_size is not None
                and reference_size != self._reference_size):
            # Normal FollowController.calibrate() has already reset the adapter.
            # Protect direct callers too, including a same-frame recalibration:
            # do not turn the setpoint jump into a derivative/motion impulse.
            self.reset()
            self._reference_size = reference_size
            self._last_frame, self._last_at = frame_id, acquired_at
            self.reason = "reference_changed_wait_new_frame"
            self.depth_update_reason = self.reason
            return self._cached
        if self._last_frame is not None and frame_id <= self._last_frame:
            self.reason = "same_or_older_frame"
            return self._cached
        elapsed = None if self._last_at is None else acquired_at-self._last_at
        if elapsed is not None and elapsed <= 0:
            self.reason = "nonadvancing_frame_time"
            return self._cached
        if elapsed is not None and elapsed > self.max_dt:
            # FollowController itself latches stale on a gap; this independent
            # adapter also prevents old integral/derivative state crossing it.
            self.reset()
            elapsed = None
        # Do not invent a minimum dt or divide by near-zero timestamp jitter.
        # Very close samples wait until true elapsed capture time reaches 1 ms.
        # A subtraction of nominal 1 ms timestamps can round just below 1 ms.
        # Accept that numerical boundary; still pass the actual dt unchanged.
        if elapsed is not None and elapsed < .001 - 1e-9:
            self.reason = "frame_interval_too_short"
            return self._cached
        # The first sample has no measured interval. Using the PID library's
        # own zero-elapsed epsilon seeds P with effectively zero I and no D;
        # later samples use their actual capture-time difference.
        dt = 1e-16 if elapsed is None else elapsed
        self._reference_size = reference_size
        xoff = int(center_x/image_width*EQUIVALENT_FRAME_WIDTH
                   - EQUIVALENT_FRAME_WIDTH*target_x_fraction)
        size_delta = (size_ratio-reference_size)*EQUIVALENT_FRAME_WIDTH
        self.relative_depth_error = depth_error
        if depth_error is not None:
            # Positive relative error means farther away; the reused PID has
            # setpoint zero, so its measurement must be negative to move forward.
            # Scaling is explicit and independent of printed/initial pixel size.
            size_delta = -max(-1., min(4., depth_error))*RELATIVE_DEPTH_EQUIVALENT_SPAN
        # Same upstream equations, with two documented R3 guards on D: a minimum
        # interval (frame jitter) and a ceiling (an abrupt target jump must not
        # turn damping into a full-scale reverse request).
        self.yaw_pid(xoff, dt=max(dt, YAW_DERIVATIVE_MIN_DT))
        proportional, integral, derivative = self.yaw_pid.components
        ceiling = YAW_DERIVATIVE_LIMIT*100
        derivative = max(-ceiling, min(ceiling, derivative))
        limit = self.horizontal_limit*100
        horizontal = int(-max(-limit, min(limit, proportional+integral+derivative)))
        if self.vertical_follow:
            # Upstream resizes to WIDTH=640 preserving aspect ratio. Use the
            # same pixel scale on both axes. The marker aims at image midheight;
            # this is image alignment, NOT an altitude measurement or limit.
            # With top-left image origin, a target above center requests UP.
            yoff = int((image_height*target_y_fraction - center_y) * EQUIVALENT_FRAME_WIDTH/image_width)
            self.vertical_request = int(-self.throttle_pid(yoff, dt=dt))/100.0
        pitch = 0
        if (forward_allowed and self.marker_tuning
                and abs(size_delta) <= DEPTH_REFERENCE_DEADBAND_PX):
            # With P=.5 and integer RC quantization, this region has no useful
            # proportional motion request. Clear I rather than retaining a
            # command from an earlier period spent unable to catch the target.
            self._clear_depth("at_reference_zero_depth")
            # Seed a zero-error measurement at this capture time. The next
            # changed frame still gets the true dt and low-pass response,
            # rather than jumping unfiltered when leaving the reference band.
            self._filtered_size_delta = 0.
            self._pitch_at = acquired_at
            self._pitch_output = self.pitch_pid(0., dt=1e-16)
        elif forward_allowed:
            pitch_dt = 1e-16 if self._pitch_at is None else acquired_at-self._pitch_at
            if self._filtered_size_delta is None or not self.depth_filter_tau:
                self._filtered_size_delta = size_delta
            else:
                # Exact first-order filter coefficient for the measured time
                # interval; no constant frame rate or invented timestamp.
                alpha = -math.expm1(-dt/self.depth_filter_tau)
                self._filtered_size_delta += alpha*(size_delta-self._filtered_size_delta)
            low, high = self.pitch_pid.output_limits
            error = -self._filtered_size_delta
            self.integral_paused = bool(self.marker_tuning
                and self._pitch_output is not None
                and ((self._pitch_output >= high and error > 0)
                     or (self._pitch_output <= low and error < 0)))
            # Standard conditional integration using the last clipped
            # PID output. One crossing sample may integrate; following same-
            # direction saturated samples cannot keep accumulating. Opposing
            # errors retain integration to unwind. The vendor remains intact.
            saved_ki = self.pitch_pid.Ki
            if self.integral_paused:
                self.pitch_pid.Ki = 0.
            try:
                self._pitch_output = self.pitch_pid(self._filtered_size_delta, dt=pitch_dt)
            finally:
                self.pitch_pid.Ki = saved_ki
            pitch = int(self._pitch_output)
            self.last_pitch_dt = None if self._pitch_at is None else pitch_dt
            self._pitch_at = acquired_at
            self.depth_update_reason = "new_measurement"
        self._last_frame, self._last_at = frame_id, acquired_at
        self.last_dt = elapsed
        self.reason = "new_frame" if forward_allowed else "depth_geometry_blocked"
        right, yaw = self._route_horizontal(horizontal, (center_x/image_width-target_x_fraction)*2.)
        self._cached = (right/100.0, pitch/100.0, yaw/100.0)
        return self._cached

    def status(self):
        return {"profile": PROFILE, "input_frame_width": EQUIVALENT_FRAME_WIDTH,
                "parameter_set": self.tuning,
                "steering_mode": self.steering_mode,
                "horizontal_limit": self.horizontal_limit,
                "horizontal_allocation": ("lateral_center_yaw_edge_shared_budget"
                                           if self.steering_mode == "adaptive" else
                                           "integer_half_lateral_remainder_yaw"
                                           if self.steering_mode == "hybrid"
                                           else self.steering_mode),
                "horizontal_budget_unit": "sum of absolute RC numbers; not physical speed or thrust",
                "yaw_gains": YAW_GAINS, "pitch_gains": self.pitch_gains,
                "vertical": {"enabled": self.vertical_follow,
                             "profile": VERTICAL_PROFILE,
                             "gains": THROTTLE_GAINS, "limit": self.vertical_limit,
                             "target_y_fraction": getattr(self, 'target_y_fraction', .5),
                             "raw_normalized_request": self.vertical_request,
                             "components": self.throttle_pid.components,
                             "physical_tuning_verified": False,
                             "altitude_limits_enforced": False},
                "depth_filter_tau_s": self.depth_filter_tau,
                "depth_reference_deadband_equivalent_px": (DEPTH_REFERENCE_DEADBAND_PX
                                                            if self.marker_tuning else 0),
                "depth_integral_paused": self.integral_paused,
                "reference_size": self._reference_size,
                "relative_depth_error": self.relative_depth_error,
                "relative_depth_equivalent_span": RELATIVE_DEPTH_EQUIVALENT_SPAN,
                "filtered_size_delta_equivalent_px": self._filtered_size_delta,
                "yaw_components": self.yaw_pid.components,
                "pitch_components": self.pitch_pid.components,
                "last_frame_id": self._last_frame, "last_capture_at": self._last_at,
                "last_dt": self.last_dt, "update_reason": self.reason,
                "last_pitch_dt": self.last_pitch_dt,
                "depth_update_reason": self.depth_update_reason,
                "raw_normalized_request": self._cached,
                "physical_tuning_verified": False,
                "distance_unit": "apparent marker ratio, not metres",
                "output_unit": "normalized RC request, not physical speed"}
