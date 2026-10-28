"""Acceptance for actual generated pixels and injected detector timing."""
import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from field_latency_demo import run
from r3_follow.openpose_control import OpenPoseMarkerControl
from r3_follow.vision import MarkerTracker


class FieldLatencyDemoTests(unittest.TestCase):
    def execute(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            output = Path(directory)/'中文 路径'
            code = run(output)
            summary = json.loads((output/'summary.json').read_text(encoding='utf-8'))
            self.assertGreater((output/'steps.csv').stat().st_size, 1000)
            return code, summary

    def test_pixels_budget_recovery_hard_timeout_and_stop_flow(self):
        code, summary = self.execute()
        self.assertEqual(code, 0)
        self.assertTrue(summary['passed'])
        self.assertEqual(summary['steps'], 141)
        self.assertEqual(summary['violations'], [])
        self.assertTrue(all(summary['checks'].values()))
        self.assertFalse(summary['hardware_verified'])
        timing = summary['constructed_field_timing']
        self.assertEqual(timing['constructed_434ms_unbounded']['state_on_return'], 'frame_stale')
        self.assertEqual(timing['constructed_434ms_cooperative']['state_on_return'], 'recovering_target')
        before = timing['constructed_434ms_unbounded']
        self.assertAlmostEqual(before['faults'][0]['time']-before['last_good_capture'], 1.004)
        self.assertAlmostEqual(before['returned_at']-before['faults'][0]['time'], .024)

    def test_constant_zero_control_cannot_pass(self):
        with patch.object(OpenPoseMarkerControl, 'update', return_value=(0., 0., 0.)):
            code, summary = self.execute()
        self.assertEqual(code, 1)
        self.assertFalse(summary['passed'])
        for target in range(3):
            self.assertFalse(summary['checks'][f'ID{target}_actually_moves'])

    def test_disabling_detection_budget_cannot_pass(self):
        original = MarkerTracker.detect
        def ignore_budget(tracker, *args, **kwargs):
            configured = tracker.processing_budget_ms
            try:
                tracker.processing_budget_ms = None
                kwargs.pop('processing_budget_ms', None)
                return original(tracker, *args, **kwargs)
            finally:
                tracker.processing_budget_ms = configured
        with patch.object(MarkerTracker, 'detect', ignore_budget):
            code, summary = self.execute()
        self.assertEqual(code, 1)
        self.assertFalse(summary['passed'])
        self.assertFalse(summary['checks']['short_budget_zero'])
        self.assertFalse(summary['checks']['constructed_chain_budget_recovers'])

    def test_label_correct_but_other_position_cannot_pass(self):
        original = MarkerTracker.detect
        def corrupt(tracker, *args, **kwargs):
            observation = original(tracker, *args, **kwargs)
            return replace(observation, center_x=observation.center_x+30) if observation.found else observation
        with patch.object(MarkerTracker, 'detect', corrupt):
            code, summary = self.execute()
        self.assertEqual(code, 1)
        self.assertFalse(summary['passed'])
        self.assertTrue(any(v['reason'] == 'wrong_marker_geometry' for v in summary['violations']))


if __name__ == '__main__':
    unittest.main()
