"""Current UnifiedRuntime/Gateway interruption, replacing retired Session tests."""
import asyncio
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
from RuntimeFixture import SyntheticFeed, container, driver, reset, runtime, sdk, settle, wait_until


class SessionInterruptTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.follow = reset(flying=True)
        self.app = runtime(follow=self.follow)
    def tearDown(self): reset()

    async def blocked(self, land=False):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def block(*args, **kwargs):
            entered.set()
            if not release.wait(2): raise RuntimeError('test barrier timeout')
            finished.set()
            return True
        with patch.object(driver, 'execute', side_effect=block), container([('move_forward',[30])]):
            turn = asyncio.create_task(self.app.run('drone.move_forward(30)'))
            try:
                await wait_until(entered.is_set)
                began = time.perf_counter()
                result = await asyncio.wait_for(self.app.interrupt(land), .5)
                self.assertTrue(result['ok'], result)
                self.assertLess(time.perf_counter()-began, .5)
                self.assertFalse(finished.is_set(), 'interrupt must complete while driver is still blocked')
            finally:
                release.set()
                record = await asyncio.wait_for(turn, 3)
        self.assertFalse(record['ok'], record)
        self.assertFalse(self.follow.is_following(), 'late completion must not restart follow')
        self.assertFalse(self.app.running)
        return result

    async def test_interrupt_does_not_wait_for_blocking_action(self):
        await self.blocked()

    async def test_land_does_not_wait_for_blocking_action(self):
        await self.blocked(True)
        settle()
        self.assertEqual(sdk.get_status().flight_state, 'landed')

    async def test_late_program_cancelled_after_interrupt(self):
        generation = self.app.generation
        await self.app.interrupt(False)
        with container([('move_forward',[30])]) as process:
            record = await self.app.run('drone.move_forward(30)', expected_generation=generation)
        self.assertEqual(record['status'], 'cancelled')
        self.assertEqual(record['reason'], 'stale_plan')
        self.assertEqual(process.index, 0, 'cancelled plan must not launch a container')

    async def test_cancel_immediately_revokes_runner_gateway_and_pending_plan(self):
        generation = self.app.generation
        self.app.cancel()
        self.assertEqual(self.app.generation, generation + 1)
        self.assertIsNone(self.app.execution)
        with container([('get_status', [])]) as process:
            record = await self.app.run('print(drone.get_status())', expected_generation=generation)
        self.assertEqual(record['status'], 'cancelled')
        self.assertEqual(process.index, 0)

    async def test_emergency_path_releases_follow_control(self):
        self.follow.set_frame_provider(SyntheticFeed())
        self.assertTrue(self.follow.start().ok)
        result = await self.app.interrupt()
        self.assertTrue(result['ok'], result)
        self.assertFalse(self.follow.is_following())
        self.assertEqual(sdk.get_status().control_owner, 'model')
        self.assertEqual(driver.snapshot()['velocity'], (0,0,0,0))

    async def test_no_deadlock_when_interrupt_and_turn_race(self):
        for _ in range(3):
            with container([('get_status', [])]):
                turn = asyncio.create_task(self.app.run('print(drone.get_status())'))
                await asyncio.sleep(0)
                await asyncio.wait_for(self.app.interrupt(), 1)
                await asyncio.wait_for(turn, 2)
        self.assertFalse(self.app.running)


if __name__ == '__main__': unittest.main(verbosity=2)
