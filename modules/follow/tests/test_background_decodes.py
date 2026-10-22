import unittest
from unittest.mock import Mock
import cv2
import numpy as np

from r3_follow.core import FollowController, State
from r3_follow.vision import MarkerTracker, synthetic_frame


class BackgroundDecodeTests(unittest.TestCase):
    # Actual logged non-target quads at video ~13, 20, 40 seconds.
    quads = [
        [[565,426],[552,427],[547,425],[563,424]],
        [[491,398],[529,400],[525,409],[472,406]],
        [[512,81],[517,80],[510,87],[500,91]],
        [[332.5,103],[347,102],[345,114.5],[326,113]],
    ]

    def detector_for(self, quad):
        mock = Mock()
        mock.detectMarkers.return_value = ([np.float32([quad])], np.int32([[17]]), [])
        return mock

    def test_logged_background_shapes_do_not_block_target_fallback(self):
        for quad in self.quads:
            with self.subTest(quad=quad):
                tracker = MarkerTracker()
                tracker.detector = self.detector_for(quad)
                obs = tracker.detect(synthetic_frame(), 1, 0)
                self.assertTrue(obs.found)
                self.assertEqual(tracker.last_diagnostics["ignored_other_markers"][0]["id"], 17)

    def test_only_background_still_has_no_target_and_zero_request(self):
        tracker = MarkerTracker()
        controller = FollowController(recovery_window=1.5)
        obs = tracker.detect(synthetic_frame(x=.7), 1, 0)
        controller.observe(obs)
        controller.calibrate(0)
        controller.start(0)
        tracker.detector = self.detector_for(self.quads[0])
        obs = tracker.detect(synthetic_frame(visible=False), 2, .1)
        self.assertFalse(obs.found)
        self.assertEqual(obs.reason, "not_found")
        self.assertIsNone(obs.center_x)
        controller.observe(obs)
        self.assertEqual(controller.tick(.1).right, 0)
        self.assertEqual(controller.state, State.RECOVERING)
        for seq, t in enumerate((.4, .7, 1., 1.3, 1.61), 3):
            controller.observe(tracker.detect(synthetic_frame(visible=False), seq, t))
            self.assertEqual(controller.tick(t).right, 0)
        self.assertEqual(controller.state, State.LOST)

    def test_normal_other_marker_is_logged_but_not_followed(self):
        tracker = MarkerTracker()
        obs = tracker.detect(synthetic_frame(marker_id=17), 1, 0)
        self.assertFalse(obs.found)
        self.assertEqual(obs.reason, "not_found")
        self.assertTrue(tracker.last_diagnostics["ignored_other_markers"])
        self.assertEqual(tracker.last_diagnostics["ignored_other_markers"][0]["id"], 17)

    def test_small_other_marker_never_becomes_follow_target(self):
        tracker = MarkerTracker()
        tracker.detector = self.detector_for([[20,20],[25,20],[25,25],[20,25]])
        obs = tracker.detect(synthetic_frame(visible=False), 1, 0)
        self.assertFalse(obs.found)
        self.assertEqual(obs.reason, "not_found")

    def test_duplicate_target_is_not_suppressed_by_other_id_filter(self):
        tracker = MarkerTracker()
        frame = np.minimum(synthetic_frame(x=.2,size=.12),synthetic_frame(x=.8,size=.12))
        obs = tracker.detect(frame,1,0)
        self.assertFalse(obs.found)
        self.assertEqual(obs.reason, "ambiguous_duplicate_id")
