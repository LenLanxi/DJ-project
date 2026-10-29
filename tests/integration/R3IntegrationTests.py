"""R3 control integration with the current grant/cancel gateway; no aircraft/cloud."""
import asyncio
import os
import time
import unittest
from ExecutionFixture import run_program, runner_for
import pytest
from pathlib import Path
from unittest.mock import patch

from RuntimeFixture import reset, SyntheticFeed, wait_until
from SafetyFixture import NoNetwork, DeviceCase
from execution.Host import Host
from drone_sdk import controller as sdk, _driver as driver

ROOT = Path(__file__).resolve().parents[2]


class R3IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        NoNetwork.setUp(self)
        self.enterContext(patch.dict('os.environ', {
            'R3_FOLLOW_CONFIG': str(ROOT/'follow/configs/aircraft_A_ID0_motion_follow.json')}))
        self.follow = reset(flying=True)
        self.feed = SyntheticFeed(size=.20)
        self.follow.set_frame_provider(self.feed)
        self.backend = sdk
        self.gateway = Host(sdk, self.follow)

    async def asyncTearDown(self):
        self.follow.close(wait=True)
        reset()

    async def start(self):
        result = await asyncio.to_thread(self.follow.start)
        self.assertTrue(result.ok, result.reason)
        self.service = self.follow
        self.gateway = Host(sdk, self.follow)

    async def move(self):
        await self.gateway.dispatch('move_back', [20], {})
        self.feed.size = .15

    async def test_regular_temporary_action_preserves_original_reference(self):
        await self.start()
        original = self.service.controller.reference_size
        await self.move()
        await self.gateway.finish(True)
        self.assertTrue(self.follow.is_following())
        self.assertEqual(self.service.controller.reference_size, original)

    async def test_explicit_follow_after_temporary_action_defers_to_original_real_ticket(self):
        await self.start()
        original = self.service.controller.reference_size
        await self.move()
        ticket = self.gateway.ticket
        self.assertIsNotNone(ticket)
        result = await self.gateway.dispatch('start_follow',[],{})
        self.assertEqual(result['status'],'accepted')
        self.assertEqual(result['reason'],'resume_after_program_success')
        self.assertTrue(result['reference_preserved'])
        self.assertIs(self.gateway.ticket,ticket)
        self.assertTrue(self.gateway.acquired)
        self.assertFalse(self.follow.is_following())
        await self.gateway.finish(True)
        self.assertTrue(self.follow.is_following())
        self.assertIs(self.follow,self.service)
        self.assertEqual(self.service.controller.reference_size,original)

    async def test_deferred_follow_request_does_not_resume_after_program_failure(self):
        await self.start()
        controller = self.service.controller
        original = self.service.controller.reference_size
        await self.move()
        result = await self.gateway.dispatch('start_follow',[],{})
        self.assertTrue(result['deferred'])
        await self.gateway.finish(False)
        self.assertFalse(self.follow.is_following())
        self.assertEqual(controller.reference_size, original)
        self.assertIsNot(self.service.controller, controller)
        self.assertIsNone(self.service.controller.reference_size)
        self.feed.size = .18
        def fresh():
            observation = self.service.controller.observation
            return observation is not None and observation.found and abs(observation.size_ratio-.18) < .005
        await wait_until(fresh)
        self.assertFalse(self.follow.is_following(), 'Fresh frames alone must not restore a failed program')
        self.assertIsNone(self.service.controller.reference_size)
        await self.start()
        self.assertAlmostEqual(self.service.controller.reference_size, .18, delta=.005)

    async def test_explicit_depth_adjustment_applies_after_new_frame_and_success(self):
        await self.start()
        original = self.service.controller.reference_size
        await self.move()
        result = await self.gateway.dispatch('adjust_follow_reference', ['depth'], {})
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(self.service.controller.reference_size, original)
        await self.gateway.finish(True)
        self.assertTrue(self.follow.is_following())
        self.assertAlmostEqual(self.service.controller.reference_size, .15, delta=.005)
        self.assertEqual(self.gateway.cleanup['reference_adjusted'], 'depth')

    async def test_failed_program_does_not_apply_accepted_adjustment(self):
        await self.start()
        controller = self.service.controller
        original = self.service.controller.reference_size
        await self.move()
        await self.gateway.dispatch('adjust_follow_reference', ['depth'], {})
        await self.gateway.finish(False)
        self.assertFalse(self.follow.is_following())
        self.assertEqual(controller.reference_size, original, 'The failed program must not apply the accepted adjustment')
        self.assertIsNot(self.service.controller, controller)
        self.assertIsNone(self.service.controller.reference_size)
        self.feed.size = .18
        def fresh():
            observation = self.service.controller.observation
            return observation is not None and observation.found and abs(observation.size_ratio-.18) < .005
        await wait_until(fresh)
        self.assertFalse(self.follow.is_following())
        self.assertIsNone(self.service.controller.reference_size)
        await self.start()
        self.assertAlmostEqual(self.service.controller.reference_size, .18, delta=.005)

    async def test_adjustment_requires_follow_and_matching_completed_motion(self):
        self.gateway = Host(sdk, self.follow)
        with self.assertRaisesRegex(RuntimeError, 'paused_follow'):
            await self.gateway.dispatch('adjust_follow_reference', ['depth'], {})
        await self.start()
        await self.move()
        with self.assertRaisesRegex(RuntimeError, 'matching_translation'):
            await self.gateway.dispatch('adjust_follow_reference', ['vertical'], {})
        await self.gateway.finish(False)

    async def test_adjustment_rejects_more_motion_but_interrupt_still_works(self):
        await self.start()
        await self.move()
        await self.gateway.dispatch('adjust_follow_reference', ['depth'], {})
        with self.assertRaisesRegex(RuntimeError, 'last_motion'):
            await self.gateway.dispatch('rotate', [90], {})
        result = await self.gateway.interrupt()
        self.assertTrue(result['ok'])
        await self.gateway.finish(False)
        self.assertFalse(self.follow.is_following())

    async def test_completed_action_does_not_fail_when_follow_cannot_resume(self):
        await self.start()
        self.feed.frozen = self.feed()
        await wait_until(lambda: self.service.last_frame_id == self.feed.frozen.frame_id)
        await self.gateway.dispatch('move_back', [20], {})
        await self.gateway.finish(True)
        self.assertEqual(self.gateway.cleanup['status'], 'follow_resume_failed')
        self.assertFalse(self.gateway.cleanup_pending)
        self.assertFalse(self.gateway.acquired)
        self.assertFalse(self.follow.is_following())

    async def test_pre_action_frame_cannot_rebase_even_if_its_id_is_new(self):
        await self.start()
        original = self.service.controller.reference_size
        self.feed.frozen = self.feed()
        await self.gateway.dispatch('move_back', [20], {})
        await self.gateway.dispatch('adjust_follow_reference', ['depth'], {})
        with self.assertRaisesRegex(RuntimeError, 'follow_resume_failed'):
            await self.gateway.finish(True)
        self.assertFalse(self.follow.is_following())
        self.assertEqual(self.service.controller.reference_size, original)

    async def test_runtime_default_uses_adaptive_depth_and_height_guards(self):
        await self.start()
        config = self.service.config
        self.assertEqual((config.steering_mode, config.pid_tuning, config.distance_mode),
                         ('adaptive', 'responsive_v1', 'relative_size'))
        self.assertTrue(config.vertical_follow)
        self.assertEqual(config.reference_size_limit, .20)
        self.assertEqual(self.service.rc_caps, (50, 40, 20, 50))

    @pytest.mark.docker
    @unittest.skipUnless(os.getenv('RUN_DOCKER_TESTS') == '1', 'explicit Docker checks only')
    async def test_actual_container_can_explicitly_adjust_and_receive_cleanup_confirmation(self):
        from execution.sandbox_runner import SandboxRunner
        await self.start()
        execute = driver.execute
        def simulated_move(*args, **kwargs):
            result = execute(*args, **kwargs)
            if args[0] == 'back':
                self.feed.size = .15
            return result
        with patch.object(driver, 'execute', side_effect=simulated_move):
            result = await run_program(runner_for(sdk, self.follow),
                "drone.move_back(20)\ndrone.adjust_follow_reference('depth')")
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['follow_cleanup']['reference_adjusted'], 'depth')
        self.assertAlmostEqual(self.service.controller.reference_size, .15, delta=.005)
        self.assertTrue(self.follow.is_following())


class HeightContractTests(DeviceCase):
    def test_status_preserves_packet_receipt_time_on_repeated_queries(self):
        self.real()
        stamp = self.fake.packet['_received_at']
        self.assertEqual(sdk.get_status().height_timestamp, stamp)
        time.sleep(.01)
        self.assertEqual(sdk.get_status().height_timestamp, stamp)

    def test_real_stale_height_does_not_become_fresh_by_polling(self):
        self.real()
        self.fake.refresh(age=2)
        stamp = self.fake.packet['_received_at']
        first, second = sdk.get_status(), sdk.get_status()
        self.assertEqual(first.height_timestamp, stamp)
        self.assertEqual(second.height_timestamp, stamp)
        self.assertFalse(first.connected)
        self.assertFalse(second.connected)
