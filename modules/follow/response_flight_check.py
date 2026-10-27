"""Actual ArUco pixels -> actual detector/PID -> invented flight response.

No hardware, no camera device, no SDK import. World meters and speed mappings are
ASSUMPTIONS, not Tello calibration or a promise of a usable following distance.
Virtual 10 Hz capture/processing; measured detector cost is recorded separately,
not inserted into virtual time. No texture/noise/exposure/rolling shutter model.
"""
import argparse
import csv
import hashlib
import inspect
import json
import math
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

W, H = 640, 480
F = 320 / math.tan(math.radians(35))  # Assumed horizontal FOV 70 deg, NOT Tello spec.
DT = .1


def render(drone, target, angle, visible=True, marker_id=0):
    image = np.full((H, W, 3), 210, np.uint8)
    x, y, z, heading = drone
    tx, ty, tz = target
    tag_m = .24
    paper_m = tag_m * 4 / 3
    # Marker texture: 300 px tag with a 50 px quiet zone on each side.
    texture = np.full((400, 400), 255, np.uint8)
    texture[50:350, 50:350] = cv2.aruco.generateImageMarker(
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), marker_id, 300)
    projected = []
    for a, b in ((-1, 1), (1, 1), (1, -1), (-1, -1)):
        dx = tx + a * paper_m / 2 * math.cos(angle) - x
        dy = ty + a * paper_m / 2 * math.sin(angle) - y
        dz = tz + b * paper_m / 2 - z
        right = dx * math.cos(heading) - dy * math.sin(heading)
        depth = dx * math.sin(heading) + dy * math.cos(heading)
        if depth <= .05: return image, False
        projected.append((W / 2 + F * right / depth, H / 2 - F * dz / depth))
    quad = np.float32(projected)
    front = math.cos(angle - heading) > .02
    intersects = quad[:, 0].max() >= 0 and quad[:, 0].min() < W and quad[:, 1].max() >= 0 and quad[:, 1].min() < H
    if visible and front and intersects:
        src = np.float32(((0, 0), (399, 0), (399, 399), (0, 399)))
        matrix = cv2.getPerspectiveTransform(src, quad)
        warped = cv2.warpPerspective(texture, matrix, (W, H), flags=cv2.INTER_NEAREST, borderValue=210)
        image = cv2.cvtColor(warped, cv2.COLOR_GRAY2BGR)
    return image, bool(visible and front and intersects)


def desired_target(case, t):
    x, y, z, angle, visible, marker_id = 0., 2., 1.2, 0., True, 0
    if case in ('walk_slow', 'walk_fast', 'curve'):
        speed = .7 if case == 'walk_fast' else .25
        y += speed * max(0, min(t - 2, 8))
        if case == 'curve': x = .45 * math.sin(.55 * max(0, t - 2))
    elif case == 'turn45_while_away':
        y += .25 * max(0, min(t - 2, 8))
        angle = math.radians(45) if 3 <= t < 9 else 0.
    elif case == 'turn180_return': angle = math.pi if 3 <= t < 8 else 0.
    elif case in ('occlude1', 'occlude10', 'explicit_stop_return'):
        visible = not (3 <= t < (13 if case == 'occlude10' else 4))
    elif case == 'height_change': z += .4 * min(1., max(0, (t - 2) / 2))
    elif case in ('show_item_approach', 'show_item_paused'):
        y -= .6 * min(1., max(0, (t - 3) / 2))
    elif case == 'wrong_id': marker_id = 1 if 3 <= t < 5 else 0
    return (x, y, z), angle, visible, marker_id


def run_scene(root, output, version, case, gain=2., recovery=3., duration=15.,
              mode="apparent_pixels", response=.25, delay=0., steering='yaw'):
    from r3_follow.core import FollowController
    from r3_follow.vision import MarkerTracker
    kwargs = dict(target_id=0, recovery_window=recovery, require_geometry=True,
                  max_request_rate=.8, steering_mode=steering, control_profile='openpose_pid')
    parameters = inspect.signature(FollowController).parameters
    config_file = ('configs/aircraft_A_ID0.json' if steering=='yaw' else
                   'configs/offline_candidates/aircraft_A_ID0_'+steering+'.json')
    config = json.loads((root / 'modules/follow' / config_file).read_text())
    for field in ('yaw_limit', 'forward_limit', 'max_request_rate', 'vertical_follow'):
        if field in config and field in parameters: kwargs[field] = config[field]
    kwargs['distance_mode'] = mode
    if mode == 'pose':
        from r3_follow.pose_depth import PoseDepth
        calibration = output / 'SYNTHETIC_camera_not_for_flight.json'
        calibration.write_text(json.dumps(dict(schema='r3_camera_calibration_v1',
            camera_id='synthetic-camera', image_size=[W,H],
            camera_matrix=[[F,0,W/2],[0,F,H/2],[0,0,1]], dist_coeffs=[0,0,0,0,0],
            rms_px=0., source='invented_intrinsics_not_for_hardware')),encoding='utf8')
        kwargs['pose_estimator'] = PoseDepth(calibration,.24,camera_id='synthetic-camera')
    c = FollowController(**kwargs)
    tracker = MarkerTracker()  # Unlimited offline decoder; timings reported separately.
    drone = np.array([0., 0., 1.2, 0.])
    velocity = np.zeros(4)
    rows, timings = [], []
    first = None
    pending = []
    for seq in range(round(duration / DT) + 1):
        t = round(seq * DT, 6)
        target, angle, visible, marker_id = desired_target(case, t)
        frame, geometric_visible = render(drone, target, angle, visible, marker_id)
        if not (case == 'frozen_then_fresh' and 3 <= t < 4):
            pending.append((t,seq,frame))
        available = [p for p in pending if p[0]+delay <= t+1e-9 or seq == 0]
        if available:
            capture,frame_id,pixels = available[-1]
            pending = [p for p in pending if p[1] > frame_id]
            started = time.perf_counter()
            obs = tracker.detect(pixels, frame_id, capture, source='virtual_camera')
            timings.append((time.perf_counter() - started) * 1000)
            c.observe(obs)
        if seq == 0:
            assert obs.found, 'renderer must provide readable initial marker'
            assert c.calibrate(t)[0] and c.start(t)[0]
            first = c.reference_size
        if t == 3 and case == 'show_item_paused': c.pause()
        if t == 3.2 and case == 'explicit_stop_return': c.stop()
        request = c.tick(t)
        channels = np.array((request.right, request.forward, getattr(request, 'up', 0.), request.yaw))
        assert all(math.isfinite(v) and abs(v) <= 1 for v in channels)
        if request.state != 'tracking': assert not np.any(channels), 'nontracking command must be zero'
        # Invented first-order response; RC 30 maps to 0.3/0.6/0.9 m/s for gain1/2/3.
        # Same mapping for translations, yaw request1.0 -> 2rad/s, response0.25s.
        setpoint = channels * np.array((gain, gain, gain, 2.))
        velocity += (1 - math.exp(-DT / response)) * (setpoint - velocity)
        dx = (math.cos(drone[3]) * velocity[0] + math.sin(drone[3]) * velocity[1]) * DT
        dy = (-math.sin(drone[3]) * velocity[0] + math.cos(drone[3]) * velocity[1]) * DT
        drone += np.array((dx, dy, velocity[2] * DT, velocity[3] * DT))
        status = c.status()
        row = dict(t=t, state=str(request.state), found=obs.found, marker_id=marker_id,
                   geometric_visible=geometric_visible, frame_age=t-obs.acquired_at,
                   rc_right=channels[0]*100, rc_forward=channels[1]*100,
                   rc_up=channels[2]*100, rc_yaw=channels[3]*100,
                   virtual_forward_m_s=velocity[1], virtual_up_m_s=velocity[2],
                   virtual_yaw_rad_s=velocity[3], target_x=target[0], target_y=target[1], target_z=target[2],
                   drone_x=drone[0], drone_y=drone[1], drone_z=drone[2], drone_heading=drone[3],
                   virtual_distance_m=float(np.linalg.norm(np.array(target)-drone[:3])),
                   cx=obs.center_x, cy=obs.center_y, size_ratio=obs.size_ratio,
                   size_error=None if obs.size_ratio is None else first-obs.size_ratio,
                   forward_allowed=status['distance_measurement']['forward_allowed'], reason=request.reason)
        rows.append(row)
        if t in (0., 3., 5., 9., 14.) and gain == 2. and recovery == 3.:
            image_name = output / 'samples' / f'{case}_response{response:g}_delay{delay:g}_{t:g}s.png'
            image_name.parent.mkdir(exist_ok=True)
            cv2.imwrite(str(image_name), frame)
    name = f'{case}_gain{gain:g}_wait{recovery:g}_response{response:g}_delay{delay:g}'
    with (output / (name + '.csv')).open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=rows[0]); writer.writeheader(); writer.writerows(rows)
    active = [r for r in rows if r['state'] == 'tracking' and r['found']]
    summary = dict(case=case, version=version, assumed_rc_gain=gain, recovery_s=recovery,
        steps=len(rows), states=dict(Counter(r['state'] for r in rows)), last_state=rows[-1]['state'],
        detected_fraction=sum(r['found'] for r in rows)/len(rows),
        visible_tracking_depth_block_fraction=sum(not r['forward_allowed'] for r in active)/max(1,len(active)),
        visible_tracking_all_zero_fraction=sum(all(r[k]==0 for k in ('rc_right','rc_forward','rc_up','rc_yaw')) for r in active)/max(1,len(active)),
        peak_rc={k:max(abs(r[k]) for r in rows) for k in ('rc_right','rc_forward','rc_up','rc_yaw')},
        final_virtual_distance_m=rows[-1]['virtual_distance_m'], final_virtual_z=rows[-1]['drone_z'],
        detected_but_latched_after_return=sum(r['found'] and r['state'] in ('target_lost','frame_stale') for r in rows),
        detector_ms_p50=statistics.median(timings), detector_ms_max=max(timings),
        max_virtual_distance_m=max(r['virtual_distance_m'] for r in rows),
        min_virtual_distance_m=min(r['virtual_distance_m'] for r in rows),
        final_depth_error_virtual_m=rows[-1]['virtual_distance_m']-2.,
        mean_absolute_depth_error_virtual_m=statistics.mean(abs(r['virtual_distance_m']-2.) for r in rows),
        response_assumed_s=response, total_image_delay_s=delay, distance_mode=mode,
        no_flight_validation=True, csv=name+'.csv',
        controller_kwargs={k:v for k,v in kwargs.items() if k != 'pose_estimator'})
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main():
    from unittest.mock import patch
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--mode',choices=['apparent_pixels','relative_size','pose'],required=True)
    p.add_argument('--matrix',action='store_true')
    p.add_argument('--steering',choices=['yaw','hybrid','lateral'],default='yaw')
    args=p.parse_args()
    if sys.version_info[:2] != (3,11):raise SystemExit('Use Python 3.11')
    root=Path(__file__).resolve().parents[2]
    args.output.mkdir(parents=True,exist_ok=True);cv2.setNumThreads(1)
    scenarios=['walk_slow','walk_fast','curve','turn45_while_away','turn180_return',
               'occlude1','occlude10','height_change','show_item_approach','show_item_paused',
               'wrong_id','frozen_then_fresh','explicit_stop_return']
    results=[]
    with patch('socket.socket',side_effect=AssertionError('Network forbidden')), \
         patch('cv2.VideoCapture',side_effect=AssertionError('Camera forbidden')), \
         patch('cv2.imshow',side_effect=AssertionError('Desktop forbidden')):
        for case in scenarios:
            results.append(run_scene(root,args.output,'r3-4f58686-braking1',case,mode=args.mode,steering=args.steering))
        results.append(run_scene(root,args.output,'r3-4f58686-braking1','occlude10',mode=args.mode,recovery=30,steering=args.steering))
        if args.matrix:
            for gain in (1.,2.,3.):
                for response in (.1,.25,1.):
                    for delay in (0.,.2,.4,.6):
                        results.append(run_scene(root,args.output,'r3-4f58686-braking1','walk_slow',
                            gain=gain,response=response,delay=delay,mode=args.mode,steering=args.steering))
    report=dict(python=sys.version,opencv=cv2.__version__,mode=args.mode,steering=args.steering,scenarios=results,
        assumptions=dict(marker_side_virtual_m=.24,initial_depth_virtual_m=2.,
            rc30_virtual_m_s='0.3/0.6/0.9 for assumed gain 1/2/3',camera_hfov_assumed_deg=70.,
            actual_marker_pixels=True,virtual_capture_hz=10.,calibration='synthetic only',
            detector_cpu_not_added_to_virtual_time=True,delay_injected_explicitly=True),
        hardware_verified=False,camera_opened=False)
    (args.output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')

if __name__=='__main__':main()
