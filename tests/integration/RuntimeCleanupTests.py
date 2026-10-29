"""Exit status and cleanup evidence with offline resource/service fakes only."""
import asyncio
from contextlib import ExitStack, redirect_stdout
import io
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app import Application as main
from app.DeviceSession import DeviceSession
from drone_sdk import _driver as driver, controller
from drone_sdk.contract import FrameResult
from SafetyFixture import FakeTello


class Log:
    def __init__(self):
        self.events = []
        self.secrets = ()

    def __call__(self, event):
        self.events.append(event)

    def clean(self, value):
        return value


class Worker:
    def __init__(self, alive=False, stopped=None):
        self.alive = alive
        self.stop = Mock(return_value=stopped)
        self.join = Mock()

    def is_alive(self):
        return self.alive


class RuntimeCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.log = Log()

    def aircraft(self, *, state='landed', policy='land', reader=None,
                 stop=True, interrupt=None, follow=None):
        """Patch every driver entry used here, never connect or send commands."""
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.args = SimpleNamespace(exit_action=policy, landing_timeout=.01)
        self.runtime = SimpleNamespace(closed=False, land_requested=False, pending=None, cancel=Mock(),
            interrupt=AsyncMock(return_value=interrupt if interrupt is not None else {'ok': True}),
            retry_cleanup=AsyncMock(return_value={'ok': True}))
        self.state = SimpleNamespace(flight_state=state)
        stack.enter_context(patch.object(controller, 'get_status', side_effect=lambda: self.state))
        self.stopfollow = Mock(return_value=SimpleNamespace(ok=True, reason=''))
        self.device = DeviceSession(self.args, self.log)
        self.device.ready = True
        follow = follow or {}
        self.device.follow = SimpleNamespace(stop=self.stopfollow,
            close=Mock(return_value=SimpleNamespace(ok=not (follow.get('background_running') or
                                                          follow.get('cleanup_pending')), reason='workers_alive')))
        stack.enter_context(patch.object(driver, 'cancel_pending', return_value=1))
        stack.enter_context(patch.object(driver, 'set_velocity', return_value=True))
        stack.enter_context(patch.object(driver, 'action_pending', return_value=False))
        stack.enter_context(patch.object(driver, '_video', reader))
        self.stopvideo = stack.enter_context(patch.object(driver, 'stop_video', return_value=stop))
        self.disconnect = stack.enter_context(patch.object(driver, 'disconnect', return_value=True))

    async def close_aircraft(self, runtime=True):
        return await self.device.close(self.runtime if runtime else None)

    def failed_steps(self, outcome):
        return {step['step'] for step in outcome['steps'] if not step['ok']}

    async def test_confirmed_landing_stops_video_before_disconnect(self):
        self.aircraft()
        result = await self.close_aircraft()
        self.assertTrue(result['ok'], result)
        self.stopvideo.assert_called_once()
        self.disconnect.assert_called_once()

    async def test_false_interrupt_is_fatal_but_remaining_cleanup_runs(self):
        self.aircraft(interrupt={'ok': False, 'reason': 'stop_failed'})
        result = await self.close_aircraft()
        self.assertFalse(result['ok'])
        self.assertIn('exit_action', self.failed_steps(result))
        self.stopvideo.assert_called_once()
        self.disconnect.assert_called_once()


    async def test_land_policy_requires_confirmed_landing(self):
        self.aircraft(state='flying')
        result = await self.close_aircraft()
        self.assertIn('landing_confirmation', self.failed_steps(result))
        self.runtime.interrupt.assert_awaited_once_with(True)
        self.stopvideo.assert_called_once()
        self.disconnect.assert_not_called()

    async def test_hover_policy_accepts_airborne_after_stop(self):
        self.aircraft(state='flying', policy='hover')
        result = await self.close_aircraft()
        self.assertTrue(result['ok'], result)
        self.runtime.interrupt.assert_awaited_once_with(False)
        self.disconnect.assert_not_called()
        self.assertEqual(result['steps'][-1]['result']['status'], 'hover_connection_retained')

    async def test_prior_land_request_is_sticky_even_under_hover_exit(self):
        self.aircraft(state='flying', policy='hover')
        self.runtime.land_requested = True
        result = await self.close_aircraft()
        self.runtime.interrupt.assert_awaited_once_with(True)
        self.assertIn('landing_confirmation', self.failed_steps(result))

    async def test_unknown_state_never_means_confirmed_landing_or_hover(self):
        for policy in ('land', 'hover'):
            with self.subTest(policy=policy):
                self.aircraft(state='unknown', policy=policy)
                result = await self.close_aircraft()
                self.assertFalse(result['ok'])
                self.runtime.interrupt.assert_awaited_once_with(False)
                self.disconnect.assert_not_called()

    async def test_pending_control_failure_retains_connection_but_stops_video(self):
        self.aircraft()
        self.runtime.pending = object()
        self.runtime.retry_cleanup.return_value = {'ok': False, 'reason': 'control_acquire_pending'}
        result = await self.close_aircraft()
        self.assertIn('pending_resources', self.failed_steps(result))
        self.stopvideo.assert_called_once()
        self.disconnect.assert_not_called()

    async def test_pending_container_failure_does_not_retain_device_connection(self):
        self.aircraft()
        self.runtime.retry_cleanup.return_value = {'ok': False,
            'control': {'ok': True}, 'sandbox': {'ok': False, 'reason': 'engine_unavailable'}}
        result = await self.close_aircraft()
        self.assertIn('pending_resources', self.failed_steps(result))
        self.stopvideo.assert_called_once()
        self.disconnect.assert_called_once()



    async def test_without_runtime_failed_follow_stop_still_zeros_and_closes_video(self):
        self.aircraft()
        self.stopfollow.return_value = SimpleNamespace(ok=False, reason='stop_failed')
        result = await self.close_aircraft(runtime=False)
        self.assertIn('stop_follow', self.failed_steps(result))
        driver.set_velocity.assert_called_once()
        self.stopvideo.assert_called_once()

    async def test_follow_worker_close_failure_retains_connection_and_reports_failure(self):
        self.aircraft(follow={'cleanup_pending': True})
        result = await self.close_aircraft()
        self.assertFalse(result['ok'])
        self.assertIn('follow_workers', self.failed_steps(result))
        self.device.follow.close.assert_called_once_with(wait=True)
        self.stopvideo.assert_called_once()
        self.disconnect.assert_not_called()

    async def test_cleanup_step_timeout_and_cancelled_operation_are_recorded(self):
        outcome = {'ok': True, 'steps': []}
        async def stalled():
            await asyncio.Event().wait()
        await main.cleanup_step(outcome, self.log, 'timeout', stalled, .01)
        async def cancelled():
            raise asyncio.CancelledError()
        await main.cleanup_step(outcome, self.log, 'cancelled', cancelled, 1)
        self.assertEqual(self.failed_steps(outcome), {'timeout', 'cancelled'})

    def session(self, *, cleanup=None, execution=None, voice=False, service=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        args = ['voice' if voice else 'run', '--real-drone'] + ([] if voice else
                                  ['--program', str(Path(__file__))])
        self.args = main.make_parser().parse_args(args)
        stack.enter_context(patch.object(main.DeviceSession, 'open', new=AsyncMock(return_value=object())))
        self.close = stack.enter_context(patch.object(main.DeviceSession, 'close', new=AsyncMock(
            return_value=cleanup if cleanup is not None else {'ok': True, 'steps': []})))
        self.closedcontrols = False
        async def controls(*args):
            try:
                await asyncio.Event().wait()
            finally:
                self.closedcontrols = True
        stack.enter_context(patch.object(main, 'operator_controls', side_effect=controls))
        stack.enter_context(patch('execution.sandbox_runner.SandboxRunner.check', new=AsyncMock()))
        runner = AsyncMock(return_value=execution if execution is not None else {'ok': True})
        self.runtime_service = SimpleNamespace(run=runner, close=Mock(), generation=0, pending=None,
            land_requested=False, close_sandbox=AsyncMock(return_value={'ok':True}), interrupt=AsyncMock(return_value={'ok': True}),
            retry_cleanup=AsyncMock(return_value={'ok': True}))
        stack.enter_context(patch.object(main, 'UnifiedRuntime', return_value=self.runtime_service))
        stack.enter_context(patch('navigation.vision.Vision'))
        if voice:
            stack.enter_context(patch('providers.DoubaoConfig.load_config',
                                      return_value=SimpleNamespace(environment={})))
            stack.enter_context(patch('providers.DoubaoVision.DoubaoVisionClient', return_value=None))
            stack.enter_context(patch('providers.RTCRuntime.validate_environment', return_value='node'))
            self.service = service or SimpleNamespace(start=AsyncMock(return_value='http://localhost'),
                wait=AsyncMock(), close=AsyncMock(return_value={'ok': True, 'steps': []}), deadline=time.time()+10)
            self.bridge = SimpleNamespace(start=AsyncMock(), wait=AsyncMock(side_effect=asyncio.Event().wait),
                revoke=AsyncMock(return_value={'ok': True}), close=AsyncMock(return_value={'ok': True}))
            stack.enter_context(patch('providers.RTCBridge.RTCBridge', return_value=self.bridge))
            stack.enter_context(patch('providers.RTCRuntime.RTCProcess', return_value=self.service))
        return runner

    async def test_run_success_remains_zero_and_failed_cleanup_becomes_nonzero(self):
        self.session()
        self.assertEqual(await main.Session(self.args, self.log).run(), 0)
        self.close.return_value = {'ok': False, 'steps': [{'step': 'video', 'ok': False}]}
        self.assertEqual(await main.Session(self.args, self.log).run(), 1)
        self.assertTrue(self.closedcontrols)
        self.assertFalse(self.log.events[-1]['result']['ok'])

    async def test_primary_program_failure_keeps_its_exit_status(self):
        self.session(cleanup={'ok': False}, execution={'ok': False, 'reason': 'primary'})
        self.assertEqual(await main.Session(self.args, self.log).run(), 2)

    async def test_primary_exception_is_preserved_after_cleanup_failure(self):
        runner = self.session(cleanup={'ok': False})
        runner.side_effect = RuntimeError('primary_execution_failed')
        with self.assertRaisesRegex(RuntimeError, 'primary_execution_failed'):
            await main.Session(self.args, self.log).run()
        self.close.assert_awaited_once()
        self.assertTrue(self.closedcontrols)

    async def test_cancellation_is_preserved_and_cleanup_finishes(self):
        runner = self.session(cleanup={'ok': False})
        entered = asyncio.Event()
        async def execute(*args):
            entered.set()
            await asyncio.Event().wait()
        runner.side_effect = execute
        task = asyncio.create_task(main.Session(self.args, self.log).run())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.close.assert_awaited_once()
        self.assertTrue(self.closedcontrols)

    async def test_cloud_close_failure_does_not_skip_aircraft_or_controls(self):
        self.session(voice=True)
        self.service.close.side_effect = RuntimeError('cloud_close_failed')
        self.assertEqual(await main.Session(self.args, self.log).run(), 1)
        self.bridge.revoke.assert_awaited_once_with('application_exit', True)
        self.bridge.close.assert_awaited_once()
        self.close.assert_awaited_once()
        self.assertTrue(self.closedcontrols)

    async def test_browser_disconnect_finishes_voice_wait_and_cancels_node_observer(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def node_wait():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        session = main.Session(SimpleNamespace(), self.log)
        session.service = SimpleNamespace(wait=node_wait)
        session.bridge = SimpleNamespace(wait=entered.wait)
        await asyncio.wait_for(session.wait_voice(), 1)
        self.assertTrue(cancelled.is_set())

    async def test_cloud_cleanup_error_is_redacted_in_console_and_evidence(self):
        self.session(voice=True)
        secret = 'PrivateCredentialValue'
        self.log.clean = lambda value: str(value).replace(secret, '[redacted]')
        self.service.close.side_effect = RuntimeError('failed using ' + secret)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(await main.Session(self.args, self.log).run(), 1)
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn(secret, repr(self.log.events))
        self.assertIn('[redacted]', output.getvalue())



    async def test_actual_driver_end_exception_reaches_actual_run_exit_status(self):
        real_disconnect = driver.disconnect
        self.aircraft()
        device, real_close = self.device, self.device.close
        self.session()
        self.close.side_effect = real_close
        self.disconnect.side_effect = real_disconnect
        tello = FakeTello()
        tello.end = Mock(side_effect=RuntimeError('end_failed'))
        with patch.object(main, 'DeviceSession', return_value=device), \
             patch.object(driver, '_tello', tello), patch.object(driver, 'SIMULATED', False), \
             patch.object(driver, '_keepalive_stop', None), patch.object(driver, '_flying', True), \
             patch.object(driver, '_height_cm', 80), patch.object(driver, '_response_quarantine', {'uncertain': True}):
            self.assertEqual(await main.Session(self.args, self.log).run(), 1)
            tello.end.assert_called_once()
            self.assertIs(driver._tello, tello)
            self.assertFalse(driver.SIMULATED)
            self.assertTrue(driver._flying)
            self.assertEqual(driver._height_cm, 80)
            self.assertEqual(driver._response_quarantine, {'uncertain': True})
        self.assertTrue(self.closedcontrols)

    async def test_driver_keeps_failed_decoder_and_still_attempts_end_after_rc_error(self):
        real_disconnect = driver.disconnect
        reader = Worker()
        reader.stop.side_effect = RuntimeError('decoder_failed')
        tello = FakeTello()
        tello.fail_rc = True
        tello.end = Mock()
        with patch.object(driver, '_tello', tello), patch.object(driver, '_video', reader), \
             patch.object(driver, 'SIMULATED', False), patch.object(driver, '_keepalive_stop', None):
            self.assertFalse(real_disconnect())
            tello.end.assert_called_once()
            self.assertIsNone(driver._tello)
            self.assertIs(driver._video, reader)
            self.assertFalse(driver.SIMULATED)


class DeviceReadyTests(unittest.IsolatedAsyncioTestCase):
    def device(self, packets):
        stack = ExitStack()
        self.addCleanup(stack.close)
        config = SimpleNamespace(control_profile='openpose_pid', steering_mode='mixed', target_id=0)
        stack.enter_context(patch('app.DeviceSession.validate_follow_config', return_value=config))
        self.follow_factory = stack.enter_context(patch('follow.r3_follow.service.FollowService', return_value=SimpleNamespace()))
        self.connect = stack.enter_context(patch.object(driver, 'connect'))
        stack.enter_context(patch.object(driver, 'snapshot', return_value={}))
        stack.enter_context(patch.object(driver, 'start_video', return_value=True))
        stack.enter_context(patch.object(driver, 'video_stats', return_value={}))
        stack.enter_context(patch.object(driver, 'health', return_value=dict(simulated=False,
            connected=True, link_ok=True, battery=80, temperature_c=35, telemetry_fresh=True, video=True)))
        self.configure = stack.enter_context(patch.object(controller, 'configure_takeoff'))
        self.frames = stack.enter_context(patch.object(controller, 'get_frame', side_effect=packets))
        args = main.make_parser().parse_args(['voice', '--real-drone', '--allow-real-takeoff', '--frame-timeout', '.16'])
        return DeviceSession(args, Log())

    async def test_ready_requires_a_second_source_identity_without_fixed_ten_second_wait(self):
        first = FrameResult(object(), 5, time.time(), False)
        second = FrameResult(object(), 6, time.time(), False)
        device = self.device([first, first, second])
        started = time.monotonic()
        self.assertIs(await device.open(), controller)
        self.assertLess(time.monotonic()-started, 2)
        self.assertEqual(self.frames.call_count, 3)
        self.configure.assert_called_with(authorized=True, min_battery=30, temp_max=95)

    async def test_a_repeated_cached_frame_never_completes_device_readiness(self):
        packet = FrameResult(object(), 5, time.time(), False)
        device = self.device(None)
        self.frames.side_effect = None
        self.frames.return_value = packet
        with self.assertRaisesRegex(RuntimeError, '连续两张'):
            await device.open()
        self.configure.assert_called_once_with(authorized=False, min_battery=30, temp_max=95)

    async def test_follow_captures_real_mode_after_connect(self):
        device = self.device([FrameResult(object(), 1, time.time(), False),
                              FrameResult(object(), 2, time.time(), False)])
        connected = False
        def connect(*args, **kwargs):
            nonlocal connected
            connected = True
        self.connect.side_effect = connect
        self.follow_factory.side_effect = lambda source, **kwargs: SimpleNamespace(
            simulated=source.get_status().simulated)
        with patch.object(controller, 'get_status', side_effect=lambda: SimpleNamespace(simulated=not connected)):
            await device.open()
        self.assertFalse(device.follow.simulated)


if __name__ == '__main__':
    unittest.main(verbosity=2)
