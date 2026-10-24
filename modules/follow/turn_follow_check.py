"""Deterministic closed-loop yaw simulation, with no hardware or camera access.

This is a pinhole projection of a square target into a virtual forward camera.
Vehicle yaw/translation change the NEXT observation; responses are first-order
invented dynamics. It does not run image detection, model occlusion physics,
calibrate Tello motion, estimate real distance, or prove physical flight safety.

Run: python -B -X utf8 turn_follow_check.py --output <result directory>
"""
import argparse
import csv
import json
import math
from pathlib import Path

from r3_follow.core import FollowController, Observation


SCENARIOS = ("curved_path", "short_occlusion", "out_of_view", "frozen_frames", "stop_then_return")


def run_scenario(scenario="curved_path", response=.25, delay=0.0, duration=20.0):
    if scenario not in SCENARIOS:
        raise ValueError("unknown scenario")
    if not math.isfinite(response) or response <= 0:
        raise ValueError("response must be finite and positive")
    if not math.isfinite(delay) or delay < 0:
        raise ValueError("delay must be finite and nonnegative")
    c = FollowController(steering_mode="yaw", require_geometry=True,
                         recovery_window=3, max_request_rate=.8)
    dt, focal, side = .1, 320/math.tan(math.radians(41)), .32
    x = z = heading = forward_speed = yaw_rate = 0.0
    captured, rows = [], []
    max_request = (0., 0., 0.)
    initial = None
    for seq in range(round(duration/dt)+1):
        t = round(seq*dt, 8)
        bearing = .5*math.sin(.25*t)
        distance = 2.0+.35*math.sin(.30*t)
        if scenario == "out_of_view" and 5 <= t < 9:
            bearing = 1.7  # leaves FOV; no automatic search is implemented
        tx, tz = distance*math.sin(bearing), distance*math.cos(bearing)
        dx, dz = tx-x, tz-z
        cam_x = dx*math.cos(heading)-dz*math.sin(heading)
        cam_z = dx*math.sin(heading)+dz*math.cos(heading)
        center = 320+focal*cam_x/cam_z if cam_z > .05 else -10000
        pixels = focal*side/cam_z if cam_z > .05 else 0
        in_fov = cam_z > .05 and pixels/2 < center < 640-pixels/2 and 0 < pixels < 480
        occluded = scenario in ("short_occlusion", "stop_then_return") and 5 <= t < 5.4
        found = in_fov and not occluded
        corners = ((center-pixels/2, 240-pixels/2), (center+pixels/2, 240-pixels/2),
                   (center+pixels/2, 240+pixels/2), (center-pixels/2, 240+pixels/2))
        obs = Observation(seq, t, "virtual-pinhole", 640, 480, 0, found,
                          center if found else None, 240 if found else None,
                          pixels/640 if found else None, corners if found else (),
                          "" if found else "not_found")
        if not (scenario == "frozen_frames" and 5 <= t < 6):
            captured.append(obs)
        if seq == 0:
            initial = obs
            c.observe(obs)
            assert c.calibrate(t)[0] and c.start(t)[0]
        # Latency is introduced after initialization, to exercise active flight
        # becoming stale rather than only a start refusal. A delayed frame keeps
        # its actual capture timestamp/id, including when repeatedly returned.
        frame_delay = delay if t >= 2 else 0
        eligible = [o for o in captured if o.acquired_at <= t-frame_delay+1e-9]
        if eligible:
            c.observe(eligible[-1])
        if scenario == "stop_then_return" and t == 5.2:
            c.stop()
        request = c.tick(t)
        values = (request.right, request.forward, request.yaw)
        assert all(math.isfinite(v) for v in values)
        assert request.right == 0 and abs(request.forward) <= .3 and abs(request.yaw) <= .2
        if request.state != "tracking":
            assert values == (0., 0., 0.), "inactive controller must issue all zeros"
        max_request = tuple(max(old, abs(new)) for old, new in zip(max_request, values))
        # Synthetic dynamics: 1 m/s and 2 rad/s at request 1.0 are assumptions,
        # NOT Tello speed measurements or mappings promised by the SDK.
        alpha = 1-math.exp(-dt/response)
        forward_speed += alpha*(request.forward-forward_speed)
        yaw_rate += alpha*(2*request.yaw-yaw_rate)
        heading += yaw_rate*dt
        x += math.sin(heading)*forward_speed*dt
        z += math.cos(heading)*forward_speed*dt
        observed = c.observation
        rows.append({"t": t, "scenario": scenario, "response_s": response, "delay_s": delay,
                     "state": str(request.state), "target_visible_now": found,
                     "observed_frame_id": observed.frame_id,
                     "observation_age_s": t-observed.acquired_at,
                     "request_right": request.right, "request_forward": request.forward,
                     "request_yaw": request.yaw, "virtual_forward_m_s": forward_speed,
                     "virtual_yaw_rad_s": yaw_rate, "virtual_heading_rad": heading,
                     "virtual_x_m": x, "virtual_z_m": z,
                     "image_horizontal_error": (2*center/640-1) if in_fov else None,
                     "apparent_size_error": (initial.size_ratio-pixels/640)/initial.size_ratio
                                             if in_fov else None,
                     "reason": request.reason})
    if delay > c.max_age or scenario == "frozen_frames":
        assert c.state == "frame_stale"
    if scenario == "out_of_view" and delay <= c.max_age:
        assert c.state == "target_lost"
    if scenario == "stop_then_return" and delay <= c.max_age:
        assert c.state == "stopped"
    summary = {"scenario": scenario, "response_s": response, "delay_s": delay,
               "steps": len(rows), "last_state": str(c.state),
               "peak_request_right_forward_yaw": max_request,
               "nontracking_nonzero": sum(r["state"] != "tracking" and
                    any(r[k] for k in ("request_right", "request_forward", "request_yaw")) for r in rows),
               "virtual_heading_changed": abs(heading) > .01,
               "final_visible_horizontal_error": rows[-1]["image_horizontal_error"],
               "physical_flight_verified": False, "image_detector_exercised": False}
    return rows, summary


def write_plot(rows, destination):
    """Dependency-free SVG, separate units instead of mixing request and speed."""
    panels = (("Requests (dimensionless)", .35,
               (("request_yaw", "#b54b36"), ("request_forward", "#167b9e"))),
              ("Virtual yaw response (rad/s; invented dynamics)", .45,
               (("virtual_yaw_rad_s", "#b54b36"),)),
              ("Image errors (normalized; different denominators)", 1.,
               (("image_horizontal_error", "#7349a2"), ("apparent_size_error", "#317954"))))
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="720" viewBox="0 0 1000 720">',
           '<rect width="1000" height="720" fill="#fff"/>',
           '<g font-family="Arial,sans-serif" font-size="12" fill="#222">',
           '<text x="65" y="28" font-size="17">Visible-target yaw feedback - virtual camera only</text>',
           '<text x="65" y="50">No image detection or physical flight validation. Forward speed and all state transitions are in CSV.</text>']
    for index, (title, scale, curves) in enumerate(panels):
        top, height, left, width = 90+index*200, 140, 65, 850
        svg.append(f'<text x="{left}" y="{top-12}">{title}</text>')
        for tick in (-scale, 0, scale):
            y = top+height/2-tick/scale*height/2
            svg.append(f'<path d="M {left} {y} h {width}" stroke="#ddd"/><text x="15" y="{y+4}">{tick:g}</text>')
        for tick in (0, 5, 10, 15, 20):
            x = left+tick/rows[-1]["t"]*width
            svg.append(f'<text x="{x}" y="{top+height+16}">{tick}s</text>')
        for curve_index, (key, color) in enumerate(curves):
            segments, current = [], []
            for row in rows:
                value = row[key]
                if value is None:
                    if current:
                        segments.append(current)
                        current = []
                    continue
                x = left+row["t"]/rows[-1]["t"]*width
                y = top+height/2-max(-scale, min(scale, value))/scale*height/2
                current.append(f"{x:.2f},{y:.2f}")
            if current:
                segments.append(current)
            for segment in segments:
                svg.append(f'<polyline points="{" ".join(segment)}" fill="none" stroke="{color}" stroke-width="1.5"/>')
            svg.append(f'<text x="{left+curve_index*310}" y="{top+height+35}" fill="{color}">{key}</text>')
    svg.extend(['</g>', '</svg>'])
    destination.write_text("\n".join(svg), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    summaries = []
    for scenario, response, delay in dict.fromkeys(
            [(s, .25, 0.) for s in SCENARIOS] +
            [("curved_path", r, d) for r in (.1, .25, 1.) for d in (0., .2, .4, .6)]):
        rows, summary = run_scenario(scenario, response, delay)
        name = f"{scenario}_response{response:g}_delay{delay:g}.csv"
        with (args.output/name).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        summary["trace"] = name
        summary["plot"] = name.replace(".csv", ".svg")
        write_plot(rows, args.output/summary["plot"])
        summaries.append(summary)
    (args.output/"summary.json").write_text(json.dumps({
        "scope": "Virtual pinhole feedback only; no hardware, detector, or physical calibration.",
        "runs": summaries}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
