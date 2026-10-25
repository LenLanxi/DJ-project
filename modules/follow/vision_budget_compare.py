"""Same-image comparison of cooperative recognition budget; offline only."""
import argparse
from collections import Counter
import importlib.util
import json
from pathlib import Path
from time import perf_counter

import cv2
import numpy as np

from r3_follow.vision import MarkerTracker, synthetic_frame


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(baseline, output, repeats=12, budget_ms=120):
    cv2.setNumThreads(1)
    old = load(baseline, 'r3_follow._unbudgeted_vision')
    cases_module = load(Path(__file__).parent/'tests'/'test_multi_marker_vision.py',
                        'budget_marker_cases')
    scene = cases_module.marker_scene
    fixture_pair = cases_module.MultiMarkerVisionTests().fixture_pair
    folder = Path(__file__).parent/'tests'/'fixtures'
    fixture = lambda name: cv2.imdecode(np.frombuffer((folder/name).read_bytes(), np.uint8), 0)
    side, contrast = fixture('side_contour_rescue.png'), fixture('contrast_rescue.png')
    clean = scene([(i, 160+i*320, 240, 100, 0, 0, 255) for i in range(3)])
    large = np.full((720, 1280), 245, np.uint8)
    large[20:280, 20:380], large[200:460, 650:1010] = side, contrast
    small = np.full((480, 960), 245, np.uint8)
    small[0:260, 0:360], small[0:260, 500:860] = side, contrast
    cases = {'three_clean_ids': clean,
             'three_blurred_ids': cv2.GaussianBlur(clean, (3, 3), .6),
             'contrast_rescue': contrast,
             'side_rescue': side,
             'motion_rescue_1': fixture('motion_marker_1.png'),
             'motion_rescue_19': fixture('motion_marker_19.png'),
             'easy_plus_side_duplicate': fixture_pair('side_contour_rescue.png', (480, 960)),
             'easy_plus_contrast_duplicate': fixture_pair('contrast_rescue.png', (480, 960)),
             'mixed_side_contrast_duplicate_960': small,
             'mixed_side_contrast_duplicate_1280': large,
             'target_missing_other_ids': scene([(1, 300, 240, 100, 0, 0, 255),
                                               (2, 650, 240, 100, 0, 0, 255)]),
             'blank_960': np.full((540, 960), 245, np.uint8)}
    rows = []
    for name, image in cases.items():
        trackers = {'before': old.MarkerTracker(0), 'reordered_unbounded': MarkerTracker(0),
                    'reordered_budgeted': MarkerTracker(0, processing_budget_ms=budget_ms)}
        samples = {label: [] for label in trackers}
        for tracker in trackers.values():
            tracker.detect(image, 0, 0)
        labels = list(trackers)
        for repeat in range(repeats):
            # Rotate execution order to avoid giving one implementation all
            # first calls. Same immutable pixels are passed to all variants.
            for label in labels[repeat % 3:]+labels[:repeat % 3]:
                tracker = trackers[label]
                started = perf_counter()
                observation = tracker.detect(image, repeat+1, repeat*.1, 'synthetic_budget_comparison')
                elapsed = (perf_counter()-started)*1000
                diag = tracker.last_diagnostics
                samples[label].append({'elapsed_ms': elapsed, 'found': observation.found,
                    'reason': observation.reason, 'center_x': observation.center_x,
                    'center_y': observation.center_y,
                    'attempts': [a['method'] for a in diag['attempts']],
                    'budget': diag.get('processing_budget')})
        row = {'case': name, 'shape': list(image.shape)}
        for label, observations in samples.items():
            elapsed = [o['elapsed_ms'] for o in observations]
            row[label] = {'median_ms': float(np.median(elapsed)),
                          'p95_ms': float(np.percentile(elapsed, 95)), 'max_ms': max(elapsed),
                          'found_count': sum(o['found'] for o in observations),
                          'reasons': dict(Counter(o['reason'] or 'detected' for o in observations)),
                          'samples': observations}
            if 'duplicate' in name:
                assert not row[label]['found_count'], (name, label, 'wrong duplicate accepted')
            if name in ('three_clean_ids', 'three_blurred_ids'):
                assert row[label]['found_count'] == repeats, (name, label, 'unexpected normal miss')
                assert all(abs(o['center_x']-159.5) < 2 for o in observations)
        rows.append(row)
    result = {'python': __import__('sys').version, 'opencv': cv2.__version__,
              'opencv_threads': cv2.getNumThreads(), 'processing_budget_ms': budget_ms,
              'repeats': repeats, 'baseline': str(Path(baseline).resolve()), 'cases': rows,
              'limitations': [
                  'Synthetic fixtures only; original long-running real frame was not supplied.',
                  'A native OpenCV call cannot be preempted, so the elapsed maximum may exceed the budget.',
                  'Budget exhaustion with unresolved identity emits no coordinates; it can increase missed frames.',
                  'No real camera/network/hardware latency, PID response, or physical flight performance is measured.']}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output/'budget_comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n',
                                               encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=12)
    parser.add_argument('--budget-ms', type=float, default=120)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('repeats must be positive')
    result = run(args.baseline, args.output, args.repeats, args.budget_ms)
    for row in result['cases']:
        print(row['case'], {label: {k: row[label][k] for k in ('median_ms', 'p95_ms', 'found_count', 'reasons')}
                           for label in ('before', 'reordered_unbounded', 'reordered_budgeted')})
