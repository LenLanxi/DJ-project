"""Offline: three real marker detectors -> follow services -> isolated mock ports.

No aircraft/camera/GUI/SDK import. This is not a three-aircraft connection demo.
Run with Python 3.11. Logical time is deterministic; measured processing time
is reported separately, not presented as a live three-camera frame rate.
"""
import argparse
import csv
import json
import math
from pathlib import Path
import socket
import time
from unittest.mock import patch

import cv2
import numpy as np

from r3_follow.config import load_config
from r3_follow.mock_port import MockPort
from r3_follow.service import FollowService


class Clock:
    value = 100.0
    def __call__(self):
        return self.value


def marker_scene(step, visible):
    frame = np.full((600, 960, 3), 255, np.uint8)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for marker_id in visible:
        side = int(110 - 18 * math.sin(step / 12))
        center = int(180 + marker_id * 300 + 45 * math.sin(step / 8))
        y = 180 + (marker_id % 2) * 170
        marker = cv2.aruco.generateImageMarker(dictionary, marker_id, side)
        frame[y:y+side, center-side//2:center-side//2+side] = marker[:, :, None]
    return frame


def expected_geometry(step, marker_id):
    side = int(110 - 18 * math.sin(step / 12))
    center = int(180 + marker_id * 300 + 45 * math.sin(step / 8))
    return (center-side//2+(side-1)/2,
            180+(marker_id % 2)*170+(side-1)/2, (side-1)/960)


def run(output):
    import sys
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("Use Python 3.11")
    output.mkdir(parents=True, exist_ok=True)
    config_dir = Path(__file__).resolve().parent / "configs"
    configs = [load_config(config_dir / f"aircraft_{letter}_ID{i}.json")
               for i, letter in enumerate("ABC")]
    if len({c.instance_id for c in configs}) != 3 or len({c.target_id for c in configs}) != 3:
        raise ValueError("Three aircraft must have distinct instance and target IDs")
    clock = Clock()
    ports = [MockPort(clock) for _ in configs]
    services = [FollowService(p, **c.to_dict(), monotonic=clock, wall_clock=clock,
                              recovery_window=3, log_directory=output / "logs")
                for p, c in zip(ports, configs)]
    rows, violations, frozen = [], [], None
    started = time.perf_counter()
    try:
        for step in range(65):
            clock.value = 100 + round(step * .1, 6)
            visible = {0, 1, 2}
            for marker_id, start in enumerate((10, 20, 30)):
                if start <= step < start + 4:
                    visible.remove(marker_id)
            frame = marker_scene(step, visible)
            if step == 0:
                ok, encoded = cv2.imencode('.png', frame)
                if not ok:
                    raise RuntimeError('Cannot encode verification image')
                (output / 'three_markers.png').write_bytes(encoded.tobytes())
            before_batch = time.perf_counter()
            for i, (service, port, config) in enumerate(zip(services, ports, configs)):
                if i == 2 and 39 <= step <= 46:
                    if frozen is None:
                        frozen = (frame.copy(), step, clock.value)
                    port.publish(*frozen)
                else:
                    port.publish(frame.copy(), step, clock.value)
                source_step = frozen[1] if i == 2 and 39 <= step <= 46 else step
                service.capture_once()
                if step == 0:
                    assert service.calibrate_reference().ok
                    assert service.start_follow().ok
                if step == 47 and i == 2:
                    assert service.start_follow().ok
                if step == 45 and i == 0 or step == 55 and i == 2:
                    service.stop_follow()
                if step == 50 and i == 1:
                    service.pause_follow()
                service.control_once()
                observation = service.controller.observation
                state = str(service.controller.state)
                nonzero = any(port.velocity)
                if nonzero and (state != "tracking" or i not in visible):
                    violations.append(dict(step=step, instance=config.instance_id,
                                           reason="inactive_or_missing_motion",
                                           state=state, channels=port.velocity))
                # 上限来自该机自己的配置（不是全局常量）：只改钳位、不改算法上限时
                # 这里仍然会抓到"超出本机预算"。升降恒为 0、横移恒为 0 是不可放松的。
                if (not all(math.isfinite(v) for v in port.velocity)
                        or port.velocity[0] != 0 or abs(port.velocity[2]) > (20 if config.vertical_follow else 0)
                        or abs(port.velocity[1]) > round(config.forward_limit * 100)
                        or abs(port.velocity[3]) > round(config.yaw_limit * 100)):
                    violations.append(dict(step=step, instance=config.instance_id,
                                           reason="invalid_channels"))
                truth_x, truth_y, truth_size = expected_geometry(source_step, i)
                if observation and observation.found:
                    if (i not in visible or abs(observation.center_x-truth_x) > 2
                            or abs(observation.center_y-truth_y) > 2
                            or abs(observation.size_ratio-truth_size) > .003):
                        violations.append(dict(step=step, instance=config.instance_id,
                                               reason="observation_not_assigned_marker"))
                    if port.velocity[3]*(truth_x-480) < 0:
                        violations.append(dict(step=step, instance=config.instance_id,
                                               reason="yaw_wrong_direction"))
                    if port.velocity[2]*(300-truth_y) < 0:
                        violations.append(dict(step=step, instance=config.instance_id,
                                               reason="vertical_wrong_direction"))
                rows.append(dict(step=step, t=round(step*.1, 3), instance=config.instance_id,
                    target_id=config.target_id, input_has_target=i in visible,
                    source_step=source_step, expected_center_x=truth_x,
                    expected_center_y=truth_y, expected_size_ratio=truth_size,
                    frame_id=service.last_frame_id, state=state,
                    found=observation.found if observation else False,
                    center_x=observation.center_x if observation else None,
                    center_y=observation.center_y if observation else None,
                    size_ratio=observation.size_ratio if observation else None,
                    left_right=port.velocity[0], forward_back=port.velocity[1],
                    up_down=port.velocity[2], yaw=port.velocity[3],
                    detector_ms=getattr(service.tracker, "last_diagnostics", {}).get("detect_ms")))
            batch_ms = (time.perf_counter() - before_batch) * 1000
            for row in rows[-3:]:
                row["three_service_batch_ms"] = batch_ms
    finally:
        for service in services:
            service.close()
    checks = {}
    for i, config in enumerate(configs):
        own = [r for r in rows if r["instance"] == config.instance_id]
        checks[config.instance_id+"_actually_follows"] = any(
            r["forward_back"] > 0 and r["yaw"] != 0 for r in own if 3 <= r["step"] <= 9)
        checks[config.instance_id+"_vertical_follows_own_marker"] = (
            any(r["up_down"]*(300-r["expected_center_y"]) > 0 for r in own if 3 <= r["step"] <= 9)
            if config.vertical_follow else all(r["up_down"] == 0 for r in own))
        loss_start = (10, 20, 30)[i]
        checks[config.instance_id+"_loss_zero"] = all(
            r["state"] == "recovering_target" and not r["found"]
            and r["left_right"] == r["forward_back"] == r["up_down"] == r["yaw"] == 0
            for r in own if loss_start <= r["step"] < loss_start+4)
        checks[config.instance_id+"_recovers"] = own[loss_start+7]["state"] == "tracking"
        stop_at, expected_state = ((45, "stopped"), (50, "paused"), (55, "stopped"))[i]
        checks[config.instance_id+"_explicit_stop_or_pause"] = all(
            r["state"] == expected_state and r["left_right"] == r["forward_back"] == r["up_down"] == r["yaw"] == 0
            for r in own if r["step"] >= stop_at)
    checks["C_frozen_frame_stops"] = all(
        r["state"] == "frame_stale" and r["left_right"] == r["forward_back"] == r["up_down"] == r["yaw"] == 0
        for r in rows if r["instance"] == configs[2].instance_id and r["step"] in (45, 46))
    checks["C_explicit_restart"] = any(
        r["state"] == "tracking" and r["yaw"] > 0
        for r in rows if r["instance"] == configs[2].instance_id and 48 <= r["step"] <= 49)
    with (output / "steps.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    result = dict(mode="offline_generated_images_three_independent_mock_ports",
                  hardware_verified=False, camera_opened=False,
                  control_profiles=[c.control_profile for c in configs],
                  bindings=[c.to_dict() for c in configs], steps=len(rows),
                  violations=violations, scenario_checks=checks,
                  all_stopped=all(not any(p.velocity) and p.lease is None for p in ports),
                  processing_wall_s=time.perf_counter()-started,
                  note="Processing time is sequential local work, not actual camera or SDK capacity.")
    result["passed"] = not violations and all(checks.values()) and result["all_stopped"]
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cv2.setNumThreads(1)
    with patch.object(socket, "socket", side_effect=AssertionError("Network forbidden")), \
         patch("cv2.VideoCapture", side_effect=AssertionError("Camera forbidden")), \
         patch("cv2.imshow", side_effect=AssertionError("GUI forbidden")):
        raise SystemExit(run(args.output))
