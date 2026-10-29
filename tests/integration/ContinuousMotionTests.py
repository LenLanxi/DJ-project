import asyncio
import threading
import unittest
from unittest.mock import patch

from RuntimeFixture import SyntheticFeed, driver, reset, runtime, sdk, wait_until
from drone_sdk.contract import CommandResult
from execution.Host import Host, SDKActionError


class ContinuousMotionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.follow = reset(flying=True)
        self.app = runtime(follow=self.follow)
        driver._events.clear()
        self.responses = []

    async def action(self, name):
        method, args = {
            'start': ('start_velocity', [0, 20, 0, 0]),
            'read': ('get_status', []), 'move': ('move_forward', [20]),
            'startfollow': ('start_follow', []), 'stopfollow': ('stop_follow', []),
            'stop': ('stop_motion', []),
        }[name]
        result = await self.app.run({'action': method, 'args': args})
        if 'action_result' in result:
            self.responses.append(result['action_result'])
        return result

    async def asyncTearDown(self):
        await self.app.stop_velocity()
        reset()

    async def test_running_motion_outlives_program_and_watchdog_with_readonly_status(self):
        self.assertTrue((await self.action('start'))['ok'])
        self.assertEqual(self.responses[-1]['status'], 'running')
        self.assertFalse(self.responses[-1]['motion_verified'])
        await asyncio.sleep(.45)
        # A loaded machine can briefly trip the independent watchdog; a live
        # task must still renew after scheduling resumes rather than stay zero.
        await wait_until(lambda: driver.snapshot()['velocity'] == (0, 20, 0, 0))
        self.assertEqual(driver.snapshot()['velocity'], (0, 20, 0, 0))
        await self.action('read')
        self.assertTrue(self.responses[-1]['continuous_motion']['running'])
        self.assertIn('not cm/s', self.responses[-1]['continuous_motion']['units'])
        self.assertEqual(driver.snapshot()['velocity'], (0, 20, 0, 0))

    async def test_interrupt_stops_renewals_and_never_restarts_follow(self):
        await self.action('start')
        self.assertTrue((await self.app.interrupt())['ok'])
        count = len(driver.snapshot()['events'])
        await asyncio.sleep(.25)
        later = driver.snapshot()['events'][count:]
        self.assertFalse(any(row.get('event') == 'velocity' and any(row['channels']) for row in later))
        self.assertEqual(driver.snapshot()['velocity'], (0, 0, 0, 0))
        self.assertFalse(self.app.velocity_status()['running'])
        self.assertFalse(self.follow.is_following())

    async def test_new_motion_and_follow_stop_take_over_cleanly(self):
        for action in ('move', 'stopfollow', 'stop'):
            with self.subTest(action=action):
                await self.action('start')
                self.assertTrue((await self.action(action))['ok'])
                self.assertFalse(self.app.velocity_status()['running'])
                self.assertEqual(driver.snapshot()['velocity'], (0, 0, 0, 0))

    async def test_follow_can_start_after_continuous_motion_and_is_not_resumed_on_stop(self):
        self.follow.set_frame_provider(SyntheticFeed())
        await self.action('start')
        self.assertTrue((await self.action('startfollow'))['ok'])
        self.assertFalse(self.app.velocity_status()['running'])
        self.assertTrue(self.follow.is_following())
        await self.action('start')
        await self.action('stop')
        self.assertFalse(self.follow.is_following())

    async def test_generated_python_is_rejected_without_starting_movement(self):
        with self.assertRaises(ValueError):
            await self.app.run('drone.start_velocity(0, 20, 0, 0)')
        self.assertFalse(self.app.velocity_status()['running'])
        self.assertEqual(driver.snapshot()['velocity'], (0, 0, 0, 0))

    async def test_close_and_cleanup_stop_the_owned_motion(self):
        await self.action('start')
        self.app.close()
        self.assertTrue((await self.app.retry_cleanup())['ok'])
        self.assertFalse(self.app.velocity_status()['running'])
        self.assertEqual(driver.snapshot()['velocity'], (0, 0, 0, 0))

    async def test_interrupt_during_late_grant_does_not_renew_or_leak_old_motion(self):
        entered, release = threading.Event(), threading.Event()
        original = sdk.request_control
        def slow_grant(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            release.wait(2)
            return result
        with patch.object(sdk, 'request_control', side_effect=slow_grant):
            starting = asyncio.create_task(self.action('start'))
            await wait_until(entered.is_set)
            stopping = asyncio.create_task(self.app.interrupt())
            try:
                await wait_until(lambda: any(row.get('event') == 'priority_override'
                    for row in driver.snapshot()['events']))
                self.assertEqual(driver.snapshot()['velocity'], (0, 0, 0, 0))
            finally:
                release.set()
            await asyncio.wait_for(stopping, 2)
            await asyncio.wait_for(starting, 2)
        self.assertIsNone(self.app.motion_host)
        self.assertFalse(self.app.velocity_status()['running'])
        self.assertFalse(any(row.get('event') == 'velocity' and any(row['channels'])
                             for row in driver.snapshot()['events']))

    async def test_failed_renewal_reports_last_reason_and_stops(self):
        original = sdk.update_velocity
        calls = 0
        def fail_renewal(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                return CommandResult(False, 'failed', 'synthetic_renewal_failure', False)
            return original(*args, **kwargs)
        with patch.object(sdk, 'update_velocity', side_effect=fail_renewal):
            await self.action('start')
            await wait_until(lambda: not self.app.velocity_status()['running'])
        self.assertIn('synthetic_renewal_failure', self.app.velocity_status()['last_reason'])
        await asyncio.shield(self.app.motion_task)
        self.assertEqual(driver.snapshot()['velocity'], (0, 0, 0, 0))

    async def test_failed_initial_send_retains_structured_sdk_error(self):
        owner = Host(sdk, self.follow)
        failure = CommandResult(False, 'failed', 'synthetic_send_failure', False,
                                details={'wire_sent': False})
        with patch.object(sdk, 'update_velocity', return_value=failure):
            with self.assertRaises(SDKActionError) as raised:
                await self.app.start_velocity(owner, [0, 20, 0, 0])
        self.assertEqual(raised.exception.result['reason'], 'synthetic_send_failure')
        self.assertFalse(raised.exception.result['details']['wire_sent'])
        self.assertIsNone(self.app.motion_host)

    async def test_status_exposes_changed_clamp_result_without_repeating_notice(self):
        events = []
        self.app.log = events.append
        calls = 0
        def velocity_result(grant, *channels, **kwargs):
            nonlocal calls
            calls += 1
            clamped = calls > 1
            return CommandResult(True, 'accepted', is_blocking=False, details={
                'requested_channels': list(channels),
                'applied_channels': [0, 0, 0, 0] if clamped else list(channels),
                'clamped': clamped, 'clamp_reason': 'height_limit' if clamped else None,
                'wire_sent': False, 'simulated': True})
        with patch.object(sdk, 'update_velocity', side_effect=velocity_result):
            result = await self.action('start')
            self.assertFalse(result['action_result']['command_sent'])
            await wait_until(lambda: calls >= 3)
            await self.action('read')
            motion = self.responses[-1]['continuous_motion']
            self.assertTrue(motion['running'])
            self.assertEqual(motion['channels'], (0, 20, 0, 0))
            self.assertEqual(motion['last_command']['applied_channels'], [0, 0, 0, 0])
            self.assertTrue(motion['last_command']['clamped'])
            self.assertEqual(len([event for event in events
                                  if event['event'] == 'continuous_motion_clamp_changed']), 1)
        await self.app.stop_velocity()
        self.assertIsNone(self.app.velocity_status()['last_command'])

    async def test_cancellation_during_continuous_action_cleanup_stops_background_motion(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = Host.finish

        async def slow_finish(host, success):
            if host.motion is self.app:
                entered.set()
                await release.wait()
            return await original(host, success)

        with patch.object(Host, 'finish', new=slow_finish):
            task = asyncio.create_task(self.action('start'))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.app.velocity_status()['running'])
        self.assertEqual(driver.snapshot()['velocity'], (0, 0, 0, 0))

    async def test_cancelled_late_device_thread_cannot_execute_after_cleanup(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = sdk.move_forward

        def slow_move(*args, **kwargs):
            entered.set()
            release.wait(2)
            try:
                return original(*args, **kwargs)
            finally:
                finished.set()

        with patch.object(sdk, 'move_forward', side_effect=slow_move):
            task = asyncio.create_task(self.action('move'))
            await wait_until(entered.is_set)
            task.cancel()
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
            finally:
                release.set()
            await wait_until(finished.is_set)
        self.assertFalse(any(row.get('event') == 'command' and row.get('action') == 'forward'
                             for row in driver.snapshot()['events']))
        self.assertEqual(driver.snapshot()['velocity'], (0, 0, 0, 0))
