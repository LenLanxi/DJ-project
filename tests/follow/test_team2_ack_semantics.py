from FollowSupport import settle_landing, settle_workers
"""Integration-wrapper sequence for acknowledgements, never physical flight."""
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from drone_sdk.contract import FrameResult
from drone_sdk import controller as Device
from follow.r3_follow.config import load_config
from follow.r3_follow import FollowService
from follow.r3_follow.vision import synthetic_frame

SOURCE = Path(__file__).resolve().parents[2] / 'follow'
REPO = Path(__file__).resolve().parents[2]


class AcknowledgementSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.socket', side_effect=AssertionError('No network'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.port = Device
        self.sdk = self.port
        owner = self.sdk.get_status().control_owner
        if owner not in ('none', None):
            self.sdk.release_control(owner)
        self.sdk.request_control('model')
        self.sdk.takeoff('model')
        self.sdk.release_control('model')
        self.service = FollowService(self.port, config=load_config(), recovery_window=3)
        self.addCleanup(self.cleanup)
        self.seq = 0
        self.now = 10.0
        self.service.clock = lambda: self.now
        self.service.frame_provider = lambda: self.packet

    def cleanup(self):
        self.service.close()
        owner = self.sdk.get_status().control_owner
        if owner not in ('none', None):
            self.sdk.land(owner)
            settle_landing(self.sdk)
            self.sdk.release_control(owner)

    def frame(self):
        self.seq += 1
        self.now += .11
        self.packet = FrameResult(synthetic_frame(x=.8), self.seq, time.time(), False)
        self.service.capture_once()
        self.service.control_once()

    def check_ack_only(self, action):
        self.frame()
        self.assertTrue(self.service.calibrate_reference().ok)
        self.assertTrue(self.service._start_tracking().ok)
        ticket = self.service.pause_if_active()
        self.assertTrue(self.sdk.request_control('model').ok)
        # Required group-2 wrapper step, not an automatic hook into its SDK.
        self.assertTrue(self.service._stop_tracking().ok)
        with patch.object(self.sdk, action, return_value=self.sdk.CommandResult(
                ok=True, status='completed', is_blocking=False)):
            result = getattr(self.sdk, action)('model')
            self.assertEqual(result.status, 'completed')
            self.assertEqual(self.sdk.get_status().flight_state, 'flying')
            self.sdk.release_control('model')
            self.frame()
            self.assertEqual(self.service.finish_action(ticket, True).reason, 'obsolete_ticket')
            self.assertFalse(self.service.is_following())
            self.assertEqual(self.service.last_channels, (0, 0, 0, 0))

    def test_land_ack_with_still_flying_status_does_not_resume_after_explicit_stop(self):
        self.check_ack_only('land')

    def test_stop_ack_with_still_flying_status_does_not_resume_after_explicit_stop(self):
        self.check_ack_only('stop_motion')
