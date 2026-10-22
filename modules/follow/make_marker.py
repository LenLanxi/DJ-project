"""生成带白边的练习标记，并检查能否识别。"""
from pathlib import Path

import cv2

folder = Path(__file__).resolve().parent
dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
marker = cv2.aruco.generateImageMarker(dictionary, 0, 600)
image = cv2.copyMakeBorder(marker, 100, 100, 100, 100, cv2.BORDER_CONSTANT, value=255)
destination = folder / "marker_0.png"
success, encoded = cv2.imencode(".png", image)
if not success:
    raise RuntimeError("PNG encoding failed")
destination.write_bytes(encoded.tobytes())
detector = cv2.aruco.ArucoDetector(dictionary)
for name, sample in [("original", image), ("rotated", cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE))]:
    _, ids, _ = detector.detectMarkers(sample)
    if ids is None or ids.flatten().tolist() != [0]:
        raise RuntimeError(f"Marker check failed: {name}")
    print(f"PASS {name}: ID 0")
print(destination)
