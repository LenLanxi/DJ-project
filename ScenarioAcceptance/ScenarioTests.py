"""Offline acceptance probes for DJ-project at its current checkout.

Run with Python 3.11, numpy 1.26.4 and opencv-python 4.8.0.76.
--all runs existing offline suites and these acceptance probes in subprocesses.
Default runs these probes only. Known gaps are XFAIL; --strict makes them FAIL.
No microphones, network calls, aircraft, cloud model or physical simulator.
Model replies and hardware are explicit fakes; camera input is synthetic.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(encoding='utf-8')

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESULTS = HERE / 'Results'
CATALOG = json.loads((HERE / 'Cases.json').read_text(encoding='utf-8'))
KNOWN_GAPS = {case['id'] for case in CATALOG['cases'] if case['expected'] == 'known_gap'}
for folder in (ROOT, ROOT / 'modules' / 'ai', ROOT / 'modules' / 'follow'):
    sys.path.insert(0, str(folder))

from drone_sdk import controller as ctl, _driver as drv, ai_bridge
from drone_sdk.contract import FrameResult
from drone_sdk.ai_bridge import UnifiedDrone
from ai_agent import Agent
from demo_text import Demo
from program_runner import Runner
from r3_follow.vision import synthetic_frame


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


entry = load_module('ReviewedAIMain', ROOT / 'modules' / 'ai' / 'main.py')


def wait_for(predicate, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(.01)
    return bool(predicate())


class PlannedReply:
    """Inject a model response, never claim to measure model understanding."""
    def __init__(self, code=None, kind='program'):
        self.plan = {'kind': kind, 'code': code, 'question': '前面的红色帐篷在哪里？'}
        self.messages = None

    def complete(self, messages):
        self.messages = messages
        return json.dumps(self.plan, ensure_ascii=False)


class Feed:
    def __init__(self):
        self.size, self.x, self.visible = .25, .5, True
        self.number, self.cache = 0, {}

    def __call__(self):
        key = (self.size, self.x, self.visible)
        if key not in self.cache:
            self.cache[key] = synthetic_frame(size=self.size, x=self.x, visible=self.visible)
        self.number += 1
        return FrameResult(self.cache[key], self.number, time.time(), False)


class FakeTello:
    def __init__(self, battery=73):
        self.battery, self.is_flying = battery, False
        self.calls, self.address = [], ('fake', 8889)
        self.land_entered, self.land_release = threading.Event(), threading.Event()
        self.land_release.set()
        self.land_done = threading.Event()
        self.move_entered, self.move_release, self.move_done = threading.Event(), threading.Event(), threading.Event()
        self.move_release.set()

    def connect(self): self.calls.append('connect')
    def set_speed(self, value): self.calls.append(('speed', value))
    def get_battery(self): return self.battery
    def get_height(self): return 80 if self.is_flying else 0
    def get_flight_time(self): return 0
    def send_rc_control(self, *channels): self.calls.append(('rc', channels))
    def send_keepalive(self): self.calls.append('keepalive')
    def takeoff(self):
        self.calls.append('takeoff')
        self.is_flying = True
    def land(self):
        self.calls.append('land')
        self.land_entered.set()
        self.land_release.wait(2)
        self.is_flying = False
        self.land_done.set()
    def move_forward(self, value):
        self.calls.append(('forward_started', value))
        self.move_entered.set()
        self.move_release.wait(2)
        self.calls.append(('forward_completed', value))
        self.move_done.set()
    def turn_motor_off(self): self.calls.append('motoroff')
    def streamoff(self): self.calls.append('streamoff')
    def end(self): self.calls.append('end')


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.network_guards = [patch.object(socket.socket, name,
            side_effect=AssertionError('Network access forbidden in offline probes'))
            for name in ('connect', 'connect_ex', 'sendto')]
        for guard in self.network_guards:
            guard.start()
            self.addCleanup(guard.stop)
        ctl.stop_follow()
        wait_for(lambda: not ctl._follow.status()['background_running'])
        ctl.set_frame_provider(None)
        ctl._frame = FrameResult(None, 0, 0., True)
        ctl._current_owner = None
        ctl._flight_state, ctl._landing_deadline, ctl._active_action = 'landed', None, None
        ctl._last_velocity_time = 0
        ai_bridge._landing_pending = None
        drv.SIMULATED, drv._tello, drv._video = True, None, None
        drv._flying, drv._height_cm, drv._velocity = False, 0, (0, 0, 0, 0)
        drv._link_ok, drv._faults = True, 0
        drv._events.clear()
        drv.set_simulated_delay(0)
        self.fake = None

    def tearDown(self):
        if self.fake:
            self.fake.land_release.set()
            self.fake.move_release.set()
            if self.fake.land_entered.is_set():
                self.fake.land_done.wait(2)
            if self.fake.move_entered.is_set():
                self.fake.move_done.wait(2)
        ctl.stop_follow()
        wait_for(lambda: not ctl._follow.status()['background_running'])
        ctl.set_frame_provider(None)
        if drv._keepalive_stop:
            drv._keepalive_stop.set()
            drv._keepalive_stop = None
        drv.SIMULATED, drv._tello, drv._video = True, None, None
        drv.set_simulated_delay(0)

    def real_runner(self, battery=73, allow=True):
        self.fake = FakeTello(battery)
        args = types.SimpleNamespace(address=None, allow_real_takeoff=allow, min_battery=20)
        with patch.object(drv, '_tello_class', return_value=lambda address: self.fake):
            return entry.build_real_runner(args)

    def following(self):
        ctl.request_control('model')
        self.assertTrue(ctl.takeoff('model').ok)
        ctl.release_control('model')
        feed = Feed()
        ctl.set_frame_provider(feed)
        started = ctl.start_follow(timeout=2)
        self.assertTrue(started.ok, started.reason)
        return feed, UnifiedDrone()

    def test01_walk_forward_produces_forward_request(self):
        feed, _ = self.following()
        feed.size = .18
        self.assertTrue(wait_for(lambda: drv.snapshot()['velocity'][1] > 0),
                        'Shrinking front-facing marker must produce a forward request')

    def test02_lost_marker_stops_motion(self):
        feed, _ = self.following()
        feed.size = .18
        self.assertTrue(wait_for(lambda: drv.snapshot()['velocity'][1] > 0))
        feed.visible = False
        self.assertTrue(wait_for(lambda: drv.snapshot()['velocity'] == (0, 0, 0, 0)))

    def test03_supported_temporary_action_restores_follow(self):
        _, drone = self.following()
        drv.set_simulated_delay(.25)
        result = Runner(Demo(drone), allow_simulated_sdk=True).run('drone.move_forward(30)')
        self.assertTrue(result['accepted'])
        self.assertTrue(ctl.is_following(), result)
        self.assertEqual(ctl.get_status().control_owner, 'follow')

    def test04_failed_action_does_not_restore_follow(self):
        _, drone = self.following()
        with patch.object(drv, 'execute', return_value=False):
            result = Runner(Demo(drone), allow_simulated_sdk=True).run('drone.move_forward(30)')
        self.assertFalse(ctl.is_following(), result)
        self.assertEqual(drv.snapshot()['velocity'], (0, 0, 0, 0))

    def test05_sdk_can_request_full_rotation(self):
        ctl.request_control('model')
        self.assertTrue(ctl.takeoff('model').ok)
        self.assertTrue(ctl.rotate('model', 360).ok)
        self.assertTrue(any(e.get('action') == 'rotate' and e.get('param') == 360
                            for e in drv.snapshot()['events']))

    def test06_ai_accepts_full_scenario_program(self):
        client = PlannedReply("drone.observe('红色帐篷')\ndrone.move_forward(30)\ndrone.rotate(360)\ndrone.start_follow()")
        outcome = Agent(client).handle('飞向前面的红色帐篷，原地旋转一周，随后继续跟随我')
        self.assertTrue(outcome['execution']['accepted'], outcome)

    def test07_ai_can_access_rotation(self):
        outcome = Agent(PlannedReply('drone.rotate(360)')).handle('原地旋转一周')
        self.assertTrue(outcome['execution']['accepted'], outcome)

    def test08_ai_can_observe_navigation_target(self):
        outcome = Agent(PlannedReply("drone.observe('红色帐篷')")).handle('查看前面的红色帐篷位置')
        self.assertTrue(outcome['execution']['accepted'], outcome)

    def test09_real_entry_opens_video(self):
        with patch.object(drv, 'start_video', return_value=True) as start:
            self.real_runner()
        self.assertTrue(start.called, 'Real entry never starts the video stream')

    def test10_real_entry_supplies_ai_camera_vision(self):
        runner, _ = self.real_runner()
        agents = []
        def create_agent(*args, **kwargs):
            agent = Agent(*args, **kwargs)
            agents.append(agent)
            return agent
        with patch.object(sys, 'argv', ['main.py', '--live', '--real-drone']), \
             patch.object(entry, 'ModelClient', return_value=PlannedReply(kind='vision')), \
             patch.object(entry, 'build_real_runner', return_value=(runner, runner.demo.drone)), \
             patch.object(entry, 'Agent', side_effect=create_agent), \
             patch.object(entry, 'shutdown_real'), \
             patch.object(entry.audio_io, 'close'), \
             patch('builtins.input', return_value='/quit'):
            entry.main()
        self.assertTrue(callable(agents[0].vision), 'Real entry leaves Agent.vision unset')

    def test11_shared_status_reports_actual_battery(self):
        _, drone = self.real_runner(battery=73)
        self.assertEqual(drone.get_status()['battery'], 73)
        self.assertEqual(ctl.get_status().battery, 73,
                         'AI wrapper patches battery, shared controller still reports 88')

    def test12_unknown_battery_blocks_takeoff(self):
        runner, _ = self.real_runner(battery=None)
        runner.run('drone.takeoff()')
        self.assertNotIn('takeoff', self.fake.calls, 'Unknown battery passed the takeoff gate')

    def test13_known_low_battery_blocks_takeoff(self):
        runner, _ = self.real_runner(battery=10)
        runner.run('drone.takeoff()')
        self.assertNotIn('takeoff', self.fake.calls)

    def test14_takeoff_requires_authorization_flag(self):
        runner, _ = self.real_runner(allow=False)
        runner.run('drone.takeoff()')
        self.assertNotIn('takeoff', self.fake.calls)

    def test15_landing_stays_pending_until_device_finishes(self):
        runner, drone = self.real_runner()
        runner.run('drone.takeoff()')
        self.fake.land_release.clear()
        runner.run('drone.land()')
        self.assertTrue(self.fake.land_entered.wait(1))
        time.sleep(.09)
        state = drone.get_status()
        self.assertTrue(self.fake.is_flying)
        self.assertTrue(state['landing_pending'], state)

    def test16_timeout_retains_barrier_until_device_action_resolves(self):
        runner, _ = self.real_runner()
        runner.run('drone.takeoff()')
        self.fake.move_release.clear()
        with patch.object(drv, '_move_deadline', return_value=.03):
            outcome = runner.run('drone.move_forward(30)')
        self.assertTrue(self.fake.move_entered.is_set())
        self.assertFalse(self.fake.move_done.is_set())
        self.assertFalse(ctl.request_control('model').ok,
                         'New control granted while timed-out hardware action is still active: ' + str(outcome))

    def test17_landing_is_not_dropped_behind_busy_command(self):
        self.real_runner()
        drv._command_lock.acquire()
        try:
            with patch.object(drv, 'LAND_DISPATCH_TIMEOUT_S', .03):
                drv._dispatch_land(self.fake)
                self.assertTrue(wait_for(lambda: any(e['event'] == 'land_deferred'
                    for e in drv.snapshot()['events']), .5))
        finally:
            drv._command_lock.release()
        self.assertTrue(self.fake.land_entered.wait(.15),
                        'Landing worker returned after lock timeout without retry')

    def test18_frame_bridge_preserves_capture_identity(self):
        from run_final_real_sim import FrameBridge
        frame = synthetic_frame()
        packets = []
        with patch.object(drv, 'get_frame', return_value=frame), \
             patch.object(ctl, 'publish_frame', side_effect=packets.append):
            bridge = FrameBridge(fps=100)
            bridge.start()
            try:
                self.assertTrue(wait_for(lambda: len(packets) >= 3))
            finally:
                bridge.stop()
                bridge.join(1)
        self.assertEqual(len({p.frame_id for p in packets}), 1,
                         'Repeated cached frame was assigned new capture IDs')

    def test19_driver_does_not_return_expired_frame_as_current(self):
        decoder = drv._VideoDecoder('offline-fake')
        decoder._frame = synthetic_frame()
        decoder.count, decoder.last_frame_at = 1, time.monotonic() - 10
        with patch.object(drv, '_video', decoder):
            self.assertGreater(drv.video_stats()['age'], 9)
            self.assertIsNone(drv.get_frame(), 'Driver returned a ten-second-old frame')


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
    @staticmethod
    def case_id(test):
        return 'T' + test._testMethodName.split('_', 1)[0].removeprefix('test')
    def known_gap(self, test):
        return os.environ.get('SCENARIO_STRICT') != '1' and self.case_id(test) in KNOWN_GAPS
    def addSuccess(self, test):
        if self.known_gap(test):
            super().addUnexpectedSuccess(test)
            self.records.append({'id': self.case_id(test), 'name': test.id(), 'status': 'resolved_gap'})
        else:
            super().addSuccess(test)
            self.records.append({'id': self.case_id(test), 'name': test.id(), 'status': 'pass'})
    def addFailure(self, test, err):
        if self.known_gap(test):
            super().addExpectedFailure(test, err)
            status = 'known_gap'
        else:
            super().addFailure(test, err)
            status = 'fail'
        self.records.append({'id': self.case_id(test), 'name': test.id(), 'status': status,
                             'detail': self._exc_info_to_string(err, test)})
    def addError(self, test, err):
        super().addError(test, err)
        self.records.append({'id': self.case_id(test), 'name': test.id(), 'status': 'error',
                             'detail': self._exc_info_to_string(err, test)})


def result_data(result, output, scope):
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True)
    return dict(tests=result.testsRun,
                passed=sum(record['status'] == 'pass' for record in result.records),
                known_gaps=len(result.expectedFailures), resolved_gaps=len(result.unexpectedSuccesses),
                failed=len(result.failures), errors=len(result.errors), cases=result.records,
                repository_commit=revision.stdout.strip(), strict=os.environ.get('SCENARIO_STRICT') == '1',
                scope=scope, output=output)


def write_result(name, data):
    RESULTS.mkdir(parents=True, exist_ok=True)
    if os.environ.get('SCENARIO_STRICT') == '1' and name in ('ScenarioResults.json', 'DocsResults.json'):
        name = name.replace('Results.json', 'StrictResults.json')
    (RESULTS / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def show_results(data):
    for record in data['cases']:
        print(record['status'].upper(), record['id'], record['name'].split('.')[-1])
        if record.get('detail'):
            print(record['detail'].splitlines()[-1][:400])
    print(json.dumps({key: data[key] for key in
                     ('tests', 'passed', 'known_gaps', 'resolved_gaps', 'failed', 'errors')}))


def run_probes():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = unittest.TextTestRunner(stream=output, verbosity=2, resultclass=RecordingResult).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(ScenarioTests))
    data = result_data(result, output.getvalue(), 'Synthetic frames, fake model responses and fake Tello only')
    write_result('ScenarioResults.json', data)
    show_results(data)
    return 0 if result.wasSuccessful() else 1


def run_all(only=None):
    commands = [
        ('RootIntegration', ['-m', 'unittest', 'test_full_integration', 'test_concurrency',
                            'test_session_interrupt', 'test_unknown_command'], ROOT),
        ('AIModule', ['-m', 'unittest', 'discover', '-s', 'modules/ai', '-p', 'test_*.py'], ROOT),
        ('FollowModule', ['-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py'], ROOT / 'modules' / 'follow'),
        ('GroundSelfCheck', ['test_ground_real.py', '--self-check'], ROOT),
        ('ScenarioAcceptance', [str(Path(__file__).resolve())], ROOT),
        ('DocsBaseline', [str(HERE / 'DocsTests.py'), '--baseline'], ROOT),
        ('DocsAcceptance', [str(HERE / 'DocsTests.py')], ROOT),
    ]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               PYTHONPATH=os.pathsep.join(map(str, (ROOT, ROOT / 'modules' / 'ai', ROOT / 'modules' / 'follow'))))
    RESULTS.mkdir(parents=True, exist_ok=True)
    destination = RESULTS / 'BaselineResults.json'
    records = json.loads(destination.read_text(encoding='utf-8')) if only and destination.exists() else []
    for name, args, cwd in commands:
        if only and name != only:
            continue
        started = time.monotonic()
        try:
            completed = subprocess.run([sys.executable, '-B', *args], cwd=cwd, env=env,
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
            record = {'name': name, 'returncode': completed.returncode,
                      'seconds': round(time.monotonic() - started, 2),
                      'stdout': completed.stdout, 'stderr': completed.stderr}
        except subprocess.TimeoutExpired as exc:
            record = {'name': name, 'returncode': None, 'error': 'timeout', 'seconds': 180}
        if only:
            records = [old for old in records if old['name'] != name]
        records.append(record)
        destination.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        print(name, 'exit=', record['returncode'], 'seconds=', record['seconds'], flush=True)
        print(record.get('stderr', '')[-2000:], flush=True)
        if name in ('ScenarioAcceptance', 'DocsAcceptance', 'DocsBaseline'):
            print(record.get('stdout', ''), flush=True)
    selected = records if only is None else [record for record in records if record['name'] == only]
    return int(any(record['returncode'] != 0 for record in selected))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--strict', action='store_true', help='Known gaps fail normally instead of being reported as XFAIL')
    parser.add_argument('--only', choices=['RootIntegration', 'AIModule', 'FollowModule', 'GroundSelfCheck', 'ScenarioAcceptance', 'DocsBaseline', 'DocsAcceptance'])
    options = parser.parse_args()
    if options.strict:
        os.environ['SCENARIO_STRICT'] = '1'
    raise SystemExit(run_all(options.only) if options.all or options.only else run_probes())
