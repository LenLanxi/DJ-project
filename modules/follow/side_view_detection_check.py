"""Paired detection benchmark; generated pixels only, no camera or network.

Synthetic pose is test ground truth only. It is never passed to MarkerTracker.
The probe compares two last-resort candidate extractors before source changes.
"""
import argparse
import csv
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import time
from unittest.mock import patch

import cv2
import numpy as np

BASE = Path(__file__).resolve().parent
DICTIONARY = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)


def generate(rng, marker_id=0, *, occluded=False, duplicate=False):
    side = float(rng.uniform(58, 145))
    yaw = float(rng.uniform(40, 79) * rng.choice([-1, 1]))
    pitch = float(rng.uniform(-20, 20))
    roll = float(rng.uniform(-65, 65))
    a, b, c = map(math.radians, (yaw, pitch, roll))
    ry = np.array([[math.cos(a), 0, math.sin(a)], [0, 1, 0],
                   [-math.sin(a), 0, math.cos(a)]])
    rx = np.array([[1, 0, 0], [0, math.cos(b), -math.sin(b)],
                   [0, math.sin(b), math.cos(b)]])
    rz = np.array([[math.cos(c), -math.sin(c), 0],
                   [math.sin(c), math.cos(c), 0], [0, 0, 1]])
    xyz = np.array([[-.2, -.2, 0], [.2, -.2, 0], [.2, .2, 0], [-.2, .2, 0]])
    xyz = xyz @ (rz @ ry @ rx).T + [0, 0, 240/side]
    center = [180 + rng.uniform(-12, 12), 130 + rng.uniform(-12, 12)]
    points = (600 * xyz[:, :2] / xyz[:, 2:] + center).astype(np.float32)
    marker = cv2.aruco.generateImageMarker(DICTIONARY, marker_id, 240)
    if occluded:
        marker[80:160, :] = 235
    matrix = cv2.getPerspectiveTransform(
        np.float32([[0, 0], [239, 0], [239, 239], [0, 239]]), points)
    image = cv2.warpPerspective(marker, matrix, (360, 260), borderValue=235)
    if duplicate:
        # Exact same difficult target twice, both inside the image.
        left = cv2.warpAffine(image, np.float32([[1, 0, -80], [0, 1, 0]]),
                              (360, 260), borderValue=235)
        right = cv2.warpAffine(image, np.float32([[1, 0, 80], [0, 1, 0]]),
                               (360, 260), borderValue=235)
        image = np.minimum(left, right)
    blur = int(rng.choice([1, 1, 3, 3, 5, 7]))
    direction = int(rng.integers(0, 3))
    kernel = np.zeros((blur, blur), np.float32)
    if direction == 0:
        kernel[blur//2, :] = 1/blur
    elif direction == 1:
        kernel[:, blur//2] = 1/blur
    else:
        np.fill_diagonal(kernel, 1/blur)
    image = cv2.filter2D(image, -1, kernel)
    brightness = rng.uniform(.5, 1.05)
    gradient = np.linspace(rng.uniform(.7, 1), rng.uniform(.7, 1), 360)[None, :]
    noise = float(rng.uniform(0, 3))
    image = np.clip(image * brightness * gradient + rng.normal(0, noise, image.shape),
                    0, 255).astype(np.uint8)
    return image, points, dict(side=side, yaw=yaw, pitch=pitch, roll=roll,
                               blur=blur, direction=direction, noise=noise,
                               marker_id=marker_id)


def load_baseline():
    path = BASE/'logs/side_view_v014/baseline/vision.py'
    spec = importlib.util.spec_from_file_location('r3_follow._baseline013', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MarkerTracker


def probe_detector(kind):
    p = cv2.aruco.DetectorParameters()
    p.errorCorrectionRate = 0.0
    p.perspectiveRemovePixelPerCell = 8
    p.perspectiveRemoveIgnoredMarginPerCell = .25
    if kind == 'edge_quad':
        p.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_APRILTAG
    else:
        p.adaptiveThreshWinSizeMax = 63
        p.adaptiveThreshWinSizeStep = 4
        p.polygonalApproxAccuracyRate = .02
        p.minCornerDistanceRate = .025
        p.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_CONTOUR
    return cv2.aruco.ArucoDetector(DICTIONARY, p)


def run(args):
    from r3_follow.vision import MarkerTracker
    old, new = load_baseline()(), MarkerTracker()
    probes = {k: probe_detector(k) for k in ('edge_quad', 'thin_contour')}
    rng = np.random.default_rng(args.seed)
    rows = []
    saved = {}
    args.output.mkdir(parents=True, exist_ok=False)
    count = args.positives + args.negatives
    for i in range(count):
        if i < args.positives:
            kind = 'target'
            image, points, meta = generate(rng)
        else:
            k = (i - args.positives) % 6
            kind = ['wrong_id', 'noise', 'rectangles', 'occluded', 'duplicate', 'stripes'][k]
            points = None
            meta = {}
            if kind in ('wrong_id', 'occluded', 'duplicate'):
                image, _, meta = generate(rng, int(rng.integers(1, 50)) if k == 0 else 0,
                                          occluded=k == 3, duplicate=k == 4)
            elif kind == 'noise':
                image = rng.integers(0, 256, (260, 360), np.uint8)
            elif kind == 'rectangles':
                image = np.full((260, 360), 235, np.uint8)
                for _ in range(12):
                    x, y = rng.integers(5, 180, 2)
                    w, h = rng.integers(5, 120, 2)
                    cv2.rectangle(image, (int(x), int(y)), (int(x+w), int(y+h)),
                                  int(rng.integers(0, 256)), 2)
            else:
                yy, xx = np.indices((260, 360))
                image = (((xx + yy//2)//int(rng.integers(2, 18))) % 2 * 200 + 25).astype(np.uint8)
        observations, times = {}, {}
        for name, tracker in ((('old', old), ('new', new)) if i % 2 else
                               (('new', new), ('old', old))):
            begin = time.perf_counter()
            observations[name] = tracker.detect(image, i, float(i), source='synthetic-side-view')
            times[name] = (time.perf_counter() - begin) * 1000
        a, b = observations['old'], observations['new']
        row = dict(index=i, kind=kind, old_found=a.found, new_found=b.found,
                   old_ms=times['old'], new_ms=times['new'], method=new.last_diagnostics['detector_method'],
                   wrong_position=bool(b.found and points is not None and
                                       np.linalg.norm([b.center_x-points[:, 0].mean(),
                                                       b.center_y-points[:, 1].mean()]) >= 8),
                   new_reason=b.reason, image_sha256=hashlib.sha256(image.tobytes()).hexdigest())
        if args.probe:
            for name, detector in probes.items():
                begin = time.perf_counter()
                corners, ids, _ = detector.detectMarkers(image)
                matches = [] if ids is None else [q[0] for q, mid in zip(corners, ids.flatten()) if mid == 0]
                row[name+'_found'] = len(matches) == 1
                row[name+'_ms'] = (time.perf_counter()-begin)*1000
                row[name+'_wrong_position'] = bool(len(matches) == 1 and points is not None and
                                                    np.linalg.norm(matches[0].mean(0)-points.mean(0)) >= 8)
        cat = ('rescued' if b.found and not a.found else 'regression' if a.found and not b.found
               else 'negative_hit' if kind != 'target' and b.found else 'both_missing' if not b.found else 'both_found')
        if args.probe and not a.found and row['edge_quad_found']:
            cat = 'probe_rescue'
        key = kind+'_'+cat
        if cat not in ('both_found', 'both_missing') and saved.get(key, 0) < 8:
            saved[key] = saved.get(key, 0)+1
            stem = f'{key}_{i:04d}'
            (args.output/(stem+'.png')).write_bytes(cv2.imencode('.png', image)[1].tobytes())
            (args.output/(stem+'.json')).write_text(json.dumps(dict(row=row, parameters=meta,
                expected_corners=None if points is None else points.tolist())), encoding='utf-8')
        rows.append(row)
        if (i+1) % 200 == 0:
            print(f'{i+1}/{count}', flush=True)
    groups = {}
    for kind in sorted({r['kind'] for r in rows}):
        rr = [r for r in rows if r['kind'] == kind]
        g = dict(count=len(rr), old_found=sum(r['old_found'] for r in rr),
                 new_found=sum(r['new_found'] for r in rr),
                 rescues=sum(r['new_found'] and not r['old_found'] for r in rr),
                 regressions=sum(r['old_found'] and not r['new_found'] for r in rr),
                 wrong_positions=sum(r['wrong_position'] for r in rr),
                 old_ms_p95=float(np.percentile([r['old_ms'] for r in rr], 95)),
                 new_ms_p95=float(np.percentile([r['new_ms'] for r in rr], 95)))
        if args.probe:
            for name in probes:
                g[name+'_rescues'] = sum(r[name+'_found'] and not r['old_found'] for r in rr)
                g[name+'_found'] = sum(r[name+'_found'] for r in rr)
                g[name+'_wrong_positions'] = sum(r[name+'_wrong_position'] for r in rr)
        groups[kind] = g
    report = dict(seed=args.seed, python=sys.version, opencv=cv2.__version__, numpy=np.__version__,
                   camera_opened=False, desktop_opened=False, hardware_connected=False,
                   synthetic_only=True, groups=groups, baseline_sha256=hashlib.sha256(
                       (BASE/'logs/side_view_v014/baseline/vision.py').read_bytes()).hexdigest(),
                   candidate_sha256=hashlib.sha256((BASE/'r3_follow/vision.py').read_bytes()).hexdigest())
    report['limits'] = ('Generated images only, not raw webcam pixels. Counts are detection results, '
                        'not flight success. A partially decoded duplicate pair can remain an '
                        'existing limitation; report duplicate hits separately.')
    report['acceptance'] = dict(
        no_lost_previous_target_hits=groups['target']['regressions'] == 0,
        no_wrong_target_positions=groups['target']['wrong_positions'] == 0,
        no_new_negative_hits=all(g['rescues'] == 0 for k, g in groups.items() if k != 'target'))
    (args.output/'summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    with (args.output/'frames.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(groups, indent=2), flush=True)
    if not args.probe and not all(report['acceptance'].values()):
        raise SystemExit('Paired acceptance failed; inspect summary.json and saved cases')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seed', type=int, default=2026102201)
    parser.add_argument('--positives', type=int, default=600)
    parser.add_argument('--negatives', type=int, default=1200)
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 11) or cv2.__version__ != '4.8.0' or np.__version__ != '1.26.4':
        raise SystemExit('Use the existing .venv311 environment')
    cv2.setNumThreads(1)
    with patch('socket.socket', side_effect=AssertionError('No network')), \
         patch('cv2.VideoCapture', side_effect=AssertionError('No camera')), \
         patch('tkinter.Tk', side_effect=AssertionError('No desktop')):
        run(args)
