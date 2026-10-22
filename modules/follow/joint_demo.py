"""CLI only: real background detector + SDK stub + group-4 scripted Agent.

All images are generated. This opens no desktop/camera and uses no real model.
"""
import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

from joint_integration_check import BASE


def scenario(repository):
    from r3_follow.service_ports import FrameSnapshot
    from r3_follow.team2_adapter import Team2StubPort
    from r3_follow.team2_joint import JointRuntime, JointDemo
    from r3_follow.vision import synthetic_frame
    seq = 0
    position = .5
    def generated():
        nonlocal seq
        seq += 1
        frame = synthetic_frame(x=position)
        return FrameSnapshot(frame, seq, time.time())
    port = Team2StubPort(repository, frame_provider=generated)
    runtime = JointRuntime(port)
    sdk, service = port.sdk, runtime.bridge.service
    records = []
    def wait_for(predicate, reason, timeout=2):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError(reason)
            time.sleep(.01)
    sys.path.insert(0, str(Path(repository)/"modules"/"ai"))
    from ai_agent import Agent
    from demo_ai import ScriptedClient
    import program_runner
    # Explicit offline-only constructor injection; validation remains upstream.
    with patch.object(program_runner, "MockDrone", JointRuntime):
        runner = program_runner.Runner(JointDemo(runtime))
    agent = Agent(ScriptedClient(), runner)
    try:
        sdk._driver.set_simulated_delay(0)
        state = sdk.get_status()
        if state.control_owner != "none" or state.flight_state != "landed":
            raise AssertionError("Start this offline demo in a fresh process with an idle SDK")
        service.capture_once()
        assert service.calibrate_reference().ok
        service.start_background()
        for text in ("还有多少电", "起飞", "跟着我"):
            records.append(agent.handle(text))
        wait_for(runtime.is_following, "Follow did not start")
        position = .8
        wait_for(lambda: bool(service.last_channels and service.last_channels[0] > 0),
                 "No positive follow request from generated marker")
        sdk._driver.set_simulated_delay(.25)
        records.append(agent.handle("向前走30厘米"))
        reply = json.loads(records[-1]["reply"].removeprefix("[模拟] "))
        assert reply["resumed"], reply
        records.append(agent.handle("降落"))
        assert runtime.phase == "landing"
        assert not json.loads(records[-1]["reply"].removeprefix("[模拟] "))["landed_confirmed"]
        landed = None
        def landing_confirmed():
            nonlocal landed
            landed = runtime.poll_landing()
            return landed["landed_confirmed"]
        wait_for(landing_confirmed, "No fresh landed status")
        assert landed["landed_confirmed"]
        assert not runtime.is_following()
        return dict(successful=True, source="generated_ID0_Bgr_frames",
                    planner="upstream_ScriptedClient_NOT_AI", records=records,
                    landing_status=landed, service_events=list(service.events),
                    adapter_events=list(port.events), coordinator_events=list(runtime.events),
                    camera_opened=False, desktop_opened=False, hardware_verified=False)
    finally:
        sdk._driver.set_simulated_delay(0)
        runtime.close()
        owner = sdk.get_status().control_owner
        if owner != "none":
            sdk.land(owner)
            sdk.release_control(owner)
        for worker in service.workers:
            worker.join(1)
        if any(worker.is_alive() for worker in service.workers):
            raise AssertionError("Follow workers did not shut down")


def main():
    parser = argparse.ArgumentParser()
    default_repo = BASE.parents[1] if (BASE.parents[1]/"drone_sdk").is_dir() else BASE.parent/"tmp"/"team2_b9f37f9"
    parser.add_argument("--repository", type=Path, default=default_repo)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    import cv2
    import numpy as np
    if sys.version_info[:2] != (3, 11) or np.__version__ != "1.26.4" or cv2.__version__ != "4.8.0":
        raise SystemExit("Use the agreed Python 3.11 environment")
    import hashlib
    pin = json.loads((BASE/"team2_candidate_sources.json").read_text(encoding="utf-8"))
    for name, expected in pin["sha256_lf"].items():
        if hashlib.sha256((args.repository/name).read_bytes().replace(b"\r\n", b"\n")).hexdigest() != expected:
            raise SystemExit("Review changed source before running: " + name)
    output = args.output or BASE/"logs"/"joint311"/("demo_"+str(time.time_ns()))
    output.mkdir(parents=True, exist_ok=True)
    console = io.StringIO()
    cv2.setNumThreads(1)
    with contextlib.redirect_stdout(console), \
         patch("socket.socket", side_effect=AssertionError("Network forbidden")), \
         patch("cv2.VideoCapture", side_effect=AssertionError("Camera forbidden")), \
         patch("tkinter.Tk", side_effect=AssertionError("Desktop forbidden")):
        result = scenario(args.repository.resolve())
    result["commit"] = pin["commit"]
    (output/"scenario.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (output/"sdk_console.txt").write_text(console.getvalue(), encoding="utf-8")
    print("Passed: generated image -> actual detector/background follow -> scripted model takeover -> conditional resume -> land acknowledgement -> status check")
    print("No real voice/model/camera/drone. Evidence:", output)


if __name__ == "__main__":
    main()
