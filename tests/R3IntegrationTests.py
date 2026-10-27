"""R3 control integration with the current grant/cancel gateway; no aircraft/cloud."""
import asyncio
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from RuntimeFixture import reset, SyntheticFeed
from SafetyTests import NoNetwork, DeviceCase
from backends import SDKBackend
from gateway import Gateway
from drone_sdk import controller as sdk, _driver as driver

ROOT = Path(__file__).resolve().parents[1]


class R3IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        NoNetwork.setUp(self)
        self.enterContext(patch.dict('os.environ', {
            'R3_FOLLOW_CONFIG': str(ROOT/'modules/follow/configs/aircraft_A_ID0_motion_follow.json')}))
        reset(flying=True)
        self.feed = SyntheticFeed(size=.20)
        sdk.set_frame_provider(self.feed)
        self.gateway = Gateway(SDKBackend(ROOT, allow_simulated=True))

    async def asyncTearDown(self):
        sdk.stop_follow()
        reset()

    async def start(self):
        result = await asyncio.to_thread(sdk.start_follow)
        self.assertTrue(result.ok, result.reason)
        self.service = sdk._follow._bridge.service
        self.gateway.begin()

    async def move(self):
        await self.gateway.dispatch('move_back', [20], {})
        self.feed.size = .15

    async def test_regular_temporary_action_preserves_original_reference(self):
        await self.start()
        original = self.service.controller.reference_size
        await self.move()
        await self.gateway.finish(True)
        self.assertTrue(sdk.is_following())
        self.assertEqual(self.service.controller.reference_size, original)

    async def test_explicit_depth_adjustment_applies_after_new_frame_and_success(self):
        await self.start()
        original = self.service.controller.reference_size
        await self.move()
        result = await self.gateway.dispatch('adjust_follow_reference', ['depth'], {})
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(self.service.controller.reference_size, original)
        await self.gateway.finish(True)
        self.assertTrue(sdk.is_following())
        self.assertAlmostEqual(self.service.controller.reference_size, .15, delta=.005)
        self.assertEqual(self.gateway.cleanup['reference_adjusted'], 'depth')

    async def test_failed_program_does_not_apply_accepted_adjustment(self):
        await self.start()
        original = self.service.controller.reference_size
        await self.move()
        await self.gateway.dispatch('adjust_follow_reference', ['depth'], {})
        await self.gateway.finish(False)
        self.assertFalse(sdk.is_following())
        self.assertEqual(self.service.controller.reference_size, original)

    async def test_adjustment_requires_follow_and_matching_completed_motion(self):
        self.gateway.begin()
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
        self.assertFalse(sdk.is_following())

    async def test_pre_action_frame_cannot_rebase_even_if_its_id_is_new(self):
        await self.start()
        original = self.service.controller.reference_size
        self.feed.frozen = self.feed()
        await self.gateway.dispatch('move_back', [20], {})
        await self.gateway.dispatch('adjust_follow_reference', ['depth'], {})
        with self.assertRaisesRegex(RuntimeError, 'follow_resume_failed'):
            await self.gateway.finish(True)
        self.assertFalse(sdk.is_following())
        self.assertEqual(self.service.controller.reference_size, original)

    async def test_runtime_default_uses_adaptive_depth_and_height_guards(self):
        await self.start()
        config = self.service.config
        self.assertEqual((config.steering_mode, config.pid_tuning, config.distance_mode),
                         ('adaptive', 'responsive_v1', 'relative_size'))
        self.assertTrue(config.vertical_follow)
        self.assertEqual(config.reference_size_limit, .20)
        self.assertEqual(self.service.rc_caps, (50, 40, 20, 50))

    @unittest.skipUnless(os.getenv('RUN_DOCKER_TESTS') == '1', 'explicit Docker checks only')
    async def test_actual_container_can_explicitly_adjust_and_receive_cleanup_confirmation(self):
        from sandbox_runner import SandboxRunner
        await self.start()
        execute = driver.execute
        def simulated_move(*args, **kwargs):
            result = execute(*args, **kwargs)
            if args[0] == 'back':
                self.feed.size = .15
            return result
        with patch.object(driver, 'execute', side_effect=simulated_move):
            result = await SandboxRunner(self.gateway).run(
                "drone.move_back(20)\ndrone.adjust_follow_reference('depth')")
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['follow_cleanup']['reference_adjusted'], 'depth')
        self.assertAlmostEqual(self.service.controller.reference_size, .15, delta=.005)
        self.assertTrue(sdk.is_following())


class HeightContractTests(DeviceCase):
    def test_status_preserves_packet_receipt_time_on_repeated_queries(self):
        self.real()
        stamp = self.fake.packet['_received_at']
        self.assertEqual(sdk.get_status().height_timestamp, stamp)
        time.sleep(.01)
        self.assertEqual(sdk.get_status().height_timestamp, stamp)

    def test_real_stale_height_does_not_become_fresh_by_polling(self):
        from r3_follow.team2_adapter import Team2StubPort
        self.real()
        port = Team2StubPort(ROOT)
        self.assertTrue(port.get_status().height_fresh)
        self.fake.refresh(age=2)
        self.assertFalse(port.get_status().height_fresh)
        self.assertFalse(port.get_status().height_fresh)
