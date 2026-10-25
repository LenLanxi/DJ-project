"""Compare pinned upstream gains and marker adaptation, without any devices.

Field replay is open loop: the recorded camera inputs cannot respond to new
requests. The separate scalar response model is invented, NOT Tello physics.
Neither experiment measures speed, stopping distance or person safety.
"""
import argparse
import csv
import json
import math
import platform
from pathlib import Path

from r3_follow.openpose_control import OpenPoseMarkerControl


TUNINGS = ("upstream_reference", "marker_v2")


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    i = (len(values)-1)*fraction
    lo, hi = math.floor(i), math.ceil(i)
    return values[lo]+(values[hi]-values[lo])*(i-lo)


def summary(rows):
    eligible = [r for r in rows if r["found"] and not r.get("seed_before_start")]
    signs, reversals, variation, previous = {}, 0, 0., {}
    for r in eligible:
        key = (r.get("segment", 0), r.get("run", 0))
        value = r["forward_rc"]
        if key in previous:
            variation += abs(value-previous[key])
        previous[key] = value
        if value:
            sign = 1 if value > 0 else -1
            reversals += key in signs and signs[key] != sign
            signs[key] = sign
    return {"found_samples": len(eligible),
            "forward_direction_reversals": reversals,
            "forward_total_variation_rc": round(variation, 9),
            "forward_saturated_samples": sum(abs(r["forward_rc"]) == 30 for r in eligible),
            "negative_forward_saturated_samples": sum(r["forward_rc"] == -30 for r in eligible),
            "depth_updates": sum(r["depth_update_reason"] == "new_measurement" for r in eligible),
            "reference_band_zero_samples": sum(r["depth_update_reason"] == "at_reference_zero_depth" for r in eligible),
            "conditional_integration_paused_samples": sum(r.get("integral_paused", False) for r in eligible),
            "abs_forward_p95_rc": percentile([abs(r["forward_rc"]) for r in eligible], .95),
            "abs_d_p95_rc": percentile([abs(r["d_rc"]) for r in eligible], .95),
            "abs_d_max_rc": max((abs(r["d_rc"]) for r in eligible), default=0),
            "all_finite_and_capped": all(math.isfinite(r[k]) and abs(r[k]) <= cap
                                         for r in rows for k, cap in (("forward_rc", 30), ("yaw_rc", 20))),
            "not_found_nonzero_samples": sum(not r["found"] and
                                               bool(r["forward_rc"] or r["yaw_rc"]) for r in rows)}


def replay_samples(samples, tuning, reference=.2):
    adapter = OpenPoseMarkerControl(tuning=tuning)
    rows, segment, run = [], None, 0
    for sample in samples:
        if sample.get("segment", 0) != segment or sample.get("reset", False):
            adapter.reset()
            segment = sample.get("segment", 0)
            run += 1
        if sample["found"]:
            request = adapter.update(frame_id=sample["frame_id"], acquired_at=sample["timestamp"],
                                     image_width=sample.get("image_width", 640),
                                     center_x=sample["center_x"], size_ratio=sample["size_ratio"],
                                     reference_size=reference,
                                     forward_allowed=sample.get("forward_allowed", True))
        else:
            adapter.reset()
            run += 1
            request = (0., 0., 0.)
        state = adapter.status()
        p, i, d = state["pitch_components"]
        rows.append(dict(sample, tuning=tuning, run=run, forward_rc=request[1]*100,
                         yaw_rc=request[2]*100, p_rc=p, i_rc=i, d_rc=d,
                         depth_update_reason=state["depth_update_reason"],
                         integral_paused=state["depth_integral_paused"],
                         filtered_input_px=state["filtered_size_delta_equivalent_px"],
                         pitch_dt=state["last_pitch_dt"]))
    return rows


def field_samples(log):
    records = [dict(json.loads(line), source_line=n) for n, line in
               enumerate(log.read_text(encoding="utf-8").splitlines(), 1)]
    observations = [r for r in records if r["event"] == "observation"]
    reference = next(r["reference_size"] for r in records if
                     r["event"] == "calibration" and r["ok"])
    starts = [r for r in records if r["event"] == "start"]
    halts = [r for r in records if r["event"] == "tracking_halted"]
    samples = []
    for segment, start in enumerate(starts, 1):
        halt = next((r for r in halts if r["source_line"] > start["source_line"]), None)
        if halt is None:
            raise ValueError("Diagnostic expects each start to have a recorded halt")
        seed = [r for r in observations if r["source_line"] < start["source_line"]][-1]
        selected = [seed]+[r for r in observations if
                           start["source_line"] < r["source_line"] < halt["source_line"]]
        for n, obs in enumerate(selected):
            samples.append({"segment": segment, "seed_before_start": n == 0,
                            "source_line": obs["source_line"], "frame_id": obs["frame_id"],
                            "timestamp": obs["timestamp"], "found": obs["found"],
                            "center_x": obs["center_x"], "size_ratio": obs["size_ratio"],
                            "image_width": 960})
    return samples, reference


def synthetic_samples(scenario, duration=8.):
    samples, t, seq = [], 0., 0
    while t <= duration+1e-9:
        size = .2
        found, allowed, reset = True, True, False
        if scenario == "step":
            size = .2 if t < 1 else .14 if t < 4 else .23
        elif scenario == "ramp":
            size = .2-.015*min(max(t-1, 0), 2)+.025*min(max(t-4, 0), 2)
        elif scenario in ("noise", "irregular_noise"):
            size = .2+.002*math.sin(2*math.pi*7*t)+.001*math.sin(2*math.pi*11*t)
        elif scenario == "loss_and_shape":
            size = .12 if t < 6 else .24
            found = not 2 <= t < 2.4
            allowed = not 4 <= t < 4.3
            reset = 5 <= t < 5.03
        elif scenario == "approach_braking":
            size = .15+.025*min(max(t-1, 0), 3)
        else:
            raise ValueError("unknown synthetic scenario")
        samples.append({"timestamp": round(t, 10), "frame_id": seq, "found": found,
                        "center_x": 360., "size_ratio": size,
                        "forward_allowed": allowed, "reset": reset})
        interval = (.011, .033, .12, .027, .22, .019)[seq % 6] if scenario == "irregular_noise" else .02
        t += interval
        seq += 1
    return samples


def synthetic_metrics(rows, scenario):
    result = summary(rows)
    if scenario == "step":
        after = [r for r in rows if 1 <= r["timestamp"] < 4]
        result["first_positive_delay_s"] = next((r["timestamp"]-1 for r in after if r["forward_rc"] > 0), None)
        result["first_above_15_rc_delay_s"] = next((r["timestamp"]-1 for r in after if r["forward_rc"] >= 15), None)
        result["reversal_delay_s"] = next((r["timestamp"]-4 for r in rows if
                                          r["timestamp"] >= 4 and r["forward_rc"] <= 0), None)
    if scenario == "approach_braking":
        before_crossing = [r for r in rows if 1 <= r["timestamp"] < 3]
        # Size crosses .2 at t=3. This reports derivative braking, not proof of
        # physical braking or a requirement for every input reversal.
        result["first_nonpositive_before_reference_t"] = next((r["timestamp"] for r in
                                                               before_crossing if r["forward_rc"] <= 0), None)
        result["last_before_reference_request_rc"] = before_crossing[-1]["forward_rc"]
    return result


def virtual_step(tuning, response=.25, latency=0., noise=False, stop_at=12.):
    """Invented dimensionless range and speed; 1 RC-normalized = 1 model unit/s.

    Both settings use the same target jump and .8/s command ramp. Frame age is
    checked at .5 seconds. This is a depth-only stress model, not service or
    detector integration. No automatic restart occurs after stale/stop.
    """
    adapter = OpenPoseMarkerControl(tuning=tuning)
    dt, distance, velocity, request = .02, 2., 0., 0.
    history, rows, blocked = [], [], False
    for seq in range(801):
        t = round(seq*dt, 10)
        if seq == 100:
            distance += .6
        measured = .4/distance
        if noise:
            measured += .002*math.sin(2*math.pi*7*t)+.001*math.sin(2*math.pi*11*t)
        history.append((seq, t, measured))
        eligible = [r for r in history if r[1] <= t-latency+1e-9]
        if eligible:
            frame, captured, size = eligible[-1]
            if t-captured > .5+1e-9:
                blocked = True
            if t >= stop_at:
                blocked = True
            if blocked:
                adapter.reset()
                raw, request = 0., 0.
            else:
                raw = adapter.update(frame_id=frame, acquired_at=captured, center_x=320,
                                     image_width=640, size_ratio=size, reference_size=.2)[1]
                # Match the production ramp: zero and reversal brake at once.
                if raw == 0 or request*raw < 0:
                    request = 0.
                else:
                    request += max(-.8*dt, min(.8*dt, raw-request))
        else:
            raw, request = 0., 0.
        velocity += -math.expm1(-dt/response)*(request-velocity)
        distance -= velocity*dt
        assert distance > .1
        rows.append({"t": t, "range_units": distance, "virtual_velocity_units_s": velocity,
                     "request_rc": request*100, "raw_request_rc": raw*100,
                     "blocked": blocked, "measurement_size_ratio": measured})
    active = [r for r in rows if 2 <= r["t"] < stop_at]
    start_speed = next(r["virtual_velocity_units_s"] for r in rows if r["t"] == stop_at)
    settled = next((r["t"]-2 for i, r in enumerate(active) if
                    all(abs(later["range_units"]-2) <= .06 for later in active[i:])), None)
    result = {"response_s": response, "latency_s": latency, "noise": noise,
              "settling_to_10pct_step_time_s": settled,
              "undershoot_below_reference_units": max(0, 2-min(r["range_units"] for r in active)),
              "first_positive_after_step_delay_s": next((r["t"]-2 for r in active if r["request_rc"] > 0), None),
              "post_stop_command_nonzero": sum(r["request_rc"] != 0 for r in rows if r["t"] >= stop_at),
              "virtual_speed_at_stop_units_s": start_speed,
              "post_stop_coast_units": next(r["range_units"] for r in rows if r["t"] == stop_at)-rows[-1]["range_units"],
              "all_finite_and_capped": all(math.isfinite(r["request_rc"]) and abs(r["request_rc"]) <= 30+1e-9 for r in rows),
              "physical_tello_model": False}
    return rows, result


def write_csv(path, rows):
    keys = list(dict.fromkeys(key for r in rows for key in r))
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"python_version": platform.python_version(), "settings": {}, "synthetic": {},
              "virtual_matrix": [], "limitations": [
                  "Original flight log is fixed-input replay; requests do not change subsequent images.",
                  "Field events lack image corners; depth shape guard and actual control eligibility cannot be replayed.",
                  "Field replay resets at start/loss; excludes outer ramp, sender, recovery waiting and ownership.",
                  "A direction reversal is not automatically erroneous or a measured flight oscillation.",
                  "Virtual range/speed are invented model units, not metres or Tello RC calibration.",
                  "This check does not demonstrate detector timing or correct actual brake/hover behaviour."]}
    for tuning in TUNINGS:
        report["settings"][tuning] = OpenPoseMarkerControl(tuning=tuning).status()
        for scenario in ("step", "ramp", "noise", "irregular_noise", "loss_and_shape", "approach_braking"):
            rows = replay_samples(synthetic_samples(scenario), tuning)
            write_csv(args.output/f"{scenario}_{tuning}.csv", rows)
            report["synthetic"].setdefault(scenario, {})[tuning] = synthetic_metrics(rows, scenario)
        for response in (.1, .25, 1.):
            for latency in (0., .2, .4, .6):
                for noise in (False, True):
                    rows, metrics = virtual_step(tuning, response, latency, noise)
                    name = f"virtual_{tuning}_response{response}_latency{latency}_noise{int(noise)}"
                    write_csv(args.output/f"{name}.csv", rows)
                    report["virtual_matrix"].append(dict(metrics, tuning=tuning))
    if args.log:
        samples, reference = field_samples(args.log)
        report["field"] = {"log": str(args.log), "reference_size": reference,
                           "width_960_assumed_from_session_metadata": True}
        for tuning in TUNINGS:
            rows = replay_samples(samples, tuning, reference)
            write_csv(args.output/f"field_{tuning}.csv", rows)
            report["field"][tuning] = summary(rows)
    (args.output/"pid_adaptation_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"python": report["python_version"], "field": report.get("field"),
                      "synthetic": report["synthetic"], "virtual_runs": len(report["virtual_matrix"])}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
