"""Deterministic, headless closed-loop experiment. No camera or drone imports.

Distances are virtual units. Pinhole projection assumes a front-facing flat tag;
first-order velocity response is invented, not a Tello calibration.
"""
import csv
import json
import math
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

from r3_follow.core import FollowController, Observation, State


@dataclass(frozen=True)
class Scenario:
    name: str
    duration: float = 24.0
    delay: float = 0.0
    response_time: float = .25
    motion: bool = False
    noise: float = 0.0
    loss_start: float = -1.0
    loss_end: float = -1.0
    freeze_start: float = -1.0
    freeze_end: float = -1.0
    stop_at: float = -1.0
    profile: str = 'normal'
    irregular_capture: bool = False


SCENARIOS = (
    Scenario('stationary'),
    Scenario('moving', motion=True),
    Scenario('delay_200ms', motion=True, delay=.2),
    Scenario('position_noise', noise=.025),
    Scenario('brief_loss', motion=True, loss_start=5, loss_end=6),
    Scenario('long_loss', motion=True, loss_start=5, loss_end=9),
    Scenario('frozen_frames', motion=True, freeze_start=5, freeze_end=7),
    Scenario('explicit_stop', motion=True, stop_at=5),
    Scenario('delay_600ms', delay=.6),
    Scenario('slow_response', motion=True, response_time=1.0),
    Scenario('deadzone_jitter', profile='deadzone'),
    Scenario('sudden_reversal', profile='reversal'),
    Scenario('rapid_retreat', profile='retreat'),
    Scenario('irregular_capture', motion=True, irregular_capture=True),
    Scenario('delay_and_loss', motion=True, delay=.2, loss_start=5, loss_end=6),
)

MATRIX = tuple(Scenario(f'matrix_tau{tau}_delay{delay}', motion=True,
                       response_time=tau, delay=delay)
               for tau in (.1, .25, 1.) for delay in (0., .2, .4, .6))


def run(scenario, ramp=.8):
    dt = .05
    controller = FollowController(recovery_window=3, max_request_rate=ramp)
    controller.observe(Observation(0, 0, 'virtual', 640, 480, 0, True, 320, 240, .2))
    controller.calibrate(0)
    controller.start(0)
    camera_x = camera_z = vx = vz = 0.0
    pending = deque()
    rows = []
    stop_sent = False
    for step in range(1, round(scenario.duration / dt) + 1):
        t = round(step * dt, 8)
        target_x = .7 + (.35 * math.sin(.35 * t) if scenario.motion else 0)
        target_z = 2.8 + (.25 * math.sin(.25 * t) if scenario.motion else 0)
        if scenario.profile == 'deadzone':
            target_z = 2/.88
            target_x = .12*target_z
        elif scenario.profile == 'reversal':
            target_x = .7 if t < 5 else -.7
        elif scenario.profile == 'retreat':
            target_z = 2.8 + .8 * max(0, min(t-4, 5))
        distance = target_z - camera_z
        if distance <= .25:
            raise RuntimeError('Virtual camera crossed near-plane; model invalid')
        x_error = (target_x - camera_x) / distance
        size = .4 / distance  # reference .2 corresponds to virtual separation 2
        capture = (step % 8 in (0, 1, 4)) if scenario.irregular_capture else step % 2 == 0
        if capture:  # nominal 10 Hz, control ticks at 20 Hz
            frozen = scenario.freeze_start <= t < scenario.freeze_end
            if not frozen:
                measured_x = x_error + scenario.noise * math.sin(17 * t)
                measured_size = size * (1 + scenario.noise * math.sin(13 * t))
                if scenario.profile == 'deadzone':
                    measured_x += .004 * math.sin(19*t)
                    measured_size *= 1 + .004 * math.cos(19*t)
                found = (abs(measured_x) < 1 and
                         not scenario.loss_start <= t < scenario.loss_end)
                obs = Observation(step, t, 'virtual', 640, 480, 0, found,
                                  320 * (1 + measured_x) if found else None,
                                  240 if found else None,
                                  measured_size if found else None)
                pending.append((t + scenario.delay, obs))
        while pending and pending[0][0] <= t + 1e-9:
            controller.observe(pending.popleft()[1])
        if scenario.stop_at >= 0 and t >= scenario.stop_at and not stop_sent:
            controller.stop()
            stop_sent = True
        request = controller.tick(t)
        rows.append(dict(time=t, x_error=x_error, size_error=(.2-size)/.2,
                         distance=distance, right=request.right, forward=request.forward,
                         velocity_x=vx, velocity_z=vz, state=str(controller.state),
                         reason=request.reason))
        # Zero request does NOT imply instantaneous physical stop.
        alpha = 1 - math.exp(-dt / scenario.response_time)
        vx += alpha * (2 * request.right - vx)
        vz += alpha * (2 * request.forward - vz)
        camera_x += vx * dt
        camera_z += vz * dt
    tail = rows[-100:]
    def reversals(axis):
        signs = [1 if r[axis] > 0 else -1 for r in rows if r[axis] != 0]
        return sum(a != b for a, b in zip(signs, signs[1:]))
    transitions = [r for i, r in enumerate(rows)
                   if i == 0 or r['state'] != rows[i-1]['state']]
    summary = dict(scenario=asdict(scenario), ramp=ramp,
                   tail_mean_abs_x=sum(abs(r['x_error']) for r in tail)/len(tail),
                   tail_mean_abs_size=sum(abs(r['size_error']) for r in tail)/len(tail),
                   max_request=max(max(abs(r['right']), abs(r['forward'])) for r in rows),
                   right_direction_reversals=reversals('right'),
                   forward_direction_reversals=reversals('forward'),
                   min_x_error=min(r['x_error'] for r in rows),
                   min_size_error=min(r['size_error'] for r in rows),
                   nontracking_nonzero=sum(r['state'] != State.TRACKING and
                                          (r['right'] != 0 or r['forward'] != 0) for r in rows),
                   transitions=[dict(time=r['time'], state=r['state'], reason=r['reason'])
                                for r in transitions])
    return rows, summary


def main():
    folder = Path(__file__).resolve().parent / 'logs' / 'continuous_sim'
    folder.mkdir(parents=True, exist_ok=True)
    summaries = []
    for scenario in (*SCENARIOS, *MATRIX):
        rows, summary = run(scenario)
        with (folder / (scenario.name + '.csv')).open('w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        summaries.append(summary)
        # Standalone SVG time series; never opens a desktop window.
        series = [('x_error', '#2266bb'), ('size_error', '#d97706'),
                  ('right', '#16803a'), ('forward', '#9333aa')]
        extent = max(.5, math.ceil(10*max(abs(r[key]) for r in rows for key, _ in series))/10)
        svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="960" height="420" viewBox="0 0 960 420">',
               '<rect width="960" height="420" fill="white"/>',
               f'<text x="45" y="25" font-size="18">{scenario.name} — virtual model only</text>',
               '<path d="M45 60 V350 H920 M45 205 H920" stroke="#999" fill="none"/>',
               '<text x="45" y="380">0 seconds</text><text x="830" y="380">24 seconds</text>',
               f'<text x="5" y="65">+{extent:g}</text><text x="5" y="210">0</text><text x="5" y="350">-{extent:g}</text>']
        for index, (key, color) in enumerate(series):
            points = ' '.join(f"{45+875*r['time']/scenario.duration:.2f},{205-145*r[key]/extent:.2f}" for r in rows)
            svg.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.6"/>')
            svg.append(f'<text x="{50+220*index}" y="405" fill="{color}">{key}</text>')
        svg.append('</svg>')
        (folder / (scenario.name + '.svg')).write_text('\n'.join(svg), encoding='utf-8')
    report = {'simulation_only': True,
              'assumptions': 'Virtual units; frontal tag; invented first-order dynamics; no image detector, hardware, wind or rotation.',
              'results': summaries}
    (folder / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['连续跟随模拟结果（虚拟模型，不是实机验收）',
             '位置和大小误差均为比例，最后5秒取绝对值均值；不是米数。',
             '每个场景24秒模拟时间；0.05秒计算，0.1秒采集。',
             '请求归零后虚拟速度仍按响应时间衰减，不能把请求归零说成飞机立即静止。', '']
    for s in summaries:
        lines.append(f"{s['scenario']['name']}: 位置误差 {s['tail_mean_abs_x']:.4f}；大小误差 {s['tail_mean_abs_size']:.4f}；非跟随状态非零请求 {s['nontracking_nonzero']}")
        lines.append('  ' + ' → '.join(f"{x['time']:.2f}s {x['state']}" for x in s['transitions']))
    (folder / '结果说明.txt').write_text('\n'.join(lines), encoding='utf-8-sig')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
