"""Bounded depth-trend lead around the reused PID, not a plant-speed model.

The vendor stays unchanged. Reliable recent relative-depth change
adds a bounded derivative/measurement-age correction, allowing earlier braking
and retreat without reducing the chase cap. It may reverse a request before
the measured reference is crossed; the outer controller must send zero first.
No continuation through lost/stale/shape-invalid targets is allowed. Rates are
reference-distances/second, NOT metres/second. This is an R3 adaptation, not an
upstream algorithm or a measured physical stopping-distance guarantee.
The opt-in responsive tuning supplies its matching P scale and a longer
approach-only allowance; the original defaults remain unchanged.
"""
from collections import deque
import math


class DepthBrake:
    PROFILE = 'bounded_depth_lead_v1'
    WINDOW = .40
    MIN_SPAN = .12
    MIN_RATE = .025
    MIN_CHANGE = .012
    RESPONSE_ALLOWANCE = .25
    MAX_HORIZON = .75
    RATE_FILTER_TAU = .10
    # Same proportional slope as marker_v2 relative depth: .5 * 128 / 100.
    PROPORTIONAL_SCALE = .64

    def __init__(self, forward_limit=.3, *, proportional_scale=.64, braking_allowance=.25):
        if (type(forward_limit) not in (int,float) or not math.isfinite(forward_limit)
                or not 0 < forward_limit <= 1):
            raise ValueError('forward_limit must be finite in (0,1]')
        self.forward_limit = forward_limit
        if (type(proportional_scale) not in (int, float)
                or not math.isfinite(proportional_scale) or not 0 < proportional_scale <= 2):
            raise ValueError('proportional_scale must be finite in (0,2]')
        self.proportional_scale = proportional_scale
        if (type(braking_allowance) not in (int, float)
                or not math.isfinite(braking_allowance) or not .25 <= braking_allowance <= .75):
            raise ValueError('braking_allowance must be finite in [.25,.75]')
        self.braking_allowance = braking_allowance
        self.correction_limit = min(.20, forward_limit*.5)
        self.reversal_threshold = min(.03,forward_limit*.25)
        self.reset('initial')

    def reset(self, reason='reset'):
        self.samples = deque(maxlen=32)
        self.last_frame = None
        self.rate = None
        self.reason = reason
        self.raw_request = self.output = 0.
        self.age = self.horizon = None
        self.predicted_error = None
        self.correction = 0.
        self.direction = 0

    def limit(self, request, *, frame_id, captured_at, now, error):
        values = (request, captured_at, now, error)
        if (any(type(v) not in (int,float) or not math.isfinite(v) for v in values)
                or type(frame_id) is not int or frame_id < 0
                or now-captured_at < -1e-6 or now-captured_at > .5+1e-6):
            self.reset('invalid_or_stale')
            return 0.
        if self.last_frame is not None and frame_id < self.last_frame:
            self.reset('older_frame')
            return 0.
        if frame_id != self.last_frame:
            previous_at = self.samples[-1][0] if self.samples else None
            if self.samples and captured_at <= self.samples[-1][0]:
                self.reset('nonadvancing_time')
                return 0.
            if self.samples and captured_at-self.samples[-1][0] > .5+1e-6:
                self.reset('sample_gap')
            self.samples.append((captured_at,error))
            self.last_frame = frame_id
            while self.samples and captured_at-self.samples[0][0] > self.WINDOW+1e-6:
                self.samples.popleft()
            estimate = self._estimate_rate()
            if estimate is None:
                self.rate = None
            elif self.rate is None or previous_at is None:
                self.rate = estimate
            else:
                alpha=-math.expm1(-(captured_at-previous_at)/self.RATE_FILTER_TAU)
                self.rate += alpha*(estimate-self.rate)
        elif self.samples and (captured_at,error) != self.samples[-1]:
            self.reset('changed_cached_measurement')
            return 0.
        self.raw_request, self.output = request, request
        self.predicted_error = None
        self.correction = 0.
        self.age = max(0., now-captured_at)
        # Extra lead is ONLY for deceleration: do not boost a departing target
        # further merely because the braking setting was increased.
        allowance = (self.braking_allowance if self.rate is not None and request*self.rate < 0
                     else self.RESPONSE_ALLOWANCE)
        self.horizon = min(self.MAX_HORIZON, allowance+self.age)
        self.reason = 'insufficient_consistent_trend'
        if self.rate is None or abs(self.rate) < .005:
            return self._filter_reversal(request)
        self.predicted_error = error + self.rate*self.horizon
        self.correction = max(-self.correction_limit,min(self.correction_limit,
            self.proportional_scale*self.rate*self.horizon))
        self.output = max(-self.forward_limit,min(self.forward_limit,request+self.correction))
        self.reason = ('approach_braking' if request*self.correction < 0
                       else 'depth_trend_response')
        return self._filter_reversal(self.output)

    def _filter_reversal(self, output):
        self.output=output
        sign = 1 if output>0 else -1 if output<0 else 0
        if self.direction*sign < 0 and abs(self.output) < self.reversal_threshold:
            # A tiny opposite request caused by pixel quantisation goes to
            # zero, not an alternating +/- pulse. Initial and same-direction
            # small requests are retained; this is not a global deadzone.
            self.output=0.
            self.reason='small_reversal_zero'
        elif sign:
            self.direction=sign
        return self.output

    def _estimate_rate(self):
        if len(self.samples) < 3:
            return None
        span = self.samples[-1][0]-self.samples[0][0]
        if span < self.MIN_SPAN-1e-6:
            return None
        # Fit all samples to avoid alternating between zero and a full-sized
        # derivative on pixel-size plateaus. Residual validation rejects jumps;
        # the elapsed-time filter softens quantisation, not stop/loss commands.
        times=[t-self.samples[0][0] for t,_ in self.samples]
        errors=[e for _,e in self.samples]
        mean_t=sum(times)/len(times);mean_e=sum(errors)/len(errors)
        rate=sum((t-mean_t)*(e-mean_e) for t,e in zip(times,errors))/sum((t-mean_t)**2 for t in times)
        if max(abs(e-mean_e-rate*(t-mean_t)) for t,e in zip(times,errors)) > .025:
            return None
        if abs(rate) < self.MIN_RATE or abs(rate)*span < self.MIN_CHANGE:
            return 0.
        return rate

    def status(self):
        return dict(profile=self.PROFILE,reason=self.reason,samples=len(self.samples),
                    proportional_scale=self.proportional_scale,
                    relative_rate_per_s=self.rate,observation_age_s=self.age,
                    horizon_s=self.horizon,predicted_relative_error=self.predicted_error,
                    raw_forward_request=self.raw_request,limited_forward_request=self.output,
                    correction=self.correction,correction_limit=self.correction_limit,
                    reversal_threshold_rc=self.reversal_threshold*100,
                    response_allowance_s=self.RESPONSE_ALLOWANCE,
                    braking_allowance_s=self.braking_allowance,
                    rate_filter_tau_s=self.RATE_FILTER_TAU,
                    physical_tuning_verified=False)
