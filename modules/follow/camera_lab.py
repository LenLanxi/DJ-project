"""本地摄像头练习：先预览，再识别。画面不保存、不上传。"""
import argparse
import time

import cv2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true", help="只预览摄像头")
    parser.add_argument("--camera", type=int, default=0)
    args = parser.parse_args()
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    detector = cv2.aruco.ArucoDetector(dictionary)
    camera = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
    if not camera.isOpened():
        camera.release()
        camera = cv2.VideoCapture(args.camera, cv2.CAP_MSMF)
    if not camera.isOpened():
        camera.release()
        camera = cv2.VideoCapture(args.camera, cv2.CAP_ANY)
    title = "Camera lab - Q or Esc to quit"
    try:
        if not camera.isOpened():
            raise RuntimeError("Cannot open camera. Close other camera apps and check Windows camera permission.")
        print("Camera opened. Click the video window; press Q or Esc to exit.")
        last_good = time.monotonic()
        failures = 0
        while True:
            ok, frame = camera.read()
            if not ok:
                failures += 1
                if failures >= 100 or time.monotonic() - last_good > 5:
                    raise RuntimeError("Camera opened but no usable frames arrived.")
                time.sleep(0.05)
                continue
            failures = 0
            last_good = time.monotonic()
            message = "PREVIEW ONLY"
            if not args.preview:
                # 在原始画面中识别，不先做镜像翻转。
                corners, ids, _ = detector.detectMarkers(frame)
                message = "NO MARKER"
                if ids is not None:
                    cv2.aruco.drawDetectedMarkers(frame, corners, ids)
                    message = "IDs: " + ", ".join(str(int(i)) for i in ids.flatten())
                    for points in corners:
                        x, y = points[0].mean(axis=0).astype(int)
                        cv2.circle(frame, (x, y), 5, (0, 0, 255), -1)
            cv2.putText(frame, message, (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 0), 2)
            cv2.imshow(title, frame)
            if cv2.waitKey(1) & 0xFF in (ord("q"), ord("Q"), 27):
                break
            if cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                break
    finally:
        camera.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
