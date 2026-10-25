"""Offline image/latency comparison; no measured aircraft/camera claims."""
import argparse
import importlib.util
import json
from pathlib import Path
from time import perf_counter

import cv2
import numpy as np

from r3_follow.vision import MarkerTracker


def run(baseline, output, repeats=20):
    cv2.setNumThreads(1)
    spec = importlib.util.spec_from_file_location('r3_follow._baseline_vision', baseline)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    test_spec = importlib.util.spec_from_file_location('marker_cases',
        Path(__file__).parent/'tests'/'test_multi_marker_vision.py')
    cases_module = importlib.util.module_from_spec(test_spec)
    test_spec.loader.exec_module(cases_module)
    scene = cases_module.marker_scene
    fixture_pair = cases_module.MultiMarkerVisionTests().fixture_pair
    clean = scene([(i, 160+i*320, 240, 100, 0, 0, 255) for i in range(3)])
    folder = Path(__file__).parent/'tests'/'fixtures'
    side = cv2.imdecode(np.frombuffer((folder/'side_contour_rescue.png').read_bytes(), np.uint8), 0)
    contrast = cv2.imdecode(np.frombuffer((folder/'contrast_rescue.png').read_bytes(), np.uint8), 0)
    mixed_large = np.full((720, 1280), 245, np.uint8)
    mixed_large[20:280, 20:380] = side
    mixed_large[200:460, 650:1010] = contrast
    mixed_small = np.full((480, 960), 245, np.uint8)
    mixed_small[0:260, 0:360] = side
    mixed_small[0:260, 500:860] = contrast
    single_large = np.full((720, 1280), 245, np.uint8)
    single_large[20:280, 20:380] = side
    cases = {'three_clean_ids': clean,
             'three_blurred_ids': cv2.GaussianBlur(clean, (3, 3), .6),
             'one_easy_plus_side_duplicate_960': fixture_pair('side_contour_rescue.png', (480, 960)),
             'one_easy_plus_side_duplicate_1280': fixture_pair('side_contour_rescue.png', (720, 1280)),
             'one_easy_plus_contrast_duplicate': fixture_pair('contrast_rescue.png', (480, 960)),
             'mixed_side_contrast_duplicate_960': mixed_small,
             'mixed_side_contrast_duplicate_1280': mixed_large,
             'single_side_multiple_rescue_crops': single_large,
             'target_missing_other_ids_visible': scene([(1, 300, 240, 100, 0, 0, 255),
                                                        (2, 650, 240, 100, 0, 0, 255)])}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, image in cases.items():
        trackers = {'baseline': old.MarkerTracker(0), 'candidate': MarkerTracker(0)}
        elapsed = {label: [] for label in trackers}
        last = {}
        for tracker in trackers.values():
            tracker.detect(image, 0, 0)
        # Interleave both implementations to reduce order/thermal bias.
        for repeat in range(repeats):
            for label in (('baseline', 'candidate') if repeat % 2 == 0 else ('candidate', 'baseline')):
                tracker = trackers[label]
                started = perf_counter()
                obs = tracker.detect(image, repeat+1, (repeat+1)*.1, 'synthetic_benchmark')
                elapsed[label].append((perf_counter()-started)*1000)
                last[label] = {'found': obs.found, 'reason': obs.reason,
                               'center_x': obs.center_x,
                               'duplicate_check': tracker.last_diagnostics.get('duplicate_check'),
                               'large_image_rescue': tracker.last_diagnostics.get('large_image_rescue')}
        row = {'case': name, 'shape': list(image.shape)}
        for label, timings in elapsed.items():
            row[label] = {**last[label], 'median_ms': float(np.median(timings)),
                          'p95_ms': float(np.percentile(timings, 95)), 'samples_ms': timings}
        rows.append(row)
        if 'duplicate' in name:
            cv2.imencode('.png', image)[1].tofile(str(output/(name+'.png')))
    # Three independent detectors sharing a recorded image, processed
    # sequentially. This does not emulate three network/camera threads.
    triple = [MarkerTracker(i) for i in range(3)]
    times = []
    for repeat in range(repeats):
        started = perf_counter()
        result = [tracker.detect(clean, repeat, repeat*.1) for tracker in triple]
        assert all(obs.found and obs.target_id == i for i, obs in enumerate(result))
        times.append((perf_counter()-started)*1000)
    data = {'source': 'deterministic_generated_images_not_camera_or_flight',
            'opencv': cv2.__version__, 'opencv_threads': cv2.getNumThreads(),
            'repeats': repeats, 'baseline_file': str(Path(baseline).resolve()),
            'cases': rows,
            'three_trackers_sequential': {'median_ms': float(np.median(times)),
                                         'p95_ms': float(np.percentile(times, 95))},
            'limitations': ['Only decoded dictionary IDs are evidence of identity.',
                'Extra duplicate scan only sees current-frame rejected quads and has fixed work budgets.',
                'A marker with no decodable contour cannot be ruled out; duplicate check is not person recognition.',
                'Complex backgrounds can exhaust the budget and intentionally suppress a unique-target output.',
                'Elapsed time is local offline compute, excluding camera, decoding, network and scheduling.']}
    (output/'comparison.json').write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('logs/multi_marker_check'))
    parser.add_argument('--repeats', type=int, default=20)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('--repeats must be positive')
    data = run(args.baseline, args.output, args.repeats)
    for row in data['cases']:
        print(row['case'], {label: {k: row[label][k] for k in ('found', 'reason', 'median_ms', 'p95_ms')}
                            for label in ('baseline', 'candidate')})
    print('three trackers:', data['three_trackers_sequential'])
