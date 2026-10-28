"""Follow handoffs through the production gateway, with simulated device I/O."""
import asyncio
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from follow.r3_follow import FollowService
from follow.r3_follow.config import load_config
from execution.Host import Host
from drone_sdk.contract import CommandResult, FrameResult
from follow.r3_follow.vision import synthetic_frame
from RuntimeFixture import reset, sdk, driver, settle, wait_until


class ActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.follow = reset(flying=True)
        self.service = self.follow
        self.clock, self.seq = 10., 0
        self.service.clock = lambda: self.clock
        self.service.wall = lambda: 1800000000. + self.clock
        self.service.tracker._clock = self.service.clock
        self.packet = None
        self.follow.set_frame_provider(lambda: self.packet)
        self.frame()
        self.assertTrue(self.service.calibrate_reference().ok)

    def tearDown(self):
        reset()

    def frame(self, **options):
        self.clock += .11
        self.seq += 1
        self.packet = FrameResult(synthetic_frame(**options), self.seq, self.service.wall(), False)
        self.service.capture_once()
        self.service.control_once()

    def following(self):
        if self.service.controller.reference_size is None:
            self.frame()
            self.assertTrue(self.service.calibrate_reference().ok)
        self.assertTrue(self.service._start_tracking().ok)
        self.frame(x=.8)
        self.assertTrue(self.follow.is_following())
        self.assertGreater(self.service.last_channels[3], 0)

    async def move(self, context):
        return await context.dispatch('move_forward', [30], {})

    async def test_successful_action_releases_model_and_resumes_original_reference(self):
        self.following()
        reference = self.service.controller.reference_size
        execute = driver.execute
        def moved(*args, **kwargs):
            self.assertFalse(self.follow.is_following())
            self.assertEqual(sdk.get_status().control_owner, 'model')
            self.frame(x=.2)
            return execute(*args, **kwargs)
        context = Host(sdk, self.follow)
        with patch.object(driver, 'execute', side_effect=moved):
            self.assertTrue((await self.move(context))['ok'])
        await context.finish(True)
        self.assertEqual(context.cleanup['status'], 'follow_resumed')
        self.assertEqual(sdk.get_status().control_owner, 'follow')
        self.assertEqual(reference, self.service.controller.reference_size)

    async def test_success_without_new_frame_reports_failed_resume(self):
        self.following()
        context = Host(sdk, self.follow)
        self.assertTrue((await self.move(context))['ok'])
        with self.assertRaisesRegex(RuntimeError, 'follow_resume_failed'):
            await context.finish(True)
        self.assertFalse(self.follow.is_following())
        self.assertEqual(sdk.get_status().control_owner, 'none')

    async def test_target_lost_during_action_cannot_resume(self):
        self.following()
        context = Host(sdk, self.follow)
        with patch.object(driver, 'execute', side_effect=lambda *a, **kw: (self.frame(visible=False), True)[1]):
            await self.move(context)
        with self.assertRaisesRegex(RuntimeError, 'follow_resume_failed'):
            await context.finish(True)
        self.assertFalse(self.follow.is_following())

    async def test_failed_exception_and_unexpected_accepted_action_never_resume(self):
        for outcome in ('timeout', 'exception', 'accepted'):
            with self.subTest(outcome=outcome):
                self.following()
                context = Host(sdk, self.follow)
                kwargs = ({'side_effect': RuntimeError('injected')} if outcome == 'exception' else
                          {'return_value': CommandResult(outcome == 'accepted', outcome)})
                with patch.object(sdk, 'move_forward', **kwargs), self.assertRaisesRegex(RuntimeError, 'SDK_action_failed|injected'):
                    await self.move(context)
                await context.finish(False)
                self.frame(x=.8)
                self.assertFalse(self.follow.is_following())
                self.assertEqual(sdk.get_status().control_owner, 'none')

    async def test_release_failure_is_reported_and_model_lease_retained(self):
        self.following()
        context = Host(sdk, self.follow)
        await self.move(context)
        release = sdk.release_control
        def denied(owner, **kwargs):
            return sdk.ControlResult(False, owner, 'release failed') if owner == 'model' else release(owner, **kwargs)
        with patch.object(sdk, 'release_control', side_effect=denied):
            with self.assertRaisesRegex(RuntimeError, 'cleanup_release_failed'):
                await context.finish(False)
        self.assertEqual(context.cleanup['status'], 'release_unconfirmed')
        self.assertEqual(sdk.get_status().control_owner, 'model')
        self.assertFalse(self.follow.is_following())
        await context.finish(False)

    async def test_cleanup_stop_exception_is_reported_without_restoring_follow(self):
        self.following()
        context = Host(sdk, self.follow)
        await self.move(context)
        stop = sdk.stop_motion
        def denied(owner, **kwargs):
            if owner == 'model':
                raise RuntimeError('stop failed')
            return stop(owner, **kwargs)
        with patch.object(sdk, 'stop_motion', side_effect=denied):
            with self.assertRaisesRegex(RuntimeError, 'stop failed'):
                await context.finish(False)
        self.assertFalse(self.follow.is_following())
        await context.finish(False)

    async def test_external_owner_is_not_released_after_busy(self):
        self.assertTrue(sdk.request_control('follow').ok)
        context = Host(sdk, self.follow)
        with self.assertRaisesRegex(RuntimeError, 'control_busy'):
            await self.move(context)
        await context.finish(False)
        self.assertEqual(sdk.get_status().control_owner, 'follow')

    async def test_invalid_program_fails_without_motion_or_automatic_follow_resume(self):
        for name, args in (('move_forward', [19]), ('move_back', [501]),
                           ('move_left', [True]), ('rotate', [0])):
            with self.subTest(name=name):
                self.following()
                context = Host(sdk, self.follow)
                with self.assertRaises(RuntimeError):
                    await context.dispatch(name, args, {})
                await context.finish(False)
                self.assertFalse(self.follow.is_following())
                self.assertEqual(sdk.get_status().velocity, (0,0,0,0))
                self.assertEqual(sdk.get_status().control_owner, 'none')
        with self.assertRaises(ValueError):
            await Host(sdk, self.follow).dispatch('__dict__', [], {})

    async def test_stop_ack_is_not_physical_hover_confirmation(self):
        self.following()
        result = await Host(sdk, self.follow).interrupt()
        self.assertEqual(result['status'], 'accepted')
        self.assertNotIn('physical_hover_confirmed', result)
        self.assertNotIn('landed_confirmed', result)
        self.frame(x=.8)
        self.assertFalse(self.follow.is_following())

    async def test_follow_cleanup_accepts_stop_send_ack_and_releases_owner(self):
        self.following()
        with patch.object(sdk, 'stop_motion', return_value=CommandResult(True, 'accepted')):
            self.assertTrue(self.follow.stop().ok)
        self.assertEqual(sdk.get_status().control_owner, 'none')

    async def test_landing_blocks_motion_and_requires_confirmed_status(self):
        self.following()
        context = Host(sdk, self.follow)
        with patch.object(sdk, '_landing_delay', .2):
            task = asyncio.create_task(context.dispatch('land', [], {}))
            await wait_until(lambda: sdk.get_status().flight_state == 'landing')
            for method, args in (('move_forward', [30]), ('takeoff', []), ('start_follow', [])):
                with self.subTest(method=method), self.assertRaises(RuntimeError):
                    await Host(sdk, self.follow).dispatch(method, args, {})
            result = await task
        self.assertTrue(result['landed_confirmed'])
        await context.finish(True)
        self.assertEqual(sdk.get_status().flight_state, 'landed')
        self.assertFalse(self.follow.is_following())

    async def test_landing_timeout_keeps_other_motion_blocked(self):
        from types import SimpleNamespace
        self.following()
        context = Host(sdk, self.follow)
        clock = [time.monotonic()]
        # Advance only the SDK simulator clock; Host timeouts use real time.
        sdk_time = SimpleNamespace(monotonic=lambda: clock[0], time=time.time, sleep=time.sleep)
        with patch.object(sdk, 'time', sdk_time), patch.object(sdk, '_landing_delay', .2), \
             patch('execution.Host.LAND_CONFIRM_TIMEOUT_S', .04):
            with self.assertRaisesRegex(RuntimeError, 'landing_confirmation_timeout'):
                await context.dispatch('land', [], {})
            with self.assertRaises(RuntimeError):
                await Host(sdk, self.follow).dispatch('move_forward', [30], {})
            grant = dict(context.grant)
            with self.assertRaisesRegex(RuntimeError, 'cleanup_stop_failed.*landing_in_progress'):
                await context.finish(False)
            self.assertTrue(context.cleanup_pending)
            self.assertTrue(context.acquired)
            self.assertEqual(context.grant, grant)
            self.assertEqual(sdk.get_status().flight_state, 'landing')
            self.assertEqual(sdk.get_status().control_owner, 'model')
            self.assertEqual(sdk.get_status().velocity, (0,0,0,0))
            self.assertFalse(self.follow.is_following())
            clock[0] += .3
            self.assertEqual(sdk.get_status().flight_state, 'landed')
            await context.finish(False)
            self.assertFalse(context.cleanup_pending)
            self.assertFalse(context.acquired)
            self.assertEqual(sdk.get_status().control_owner, 'none')
            self.assertEqual(sdk.get_status().velocity, (0,0,0,0))
            self.assertFalse(self.follow.is_following())

    async def test_old_unknown_or_disconnected_status_does_not_confirm_landing(self):
        context = Host(sdk, self.follow)
        state = sdk.get_status()
        for status in (replace(state, flight_state='landed', height_cm=0, timestamp=time.time()-2),
                       replace(state, flight_state='landed', height_cm=0, connected=False),
                       replace(state, flight_state='unknown', height_cm=0)):
            with self.subTest(status=status), patch.object(sdk, 'get_status', return_value=status), \
                 patch('execution.Host.LAND_CONFIRM_TIMEOUT_S', .02):
                with self.assertRaisesRegex(RuntimeError, 'landing_confirmation_timeout'):
                    await context._confirm_landed()

    async def test_failed_land_invalidates_follow_recovery(self):
        self.following()
        with patch.object(sdk, 'land', return_value=CommandResult(False, 'failed')):
            self.assertFalse((await Host(sdk, self.follow).interrupt(True))['ok'])
        self.frame(x=.8)
        self.assertFalse(self.follow.is_following())

    async def test_target_visibility_and_cached_age_are_actual_observations(self):
        self.following()
        self.assertTrue(self.follow.status()['target_visible'])
        self.frame(visible=False)
        self.assertFalse(self.follow.status()['target_visible'])
        self.frame()
        self.clock += 1.01
        self.assertFalse(self.follow.status()['target_visible'])
        self.assertFalse(self.follow.is_following())
        self.assertEqual(sdk.get_status().control_owner, 'follow')
        self.service.control_once()
        self.assertEqual(sdk.get_status().control_owner, 'none')

    async def test_send_failure_never_keeps_follow_active(self):
        self.following()
        with patch.object(sdk, 'update_velocity', return_value=CommandResult(False, 'failed')):
            self.frame(x=.8)
        self.assertFalse(self.follow.is_following())

    async def blocked_action(self, *, land=False, explicit_stop=False):
        self.following()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        context = Host(sdk, self.follow)
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test barrier timed out')
            finished.set()
            return True
        with patch.object(driver, 'execute', side_effect=blocked):
            task = asyncio.create_task(self.move(context))
            try:
                await wait_until(entered.is_set)
                if explicit_stop:
                    result = await asyncio.wait_for(asyncio.to_thread(self.follow.stop), .5)
                    self.assertTrue(result.ok)
                else:
                    result = await asyncio.wait_for(Host(sdk, self.follow).interrupt(land), .5)
                    self.assertTrue(result['ok'])
                    self.assertNotIn('landed_confirmed', result)
                self.assertFalse(finished.is_set(), 'interrupt must not wait for the blocked driver')
            finally:
                release.set()
            if explicit_stop:
                await task
                with self.assertRaisesRegex(RuntimeError, 'follow_resume_failed'):
                    await context.finish(True)
            else:
                with self.assertRaisesRegex(RuntimeError, 'SDK_action_failed'):
                    await task
                await context.finish(False)
        self.frame(x=.8)
        self.assertFalse(self.follow.is_following())

    async def test_stop_bypasses_blocked_action_and_late_completion(self):
        await self.blocked_action()

    async def test_land_bypasses_blocked_action_and_late_completion(self):
        await self.blocked_action(land=True)
        settle()

    async def test_explicit_follow_stop_cancels_blocked_action_recovery(self):
        await self.blocked_action(explicit_stop=True)

    async def test_slow_detector_cannot_block_stop(self):
        self.following()
        entered, release = threading.Event(), threading.Event()
        detect = self.service.tracker.detect
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(2)
            return detect(*args, **kwargs)
        self.clock += .11
        self.seq += 1
        self.packet = FrameResult(synthetic_frame(x=.8), self.seq, self.service.wall(), False)
        worker = threading.Thread(target=self.service.capture_once)
        with patch.object(self.service.tracker, 'detect', side_effect=blocked):
            worker.start()
            try:
                await wait_until(entered.is_set)
                self.assertTrue((await asyncio.wait_for(Host(sdk, self.follow).interrupt(), .5))['ok'])
                self.assertTrue(worker.is_alive())
            finally:
                release.set()
                await asyncio.to_thread(worker.join, 2)
        self.assertFalse(self.follow.is_following())
