"""Generate exact DICT_4X4_50 ID 0/1/2 print assets; no camera or device use.

The square PNG includes one marker-cell-wide white margin on every side.
Use the matching target_id on each independent FollowService. A filename or
label is not the encoded identity. Physical print scale must be calibrated.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'follow'
sys.path.insert(0, str(ROOT))

import argparse
import hashlib
import json

import cv2

def generate(output, marker_side=600):
    if isinstance(marker_side, bool) or not isinstance(marker_side, int) or marker_side < 60 or marker_side % 6:
        raise ValueError('marker_side must be an integer multiple of 6, at least 60')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    detector = cv2.aruco.ArucoDetector(dictionary)
    margin = marker_side//6
    records = []
    for marker_id in range(3):
        marker = cv2.aruco.generateImageMarker(dictionary, marker_id, marker_side)
        image = cv2.copyMakeBorder(marker, margin, margin, margin, margin,
                                   cv2.BORDER_CONSTANT, value=255)
        _, ids, _ = detector.detectMarkers(image)
        if ids is None or ids.flatten().tolist() != [marker_id]:
            raise RuntimeError(f'Print asset failed independent decode for ID {marker_id}')
        success, encoded = cv2.imencode('.png', image)
        if not success:
            raise RuntimeError('PNG encode failed')
        data = encoded.tobytes()
        filename = f'DICT_4X4_50_ID_{marker_id}.png'
        (output/filename).write_bytes(data)
        records.append({'target_id': marker_id, 'file': filename,
                        'sha256': hashlib.sha256(data).hexdigest(),
                        'black_marker_side_px': marker_side, 'white_margin_px': margin,
                        'decoded_id': int(ids[0, 0])})
    result = {'dictionary': 'DICT_4X4_50', 'synthetic_print_assets': True,
              'physical_print_size_not_calibrated': True, 'assets': records}
    (output/'marker_manifest.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('markers/three_targets'))
    parser.add_argument('--marker-side', type=int, default=600)
    args = parser.parse_args()
    print(json.dumps(generate(args.output, args.marker_side), ensure_ascii=False, indent=2))
