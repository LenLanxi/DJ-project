"""Example caller for team 2: generated BGR frames, no camera/device/network.

These are sample local call sequences, NOT an agreed team-2 transport protocol.
The real owner must schedule tick even while no frames arrive.
"""
from dataclasses import asdict
import json
from pathlib import Path
from unittest.mock import patch

from r3_follow import FollowController, MarkerTracker, __version__
from r3_follow.core import State
from r3_follow.vision import synthetic_frame


def run():
    tracker = MarkerTracker(target_id=0)
    # Integration defaults: no automatic restart and no simulated ramp.
    controller = FollowController(target_id=0, max_age=.5)
    checks = []
    seq = 0

    def consume(frame, frame_id, received_at, now):
        # The image owner supplies a new frame id and local monotonic timestamp.
        # Never assign a fresh timestamp to the same cached image.
        observation = tracker.detect(frame, frame_id, received_at, source="team2-mock")
        controller.observe(observation)
        return controller.tick(now)

    def new_frame(now, **options):
        nonlocal seq
        seq += 1
        return consume(synthetic_frame(**options), seq, now, now)

    def check(name, expected_state, sign=None):
        request = controller.last_request
        assert controller.state == expected_state, (name, controller.state)
        assert request.simulation_only is True
        if expected_state != State.TRACKING:
            assert request.right == request.forward == 0
        if sign is not None:
            assert request.right*sign > 0
        checks.append({"check": name, "request": asdict(request),
                       "status": {**controller.status(), "request": asdict(request)}})

    # This guard makes accidental networking fail during the example.
    with patch("socket.socket", side_effect=AssertionError("Offline example cannot use network")):
        new_frame(0)
        check("initially_stopped", State.STOPPED)
        assert controller.calibrate(0)[0]
        assert controller.start(0)[0]
        new_frame(.1, x=.8)
        check("current_target_right", State.TRACKING, 1)

        # Logical handoff example: pause before another module requests control.
        # Actual leases, command cancellation and manual override belong to team 2.
        controller.pause()
        check("pause_before_handoff", State.PAUSED)
        new_frame(.2, x=.2)
        check("new_frame_does_not_undo_pause", State.PAUSED)
        assert controller.resume(.2)[0]
        check("explicit_resume_rechecks_target", State.TRACKING, -1)

        new_frame(.3, visible=False)
        check("loss_zero_default_requires_start", State.LOST)
        new_frame(.4)
        check("reappearance_cannot_restart_default", State.LOST)
        assert controller.start(.4)[0]

        controller.tick(1.0)  # no incoming frames, but owner still calls watchdog
        check("no_frame_watchdog", State.STALE)
        consume(synthetic_frame(x=.8), seq, 1.0, 1.0)
        check("cached_frame_cannot_refresh", State.STALE)
        new_frame(1.1)
        check("new_frame_after_stale_requires_start", State.STALE)
        assert controller.start(1.1)[0]

        controller.stop()
        new_frame(1.2, x=.8)
        check("manual_stop_not_overridden", State.STOPPED)

    report = {"version": __version__, "input": "generated_images",
              "hardware_verified": False, "team2_protocol_agreed": False,
              "checks_passed": len(checks), "checks": checks,
              "defaults": {"recovery_window": controller.recovery_window,
                           "max_request_rate": controller.max_request_rate}}
    output = Path(__file__).parent / "logs" / "group2_offline_example.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"PASS {len(checks)} integration examples; no camera, drone or network.\n{output}")
    return report


if __name__ == "__main__":
    run()
