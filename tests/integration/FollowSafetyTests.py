"""Authorization and cleanup regressions against the disconnected SDK."""
import unittest
from unittest.mock import patch
from RuntimeFixture import reset, sdk, SyntheticFeed


class FollowAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.follow = reset(flying=True)
        self.addCleanup(reset)

    def grant(self):
        result = sdk.request_control('follow', exclusive=True)
        self.assertTrue(result.ok)
        return dict(control_epoch=result.control_epoch, command_epoch=result.command_epoch)

    def test_stop_between_admission_and_send_rejects_captured_command(self):
        grant = self.grant()
        send = sdk.update_velocity
        def stopped_then_send(*args, **kwargs):
            self.assertTrue(sdk.stop_motion('follow').ok)
            return send(*args, **kwargs)
        with patch.object(sdk, 'update_velocity', side_effect=stopped_then_send):
            result = sdk.update_velocity('follow', 0, 20, 0, 0, **grant)
        self.assertFalse(result.ok)
        self.assertEqual(sdk.get_status().velocity, (0, 0, 0, 0))

    def test_confirmation_does_not_borrow_a_replacement_grant(self):
        request = sdk.request_control
        def replaced(owner, **kwargs):
            original = request(owner, **kwargs)
            self.assertTrue(sdk.release_control(owner).ok)
            self.assertTrue(request(owner).ok)
            return original
        with patch.object(sdk, 'request_control', side_effect=replaced):
            grant = self.grant()
        self.assertNotEqual(grant['control_epoch'], sdk.get_status().control_epoch)
        result = sdk.update_velocity('follow', 0, 20, 0, 0, **grant)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'obsolete_control')

    def test_command_epoch_cannot_refresh_after_a_stop(self):
        grant = self.grant()
        self.assertTrue(sdk.stop_motion('follow', control_epoch=grant['control_epoch']).ok)
        self.assertFalse(sdk.update_velocity('follow', 0, 20, 0, 0, **grant).ok)
        self.assertEqual(sdk.get_status().velocity, (0, 0, 0, 0))

    def test_old_control_epoch_is_rejected_for_same_owner(self):
        grant = self.grant()
        self.assertTrue(sdk.release_control('follow').ok)
        self.grant()
        self.assertFalse(sdk.update_velocity('follow', 0, 20, 0, 0, **grant).ok)
        self.assertEqual(sdk.get_status().velocity, (0, 0, 0, 0))

    def start(self):
        self.follow.set_frame_provider(SyntheticFeed(position=.8))
        self.assertTrue(self.follow.start().ok)

    def test_telemetry_exception_does_not_prevent_atomic_zero_and_release(self):
        self.start()
        with patch.object(sdk, 'get_status', side_effect=RuntimeError('telemetry lost')):
            # Cleanup uses the captured grant, independent of telemetry reads.
            self.assertTrue(self.follow.stop().ok)
        self.assertEqual(sdk.get_status().velocity, (0, 0, 0, 0))
        self.assertEqual(sdk.get_status().control_owner, 'none')

    def test_failed_release_keeps_authorization_for_retry(self):
        self.start()
        with self.follow.lock:
            with patch.object(sdk, 'release_control', return_value=sdk.ControlResult(False, 'follow', 'injected')):
                self.assertFalse(self.follow.stop().ok)
                self.assertIsNotNone(self.follow.grant)
            self.assertTrue(self.follow.stop().ok)
        self.assertEqual(sdk.get_status().velocity, (0, 0, 0, 0))
        self.assertEqual(sdk.get_status().control_owner, 'none')

    def test_replacement_owner_is_not_stopped(self):
        self.start()
        with self.follow.lock:
            self.assertTrue(sdk.release_control('follow').ok)
            self.assertTrue(sdk.request_control('model').ok)
            self.assertTrue(sdk.update_velocity('model', 0, 11, 0, 12).ok)
            self.assertTrue(self.follow.stop().ok)
        self.assertEqual(sdk.get_status().velocity, (0, 11, 0, 12))
        self.assertEqual(sdk.get_status().control_owner, 'model')
