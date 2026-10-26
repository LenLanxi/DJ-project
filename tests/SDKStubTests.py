"""Former print-only SDK demo expressed as offline, discoverable assertions."""
import time
import unittest
from RuntimeFixture import driver, reset, sdk

class SDKStubTests(unittest.TestCase):
    def setUp(self): reset(flying=True)
    def tearDown(self): reset()

    def test_status_and_missing_frame_are_explicit(self):
        state=sdk.get_status()
        self.assertTrue(state.simulated)
        self.assertEqual(state.flight_state,'flying')
        self.assertIsNone(sdk.get_frame().frame)

    def test_competing_owner_is_rejected(self):
        self.assertTrue(sdk.request_control('model').ok)
        self.assertEqual(sdk.request_control('follow').reason,'busy')

    def test_move_uses_current_grant(self):
        grant=sdk.request_control('model')
        self.assertTrue(grant.ok)
        result=sdk.move_forward('model',50,control_epoch=grant.control_epoch,command_epoch=grant.command_epoch)
        self.assertTrue(result.ok)
        self.assertEqual(result.status,'completed')

    def test_velocity_watchdog_requests_zero(self):
        grant=sdk.request_control('model')
        self.assertTrue(sdk.update_velocity('model',0,50,0,0,control_epoch=grant.control_epoch,command_epoch=grant.command_epoch).ok)
        sdk._last_velocity_time=time.monotonic()-sdk.VELOCITY_WATCHDOG_S-.01
        sdk._watchdog_tick()
        self.assertEqual(driver.snapshot()['velocity'],(0,0,0,0))

    def test_release_confirms_owner_is_none(self):
        grant=sdk.request_control('model')
        self.assertTrue(sdk.release_control('model',control_epoch=grant.control_epoch,command_epoch=grant.command_epoch).ok)
        self.assertEqual(sdk.get_status().control_owner,'none')
