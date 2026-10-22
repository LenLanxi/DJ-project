from simulation_test_support import settle_landing, settle_workers
"""Integration-wrapper sequence for acknowledgements, never physical flight."""
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from r3_follow.service_ports import FrameSnapshot
from r3_follow.team2_adapter import Team2StubPort
from r3_follow.team2_bridge import Team3FollowBridge
from r3_follow.vision import synthetic_frame

ROOT = Path(__file__).resolve().parents[1]
REPO = (ROOT.parents[1] if (ROOT.parents[1] / 'drone_sdk').is_dir()
        else ROOT.parent / 'tmp' / 'team2_b9f37f9')


class AcknowledgementSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.socket', side_effect=AssertionError('No network'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.port = Team2StubPort(REPO)
        self.sdk = self.port.sdk
        owner = self.sdk.get_status().control_owner
        if owner not in ('none', None):
            self.sdk.release_control(owner)
        self.sdk.request_control('model')
        self.sdk.takeoff('model')
        self.sdk.release_control('model')
        self.bridge = Team3FollowBridge(self.port)
        self.addCleanup(self.cleanup)
        self.seq = 0
        self.now = 10.0
        self.bridge.service.clock = lambda: self.now
        self.port.frame_provider = lambda: self.packet

    def cleanup(self):
        self.bridge.close()
        owner = self.sdk.get_status().control_owner
        if owner not in ('none', None):
            self.sdk.land(owner)
            settle_landing(self.sdk)
            self.sdk.release_control(owner)

    def frame(self):
        self.seq += 1
        self.now += .11
        self.packet = FrameSnapshot(synthetic_frame(x=.8), self.seq, time.time())
        self.bridge.service.capture_once()
        self.bridge.service.control_once()

    def check_ack_only(self, action):
        self.frame()
        self.assertTrue(self.bridge.service.calibrate_reference().ok)
        self.assertTrue(self.bridge.start_follow().ok)
        _, ticket = self.bridge.pause_for_action()
        self.assertTrue(self.sdk.request_control('model').ok)
        # Required group-2 wrapper step, not an automatic hook into its SDK.
        self.assertTrue(self.bridge.stop_follow().ok)
        with patch.object(self.sdk, action, return_value=self.sdk.CommandResult(
                ok=True, status='completed', is_blocking=False)):
            result = getattr(self.sdk, action)('model')
            self.assertEqual(result.status, 'completed')
            self.assertEqual(self.sdk.get_status().flight_state, 'flying')
            self.sdk.release_control('model')
            self.frame()
            self.assertEqual(self.bridge.finish_action(ticket, 'completed').reason, 'obsolete_ticket')
            self.assertFalse(self.bridge.is_following())
            self.assertEqual(self.bridge.service.last_channels, (0, 0, 0, 0))

    def test_land_ack_with_still_flying_status_does_not_resume_after_explicit_stop(self):
        self.check_ack_only('land')

    def test_stop_ack_with_still_flying_status_does_not_resume_after_explicit_stop(self):
        self.check_ack_only('stop_motion')
