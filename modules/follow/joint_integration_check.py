import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from simulation_test_support import settle_landing, settle_workers
"""Python 3.11 only; guarded, repeatable local joint integration checks."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parent


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_suite(suite):
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    report = dict(run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                  skipped=len(result.skipped), passed=result.testsRun-len(result.failures)
                  -len(result.errors)-len(result.skipped),
                  successful=result.wasSuccessful() and not result.skipped)
    return report, stream.getvalue()


def main():
    parser = argparse.ArgumentParser()
    default_repo = BASE.parents[1] if (BASE.parents[1]/"drone_sdk").is_dir() else BASE.parent/"tmp"/"team2_b9f37f9"
    parser.add_argument("--repository", type=Path, default=default_repo)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", choices=('upstream', 'candidate'), default='upstream')
    parser.add_argument("--require-team-env", action="store_true",
                        help="Compatibility option; this check always requires Python 3.11")
    args = parser.parse_args()
    import cv2
    import numpy as np
    if (sys.version_info[:2] != (3, 11) or np.__version__ != "1.26.4"
            or cv2.__version__ != "4.8.0"):
        raise SystemExit("Use Python 3.11 / NumPy 1.26.4 / OpenCV 4.8.0")
    cv2.setNumThreads(1)
    repo = args.repository.resolve()
    pin_name = 'team2_candidate_sources.json' if args.profile == 'candidate' else 'team2_reviewed_sources.json'
    pin = json.loads((BASE/pin_name).read_text(encoding="utf-8"))
    for name, expected in pin["sha256_lf"].items():
        actual = hashlib.sha256((repo/name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        if actual != expected:
            raise SystemExit("Source changed; review before execution: " + name)
    output = args.output.resolve() if args.output else BASE/"logs"/"joint311"/str(time.time_ns())
    output.mkdir(parents=True, exist_ok=True)
    report = dict(commit=pin["commit"], sdk_commit=pin.get("sdk_commit", pin["commit"]),
                  python=sys.version, executable=sys.executable,
                  numpy=np.__version__, opencv=cv2.__version__, repository=str(repo),
                  camera_opened=False, desktop_opened=False, hardware_connected=False,
                  real_model_used=False, synthetic_frames_injected=True,
                  upstream_modified=args.profile=='candidate', profile=args.profile,
                  group4_v04_validated=False, suites={})
    console, details = io.StringIO(), []
    with contextlib.redirect_stdout(console), \
         patch("socket.socket", side_effect=AssertionError("Network forbidden")), \
         patch("cv2.VideoCapture", side_effect=AssertionError("Camera forbidden")), \
         patch("tkinter.Tk", side_effect=AssertionError("Desktop forbidden")):
        sys.path.insert(0, str(repo))
        sys.path.insert(0, str(repo/"modules"/"ai"))
        from r3_follow.team2_adapter import load_reviewed_stub
        sdk = load_reviewed_stub(repo)
        for name in ("test_team2_integration", "test_team2_ack_semantics"):
            module = load(BASE/"tests"/(name+".py"), name)
            module.REPO = repo
            suite = unittest.defaultTestLoader.loadTestsFromModule(module)
            result, text = run_suite(suite)
            report["suites"][name] = result
            details.append(text)
        joint = load(BASE/"tests"/"test_team2_joint.py", "test_team2_joint")
        joint.REPO = repo
        for name, suite in (
                ("joint_behavior", unittest.defaultTestLoader.loadTestsFromTestCase(joint.JointTests)),
                ("updated_session_interrupt", unittest.TestSuite([
                    joint.UpstreamSessionTests("test_updated_session_dispatches_stop_before_blocked_action_finishes")]))):
            result, text = run_suite(suite)
            report["suites"][name] = result
            details.append(text)
        report["interrupt_measurements"] = joint.MEASUREMENTS
        for name in ('test_agent', 'test_demo', 'test_independent'):
            module = load(repo/'modules'/'ai'/(name+'.py'), 'upstream_ai_'+name)
            result, text = run_suite(unittest.defaultTestLoader.loadTestsFromModule(module))
            report['suites']['upstream_ai_'+name] = result
            details.append(text)
        # Baseline upstream checks kept separate from the joint behavior counts.
        concurrency = load(repo/"test_concurrency.py", "upstream_concurrency")
        result, text = run_suite(unittest.defaultTestLoader.loadTestsFromModule(concurrency))
        report["suites"]["upstream_concurrency"] = result
        details.append(text)
        full = load(repo/"test_full_integration.py", "upstream_full_integration")
        result, text = run_suite(unittest.defaultTestLoader.loadTestsFromModule(full))
        report["suites"]["upstream_full_with_local_follow"] = result
        details.append(text)
        interrupts = load(repo/"test_session_interrupt.py", "upstream_session_interrupt")
        result, text = run_suite(unittest.defaultTestLoader.loadTestsFromModule(interrupts))
        report["suites"]["upstream_session_interrupt"] = result
        details.append(text)
        smoke = load(repo/"integration_smoke.py", "upstream_smoke")
        sdk._driver.set_simulated_delay(0)
        # The full suite may leave a flying stub with owner=none. Establish the
        # smoke suite's documented landed precondition through public APIs.
        sdk.stop_follow()
        owner = sdk.get_status().control_owner
        if owner != "none":
            sdk.release_control(owner)
        assert sdk.request_control("model").ok
        assert sdk.land("model").ok
        settle_landing(sdk)
        assert sdk.release_control("model").ok
        assert sdk.get_status().flight_state == "landed"
        smoke.reset(smoke.UnifiedDrone())
        smoke.main()
        report["upstream_smoke"] = dict(passed=len(smoke.PASSED), failed=len(smoke.FAILED),
                                         failures=smoke.FAILED)
        owner = sdk.get_status().control_owner
        if owner != "none":
            sdk.land(owner)
            settle_landing(sdk)
            sdk.release_control(owner)
        sdk.stop_follow()
    report["successful"] = (all(v["successful"] for v in report["suites"].values())
                             and not report["upstream_smoke"]["failed"])
    report["follow_module_used"] = str(Path(sys.modules["r3_follow.service"].__file__).resolve())
    report["known_gaps"] = [
        "Simulator only; no physical camera feed, stop or landing verified",
        "Frame producer must publish actual capture ids/timestamps when hardware is integrated",
        "Epoch protection requires reviewed callers to carry the grant; legacy owner-only calls lack ABA protection",
        "v4 host protocol/coordination is checked separately; Docker and live voice remain untested",
    ]
    if args.profile == 'upstream':
        report['known_gaps'] += [
            'SDK start holds lifecycle lock while waiting for a frame; concurrent stop waits too',
            'Failed SDK start retains capture/control workers until an explicit stop',
            'Session dispatch lock is fixed, but Demo can still restart after a cancelled action',
        ]
    report['group4_scope'] = ('This suite runs modules/ai Session, Agent and restricted Runner. '
        'run_offline.py separately checks the v4 gateway, event flow and simulated container protocol.')
    report["source_sha256"] = {p.relative_to(BASE).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in [*(BASE/"r3_follow").glob("*.py"),
                                         *(BASE/"tests").glob("test_team2_*.py"),
                                         BASE/"joint_integration_check.py", BASE/"team2_reviewed_sources.json"]}
    (output/"result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output/"tests.txt").write_text("\n".join(details), encoding="utf-8")
    (output/"sdk_console.txt").write_text(console.getvalue(), encoding="utf-8")
    print(json.dumps({k:report[k] for k in ("commit", "suites", "upstream_smoke", "successful")}, indent=2))
    print("Evidence:", output)
    return 0 if report["successful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
