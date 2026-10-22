"""Paired screen-recording replay; never a raw-camera recall estimate.

No UI, webcam, networking, flight calls, overlay removal or frame interpolation.
Both detectors receive exactly the same cropped pixels. All decoded frames count,
including repeated display frames. PTS-based gaps are recording gaps, not proof
of camera detection failure. See summary.json limitations.
"""
import argparse
from collections import Counter
import csv
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import platform
import time

import cv2
import numpy as np

from r3_follow.vision import MarkerTracker


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_baseline(path):
    spec = importlib.util.spec_from_file_location("r3_follow._replay_v08", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MarkerTracker


def crop_frame(frame, roi):
    x, y, width, height = roi
    if min(x, y) < 0 or min(width, height) <= 0 or x+width > frame.shape[1] or y+height > frame.shape[0]:
        raise ValueError("Crop is outside this recording; inspect the layout first")
    return frame[y:y+height, x:x+width].copy()


def gap_statistics(rows, key, end_time):
    if not rows:
        return dict(found=0, total=0, rate=None, runs=[], longest_gap_seconds=0)
    times = [row["pts"] for row in rows]
    if any(not math.isfinite(t) for t in times) or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("Non-increasing recording timestamps")
    if end_time <= times[-1]:
        raise ValueError("End time must include the last frame interval")
    runs, begin = [], None
    for i, row in enumerate(rows):
        if not row[key] and begin is None:
            begin = i
        if row[key] and begin is not None:
            runs.append(dict(start=times[begin], end=times[i], seconds=times[i]-times[begin],
                             frames=i-begin, edge=begin == 0))
            begin = None
    if begin is not None:
        runs.append(dict(start=times[begin], end=end_time, seconds=end_time-times[begin],
                         frames=len(rows)-begin, edge=True))
    found = sum(bool(row[key]) for row in rows)
    return dict(found=found, total=len(rows), rate=found/len(rows),
                runs=runs, gap_count=len(runs),
                longest_gap_seconds=max((run["seconds"] for run in runs), default=0),
                total_not_detected_seconds=sum(run["seconds"] for run in runs),
                gaps_over_half_second=sum(run["seconds"] > .5 for run in runs))


def paired_summary(rows, end_time):
    return dict(baseline=gap_statistics(rows, "baseline_found", end_time),
                current=gap_statistics(rows, "current_found", end_time),
                newly_detected=sum(not r["baseline_found"] and r["current_found"] for r in rows),
                regressions=sum(r["baseline_found"] and not r["current_found"] for r in rows),
                both_missing=sum(not r["baseline_found"] and not r["current_found"] for r in rows),
                both_detected=sum(r["baseline_found"] and r["current_found"] for r in rows))


def replay(path, baseline_class, roi, sizes, output):
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise ValueError(f"Cannot decode {path}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    expected = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("Recording FPS is missing")
    buckets = {f"{w}x{h}": dict(size=(w,h), old=baseline_class(), new=MarkerTracker(), rows=[],
                  hashes=set(), duplicate_frames=0, methods=Counter(), examples={}, last_example={}) for w,h in sizes}
    metadata = dict(file=str(path.resolve()), sha256=sha256(path), fps=fps,
                    reported_frame_count=expected, camera_roi=list(roi),
                    recording_width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    recording_height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    index, previous_pts = 0, -math.inf
    try:
        while True:
            ok, screen = cap.read()
            if not ok:
                break
            pts = cap.get(cv2.CAP_PROP_POS_MSEC)/1000
            if not math.isfinite(pts) or pts <= previous_pts:
                raise ValueError(f"Invalid recording PTS at frame {index}")
            previous_pts = pts
            cropped = crop_frame(screen, roi)
            for size_name, bucket in buckets.items():
                size = bucket["size"]
                frame = cropped if size == (roi[2], roi[3]) else cv2.resize(cropped, size, interpolation=cv2.INTER_AREA)
                fingerprint = hashlib.sha256(frame.tobytes()).digest()
                bucket["duplicate_frames"] += fingerprint in bucket["hashes"]
                bucket["hashes"].add(fingerprint)
                observations, elapsed = [], []
                # Alternate order to reduce systematic warm-cache bias.
                results = {}
                for name in (("old", "new") if index % 2 == 0 else ("new", "old")):
                    before = time.perf_counter()
                    obs = bucket[name].detect(frame, index, pts, source="screen_replay")
                    results[name] = (obs, (time.perf_counter()-before)*1000)
                old, old_ms = results["old"]
                new, new_ms = results["new"]
                method = bucket["new"].last_diagnostics.get("detector_method")
                bucket["methods"][method] += 1
                row = dict(index=index, pts=pts, baseline_found=old.found, current_found=new.found,
                           baseline_ms=old_ms, current_ms=new_ms, current_method=method,
                           baseline_reason=old.reason, current_reason=new.reason,
                           center_x=new.center_x, center_y=new.center_y, size_ratio=new.size_ratio)
                bucket["rows"].append(row)
                category = ("regression" if old.found and not new.found else
                            "rescue" if new.found and not old.found else
                            "both_miss" if not new.found else "both_hit")
                example_list = bucket["examples"].setdefault(category, [])
                if len(example_list) < 8 and pts - bucket["last_example"].get(category, -100) >= 2:
                    example_dir = output/path.stem/size_name
                    example_dir.mkdir(parents=True, exist_ok=True)
                    filename = f"{category}_{index:05d}_{pts:.2f}s.png"
                    saved = frame.copy()
                    # Only add replay labels to evidence copies, never to detector input.
                    if new.found:
                        cv2.polylines(saved, [np.array(new.corners, dtype=np.int32)], True, (255, 0, 255), 2)
                    cv2.rectangle(saved, (0,0), (size[0],25), (250,250,250), -1)
                    cv2.putText(saved, f"{pts:.2f}s {category} old={int(old.found)} new={int(new.found)}",
                                (6,18), cv2.FONT_HERSHEY_SIMPLEX, .48, (0,0,0), 1)
                    if not cv2.imwrite(str(example_dir/filename), saved):
                        raise OSError("Could not save evidence image")
                    example_list.append(dict(frame=index, pts=pts, file=str((example_dir/filename).relative_to(output))))
                    bucket["last_example"][category] = pts
            index += 1
            if index % 300 == 0:
                print(f"{path.name}: {index}/{expected} frames", flush=True)
    finally:
        cap.release()
    if not index:
        raise ValueError("No decoded frames")
    if expected and index != expected:
        raise ValueError(f"Incomplete decode: {index} of {expected}; do not accept this run")
    metadata["decoded_frames"] = index
    metadata["timestamp_basis"] = "OpenCV decoded presentation timestamps; final interval estimated as 1/fps"
    end = previous_pts + 1/fps
    metadata["end_seconds"] = end
    metadata["sizes"] = {}
    for name, bucket in buckets.items():
        rows = bucket["rows"]
        summary = paired_summary(rows, end)
        summary["exact_duplicate_cropped_frames"] = bucket["duplicate_frames"]
        summary["detector_methods"] = dict(bucket["methods"])
        summary["examples"] = bucket["examples"]
        summary["timing_ms"] = {label:dict(median=float(np.median([r[key] for r in rows])),
                                          p95=float(np.percentile([r[key] for r in rows], 95)))
                                for label,key in (("baseline", "baseline_ms"), ("current", "current_ms"))}
        destination = output/path.stem/name
        destination.mkdir(parents=True, exist_ok=True)
        with (destination/"frames.csv").open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        summary["per_frame_csv"] = str((destination/"frames.csv").relative_to(output))
        metadata["sizes"][name] = summary
    print(f"DONE {path.name}: {index} frames", flush=True)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("videos", type=Path, nargs="+")
    parser.add_argument("--baseline", type=Path, default=Path(__file__).parent/"replay_baselines/vision_v08.py")
    parser.add_argument("--roi", type=int, nargs=4, default=(24,175,960,720), metavar=("X","Y","W","H"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output directory to preserve prior evidence")
    args.output.mkdir(parents=True)
    cv2.setNumThreads(1)
    base = load_baseline(args.baseline)
    current = Path(__file__).parent/"r3_follow/vision.py"
    # Preserve exact source evidence so future edits cannot rewrite this result.
    (args.output/"baseline_source.py.txt").write_bytes(args.baseline.read_bytes())
    (args.output/"current_source.py.txt").write_bytes(current.read_bytes())
    report = dict(python=platform.python_version(), opencv=cv2.__version__, numpy=np.__version__,
                  cv2_threads=cv2.getNumThreads(), baseline_sha256=sha256(args.baseline),
                  current_sha256=sha256(current), baseline="archived_v0.8",
                  current="current_v0.9", completed=False, recordings=[],
                  limitations=["Screen recording, not raw camera frames; existing green boxes, red dots, scaling and compression remain.",
                    "Detection fraction is over all decoded screen frames, NOT recall: no complete manual visibility labels.",
                    "Repeated camera/display frames may be encoded differently; exact pixel duplicate counts cannot deduplicate camera frames.",
                    "Missing intervals may include target out of view, blur, overlays or occlusion; not automatically classified as detector failures.",
                    "640x480 is a resized screen crop, not reconstructed raw input. 960x720 is the native display crop.",
                    "v0.8 recording was previously used during v0.9 tuning; this is regression evidence, not an independent holdout.",
                    "Timing is local offline detector time, not end-to-end latency or flight performance."])
    for path in args.videos:
        report["recordings"].append(replay(path, base, args.roi, [(640,480),(960,720)], args.output))
        (args.output/"summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["completed"] = True
    (args.output/"summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["旧录屏复测结果：v0.8 对比当前 v0.9", "", "注意：下面是录屏帧检出比例，不是真实摄像头识别率。", ""]
    for clip in report["recordings"]:
        lines.append(Path(clip["file"]).name + f"：{clip['decoded_frames']}帧，约{clip['end_seconds']:.2f}秒")
        for size, result in clip["sizes"].items():
            a,b=result["baseline"],result["current"]
            lines.extend([f"  {size}: v0.8 {a['found']}/{a['total']} ({a['rate']:.1%})；v0.9 {b['found']}/{b['total']} ({b['rate']:.1%})",
                          f"  新增检出{result['newly_detected']}帧，回退{result['regressions']}帧；两版均未检出{result['both_missing']}帧。",
                          f"  最长连续未检出：{a['longest_gap_seconds']:.3f}秒 -> {b['longest_gap_seconds']:.3f}秒。"])
        lines.append("")
    lines += ["同一段视频、同一裁剪范围、同一缩放方式下逐帧比较；没有删除绿框或修补图案。",
              "未检出不一定是算法漏检，也可能是标记移出画面、遮挡、模糊或录屏叠加造成。",
              "640和960两种尺寸仅用于检查画面缩放的影响，不据此替换实时算法。",
              "逐帧结果见各目录frames.csv，完整参数、文件哈希和局限见summary.json。"]
    (args.output/"自动复测结果.txt").write_text("\n".join(lines)+"\n", encoding="utf-8-sig")
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    main()
