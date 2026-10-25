"""Historical lateral virtual plant with current ArUco detection; offline only.

The plant models translation, not heading. This is a legacy lateral regression,
not validation of the current open-source yaw/PID profile or actual flight.
"""
import argparse
import csv
import hashlib
import json
import math
import time
from pathlib import Path

import cv2
import numpy as np
from r3_follow.vision import MarkerTracker
from r3_follow.mock_port import MockPort
from r3_follow.service import FollowService
from r3_follow.service_ports import Result


def render(x_error=0., size=.2, effect='clean'):
    """Ground truth describes our constructed image, never camera observations."""
    frame = np.full((240, 320, 3), 245, np.uint8)
    if effect == 'out_of_view':
        x_error = 1.5
    marker_id = 1 if effect == 'wrong_id' else 0
    side = max(8, min(200, round(size*320)))
    if effect == 'small':
        side = max(8, round(side*.4))
    left, top = 160*(1+x_error)-side/2, 120-side/2
    quad = np.float32([[left,top], [left+side-1,top],
                       [left+side-1,top+side-1], [left,top+side-1]])
    if effect == 'tilted':
        quad[1] += [-side*.30, side*.18]
        quad[2] += [-side*.30, -side*.18]
    full = bool((quad[:,0] >= 0).all() and (quad[:,0] < 320).all()
                and (quad[:,1] >= 0).all() and (quad[:,1] < 240).all())
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    marker = cv2.aruco.generateImageMarker(dictionary, marker_id, 120)
    transform = cv2.getPerspectiveTransform(np.float32([[0,0],[119,0],[119,119],[0,119]]), quad)
    gray = cv2.warpPerspective(marker, transform, (320,240), borderValue=245,
                              flags=cv2.INTER_NEAREST)
    frame = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    if effect == 'occluded':
        cv2.rectangle(frame, (max(0,round(left)), max(0,round(top))),
                      (min(319,round(left+side)), min(239,round(top+side*.6))), (245,245,245), -1)
    if effect in ('blur', 'severe_blur'):
        n = 5 if effect == 'blur' else 21
        kernel = np.zeros((n,n), np.float32)
        kernel[n//2,:] = 1/n
        frame = cv2.filter2D(frame, -1, kernel)
    if effect == 'dim':
        frame = (frame.astype(float)*.22).astype(np.uint8)
    category = ('wrong_id' if marker_id else 'out_of_view' if not full else
                'occluded' if effect == 'occluded' else 'visible_target')
    return frame, dict(category=category, effect=effect, marker_id=marker_id,
                      full_target_visible=category == 'visible_target')


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def benchmark(folder):
    """Compare default OpenCV baseline and current multiscale on identical pixels."""
    tracker = MarkerTracker()
    rows, saved = [], set()
    effects = ('clean','small','tilted','blur','severe_blur','dim','occluded','wrong_id','out_of_view')
    for effect in effects:
        for size in (.12, .2, .3):
            for x in (-.4, 0, .4):
                image, truth = render(x, size, effect)
                digest = hashlib.sha256(image.tobytes()).hexdigest()
                start = time.perf_counter()
                _, ids, _ = tracker.detector.detectMarkers(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
                baseline = ids is not None and sum(int(v)==0 for v in ids.flatten()) == 1
                baseline_ms = (time.perf_counter()-start)*1000
                start = time.perf_counter()
                obs = tracker.detect(image, len(rows), 0, 'generated')
                current_ms = (time.perf_counter()-start)*1000
                rows.append(dict(**truth, size=size, x=x, sha256=digest,
                                 baseline_found=bool(baseline), current_found=obs.found,
                                 baseline_ms=baseline_ms, current_ms=current_ms))
                if not obs.found and effect not in saved:
                    ok, encoded = cv2.imencode('.png', image)
                    if ok:
                        (folder / f'example_{effect}.png').write_bytes(encoded.tobytes())
                    saved.add(effect)
    with (folder/'detector_comparison.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f,fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(rows)
    result = []
    for effect in effects:
        group = [r for r in rows if r['effect']==effect]
        result.append(dict(effect=effect, count=len(group), category=group[0]['category'],
                           baseline_found=sum(r['baseline_found'] for r in group),
                           current_found=sum(r['current_found'] for r in group),
                           regression=sum(r['baseline_found'] and not r['current_found'] for r in group),
                           current_mean_ms=sum(r['current_ms'] for r in group)/len(group)))
    return result


class FailingPort(MockPort):
    fail_send = False
    def update_velocity(self, *args, **kwargs):
        if self.fail_send:
            return Result('failed', 'injected_send_failure')
        return super().update_velocity(*args, **kwargs)


def service_loop(case='clean', steps=100):
    now = [0.0]
    port = FailingPort(lambda: now[0])
    service = FollowService(port, monotonic=lambda: now[0],
                            wall_clock=lambda: 1800000000+now[0], recovery_window=3,
                            steering_mode='lateral', control_profile='legacy', log_directory=False)
    frame, _ = render()
    port.publish(frame, 0, 1800000000)
    service.capture_once()
    assert service.calibrate_reference().ok and service.start_follow().ok
    x = z = vx = vz = 0.
    rows = []
    ticket = other = None
    try:
        for i in range(1, steps+1):
            t = i*.1
            now[0] = t
            target_x = .6 + .25*math.sin(.5*t)
            target_z = 2.6 + .2*math.sin(.3*t)
            distance = target_z-z
            if distance <= .25:
                raise RuntimeError('virtual model invalid')
            effect = case if case in ('dim','blur','tilted','small') else 'clean'
            if case == 'loss' and 2 <= t < 3:
                effect = 'occluded'
            if case == 'wrong_id' and t >= 2:
                effect = 'wrong_id'
            image, truth = render((target_x-x)/distance, .4/distance, effect)
            if not (case == 'freeze' and 2 <= t < 4):
                port.publish(image, i, 1800000000+t)
            before = time.perf_counter()
            service.capture_once()
            detect_ms = (time.perf_counter()-before)*1000
            if i == 20:
                if case in ('handoff', 'late_completion', 'action_failed'):
                    result, ticket = service.pause_for_action()
                    assert result.ok
                    other = port.request_control('model').lease
                    service.finish_action(ticket, 'accepted')
                elif case == 'control_busy':
                    other = port.revoke_for_test('model')
                    service.control_once()
                    assert service.start_follow().reason == 'busy'
                elif case == 'send_failure':
                    port.fail_send = True
            if case == 'late_completion' and i == 25:
                service.stop_follow()
            if i == 30 and ticket is not None:
                port.release_control(other)
                service.finish_action(ticket, 'failed' if case == 'action_failed' else 'completed')
            service.control_once()
            port.watchdog_step()
            state = str(service.controller.state)
            rows.append(dict(time=t, x_error=(target_x-x)/distance,
                             size_error=(.2-.4/distance)/.2, state=state,
                             right=port.velocity[0]/100, forward=port.velocity[1]/100,
                             velocity_x=vx, velocity_z=vz, category=truth['category'],
                             found=service.controller.observation.found,
                             detection_ms=detect_ms, reason=service.last_reason))
            alpha = 1-math.exp(-.1/.25)
            vx += alpha*(2*port.velocity[0]/100-vx)
            vz += alpha*(2*port.velocity[1]/100-vz)
            x += vx*.1; z += vz*.1
        return rows, list(service.events)
    finally:
        service.close()


def main(output=None):
    cv2.setNumThreads(1)
    folder = Path(output) if output is not None else Path(__file__).resolve().parent/'logs'/'image_loop'
    folder.mkdir(parents=True, exist_ok=True)
    comparison = benchmark(folder)
    cases = ('clean','dim','blur','tilted','small','loss','wrong_id','freeze',
             'handoff','late_completion','action_failed','control_busy','send_failure')
    summaries = []
    for case in cases:
        rows, events = service_loop(case)
        with (folder/(case+'.csv')).open('w',newline='',encoding='utf-8-sig') as f:
            writer=csv.DictWriter(f,fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(rows)
        write_json(folder/(case+'_events.json'), events)
        summary=dict(case=case, frames=len(rows), final_state=rows[-1]['state'],
                     nontracking_nonzero=sum(r['state']!='tracking' and
                                            (r['right'] or r['forward']) != 0 for r in rows),
                     final_x_error=rows[-1]['x_error'], final_size_error=rows[-1]['size_error'])
        summaries.append(summary)
        print(case, summary['final_state'], flush=True)
    write_json(folder/'summary.json', dict(simulation_only=True, hardware_verified=False,
               baseline='OpenCV default detector', candidate='current R3 marker detector',
               control_profile='legacy', steering_mode='lateral',
               current_open_source_yaw_validated=False,
               experiment='historical lateral-only virtual plant regression',
               timing='Measured processing time; virtual motion clock does not include computation time',
               comparison=comparison, service_cases=summaries))
    assert all(s['nontracking_nonzero']==0 for s in summaries)
    print('Historical lateral image-loop regression complete; not current yaw/PID acceptance; no UI/camera/network.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    main(parser.parse_args().output)
