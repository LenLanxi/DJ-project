"""Reproducible midterm scenario; outputs real detections on generated images."""
import json
from dataclasses import asdict
from pathlib import Path
from .core import FollowController
from .vision import MarkerTracker, synthetic_frame


def run(path):
    controller, detector = FollowController(), MarkerTracker()
    rows = []
    seq = 0

    def frame(now, **kwargs):
        nonlocal seq
        seq += 1
        controller.observe(detector.detect(synthetic_frame(**kwargs), seq, now, "synthetic"))

    def record(name, now):
        request = controller.tick(now)
        rows.append({"event": name, "test_time_s": now, "input_type": "synthetic_image",
                     "observation": asdict(controller.observation), "request": asdict(request)})
        return request

    def zero(request):
        return request.right == request.forward == request.up == request.yaw == 0

    frame(0)
    assert zero(record("default_stopped", 0))
    assert controller.calibrate(0)[0]
    assert controller.start(0)[0]
    assert zero(record("start_centered", 0))
    frame(.1, x=.8)
    assert record("target_right", .1).right > 0
    frame(.2, size=.15)
    assert record("target_smaller", .2).forward > 0
    controller.pause()
    assert zero(record("paused", .2))
    frame(.3, x=.2)
    assert zero(record("paused_target_moved", .3))
    assert controller.resume(.3)[0]
    assert record("resumed_left", .3).right < 0
    frame(.35, marker_id=1)
    assert zero(record("wrong_marker_not_followed", .35))
    frame(.37)
    assert controller.start(.37)[0]
    frame(.4, visible=False)
    assert zero(record("target_lost", .4))
    frame(.5)
    assert zero(record("reacquired_requires_start", .5))
    assert controller.start(.5)[0]
    assert zero(record("stream_frozen_watchdog", 1.1))
    frame(1.2)
    assert zero(record("new_frame_still_inactive", 1.2))
    assert controller.start(1.2)[0]
    controller.stop()
    frame(1.3, x=.8)
    assert zero(record("stop_stays_stopped", 1.3))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return rows
