"""Reproducible control-only comparison. No cameras, windows, network or drone.

Saved v0.10 code is the baseline, not an approximation of it. Measurements below
describe requests and an invented virtual plant, not actual flight performance.
"""
import contextlib
import csv
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
from unittest.mock import patch

import cv2
from continuous_follow_sim import run, SCENARIOS, MATRIX
import continuous_follow_sim
import image_loop_lab
from r3_follow.core import FollowController, Observation
from r3_follow.service import FollowService
from r3_follow.mock_port import MockPort

BASE = Path(__file__).resolve().parent
DEST = BASE/'logs'/'stability_v011'


def load_baseline():
    modules = []
    for name in ('core', 'service'):
        spec = importlib.util.spec_from_file_location(
            'r3_follow._v010_' + name, BASE/'replay_baselines'/'control_v010'/f'{name}.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        modules.append(module)
    modules[1].FollowController = modules[0].FollowController
    return modules[0].FollowController, modules[1].FollowService


def csv_file(name, rows):
    with (DEST/name).open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)


def pulse_metrics(rows, keys=('right','forward')):
    metrics = {}
    for key in keys:
        values = [r[key] for r in rows]
        jumps = [abs(b-a) for a, b in zip(values, values[1:])]
        channels = [round(100*v) for v in values]
        metrics[key] = dict(max_abs=max(map(abs, values)),
            max_step=max(jumps, default=0), total_variation=sum(jumps),
            nonzero_channel_samples=sum(v != 0 for v in channels),
            zero_nonzero_transitions=sum((a==0)!=(b==0) for a,b in zip(channels,channels[1:])))
    return metrics


def boundary_case(cls):
    c = cls(recovery_window=3, max_request_rate=.8)
    obs_type = sys.modules[cls.__module__].Observation
    c.observe(obs_type(0, 0., 'test', 640, 480, 0, True, 320., 240., .25))
    assert c.calibrate(0)[0] and c.start(0)[0]
    rows = []
    for i in range(1, 101):
        t = i*.1
        e = .118 if i%2 else .122
        c.observe(obs_type(i,t,'test',640,480,0,True,320*(1+e),240.,.25*(1-e)))
        r = c.tick(t)
        rows.append(dict(time=t, measured_x_error=e, measured_size_error=e,
                         right=r.right, forward=r.forward, state=str(r.state)))
    return rows


class Tracker:
    def __init__(self, observation_type=Observation):
        self.observation_type = observation_type

    def detect(self, frame, frame_id, acquired_at, source):
        x, s, found = frame
        return self.observation_type(frame_id,acquired_at,source,640,480,0,found,
                           320*(1+x) if found else None,240 if found else None,
                           .25*(1-s) if found else None)


def service_case(cls):
    now = [0.]
    port = MockPort(lambda: now[0])
    controller_type = sys.modules[cls.__module__].FollowController
    obs_type = sys.modules[controller_type.__module__].Observation
    service = cls(port, tracker=Tracker(obs_type), monotonic=lambda:now[0],
                  wall_clock=lambda:1800000000+now[0], recovery_window=3)
    port.publish((0.,0.,True),0,1800000000)
    service.capture_once()
    assert service.calibrate_reference().ok
    rows = []
    ticket = None
    try:
        for i in range(1, 41):
            t = round(i*.1, 8)
            now[0] = t
            e = .8 if t < 1.5 else -.8
            found = not 2 <= t < 2.2
            port.publish((e,e,found),i,1800000000+t)
            service.capture_once()
            action = ''
            if i == 1:
                assert service.start_follow().ok
                action = 'start'
            elif i == 30:
                result, ticket = service.pause_for_action()
                assert result.ok
                action = 'handoff_pause'
            elif i == 32:
                assert service.finish_action(ticket,'completed').ok
                action = 'handoff_completed'
            elif i == 38:
                service.stop_follow()
                action = 'explicit_stop'
            service.control_once()
            rows.append(dict(time=t, action=action, found=found,
                state=str(service.controller.state), right=port.velocity[0]/100,
                forward=port.velocity[1]/100))
        return rows
    finally:
        service.close()


def svg_compare(name, pairs, key='right'):
    # One standalone standard SVG plot. It is saved, never opened as a window.
    lines = ['<svg xmlns="http://www.w3.org/2000/svg" width="960" height="400">',
        '<rect width="960" height="400" fill="white"/>',
        f'<text x="50" y="25" font-size="18">{name}: {key} request (not physical velocity)</text>',
        '<path d="M50 50V340H920 M50 195H920" fill="none" stroke="#aaa"/>',
        '<text x="4" y="55">+0.3</text><text x="20" y="200">0</text><text x="4" y="340">-0.3</text>']
    for j,(label, rows) in enumerate(pairs.items()):
        color = ('#c35b34','#236fa1')[j]
        points = ' '.join(f"{50+870*r['time']/rows[-1]['time']:.2f},{195-145*r[key]/.3:.2f}" for r in rows)
        lines.extend([f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>',
            f'<text x="{60+250*j}" y="370" fill="{color}">{label}</text>'])
    lines.append('</svg>')
    (DEST/f'{name}.svg').write_text('\n'.join(lines),encoding='utf-8')


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    cv2.setNumThreads(1)
    baseline_c, baseline_s = load_baseline()
    report = dict(simulation_only=True, physical_flight_verified=False,
                  detector_changed=False, recovery_policy_changed=False,
                  limits='Fixed measurement sequences and invented virtual dynamics; no new camera inputs.')
    with contextlib.ExitStack() as stack:
        for target in ('socket.socket','cv2.VideoCapture','tkinter.Tk'):
            stack.enter_context(patch(target,side_effect=AssertionError('offline comparison only')))
        report['fixed_input'] = {}
        for name, function, classes in (
            ('boundary', boundary_case, (baseline_c,FollowController)),
            ('service', service_case, (baseline_s,FollowService))):
            pairs = {}
            for label, cls in zip(('baseline','candidate'),classes):
                rows = function(cls)
                pairs[label] = rows
                csv_file(f'{name}_{label}.csv', rows)
                report['fixed_input'].setdefault(name,{})[label] = pulse_metrics(rows)
            svg_compare(name,pairs)
        report['virtual_matrix'] = []
        for case in (*SCENARIOS,*MATRIX):
            entry = {'case':case.name}
            for label, cls in (('baseline',baseline_c),('candidate',FollowController)):
                with patch.object(continuous_follow_sim,'FollowController',cls):
                    with patch.object(continuous_follow_sim,'Observation',sys.modules[cls.__module__].Observation):
                        rows, summary = run(case)
                csv_file(f'{case.name}_{label}.csv',rows)
                summary.update(final_state=rows[-1]['state'], request_metrics=pulse_metrics(rows))
                entry[label] = summary
            report['virtual_matrix'].append(entry)
        report['rendered_service'] = []
        for case in ('clean','dim','blur','tilted','small','loss','wrong_id','freeze',
                     'handoff','late_completion','action_failed','control_busy','send_failure'):
            entry = {'case':case, 'comparison':'Closed-loop images diverge with motion; not a fixed-image detector comparison.'}
            for label, cls in (('baseline',baseline_s),('candidate',FollowService)):
                # The detector uses the live Observation dataclass. Adapt only
                # its type for the historical controller; all fields unchanged.
                controller_type = sys.modules[cls.__module__].FollowController
                old_observation = sys.modules[controller_type.__module__].Observation
                with patch.object(image_loop_lab,'FollowService',cls), \
                     patch('r3_follow.vision.Observation',old_observation):
                    rows, events = image_loop_lab.service_loop(case,steps=100)
                csv_file(f'rendered_{case}_{label}.csv',rows)
                entry[label] = dict(final_state=rows[-1]['state'],
                    nontracking_nonzero=sum(r['state']!='tracking' and (r['right']!=0 or r['forward']!=0) for r in rows),
                    request_metrics=pulse_metrics(rows))
            report['rendered_service'].append(entry)
    paths = [BASE/'r3_follow'/'core.py', BASE/'r3_follow'/'service.py',
             *sorted((BASE/'replay_baselines'/'control_v010').glob('*.py'))]
    report['sha256'] = {str(p.relative_to(BASE)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report['checks'] = dict(
        matrix_finals_match=all(e['baseline']['final_state']==e['candidate']['final_state'] for e in report['virtual_matrix']),
        matrix_zero_while_inactive=all(e['candidate']['nontracking_nonzero']==0 for e in report['virtual_matrix']),
        matrix_bounded=all(math.isfinite(e['candidate']['max_request']) and e['candidate']['max_request']<=.3 for e in report['virtual_matrix']),
        rendered_finals_match=all(e['baseline']['final_state']==e['candidate']['final_state'] for e in report['rendered_service']),
        rendered_zero_while_inactive=all(e['candidate']['nontracking_nonzero']==0 for e in report['rendered_service']))
    (DEST/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report['fixed_input'],indent=2))
    for entry in report['virtual_matrix']:
        old, new = entry['baseline'], entry['candidate']
        print(entry['case'], 'tail error:',
              round(old['tail_mean_abs_x'],4),round(new['tail_mean_abs_x'],4),
              round(old['tail_mean_abs_size'],4),round(new['tail_mean_abs_size'],4),new['final_state'])
    for entry in report['rendered_service']:
        print('rendered',entry['case'],entry['baseline']['final_state'],entry['candidate']['final_state'])
    print('Evidence:',DEST)
    if not all(report['checks'].values()):
        raise SystemExit('Comparison invariants failed; inspect report before using candidate')


if __name__ == '__main__':
    main()
