"""Paired legacy/open-source control comparisons; never opens a device.

Open loop uses exactly the same observations. Closed loop uses the same target
trajectory and explicitly invented vehicle dynamics, so subsequent camera
observations differ because each controller moves its own virtual vehicle.
Neither procedure measures Tello response or passes a physical flight test.
"""
import argparse
import csv
import json
import math
from pathlib import Path

from r3_follow.core import FollowController, Observation


PROFILES = ("legacy", "openpose_pid")
SCENARIOS = ("curve", "distance_step", "off_axis_distance_step", "size_noise",
             "brief_loss", "stale_video", "stop")


def make_observation(seq, t, x, size, found=True, narrow=False):
    side, y = size*640, 240
    horizontal = side*(.5 if narrow else 1)
    corners = ((x-horizontal/2, y-side/2), (x+horizontal/2, y-side/2),
               (x+horizontal/2, y+side/2), (x-horizontal/2, y+side/2))
    return Observation(seq, t, "paired-control-simulation", 640, 480, 0, found,
                       x if found else None, y if found else None,
                       size if found else None, corners if found else (),
                       "" if found else "not_found")


def controller(profile):
    return FollowController(control_profile=profile, steering_mode="yaw",
                            require_geometry=True, max_request_rate=.8,
                            recovery_window=3.)


def _verify_request(r):
    assert all(math.isfinite(v) for v in (r.right, r.forward, r.yaw))
    assert r.right == 0 and abs(r.forward) <= .3 and abs(r.yaw) <= .2
    if r.state != "tracking":
        assert r.forward == r.yaw == 0


def identical_input_trace():
    """Independent of either controller: centering, distance, shape, loss, stop."""
    controllers = {name: controller(name) for name in PROFILES}
    rows = []
    for seq in range(181):
        t = round(seq*.1, 8)
        x, size = 320., .2
        if 2 <= t < 6:
            x, size = 480., .12
        elif 6 <= t < 9:
            x, size = 320+100*math.sin(4*t), .18+.006*math.sin(20*t)
        elif 9 <= t < 11:
            x, size = 380., .12
        elif t >= 11:
            x, size = 400., .15
        found = not 12 <= t < 12.4
        obs = make_observation(seq, t, x, size, found, narrow=9 <= t < 10)
        for profile, c in controllers.items():
            c.observe(obs)
            if seq == 0:
                assert c.calibrate(t)[0] and c.start(t)[0]
            if t == 16:
                c.stop()
            r = c.tick(t)
            _verify_request(r)
            rows.append({"t": t, "profile": profile, "frame_id": seq,
                         "input_center_x": x, "input_size_ratio": size,
                         "input_found": found, "shape_narrow": 9 <= t < 10,
                         "request_yaw": r.yaw, "request_forward": r.forward,
                         "state": str(r.state), "reason": r.reason})
    return rows


def closed_loop(profile, scenario="curve", response=.25, delay=0., duration=20.):
    if profile not in PROFILES or scenario not in SCENARIOS:
        raise ValueError("unsupported profile/scenario")
    if not math.isfinite(response) or response <= 0 or not math.isfinite(delay) or delay < 0:
        raise ValueError("response/delay must be finite valid durations")
    c = controller(profile)
    # These response constants are illustrative, not Tello measurements.
    dt, focal, marker_width = .1, 320/math.tan(math.radians(41)), .32
    x = z = heading = forward_speed = yaw_rate = 0.
    captured, rows = [], []
    for seq in range(round(duration/dt)+1):
        t = round(seq*dt, 8)
        bearing = .5*math.sin(.25*t)
        distance = 2+.35*math.sin(.3*t)
        if scenario in ("distance_step", "size_noise"):
            bearing = 0.
            distance = 2. if t < 3 else 2.6
        elif scenario == "off_axis_distance_step":
            bearing = 0. if t < 3 else .5
            distance = 2. if t < 3 else 2.6
        tx, tz = distance*math.sin(bearing), distance*math.cos(bearing)
        dx, dz = tx-x, tz-z
        cam_x = dx*math.cos(heading)-dz*math.sin(heading)
        cam_z = dx*math.sin(heading)+dz*math.cos(heading)
        cx = 320+focal*cam_x/cam_z if cam_z > .05 else -10000.
        pixel_size = focal*marker_width/cam_z if cam_z > .05 else 0.
        in_view = cam_z > .05 and pixel_size/2 < cx < 640-pixel_size/2 and 0 < pixel_size < 480
        visible = in_view and not (scenario == "brief_loss" and 7 <= t < 7.4)
        reported_ratio = pixel_size/640
        if scenario == "size_noise" and t >= 3:
            reported_ratio *= 1+.035*math.sin(26*t)
        obs = make_observation(seq, t, cx, reported_ratio, visible)
        if not (scenario == "stale_video" and 7 <= t < 8):
            captured.append(obs)
        if seq == 0:
            c.observe(obs)
            assert c.calibrate(t)[0] and c.start(t)[0]
        latency = delay if t >= 2 else 0.
        eligible = [o for o in captured if o.acquired_at <= t-latency+1e-9]
        if eligible:
            c.observe(eligible[-1])
        if scenario == "stop" and t == 7:
            c.stop()
        r = c.tick(t)
        _verify_request(r)
        alpha = 1-math.exp(-dt/response)
        forward_speed += alpha*(r.forward-forward_speed)
        yaw_rate += alpha*(2*r.yaw-yaw_rate)
        heading += yaw_rate*dt
        x += math.sin(heading)*forward_speed*dt
        z += math.cos(heading)*forward_speed*dt
        rows.append({"t": t, "profile": profile, "scenario": scenario,
                     "response_s": response, "delay_s": delay,
                     "frame_id": c.observation.frame_id,
                     "observation_age_s": t-c.observation.acquired_at,
                     "state": str(r.state), "reason": r.reason,
                     "target_visible": visible, "request_yaw": r.yaw,
                     "request_forward": r.forward,
                     "virtual_forward_m_s": forward_speed,
                     "virtual_yaw_rad_s": yaw_rate,
                     "virtual_heading_rad": heading,
                     "virtual_range_m": math.hypot(dx,dz),
                     "image_horizontal_error": (cx-320)/320 if in_view else None,
                     "relative_size_error": (c.reference_size-reported_ratio)/c.reference_size
                                            if in_view else None})
    tracking = [r for r in rows if r["state"] == "tracking"]
    def mean_abs(key):
        values = [abs(r[key]) for r in tracking if r[key] is not None]
        return sum(values)/len(values) if values else None
    def reversals(key):
        signs = [math.copysign(1, r[key]) for r in tracking if r[key] != 0]
        return sum(a != b for a, b in zip(signs, signs[1:]))
    summary = {"profile": profile, "scenario": scenario, "response_s": response,
               "delay_s": delay, "samples": len(rows), "final_state": rows[-1]["state"],
               "mean_abs_horizontal_error_during_tracking": mean_abs("image_horizontal_error"),
               "mean_abs_relative_size_error_during_tracking": mean_abs("relative_size_error"),
               "simultaneous_yaw_depth_samples": sum(r["request_yaw"] != 0 and
                                                     r["request_forward"] != 0 for r in rows),
               "legacy_alignment_gate_samples": sum(r["reason"] == "turn_to_align_before_depth"
                                                    for r in rows),
               "forward_direction_reversals": reversals("request_forward"),
               "yaw_direction_reversals": reversals("request_yaw"),
               "nontracking_nonzero": sum(r["state"] != "tracking" and
                                            (r["request_yaw"] != 0 or r["request_forward"] != 0)
                                            for r in rows),
               "physical_flight_verified": False, "image_detector_exercised": False}
    return rows, summary


def _csv(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _plot(path, traces):
    panels = (("Horizontal image error (half-width units)", "image_horizontal_error", 1.),
              ("Size error (relative to calibration)", "relative_size_error", 1.),
              ("Forward RC request (normalized; not m/s)", "request_forward", .3),
              ("Yaw RC request (normalized; not rad/s)", "request_yaw", .2))
    colors = {"legacy": "#8c8c8c", "openpose_pid": "#167b9e"}
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="860">',
           '<rect width="1000" height="860" fill="white"/>',
           '<g font-family="Arial" font-size="12">',
           '<text x="60" y="26" font-size="17">Paired virtual feedback: gray=legacy, blue=open-source PID</text>',
           '<text x="60" y="49">Invented vehicle dynamics. Same target path, independent feedback. No physical-flight validation.</text>']
    for index, (title, key, scale) in enumerate(panels):
        top = 95+index*185
        svg.append(f'<text x="60" y="{top-12}">{title}</text>')
        for value in (-scale, 0, scale):
            y = top+65-value/scale*65
            svg.append(f'<path d="M60 {y} H950" stroke="#ddd"/><text x="8" y="{y+4}">{value:g}</text>')
        for profile, rows in traces.items():
            points = []
            for r in rows:
                if r[key] is None:
                    continue
                px = 60+r["t"]/rows[-1]["t"]*890
                py = top+65-max(-scale, min(scale, r[key]))/scale*65
                points.append(f"{px:.2f},{py:.2f}")
            svg.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{colors[profile]}" stroke-width="1.5"/>')
        svg.append(f'<text x="60" y="{top+148}">0s</text><text x="925" y="{top+148}">{rows[-1]["t"]:g}s</text>')
    svg.extend(['</g>', '</svg>'])
    path.write_text("\n".join(svg), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    _csv(args.output/"identical_inputs.csv", identical_input_trace())
    summaries = []
    cases = list(dict.fromkeys([(s, .25, 0.) for s in SCENARIOS] +
                  [("curve", r, d) for r in (.1,.25,1.) for d in (0.,.2,.4,.6)]))
    for scenario, response, delay in cases:
        traces = {}
        name = f"{scenario}_response{response:g}_delay{delay:g}"
        for profile in PROFILES:
            rows, summary = closed_loop(profile, scenario, response, delay)
            traces[profile] = rows
            summary["trace"] = f"{name}_{profile}.csv"
            summary["plot"] = f"{name}.svg"
            _csv(args.output/summary["trace"], rows)
            summaries.append(summary)
        _plot(args.output/f"{name}.svg", traces)
    result = {"scope": "synthetic observations and invented response; no images, camera or drone",
              "same_limits": {"yaw": .2, "forward": .3, "ramp_per_s": .8},
              "cases": len(cases), "closed_loop_runs": len(summaries),
              "physical_speed_mapping": "not measured; virtual 1m/s and 2rad/s at request 1 are assumptions",
              "first_sample_dt": "PID zero-elapsed epsilon; subsequent real capture intervals",
              "direction_reversal_is_not_oscillation_count": True, "runs": summaries}
    (args.output/"summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"cases": len(cases), "runs": len(summaries),
                      "nontracking_nonzero": sum(r["nontracking_nonzero"] for r in summaries),
                      "physical_flight_verified": False}, indent=2))


if __name__ == "__main__":
    main()
