"""Paired offline render -> detect -> control experiment, never opens devices.

Known virtual pose is used only to label synthetic tests. Production control
does NOT receive that pose, camera intrinsics, or metric depth.
"""
import argparse
import csv
from dataclasses import asdict
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

from r3_follow.core import FollowController
from r3_follow.vision import MarkerTracker

BASE = Path(__file__).resolve().parent


def render(yaw=0, pitch=0, roll=0, depth_factor=1, x=0):
    a, b, c = map(math.radians, (yaw, pitch, roll))
    ry = np.array([[math.cos(a), 0, math.sin(a)], [0, 1, 0],
                   [-math.sin(a), 0, math.cos(a)]])
    rx = np.array([[1, 0, 0], [0, math.cos(b), -math.sin(b)],
                   [0, math.sin(b), math.cos(b)]])
    rz = np.array([[math.cos(c), -math.sin(c), 0],
                   [math.sin(c), math.cos(c), 0], [0, 0, 1]])
    points = np.array([[-.2, -.2, 0], [.2, -.2, 0], [.2, .2, 0], [-.2, .2, 0]])
    points = points @ (rz @ ry @ rx).T + [x, 0, 1.6*depth_factor]
    projected = (600*points[:, :2]/points[:, 2:]+[320, 240]).astype(np.float32)
    marker = cv2.aruco.generateImageMarker(
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), 0, 256)
    transform = cv2.getPerspectiveTransform(
        np.array([[0, 0], [255, 0], [255, 255], [0, 255]], np.float32), projected)
    image = cv2.warpPerspective(marker, transform, (640, 480),
                                flags=cv2.INTER_LINEAR, borderValue=255)
    return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)


def load_baseline(path):
    spec = importlib.util.spec_from_file_location("side_turn_baseline", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_comparison(output, baseline):
    cases = []
    # Equal depth; only pose and optional horizontal position change.
    for x in (0, .24):
        for axis in ("yaw", "pitch"):
            for angle in (-60, -45, -30, -15, 0, 15, 30, 45, 60):
                cases.append(dict(name=f"same_depth_{axis}_{angle}_x{x}",
                                  kind="same_depth", pose={axis: angle, "x": x}))
        for angle in (15, 45, 90):
            cases.append(dict(name=f"same_depth_roll_{angle}_x{x}",
                              kind="same_depth", pose=dict(roll=angle, x=x)))
        for yaw, pitch in ((45, 45), (-45, 45), (45, -45), (-45, -45)):
            cases.append(dict(name=f"compound_{yaw}_{pitch}_x{x}",
                              kind="same_depth", pose=dict(yaw=yaw, pitch=pitch, x=x)))
        for factor in (.75, 1.25, 1.5):
            cases.append(dict(name=f"distance_{factor}_x{x}", kind="distance",
                              pose=dict(depth_factor=factor, x=x)))
    tracker = MarkerTracker()
    rows, summaries = [], []
    for case in cases:
        old = baseline.FollowController(max_request_rate=.8, recovery_window=3)
        new = FollowController(max_request_rate=.8, recovery_window=3, require_geometry=True)
        reference = tracker.detect(render(), 0, 0, source="virtual-marker")
        if not reference.found:
            raise AssertionError("Reference marker did not decode")
        old.observe(baseline.Observation(**asdict(reference)))
        new.observe(reference)
        assert old.calibrate(0)[0] and new.calibrate(0)[0]
        assert old.start(0)[0] and new.start(0)[0]
        image = render(**case["pose"])
        hits = 0
        for seq in range(1, 7):
            began = time.perf_counter()
            obs = tracker.detect(image, seq, seq*.1, source="virtual-marker")
            detection_ms = (time.perf_counter()-began)*1000
            hits += obs.found
            old.observe(baseline.Observation(**asdict(obs)))
            new.observe(obs)
            before, after = old.tick(seq*.1), new.tick(seq*.1)
            rows.append(dict(case=case["name"], kind=case["kind"], step=seq,
                found=obs.found, detection_ms=detection_ms,
                size_ratio=obs.size_ratio, baseline_right=before.right,
                baseline_forward=before.forward, candidate_right=after.right,
                candidate_forward=after.forward, candidate_state=after.state,
                guard_reason=new.distance_guard.reason, **(new.distance_guard.shape or {})))
        row = rows[-1]
        same_depth = case["kind"] == "same_depth"
        expected_direction = (0 if same_depth else
                              (1 if case["pose"]["depth_factor"] > 1 else -1))
        correct = (hits == 6 and (abs(after.forward) < 1e-12 if same_depth else
                                   after.forward*expected_direction > 0)
                   and math.isclose(before.right, after.right, abs_tol=1e-12))
        summaries.append(dict(**case, detected=hits, frames=6,
            baseline_final_forward=before.forward, candidate_final_forward=after.forward,
            guard_reason=row["guard_reason"], correct=correct))
        if case["name"] in ("same_depth_yaw_60_x0", "compound_45_45_x0", "distance_1.5_x0"):
            cv2.imencode(".png", image)[1].tofile(output/(case["name"]+".png"))
    with (output/"steps.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
        writer.writeheader()
        writer.writerows(rows)
    same = [r for r in summaries if r["kind"] == "same_depth"]
    distance = [r for r in summaries if r["kind"] == "distance"]
    report = dict(experiment="side_turn_shape_guard_v1", synthetic_only=True,
        hardware_verified=False, camera_opened=False, desktop_opened=False,
        python=sys.version, numpy=np.__version__, opencv=cv2.__version__,
        assumptions="Virtual square 0.4 units, focal length 600px, reference depth 1.6 units; no distortion/noise. No real camera angle or range claims.",
        cases=len(summaries), processed_frames=len(rows), detected_frames=sum(r["found"] for r in rows),
        same_depth_cases=len(same), distance_change_cases=len(distance),
        baseline_same_depth_nonzero=sum(abs(r["baseline_final_forward"]) > 1e-12 for r in same),
        candidate_same_depth_nonzero=sum(abs(r["candidate_final_forward"]) > 1e-12 for r in same),
        correct_distance_directions=sum(r["correct"] for r in distance),
        all_correct=all(r["correct"] for r in summaries),
        baseline_sha256=hashlib.sha256(Path(baseline.__file__).read_bytes()).hexdigest(),
        source_sha256={p: hashlib.sha256((BASE/p).read_bytes()).hexdigest()
                       for p in ("side_turn_check.py", "r3_follow/core.py", "r3_follow/vision.py")},
        details=summaries)
    (output/"comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    text = (f"侧转保护：合成图片经过现有识别器，再分别交给修改前后控制器。\n"
            f"共{report['cases']}个场景、{report['processed_frames']}帧，检出{report['detected_frames']}帧。\n"
            f"中心距离不变的{len(same)}个场景：旧版{report['baseline_same_depth_nonzero']}个出现前后请求，"
            f"候选版{report['candidate_same_depth_nonzero']}个。\n"
            f"正对镜头、实际虚拟距离变化：{report['correct_distance_directions']}/{len(distance)}个方向正确。\n"
            "左右请求逐项与旧版相同。所有场景使用同一组识别输出；没有打开摄像头。\n"
            "此结果只说明这些理想合成图条件下的行为；不代表真实镜头、佩戴、光线或飞行验收。\n"
            "倾斜时暂停前后调整，不是估算出准确距离；同样会暂时不响应倾斜时的真实远近变化。\n")
    (output/"结论.txt").write_text(text, encoding="utf-8-sig")
    print(json.dumps({k: v for k, v in report.items() if k not in ("details", "source_sha256")}, indent=2))
    if not report["all_correct"]:
        raise SystemExit("Comparison failed; inspect comparison.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-core", type=Path,
                        default=BASE/"logs/side_turn_guard_v013/baseline/core.py")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 11) or cv2.__version__ != "4.8.0" or np.__version__ != "1.26.4":
        raise SystemExit("Use the existing .venv311 environment")
    args_baseline = args.baseline_core.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cv2.setNumThreads(1)
    with patch("socket.socket", side_effect=AssertionError("No network")), \
         patch("cv2.VideoCapture", side_effect=AssertionError("No camera")), \
         patch("tkinter.Tk", side_effect=AssertionError("No desktop")):
        run_comparison(output, load_baseline(args_baseline))
