"""Exercise installed DJITelloPy APIs without creating a device or socket."""
from dataclasses import asdict
import json
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import patch
import importlib.metadata

import cv2
import numpy as np
from djitellopy import Tello
from djitellopy.tello import BackgroundFrameRead

from r3_follow.core import FollowController, State
from r3_follow.tello_adapter import LatestSDKFrame, preview_rc
from r3_follow.vision import MarkerTracker, synthetic_frame


def run():
    rows = []
    # Use the installed SDK frame property/setter, but bypass decoder startup.
    sdk_reader = object.__new__(BackgroundFrameRead)
    sdk_reader.lock = Lock()
    sdk_reader.with_queue = False
    sdk_reader.stopped = False
    sdk_reader.frame = np.zeros((300, 400, 3), dtype=np.uint8)
    adapter = LatestSDKFrame(sdk_reader)
    tracker = MarkerTracker()
    controller = FollowController(recovery_window=1.5)
    wire = []
    transport = SimpleNamespace(last_rc_control_timestamp=0,
        TIME_BTW_RC_CONTROL_COMMANDS=0, send_command_without_return=wire.append)

    def frame(t, x=.7, visible=True):
        sdk_reader.frame = cv2.cvtColor(synthetic_frame(x=x, visible=visible),
                                        cv2.COLOR_BGR2RGB)
        seq, stamp, bgr = adapter.poll(t)
        controller.observe(tracker.detect(bgr, seq, stamp, "tello-offline"))
        return controller.tick(t)

    def checkpoint(label, expected_state):
        request = controller.last_request
        assert controller.state == expected_state, (label, controller.state)
        rc = preview_rc(request, full_scale=100)
        # Call the actual installed SDK encoder with a recording-only transport.
        # No Tello instance is created and socket creation is prohibited below.
        transport.last_rc_control_timestamp = 0
        Tello.send_rc_control(transport, rc.left_right, rc.forward_backward,
                             rc.up_down, rc.yaw)
        rows.append({"check": label, "request": asdict(request),
                     "rc_preview": asdict(rc), "recorded_sdk_command": wire[-1]})
        if expected_state != State.TRACKING:
            assert wire[-1] == "rc 0 0 0 0"

    with patch("socket.socket", side_effect=AssertionError("Offline test cannot open sockets")):
        frame(0)
        assert controller.calibrate(0)[0]
        assert controller.start(0)[0]
        frame(.1, x=.7)
        checkpoint("right_adjustment", State.TRACKING)
        assert rows[-1]["rc_preview"]["left_right"] > 0
        frame(.2, visible=False)
        checkpoint("missing_target_zero", State.RECOVERING)
        for t in (.4, .5, .6):
            frame(t)
        checkpoint("stable_reacquisition", State.TRACKING)
        # Poll the same cached SDK frame: timestamp must remain .6.
        seq, stamp, bgr = adapter.poll(1.2)
        assert stamp == .6
        controller.observe(tracker.detect(bgr, seq, stamp, "tello-offline"))
        controller.tick(1.2)
        checkpoint("frozen_sdk_frame_zero", State.STALE)
        frame(1.3)
        checkpoint("fresh_frame_cannot_restart_after_stale", State.STALE)
        controller.start(1.3)
        for t in (1.4, 1.6, 1.8, 2., 2.2, 2.4, 2.6, 2.8, 3.0):
            frame(t, visible=False)
        checkpoint("long_loss_timeout_zero", State.LOST)
        frame(3.1)
        checkpoint("reappearance_after_timeout_stays_zero", State.LOST)
        controller.start(3.1)
        controller.stop()
        frame(3.2)
        checkpoint("manual_stop_stays_zero", State.STOPPED)

    report = {"mode": "offline_only", "hardware_verified": False,
        "djitellopy": importlib.metadata.version("djitellopy"),
        "checks_passed": len(rows), "checks": rows,
        "note": "SDK commands were captured in a list; no network or aircraft control."}
    path = Path(__file__).parent / "logs" / "tello_offline_check.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"PASS {len(rows)} DJITelloPy offline checks. No drone connection.\n{path}")
    return report


if __name__ == "__main__":
    run()
