"""Paired, synthetic-only resolution check. Never opens a camera or network.

Usage: python resolution_rescue_check.py --baseline PATH/vision.py --output DIR
The baseline is loaded read-only; all results are written under --output.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
from time import perf_counter

import cv2
import numpy as np

from r3_follow.vision import MarkerTracker, synthetic_frame
from side_view_detection_check import generate


def run(baseline, output):
    spec = importlib.util.spec_from_file_location('r3_follow._resolution_baseline', baseline)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    trackers = {'baseline': module.MarkerTracker(), 'candidate': MarkerTracker()}
    rng = np.random.default_rng(20261024)
    root = Path(__file__).parent
    fixture = cv2.imdecode(np.frombuffer(
        (root/'tests/fixtures/side_contour_rescue.png').read_bytes(), np.uint8), 0)
    scenarios = [('side_fixture', fixture, np.asarray(json.loads(
        (root/'tests/fixtures/side_contour_rescue.json').read_text())['expected_corners']))]
    for i in range(32):
        image, corners, _ = generate(rng)
        scenarios.append(('random_side_target', image, corners))
    for i in range(16):
        image, _, _ = generate(rng, marker_id=1+i)
        scenarios.append(('wrong_id', image, None))
    for i in range(8):
        image, _, _ = generate(rng, occluded=True)
        scenarios.append(('occluded', image, None))
    scenarios.extend([
        ('blank', np.full((260, 360), 235, np.uint8), None),
        ('noise', rng.integers(0, 256, (260, 360), np.uint8), None)])
    normal = cv2.cvtColor(synthetic_frame(size=.125), cv2.COLOR_BGR2GRAY)
    normal = normal[110:370, 140:500]
    scenarios.append(('normal_target', normal, None))
    # Two distinct identical hard markers: any single-target return is wrong.
    scenarios.append(('duplicate', np.concatenate([fixture, fixture], axis=1), None))
    rows = []
    output.mkdir(parents=True, exist_ok=True)
    for index, (kind, image, expected) in enumerate(scenarios):
        for resolution in ('native', '1280x720', '1920x1080'):
            shift = (0, 0) if resolution == 'native' else (200, 100)
            if resolution == 'native':
                frame = image
            else:
                shape = (720, 1280) if resolution == '1280x720' else (1080, 1920)
                frame = np.full(shape, 235, np.uint8)
                frame[100:100+image.shape[0], 200:200+image.shape[1]] = image
            row = {'index': index, 'kind': kind, 'resolution': resolution,
                   'sha256': hashlib.sha256(frame.tobytes()).hexdigest()}
            order = ('baseline', 'candidate') if index % 2 else ('candidate', 'baseline')
            for name in order:
                t = perf_counter()
                obs = trackers[name].detect(frame, index, float(index), 'synthetic-resolution')
                row[name] = {'found': obs.found, 'reason': obs.reason,
                    'ms': (perf_counter()-t)*1000,
                    'wrong_position': bool(obs.found and expected is not None and
                        np.linalg.norm(np.asarray((obs.center_x, obs.center_y))-
                                       expected.mean(0)-shift) >= 8),
                    'method': trackers[name].last_diagnostics['detector_method']}
            row['rescue'] = trackers['candidate'].last_diagnostics['large_image_rescue']
            rows.append(row)
    summary = {'synthetic_only': True, 'baseline_sha256': hashlib.sha256(baseline.read_bytes()).hexdigest(),
               'candidate_sha256': hashlib.sha256((root/'r3_follow/vision.py').read_bytes()).hexdigest(),
               'seed': 20261024, 'row_count': len(rows), 'groups': {},
               'limits': {'extra_rois': MarkerTracker.RESCUE_MAX_ROIS,
                          'extra_roi_side': MarkerTracker.RESCUE_MAX_ROI_SIDE,
                          'extra_pixels': MarkerTracker.RESCUE_MAX_PIXELS},
               'limitations': ['Synthetic images only, not camera or Tello performance.',
                   'Timing includes shared-host load; no hard real-time guarantee.',
                   'New rescue needs a rejected quadrilateral; it cannot recover absent pixels.',
                   'Existing full-frame paths run before ROI rescue; input caps apply to extra rescue only.']}
    for kind, resolution in sorted({(r['kind'], r['resolution']) for r in rows}):
        part = [r for r in rows if r['kind'] == kind and r['resolution'] == resolution]
        group = {'count': len(part)}
        for name in ('baseline', 'candidate'):
            group[name] = {'found': sum(r[name]['found'] for r in part),
                'wrong_positions': sum(r[name]['wrong_position'] for r in part),
                'median_ms': float(np.median([r[name]['ms'] for r in part])),
                'p95_ms': float(np.percentile([r[name]['ms'] for r in part], 95))}
        group['rescues'] = sum(r['candidate']['found'] and not r['baseline']['found'] for r in part)
        group['regressions'] = sum(r['baseline']['found'] and not r['candidate']['found'] for r in part)
        summary['groups'][kind+'/'+resolution] = group
    (output/'rows.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    (output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.baseline, args.output)
