"""Supplemental acceptance tests for docs AI vision and its runtime candidates.

The user requires docs AI vision. Realtime voice and full-program sandbox are
also audited as existing integration candidates, not an authorized migration.

No aircraft, microphones, cloud calls or Docker execution. Explicit fake model
responses, in-memory process pipes and synthetic image input are used.
Failures are unmet requirements; passing controls exercise supported behavior.
"""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

import ScenarioTests as prior

ROOT, HERE = prior.ROOT, prior.HERE
DOCS = ROOT / 'docs' / 'v4-reference'
sys.path.insert(0, str(DOCS))
from backends import MockBackend, SDKBackend
from gateway import Gateway
from vision import Vision
from realtime import RealtimeSession
from duplex_audio import PlaybackBuffer
from sandbox_runner import SandboxRunner
from drone_sdk import controller as ctl, _driver as drv


class Reply:
    def __init__(self, visible=True, center=.5, width=.2, clock=None):
        self.value = dict(visible=visible, center_x=center, width_fraction=width, description='explicit test fixture')
        self.clock = clock
        self.calls = 0
    def complete(self, messages):
        self.calls += 1
        if self.clock is not None:
            self.clock[0] += 3
        return json.dumps(self.value)


class Camera:
    simulated = True
    def get_frame(self):
        return dict(frame=prior.synthetic_frame(), frame_id=1, timestamp=100., stale=False)


class HardwareTests(unittest.TestCase):
    setUp = prior.ScenarioTests.setUp
    tearDown = prior.ScenarioTests.tearDown
    real_runner = prior.ScenarioTests.real_runner

    def test20_docs_adapter_preserves_real_mode(self):
        self.real_runner()
        backend = SDKBackend(ROOT)
        self.assertFalse(backend.simulated, 'Connected real driver is labelled simulated by the required docs adapter')

    def test21_docs_adapter_cannot_bypass_low_battery_gate(self):
        self.real_runner(battery=10)
        backend = SDKBackend(ROOT)
        backend.request_control('model')
        backend.action('takeoff')
        self.assertNotIn('takeoff', self.fake.calls, 'Direct docs SDK path bypassed the main-entry takeoff gate')

    def test22_failed_rc_transmission_is_not_reported_accepted(self):
        runner, _ = self.real_runner()
        runner.run('drone.takeoff()')
        ctl.request_control('model')
        with patch.object(self.fake, 'send_rc_control', side_effect=OSError('explicit failed RC transport')):
            result = ctl.update_velocity('model', 0, 20, 0, 0)
        self.assertFalse(result.ok, 'Hardware send raised, yet SDK returned ' + str(result))

    def test23_real_execution_is_not_labelled_simulated(self):
        runner, _ = self.real_runner()
        result = runner.run('drone.takeoff()')
        self.assertFalse(result['simulated'], result)

class VisionTests(unittest.TestCase):
    def test25_slow_visual_result_is_rejected_after_inference(self):
        clock = [100.]
        visual = Vision(Reply(clock=clock), Camera())
        with patch('vision.time.time', side_effect=lambda: clock[0]):
            with self.assertRaises((RuntimeError, ValueError), msg='Three-second-old inference was returned as current'):
                visual.observe('red tent')

    def test26_visible_target_cannot_have_zero_width(self):
        with patch('vision.time.time', return_value=100.):
            with self.assertRaises(ValueError, msg='visible=true and width=0 are inconsistent'):
                Vision(Reply(width=0), Camera()).observe('red tent')

    def test27_fresh_visual_observation_is_supported(self):
        with patch('vision.time.time', return_value=100.1):
            result = Vision(Reply(), Camera()).observe('red tent')
        self.assertEqual(result['source'], 'sdk_camera')
        self.assertTrue(result['visible'])
        self.assertEqual(result['timestamp'], 100.)

    def test28_stale_input_never_calls_visual_model(self):
        client = Reply()
        with patch('vision.time.time', return_value=102.):
            with self.assertRaises(RuntimeError):
                Vision(client, Camera()).observe('red tent')
        self.assertEqual(client.calls, 0)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        prior.ScenarioTests.setUp(self)
    tearDown = prior.ScenarioTests.tearDown
    following = prior.ScenarioTests.following
    real_runner = prior.ScenarioTests.real_runner

    async def test24_docs_cannot_fly_real_hardware_using_static_test_image(self):
        self.real_runner()
        backend = SDKBackend(ROOT)
        backend.request_control('model')
        backend.action('takeoff')
        backend.release_control('model')
        observation = dict(source='static_test_image', simulated=True, frame_id='fixture', timestamp=100.,
                           visible=True, center_x=.5, width_fraction=.2)
        visual = types.SimpleNamespace(observe=lambda target: observation)
        gateway = Gateway(backend, visual)
        gateway.begin()
        await gateway.dispatch('observe', ['red tent'], {})
        try:
            await gateway.dispatch('move_forward', [20], {})
        except (RuntimeError, ValueError):
            pass
        await gateway.finish(False)
        self.assertNotIn(('forward_started', 20), self.fake.calls,
                         'A static test image authorized a real-mode navigation step')

    async def test29_navigation_cannot_move_using_expired_observation(self):
        clock = [100.]
        observation = dict(source='sdk_camera', frame_id=1, timestamp=100., visible=True,
                           center_x=.5, width_fraction=.2, simulated=True)
        backend = MockBackend()
        backend.flying = True
        gateway = Gateway(backend, types.SimpleNamespace(observe=lambda target: observation))
        gateway.begin()
        with patch('time.time', side_effect=lambda: clock[0]):
            await gateway.dispatch('observe', ['red tent'], {})
            clock[0] = 103.
            try:
                await gateway.dispatch('move_forward', [20], {})
            except (ValueError, RuntimeError):
                pass
        await gateway.finish(False)
        self.assertNotIn(('move_forward', (20,)), backend.calls,
                         'Navigation motion was sent from an expired visual observation')

    async def test30_normal_arrival_stop_preserves_requested_follow_resume(self):
        backend = MockBackend()
        backend.flying = True
        backend.start_follow()
        gateway = Gateway(backend, fixture_vision=True)
        gateway.begin()
        # Same calls as the successful threshold branch of red_tent.py.
        for _ in range(12):
            target = await gateway.dispatch('observe', ['red tent'], {})
            if target['width_fraction'] >= .35:
                await gateway.dispatch('stop_motion', [], {})
                break
            if abs(target['center_x'] - .5) > .12:
                await gateway.dispatch('rotate', [10], {})
            else:
                await gateway.dispatch('move_forward', [20], {})
        await gateway.finish(True)
        self.assertTrue(backend.following, 'Normal arrival stop disabled automatic follow restoration')

    async def test31_release_failure_cannot_finish_successfully(self):
        class RefusesRelease(MockBackend):
            def release_control(self, owner):
                return {'ok': False, 'reason': 'explicit release failure'}
        backend = RefusesRelease()
        gateway = Gateway(backend)
        gateway.begin()
        await gateway.dispatch('takeoff', [], {})
        with self.assertRaises(RuntimeError, msg='Failed release was ignored while owner remains model'):
            await gateway.finish(True)

    async def test32_failed_stop_cannot_be_silently_cleaned_up(self):
        class RefusesStop(MockBackend):
            def action(self, name, *args):
                if name == 'stop_motion':
                    return {'ok': False, 'status': 'failed', 'reason': 'explicit stop failure'}
                return super().action(name, *args)
        gateway = Gateway(RefusesStop())
        gateway.begin()
        await gateway.dispatch('takeoff', [], {})
        with self.assertRaises(RuntimeError, msg='Failed stop was ignored during cleanup'):
            await gateway.finish(False)

    async def test33_docs_handoff_preserves_original_follow_reference(self):
        feed, _ = self.following()
        original = ctl._follow.status()['reference_size']
        gateway = Gateway(SDKBackend(ROOT))
        gateway.begin()
        await gateway.dispatch('move_forward', [30], {})
        feed.size = .18
        await gateway.finish(True)
        self.assertTrue(ctl.is_following())
        self.assertAlmostEqual(ctl._follow.status()['reference_size'], original, places=5,
                               msg='Docs handoff destroyed the service and recalibrated at a new distance')

    async def test34_old_docs_action_cannot_use_new_same_owner_grant(self):
        entered, release = threading.Event(), threading.Event()
        class DelayedSDK(SDKBackend):
            def action(self, name, *args):
                if name == 'move_forward':
                    entered.set()
                    release.wait(2)
                return super().action(name, *args)
        backend = DelayedSDK(ROOT)
        backend.request_control('model')
        backend.action('takeoff')
        backend.release_control('model')
        drv._events.clear()
        gateway = Gateway(backend)
        gateway.begin()
        old = asyncio.create_task(gateway.dispatch('move_forward', [20], {}))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            await gateway.interrupt(False)
            await gateway.finish(False)
            gateway.begin()
            await gateway.acquire()
            release.set()
            await old
            self.assertFalse(any(e.get('action') == 'forward' for e in drv.snapshot()['events']),
                             'Old action executed after interrupt using a new model control grant')
        finally:
            release.set()
            await asyncio.gather(old, return_exceptions=True)
            await gateway.finish(False)

    async def test35_explicit_emergency_stop_does_not_resume_follow(self):
        backend = MockBackend()
        backend.flying = True
        backend.start_follow()
        gateway = Gateway(backend)
        gateway.begin()
        await gateway.interrupt(False)
        await gateway.finish(False)
        self.assertFalse(backend.following)

    async def test36_full_rotation_is_supported_on_docs_route(self):
        backend = MockBackend()
        gateway = Gateway(backend)
        gateway.begin()
        await gateway.dispatch('takeoff', [], {})
        await gateway.dispatch('rotate', [360], {})
        await gateway.finish(True)
        self.assertIn(('rotate', (360,)), backend.calls)


class RealtimeTests(unittest.IsolatedAsyncioTestCase):
    async def test37_network_cancel_send_cannot_delay_local_stop(self):
        send_entered, release_send = asyncio.Event(), asyncio.Event()
        async def blocked_send(event):
            send_entered.set()
            await release_send.wait()
        gateway = types.SimpleNamespace(interrupt=AsyncMock(return_value={'ok': True}))
        runner = types.SimpleNamespace(running=True, cancel=lambda: None, gateway=gateway)
        speaker = types.SimpleNamespace(interrupt=lambda: None)
        session = RealtimeSession(runner, types.SimpleNamespace(speaker=speaker))
        session.ws = types.SimpleNamespace(send=blocked_send)
        session.response_active = True
        session.barge_cooldown = 0
        task = asyncio.create_task(session.barge_in())
        try:
            await asyncio.wait_for(send_entered.wait(), .5)
            await asyncio.sleep(.04)
            self.assertTrue(gateway.interrupt.called,
                            'Barge-in waited on WebSocket response.cancel before local device interruption')
        finally:
            release_send.set()
            await task
            await asyncio.gather(*list(session.tool_tasks), return_exceptions=True)

    async def test43_separate_visual_provider_key_is_not_sent_to_realtime(self):
        attempts = []
        def blocked_connect(url, **kwargs):
            attempts.append((url, kwargs['additional_headers']))
            raise RuntimeError('Explicit fake socket: no network request was made')
        fake_modules = {name: types.ModuleType(name) for name in
                        ('websockets', 'websockets.asyncio', 'websockets.asyncio.client')}
        fake_modules['websockets.asyncio.client'].connect = blocked_connect
        gateway = types.SimpleNamespace(interrupt=AsyncMock(return_value={'ok': True}), finish=AsyncMock())
        runner = types.SimpleNamespace(check=AsyncMock(), cancel=lambda: None, gateway=gateway)
        audio = types.SimpleNamespace(close=lambda: None)
        session = RealtimeSession(runner, audio)
        settings = {'AI_CHAT_URL': 'https://api.deepseek.com/v1/chat/completions',
                    'AI_API_KEY': 'explicit-fake-visual-provider-key', 'REALTIME_MODEL': 'explicit-fake-model'}
        with patch.dict(sys.modules, fake_modules), patch.dict(os.environ, settings):
            os.environ.pop('OPENAI_API_KEY', None)
            with self.assertRaises((ValueError, RuntimeError)):
                await session.run()
        self.assertEqual(attempts, [],
                         'Separate visual-service key was selected for the hardcoded OpenAI Realtime endpoint')


class PlaybackTests(unittest.TestCase):
    def test38_interrupt_truncates_new_unplayed_item_not_completed_old_item(self):
        buffer = PlaybackBuffer()
        buffer.append('old', 0, b'\0\0' * 240)
        buffer.consume(240)
        buffer.append('new', 0, b'\0\0' * 240)
        result = buffer.interrupt()
        self.assertEqual(result['item_id'], 'new', result)
        self.assertEqual(result['audio_end_ms'], 0)

    def test39_sample_covers_rotation_and_follow_restoration(self):
        import ast
        source = (DOCS / 'examples' / 'red_tent.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == 'drone']
        full_turn = any(node.func.attr == 'rotate' and len(node.args) == 1
                        and isinstance(node.args[0], ast.Constant) and node.args[0].value == 360 for node in calls)
        self.assertTrue(full_turn, 'The existing sample contains only +/-10 degree alignment, no requested 360 degree turn')

    def test40_legal_but_wrong_model_action_is_not_intent_validation(self):
        # Fault injection, NOT a claim that any live model made this mistake.
        client = prior.PlannedReply('drone.takeoff()')
        agent = prior.Agent(client)
        result = agent.handle('不要起飞，只告诉我目前的状态')
        self.assertFalse(agent.runner.demo.drone.get_status()['flying'],
                         'A faulty planner returned a whitelisted action contradicting the request: ' + str(result))

    def test41_flight_sop_fifty_cm_action_is_supported_by_its_entry(self):
        # FLIGHT_SOP.md names modules/ai/main.py, whose Runner accepts only 30.
        outcome = prior.Runner().run('drone.move_forward(50)')
        self.assertTrue(outcome['accepted'], 'The SOP 50cm demonstration is rejected by its named entry')

    def test42_sample_explicitly_restores_follow_after_arrival(self):
        import ast
        tree = ast.parse((DOCS / 'examples' / 'red_tent.py').read_text(encoding='utf-8'))
        calls = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == 'drone']
        self.assertIn('start_follow', calls, 'Full requested mission needs final follow restoration; sample omits it')


def run():
    buffer = io.StringIO()
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                              for cls in (HardwareTests, VisionTests, GatewayTests, RealtimeTests, PlaybackTests))
    with contextlib.redirect_stdout(buffer):
        result = unittest.TextTestRunner(stream=buffer, verbosity=2, resultclass=prior.RecordingResult).run(suite)
    output = prior.result_data(result, buffer.getvalue(),
        'User-specified docs AI vision plus gateway/Realtime candidates; fake hardware/model/audio/socket only')
    prior.write_result('DocsResults.json', output)
    prior.show_results(output)
    return int(not result.wasSuccessful())


def baseline():
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               PYTHONPATH=os.pathsep.join(map(str, (DOCS, ROOT / 'modules' / 'ai', ROOT / 'modules' / 'follow', ROOT))),
               RUN_DOCKER_TESTS='0')
    result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                            cwd=DOCS, env=env, capture_output=True, text=True, encoding='utf-8', timeout=90)
    record = dict(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
                  scope='Existing docs tests; Docker execution explicitly disabled')
    prior.write_result('DocsBaseline.json', record)
    print(result.stderr[-4500:])
    return result.returncode


if __name__ == '__main__':
    if '--strict' in sys.argv:
        os.environ['SCENARIO_STRICT'] = '1'
    raise SystemExit(baseline() if '--baseline' in sys.argv else run())
