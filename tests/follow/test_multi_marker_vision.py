"""Three targets, selected identity, and bounded mixed-difficulty duplicates.

All images are synthetic, including the existing deterministic rescue fixtures.
These tests do not prove real camera or aircraft performance.
"""
from pathlib import Path
import unittest

import cv2
import numpy as np

from follow.r3_follow.vision import MarkerTracker


def marker_scene(items, shape=(480, 960)):
    """items: (ID, center_x, center_y, side_px, quarter_turns, dark, light)."""
    frame = np.full(shape, 245, np.uint8)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for marker_id, cx, cy, side, turns, dark, light in items:
        marker = cv2.aruco.generateImageMarker(dictionary, marker_id, side)
        marker = np.rot90(marker, turns)
        marker = np.where(marker == 0, dark, light).astype(np.uint8)
        x, y = int(cx-side/2), int(cy-side/2)
        frame[y:y+side, x:x+side] = marker
    return frame


class MultiMarkerVisionTests(unittest.TestCase):
    def setUp(self):
        cv2.setNumThreads(1)

    def assert_position(self, tracker, image, frame_id, x, y):
        before = image.copy()
        obs = tracker.detect(image, frame_id, frame_id*.1, 'synthetic_three_ids')
        self.assertTrue(obs.found, tracker.last_diagnostics)
        self.assertAlmostEqual(obs.center_x, x-.5, delta=1.5)
        self.assertAlmostEqual(obs.center_y, y-.5, delta=1.5)
        self.assertEqual(obs.target_id, tracker.target_id)
        np.testing.assert_array_equal(image, before)
        return obs

    def test_dictionary_boundaries_and_numpy_integer_are_supported(self):
        for value in (0, 1, 2, 49, np.int64(2)):
            self.assertEqual(MarkerTracker(value).target_id, int(value))

    def test_three_visible_ids_each_tracker_selects_only_configured_target(self):
        frame = marker_scene([(i, 160+i*320, 240, 100, 0, 0, 255) for i in range(3)])
        for marker_id in range(3):
            tracker = MarkerTracker(marker_id)
            self.assert_position(tracker, frame, 1, 160+marker_id*320, 240)
            self.assertEqual(tracker.last_diagnostics['duplicate_check']['roi_count'], 0)
            self.assertEqual([a['method'] for a in tracker.last_diagnostics['attempts']],
                             ['default', 'identity_half_contrast'])

    def test_targets_exchange_positions_without_identity_swap(self):
        trackers = [MarkerTracker(i) for i in range(3)]
        # Crossing is represented without physical occlusion first; the
        # occlusion case is separately checked and must give no position.
        for frame_id, order in enumerate(((0, 1, 2), (2, 0, 1), (1, 2, 0)), 1):
            frame = marker_scene([(i, 160+p*320, 240, 100, 0, 0, 255)
                                  for p, i in enumerate(order)])
            for marker_id, tracker in enumerate(trackers):
                self.assert_position(tracker, frame, frame_id,
                                     160+order.index(marker_id)*320, 240)

    def test_rotations_and_different_scales_do_not_choose_largest_marker(self):
        frame = marker_scene([(0, 140, 240, 60, 1, 0, 255),
                              (1, 450, 240, 180, 2, 0, 255),
                              (2, 780, 240, 120, 3, 0, 255)])
        for marker_id, center in enumerate((140, 450, 780)):
            self.assert_position(MarkerTracker(marker_id), frame, 1, center, 240)

    def test_moderate_brightness_and_blur_preserve_selected_identity(self):
        frame = marker_scene([(i, 160+i*320, 240, 100, i, 45, 190) for i in range(3)])
        frame = cv2.GaussianBlur(frame, (3, 3), .6)
        for marker_id in range(3):
            self.assert_position(MarkerTracker(marker_id), frame, 1, 160+marker_id*320, 240)

    def test_moderate_perspective_retains_each_id_without_relabelling(self):
        frame = np.full((480, 960), 245, np.uint8)
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        for marker_id in range(3):
            x = 160+320*marker_id
            quad = np.float32([[x-65, 170], [x+65, 188], [x+65, 292], [x-65, 310]])
            matrix = cv2.getPerspectiveTransform(np.float32([[0, 0], [119, 0], [119, 119], [0, 119]]), quad)
            marker = cv2.aruco.generateImageMarker(dictionary, marker_id, 120)
            warped = cv2.warpPerspective(marker, matrix, (960, 480), borderValue=245)
            mask = np.zeros_like(frame)
            cv2.fillConvexPoly(mask, quad.astype(np.int32), 255)
            frame[mask > 0] = warped[mask > 0]
        for marker_id in range(3):
            obs = MarkerTracker(marker_id).detect(frame, 1, .1)
            self.assertTrue(obs.found)
            self.assertAlmostEqual(obs.center_x, 160+320*marker_id, delta=2)
            self.assertAlmostEqual(obs.center_y, 240, delta=2)
            self.assertEqual(obs.target_id, marker_id)

    def test_target_exit_does_not_switch_to_other_present_id(self):
        trackers = [MarkerTracker(i) for i in range(3)]
        full = marker_scene([(i, 160+i*320, 240, 100, 0, 0, 255) for i in range(3)])
        for target, tracker in enumerate(trackers):
            self.assert_position(tracker, full, 1, 160+target*320, 240)
            missing = marker_scene([(i, 160+i*320, 240, 100, 0, 0, 255)
                                    for i in range(3) if i != target])
            obs = tracker.detect(missing, 2, .2)
            self.assertFalse(obs.found)
            self.assertIsNone(obs.center_x)
            self.assertEqual(obs.corners, ())

    def test_target_occlusion_does_not_borrow_other_person_position(self):
        frame = marker_scene([(i, 160+i*320, 240, 100, 0, 0, 255) for i in range(3)])
        frame[180:300, 440:510] = 245
        self.assertFalse(MarkerTracker(1).detect(frame, 1, .1).found)
        self.assert_position(MarkerTracker(0), frame, 1, 160, 240)
        self.assert_position(MarkerTracker(2), frame, 1, 800, 240)

    def test_unrelated_id_is_never_relabelled_as_one_of_three_targets(self):
        frame = marker_scene([(49, 480, 240, 180, 0, 0, 255)])
        for marker_id in range(3):
            obs = MarkerTracker(marker_id).detect(frame, 1, 0)
            self.assertFalse(obs.found)
            self.assertIsNone(obs.center_x)

    def test_reappearing_target_recovers_its_own_position(self):
        tracker = MarkerTracker(2)
        self.assertFalse(tracker.detect(marker_scene([(0, 150, 240, 100, 0, 0, 255)]), 1, 0).found)
        self.assert_position(tracker, marker_scene([(2, 750, 240, 100, 0, 0, 255),
                                                   (1, 250, 240, 200, 0, 0, 255)]),
                             2, 750, 240)

    def test_two_decoded_copies_of_selected_id_are_ambiguous(self):
        for marker_id in range(3):
            frame = marker_scene([(marker_id, 160, 240, 100, 0, 0, 255),
                                  (marker_id, 800, 240, 100, 2, 0, 255)])
            obs = MarkerTracker(marker_id).detect(frame, 1, 0)
            self.assertEqual(obs.reason, 'ambiguous_duplicate_id')
            self.assertFalse(obs.found)
            self.assertIsNone(obs.center_x)

    def fixture_pair(self, name, shape):
        fixture = cv2.imdecode(np.frombuffer((Path(__file__).resolve().parents[1]/'fixtures'/'follow'/name).read_bytes(),
                                              np.uint8), 0)
        frame = marker_scene([(0, 150, 160, 100, 0, 0, 255)], shape)
        h, w = fixture.shape
        frame[100:100+h, 500:500+w] = fixture
        return frame

    def test_easy_target_does_not_hide_side_view_duplicate(self):
        for shape in ((480, 960), (720, 1280)):
            tracker = MarkerTracker()
            obs = tracker.detect(self.fixture_pair('side_contour_rescue.png', shape), 1, 0)
            self.assertFalse(obs.found)
            self.assertEqual(obs.reason, 'ambiguous_duplicate_id')
            self.assertGreater(tracker.last_diagnostics['duplicate_check']['roi_count'], 0)

    def test_easy_target_does_not_hide_contrast_duplicate(self):
        tracker = MarkerTracker()
        obs = tracker.detect(self.fixture_pair('contrast_rescue.png', (480, 960)), 1, 0)
        self.assertFalse(obs.found)
        self.assertEqual(obs.reason, 'ambiguous_duplicate_id')

    def test_different_rescue_decoders_must_not_hide_large_image_duplicate(self):
        folder = Path(__file__).resolve().parents[1]/'fixtures'/'follow'
        side = cv2.imdecode(np.frombuffer((folder/'side_contour_rescue.png').read_bytes(), np.uint8), 0)
        contrast = cv2.imdecode(np.frombuffer((folder/'contrast_rescue.png').read_bytes(), np.uint8), 0)
        self.assertTrue(MarkerTracker().detect(side, 1, 0).found)
        self.assertTrue(MarkerTracker().detect(contrast, 1, 0).found)
        frame = np.full((720, 1280), 245, np.uint8)
        frame[20:280, 20:380] = side
        frame[200:460, 650:1010] = contrast
        tracker = MarkerTracker()
        obs = tracker.detect(frame, 1, 0)
        self.assertFalse(obs.found)
        self.assertEqual(obs.reason, 'ambiguous_duplicate_id')
        self.assertIsNone(obs.center_x)
        diag = tracker.last_diagnostics['large_image_rescue']
        self.assertLessEqual(diag['roi_count'], tracker.RESCUE_MAX_ROIS)
        self.assertLessEqual(diag['decoder_calls'], tracker.RESCUE_MAX_ROIS*3)
        self.assertLessEqual(diag['decoder_pixels'], tracker.RESCUE_MAX_PIXELS*3)

    def test_different_rescue_decoders_must_not_hide_small_image_duplicate(self):
        folder = Path(__file__).resolve().parents[1]/'fixtures'/'follow'
        side = cv2.imdecode(np.frombuffer((folder/'side_contour_rescue.png').read_bytes(), np.uint8), 0)
        contrast = cv2.imdecode(np.frombuffer((folder/'contrast_rescue.png').read_bytes(), np.uint8), 0)
        for y, x in ((0, 500), (60, 590), (120, 500), (200, 590)):
            frame = np.full((480, 960), 245, np.uint8)
            frame[0:260, 0:360] = side
            frame[y:y+260, x:x+360] = contrast
            tracker = MarkerTracker()
            obs = tracker.detect(frame, 1, 0)
            with self.subTest(y=y, x=x):
                self.assertFalse(obs.found)
                self.assertIn(obs.reason, ('ambiguous_duplicate_id', 'ambiguous_scan_budget_exceeded'))
                self.assertIsNone(obs.center_x)

    def test_one_side_target_in_overlapping_crops_is_not_false_duplicate(self):
        folder = Path(__file__).resolve().parents[1]/'fixtures'/'follow'
        side = cv2.imdecode(np.frombuffer((folder/'side_contour_rescue.png').read_bytes(), np.uint8), 0)
        for y, x in ((20, 20), (100, 200), (300, 700)):
            frame = np.full((720, 1280), 245, np.uint8)
            frame[y:y+260, x:x+360] = side
            with self.subTest(y=y, x=x):
                tracker = MarkerTracker()
                obs = tracker.detect(frame, 1, 0)
                self.assertTrue(obs.found, tracker.last_diagnostics)
                self.assertEqual(tracker.last_diagnostics['detected_ids'], [0])

    def test_partial_duplicate_scan_never_reports_unique_target(self):
        tracker = MarkerTracker()
        tracker.DUPLICATE_MAX_ROIS = 0
        obs = tracker.detect(self.fixture_pair('side_contour_rescue.png', (480, 960)), 1, 0)
        self.assertFalse(obs.found)
        self.assertEqual(obs.reason, 'ambiguous_scan_budget_exceeded')
        self.assertTrue(tracker.last_diagnostics['duplicate_check']['budget_exceeded'])

    def test_successful_duplicate_scan_has_explicit_work_bounds(self):
        tracker = MarkerTracker()
        tracker.detect(self.fixture_pair('side_contour_rescue.png', (720, 1280)), 1, 0)
        diag = tracker.last_diagnostics['duplicate_check']
        self.assertLessEqual(diag['roi_count'], tracker.DUPLICATE_MAX_ROIS)
        self.assertLessEqual(diag['input_pixels'], tracker.RESCUE_MAX_PIXELS)
        self.assertLessEqual(diag['decoder_calls'], tracker.DUPLICATE_MAX_ROIS*3)
        self.assertLessEqual(diag['decoder_pixels'], tracker.RESCUE_MAX_PIXELS*3)
        attempts = [a for a in tracker.last_diagnostics['attempts']
                    if a['method'].startswith('duplicate_roi_')]
        self.assertGreaterEqual(len(attempts), diag['roi_count'])
        self.assertLessEqual(len(attempts), 3*diag['roi_count'])
        self.assertTrue(all(a['input_pixels'] <= 512*512 for a in attempts))


if __name__ == '__main__':
    unittest.main()
