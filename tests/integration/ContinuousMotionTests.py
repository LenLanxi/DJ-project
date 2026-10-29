import asyncio
import threading
from types import SimpleNamespace
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

        async def execute(code, host, outcome):
            outcome.executing = True
            try:
                if code in ('start', 'fail'):
                    self.responses.append(await host.dispatch('start_velocity', [0, 20, 0, 0], {}))
                elif code == 'read':
                    self.responses.append(await host.dispatch('get_status', [], {}))
                elif code == 'move':
                    self.responses.append(await host.dispatch('move_forward', [20], {}))
                elif code == 'startfollow':
                    self.responses.append(await host.dispatch('start_follow', [], {}))
                elif code == 'stopfollow':
                    self.responses.append(await host.dispatch('stop_follow', [], {}))
                elif code == 'stop':
                    self.responses.append(await host.dispatch('stop_motion', [], {}))
                outcome.result = {'ok': code != 'fail', 'status': 'failed' if code == 'fail' else 'completed'}
            finally:
                outcome.executing = False
        self.app.runner = SimpleNamespace(run=execute)

    async def asyncTearDown(self):
        await self.app.stop_velocity()
        reset()

    async def test_running_motion_outlives_program_and_watchdog_with_readonly_status(self):
        self.assertTrue((await self.app.run('start'))['ok'])
        self.assertEqual(self.responses[-1]['status'], 'running')
        self.assertFalse(self.responses[-1]['motion_verified'])
        await asyncio.sleep(.45)
        self.assertEqual(driver.snapshot()['velocity'], (0, 20, 0, 0))
        await self.app.run('read')
        self.assertTrue(self.responses[-1]['continuous_motion']['running'])
        self.assertIn('not cm/s', self.responses[-1]['continuous_motion']['units'])
        self.assertEqual(driver.snapshot()['velocity'], (0, 20, 0, 0))

    async def test_interrupt_stops_renewals_and_never_restarts_follow(self):
        await self.app.run('start')
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
                await self.app.run('start')
                self.assertTrue((await self.app.run(action))['ok'])
                self.assertFalse(self.app.velocity_status()['running'])
                self.assertEqual(driver.snapshot()['velocity'], (0, 0, 0, 0))

    async def test_follow_can_start_after_continuous_motion_and_is_not_resumed_on_stop(self):
        self.follow.set_frame_provider(SyntheticFeed())
        await self.app.run('start')
        self.assertTrue((await self.app.run('startfollow'))['ok'])
        self.assertFalse(self.app.velocity_status()['running'])
        self.assertTrue(self.follow.is_following())
        await self.app.run('start')
        await self.app.run('stop')
        self.assertFalse(self.follow.is_following())

    async def test_failed_starting_program_does_not_leave_background_movement(self):
        self.assertFalse((await self.app.run('fail'))['ok'])
        self.assertFalse(self.app.velocity_status()['running'])
        self.assertEqual(driver.snapshot()['velocity'], (0, 0, 0, 0))

    async def test_close_and_cleanup_stop_the_owned_motion(self):
        await self.app.run('start')
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
            starting = asyncio.create_task(self.app.run('start'))
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
            await self.app.run('start')
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
