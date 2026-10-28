"""Camera calibration and metric-pose rejection boundaries, without devices."""
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest

import cv2
import numpy as np

from FollowFixtures import calibration, projected
from calibrate_follow_camera import view_conditioning
from follow.r3_follow.pose_depth import PoseDepth


class PoseAndCalibrationStressTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'camera.json';self.data=calibration(self.path)
        self.pose=PoseDepth(self.path,.24,camera_id='synthetic-camera')

    def test_many_front_parallel_views_do_not_prove_focal_length(self):
        board=np.zeros((35,3),np.float32);board[:,:2]=np.mgrid[0:7,0:5].T.reshape(-1,2)*.03
        samples=[(board[:,:2]*(1000+i*5)+[50+(i%5)*40,40+(i//5)*50]).reshape(-1,1,2) for i in range(20)]
        with self.assertRaisesRegex(ValueError,'tilted views'):view_conditioning(board,samples,(640,480))

    def test_correlated_corner_translation_does_not_change_depth_on_side_turn(self):
        for angle in (0,30,45,60):
            obs=projected(0,0,2.,angle)
            for shift in ((2,1),(-2,-1)):
                corners=np.array(obs.corners)+shift
                result=self.pose.estimate(replace(obs,corners=tuple(map(tuple,corners.tolist()))))
                self.assertTrue(result.valid,result.reason)
                self.assertLess(abs(result.depth_m-2.),.03)

    def test_near_edge_extreme_tilt_and_bad_corner_cannot_fall_back_to_size(self):
        obs=projected(0,0,2.,45)
        warped=list(obs.corners);warped[1]=(warped[1][0]+30,warped[1][1]-25)
        for bad in (replace(obs,corners=tuple(warped)),projected(0,0,2.,80),projected(0,0,2.,0,x=1.25)):
            result=self.pose.estimate(bad);self.assertFalse(result.valid,result)

    def test_wrong_measured_marker_size_bias_is_documented_not_hidden_by_low_fit_error(self):
        wrong=PoseDepth(self.path,.30,camera_id='synthetic-camera')
        result=wrong.estimate(projected(0,0,2.,45))
        self.assertTrue(result.valid)
        self.assertAlmostEqual(result.depth_m,2.5,places=5)
        self.assertLess(result.reprojection_px,.001)
        # Establish why the field ruler check is mandatory: reprojection alone
        # cannot detect a wrong physical side length. No guessed correction.

    def test_calibration_with_failed_view_quality_is_not_loaded(self):
        for value in (.0001, True, float('nan'), None, [.1]):
            self.data['view_conditioning']={'intrinsic_constraint_rank5_ratio':value}
            self.path.write_text(json.dumps(self.data),encoding='utf8')
            with self.assertRaisesRegex(ValueError,'conditioning'):
                PoseDepth(self.path,.24,camera_id='synthetic-camera')

    def test_corner_noise_reports_acceptance_and_error_not_a_perfect_detection_claim(self):
        rng=np.random.default_rng(211);errors=[];rejections=0
        for i in range(80):
            obs=projected(i,i*.1,2.,45)
            pixels=np.array(obs.corners)+rng.normal(0,1.5,(4,2))
            est=self.pose.estimate(replace(obs,corners=tuple(map(tuple,pixels.tolist()))))
            if est.valid:errors.append(abs(est.depth_m-2.))
            else:rejections+=1
        self.assertGreater(rejections,0);self.assertGreater(len(errors),0)
        self.assertLess(max(errors),.3)
