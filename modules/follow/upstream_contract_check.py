import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from simulation_test_support import settle_landing, settle_workers
"""Desired-behavior checks for the unchanged public SDK/AI path with local v14.

Failures are retained as unresolved integration issues, never counted as passes.
No camera, desktop, model, Docker or aircraft is used.
"""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parent
REPO = None
MEASUREMENTS = {}


class PublicPathChecks(unittest.TestCase):
    def setUp(self):
        from r3_follow.team2_adapter import load_reviewed_stub
        from r3_follow.vision import synthetic_frame
        from drone_sdk.ai_bridge import UnifiedDrone
        self.sdk = load_reviewed_stub(REPO)
        self.sdk._driver.set_simulated_delay(0)
        self.sdk.stop_follow()
        self.sdk.set_frame_provider(None)
        owner = self.sdk.get_status().control_owner
        if owner != 'none':
            self.sdk.release_control(owner)
        self.sdk.request_control('model')
        self.sdk.land('model')
        settle_landing(self.sdk)
        UnifiedDrone().get_status()  # Confirm prior asynchronous landing before next case.
        self.sdk.request_control('model')
        self.sdk.takeoff('model')
        self.sdk.release_control('model')
        self.drone = UnifiedDrone()
        self.image = synthetic_frame(x=.65, size=.25)
        self.seq = 0
        self.sessions = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for session in self.sessions:
            session.close()
        self.sdk.stop_follow()
        self.sdk.set_frame_provider(None)
        self.sdk._driver.set_simulated_delay(0)
        owner = self.sdk.get_status().control_owner
        if owner != 'none':
            self.sdk.land(owner)
            settle_landing(self.sdk)
            self.drone.get_status()
            self.sdk.release_control(owner)

    def feed(self):
        from drone_sdk.contract import FrameResult
        self.seq += 1
        return FrameResult(self.image, self.seq, time.time(), False)

    def following(self):
        self.sdk.set_frame_provider(self.feed)
        self.assertTrue(self.sdk.start_follow(timeout=1).ok)

    def session(self):
        import program_runner
        from ai_agent import Agent
        from demo_text import Demo
        from drone_sdk.ai_bridge import UnifiedDrone
        from session import Session
        class Client:
            last_usage = {}
            def complete(self, messages):
                return json.dumps({'kind': 'program', 'code': 'drone.move_forward(30)'})
        # Same explicit simulation-only seam used by upstream integration tests.
        with patch.object(program_runner, 'MockDrone', UnifiedDrone):
            runner = program_runner.Runner(Demo(self.drone))
        result = Session(Agent(Client(), runner))
        self.sessions.append(result)
        return result

    def test_failed_start_cleans_workers(self):
        result = self.sdk.start_follow(timeout=.1)
        self.assertFalse(result.ok)
        bridge = self.sdk._follow._bridge
        alive = [] if bridge is None else [t.name for t in bridge.service.workers if t.is_alive()]
        MEASUREMENTS['failed_start_live_workers'] = alive
        self.assertEqual(alive, [], 'Failed startup must close its workers')

    def test_stop_cancels_waiting_start(self):
        from drone_sdk.contract import FrameResult
        waiting = threading.Event()
        def no_frame():
            waiting.set()
            return FrameResult(None, 0, time.time(), True)
        self.sdk.set_frame_provider(no_frame)
        result = []
        worker = threading.Thread(target=lambda: result.append(self.sdk.start_follow(timeout=.6)))
        try:
            worker.start()
            self.assertTrue(waiting.wait(1))
            begin = time.perf_counter()
            stopped = self.sdk.stop_follow()
            elapsed = time.perf_counter() - begin
        finally:
            worker.join(2)
        MEASUREMENTS['stop_during_start_wait_ms'] = elapsed * 1000
        self.assertTrue(stopped.ok)
        self.assertLess(elapsed, .2, 'Stop waited for startup timeout')

    def test_session_stop_reaches_sdk_while_action_is_blocked(self):
        self.following()
        session = self.session()
        entered, release, stop_sent = threading.Event(), threading.Event(), threading.Event()
        original_stop = self.sdk.stop_motion
        def blocking(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('Test barrier timeout')
        def stop(owner, **grant):
            stop_sent.set()
            return original_stop(owner, **grant)
        with patch.object(self.sdk._driver, 'execute', side_effect=blocking), \
             patch.object(self.sdk, 'stop_motion', side_effect=stop):
            try:
                turn = session.submit('move')
                self.assertTrue(entered.wait(2))
                stop_sent.clear()  # Ignore the zero/stop issued when pausing follow.
                begin = time.perf_counter()
                stopped = session.interrupt()
                dispatch_ms = (time.perf_counter() - begin) * 1000
                self.assertTrue(stop_sent.wait(.5))
                arrival_ms = (time.perf_counter() - begin) * 1000
                self.assertTrue(stopped.result(timeout=1)['accepted'])
                self.assertFalse(turn.done())
            finally:
                release.set()
                record = turn.result(timeout=3)
        MEASUREMENTS['session_interrupt'] = dict(dispatch_ms=dispatch_ms,
            stop_reached_sdk_ms=arrival_ms, action_was_still_blocked=True,
            late_reply_cancelled=record.get('cancelled', False), physical_stop_verified=False)

    def test_late_action_does_not_restart_after_explicit_stop(self):
        self.following()
        session = self.session()
        entered, release = threading.Event(), threading.Event()
        def blocking(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('Test barrier timeout')
        with patch.object(self.sdk._driver, 'execute', side_effect=blocking):
            try:
                turn = session.submit('move')
                self.assertTrue(entered.wait(2))
                session.interrupt().result(timeout=1)
                self.assertFalse(self.sdk.is_following())
            finally:
                release.set()
                record = turn.result(timeout=3)
        following = self.sdk.is_following()
        MEASUREMENTS['late_action'] = dict(reply_cancelled=record.get('cancelled', False),
            following_after_return=following, owner=self.sdk.get_status().control_owner)
        self.assertFalse(following, 'Cancelled reply is insufficient: old Demo restarted follow')

    def test_resume_keeps_original_reference_size(self):
        from r3_follow.vision import synthetic_frame
        from demo_text import Demo
        self.following()
        before = self.sdk._follow._bridge.service.controller.reference_size
        def moved(*args):
            self.image = synthetic_frame(x=.65, size=.35)
            time.sleep(.09)  # Background detector must see a post-handoff frame.
            return True
        with patch.object(self.sdk._driver, 'execute', side_effect=moved):
            Demo(self.drone).handle('前进30厘米')
        after = self.sdk._follow._bridge.service.controller.reference_size
        self.assertTrue(self.sdk.is_following(), 'Valid completed action should resume')
        MEASUREMENTS['reference_size'] = dict(before=before, after=after)
        self.assertAlmostEqual(before, after, places=6,
                               msg='Restart recalibrated instead of retaining follow distance')

    def test_target_visible_is_actual_detection(self):
        from r3_follow.vision import synthetic_frame
        self.following()
        self.image = synthetic_frame(visible=False)
        service = self.sdk._follow._bridge.service
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            obs = service.controller.observation
            if obs and not obs.found:
                break
            time.sleep(.01)
        self.assertIsNotNone(service.controller.observation)
        self.assertFalse(service.controller.observation.found)
        self.assertFalse(self.drone.get_status()['target_visible'])

    def test_async_land_ack_does_not_claim_landed(self):
        from drone_sdk.contract import CommandResult
        self.drone.request_control('model')
        with patch.object(self.sdk, 'land', return_value=CommandResult(True, 'accepted')):
            result = self.drone.land('model')
        self.assertEqual(self.sdk.get_status().flight_state, 'flying')
        MEASUREMENTS['async_land_reply'] = result
        self.assertNotIn('完成', result['message'], 'Send acknowledgement is not landing completion')

    def test_accepted_normal_action_never_resumes_follow(self):
        from demo_text import Demo
        from drone_sdk.contract import CommandResult
        self.following()
        with patch.object(self.sdk, 'move_forward', return_value=CommandResult(True, 'accepted')):
            Demo(self.drone).handle('前进30厘米')
        self.assertFalse(self.sdk.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, 'none')

    def test_target_lost_during_action_cannot_resume(self):
        from demo_text import Demo
        from r3_follow.vision import synthetic_frame
        self.following()
        def moved(*args):
            self.image = synthetic_frame(visible=False)
            time.sleep(.1)
        with patch.object(self.sdk._driver, 'execute', side_effect=moved):
            Demo(self.drone).handle('前进30厘米')
        self.assertFalse(self.sdk.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, 'none')

    def test_frozen_frame_during_action_cannot_resume(self):
        from demo_text import Demo
        self.following()
        packet = self.feed()
        self.sdk.set_frame_provider(lambda: packet)
        with patch.object(self.sdk._driver, 'execute', side_effect=lambda *a: time.sleep(.6)):
            Demo(self.drone).handle('前进30厘米')
        self.assertFalse(self.sdk.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, 'none')

    def test_action_exception_does_not_resume(self):
        from demo_text import Demo
        self.following()
        with patch.object(self.sdk._driver, 'execute', side_effect=RuntimeError('injected_failure')):
            reply = Demo(self.drone).handle('前进30厘米')
        self.assertIn('不恢复跟随', reply)
        self.assertFalse(self.sdk.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, 'none')

    def test_busy_start_cannot_release_model_control(self):
        self.sdk.set_frame_provider(self.feed)
        self.sdk.request_control('model')
        result = self.sdk.start_follow()
        self.assertFalse(result.ok)
        self.assertEqual(self.sdk.get_status().control_owner, 'model')

    def test_pending_landing_blocks_other_motion_until_fresh_status(self):
        from dataclasses import replace
        from drone_sdk.ai_bridge import UnifiedDrone
        from drone_sdk.contract import CommandResult
        self.drone.request_control('model')
        old = self.sdk.get_status()
        with patch.object(self.sdk, 'land', return_value=CommandResult(True, 'accepted')):
            result = self.drone.land('model')
        self.assertTrue(result['ok'])
        other = UnifiedDrone()  # Barrier must apply to every wrapper instance.
        with patch.object(self.sdk, 'stop_motion', wraps=self.sdk.stop_motion) as stop, \
             patch.object(self.sdk, 'move_forward', wraps=self.sdk.move_forward) as move:
            self.assertFalse(other.stop_motion('model')['ok'])
            self.assertFalse(other.move_forward('model', 30)['ok'])
            self.assertFalse(other.takeoff('model')['ok'])
            self.assertFalse(other.start_follow()['ok'])
            self.assertFalse(other.release_control('model')['ok'])
            self.assertEqual(stop.call_count, 0)
            self.assertEqual(move.call_count, 0)
        for state in (replace(old, flight_state='landed'),
                      replace(old, flight_state='landed', timestamp=time.time()-1),
                      replace(old, flight_state='unknown', timestamp=time.time()),
                      replace(old, connected=False, flight_state='landed', timestamp=time.time())):
            with patch.object(self.sdk, 'get_status', return_value=state):
                self.assertFalse(other.stop_motion('model')['ok'])
        self.sdk.land('model')
        settle_landing(self.sdk)
        time.sleep(.002)
        self.assertEqual(other.get_status()['flight_state'], 'landed')
        self.assertFalse(self.sdk.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, 'none')

    def test_stop_during_start_cannot_be_undone_by_late_frame(self):
        from drone_sdk.contract import FrameResult
        waiting = threading.Event()
        def empty():
            waiting.set()
            return FrameResult(None, 0, time.time(), True)
        self.sdk.set_frame_provider(empty)
        results = []
        worker = threading.Thread(target=lambda: results.append(self.sdk.start_follow(timeout=.3)))
        worker.start()
        try:
            self.assertTrue(waiting.wait(1))
            self.sdk.stop_follow()
            self.sdk.set_frame_provider(self.feed)
        finally:
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertFalse(results[0].ok)
        self.assertFalse(self.sdk.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, 'none')


def main():
    global REPO
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profile', choices=('upstream','candidate'), default='upstream')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 11):
        raise SystemExit('Python 3.11 required')
    REPO = args.repository.resolve()
    pin_name = 'team2_candidate_sources.json' if args.profile == 'candidate' else 'team2_reviewed_sources.json'
    pin = json.loads((BASE/pin_name).read_text(encoding='utf-8'))
    for name, digest in pin['sha256_lf'].items():
        if hashlib.sha256((REPO/name).read_bytes().replace(b'\r\n', b'\n')).hexdigest() != digest:
            raise SystemExit('Unreviewed source: ' + name)
    args.output.mkdir(parents=True, exist_ok=True)
    import cv2
    cv2.setNumThreads(1)
    console, tests = io.StringIO(), io.StringIO()
    sys.path[:0] = [str(BASE), str(REPO), str(REPO/'modules'/'ai')]
    with contextlib.redirect_stdout(console), \
         patch('socket.socket', side_effect=AssertionError('Network forbidden')), \
         patch('cv2.VideoCapture', side_effect=AssertionError('Camera forbidden')), \
         patch('tkinter.Tk', side_effect=AssertionError('Desktop forbidden')):
        from r3_follow.team2_adapter import load_reviewed_stub
        load_reviewed_stub(REPO)
        result = unittest.TextTestRunner(stream=tests, verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(PublicPathChecks))
    report = dict(commit=pin['commit'], python=sys.version, tests_run=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
        passed=result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
        successful=result.wasSuccessful(), measurements=MEASUREMENTS,
        unresolved=[dict(test=t.id(), detail=detail) for t, detail in result.failures+result.errors],
        actual_follow_module=sys.modules['r3_follow.service'].__file__,
        sdk_ai_modified=args.profile=='candidate', generated_images=True, real_camera_or_flight=False,
        profile=args.profile, scope='Public SDK and AI Demo/Session with local v14; desired behaviors')
    (args.output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (args.output/'tests.txt').write_text(tests.getvalue(),encoding='utf-8')
    (args.output/'console.txt').write_text(console.getvalue(),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('tests_run','passed','failures','errors','measurements')},ensure_ascii=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
