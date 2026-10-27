"""Acceptance must reject inert control and falsely attributed marker positions."""
import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from multi_target_demo import run
from r3_follow.openpose_control import OpenPoseMarkerControl
from r3_follow.vision import MarkerTracker


class MultiTargetDemoAcceptanceTests(unittest.TestCase):
    def execute(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            path = Path(directory)/'中文与空格 验证'
            code = run(path)
            self.assertGreater((path/'three_markers.png').stat().st_size, 0)
            return code, json.loads((path/'summary.json').read_text(encoding='utf-8'))

    def test_real_pipeline_moves_recovers_freezes_and_stops(self):
        code, result = self.execute()
        self.assertEqual(code, 0)
        self.assertTrue(result['passed'])
        self.assertEqual(result['steps'], 195)
        self.assertEqual(result['violations'], [])
        self.assertTrue(all(result['scenario_checks'].values()))

    def test_permanently_zero_controller_cannot_pass(self):
        with patch.object(OpenPoseMarkerControl, 'update', return_value=(0., 0., 0.)):
            code, result = self.execute()
        self.assertEqual(code, 1)
        self.assertFalse(result['passed'])
        self.assertFalse(result['scenario_checks']['aircraft-A_actually_follows'])

    def test_wrong_marker_position_with_correct_label_cannot_pass(self):
        original = MarkerTracker.detect
        def corrupt(tracker, *args, **kwargs):
            observation = original(tracker, *args, **kwargs)
            if observation.found:
                observation = replace(observation, center_x=observation.center_x+30)
            return observation
        with patch.object(MarkerTracker, 'detect', corrupt):
            code, result = self.execute()
        self.assertEqual(code, 1)
        self.assertFalse(result['passed'])
        self.assertTrue(any(v['reason'] == 'observation_not_assigned_marker'
                            for v in result['violations']))

    def test_missing_vertical_channel_cannot_pass(self):
        original=OpenPoseMarkerControl.update
        def suppress_vertical(control,*args,**kwargs):
            horizontal=original(control,*args,**kwargs)
            control.vertical_request=0
            return horizontal
        with patch.object(OpenPoseMarkerControl,'update',suppress_vertical):
            code,result=self.execute()
        self.assertEqual(code,1)
        self.assertFalse(result['scenario_checks']['aircraft-A_vertical_follows_own_marker'])

    def test_reversed_vertical_direction_cannot_pass(self):
        original=OpenPoseMarkerControl.update
        def reverse_vertical(control,*args,**kwargs):
            horizontal=original(control,*args,**kwargs)
            control.vertical_request=-control.vertical_request
            return horizontal
        with patch.object(OpenPoseMarkerControl,'update',reverse_vertical):
            code,result=self.execute()
        self.assertEqual(code,1)
        self.assertTrue(any(v['reason']=='vertical_wrong_direction' for v in result['violations']))


if __name__ == '__main__':
    unittest.main()
