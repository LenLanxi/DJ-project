"""Saved-image calibration contract, known synthetic optics and rejection paths."""
from pathlib import Path
import tempfile
import unittest
import cv2
import numpy as np
from calibrate_follow_camera import calibrate
from r3_follow.pose_depth import PoseDepth
import json


class CameraCalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        texture = np.full((480,600),255,np.uint8)
        for j in range(6):
            for i in range(8):
                texture[(j+1)*60:(j+2)*60,(i+1)*60:(i+2)*60] = 255*((i+j)%2)
        cls.K = np.array([[500.,0,320],[0,500.,240],[0,0,1.]])
        extent = np.array([[-.06,-.06,0],[.24,-.06,0],[.24,.18,0],[-.06,.18,0]])
        cls.files = []
        for i in range(24):
            rotation = np.array([((i%4)-1.5)*.17,((i//4%3)-1)*.23,(i%3-1)*.12])
            translation = np.array([-.09+((i%4)-1.5)*.075,-.06+((i//4%3)-1)*.08,.55+(i//12)*.1])
            dest = cv2.projectPoints(extent,rotation,translation,cls.K,np.zeros(5))[0].reshape(4,2).astype(np.float32)
            H = cv2.getPerspectiveTransform(np.array([[0,0],[600,0],[600,480],[0,480]],np.float32),dest)
            frame = cv2.warpPerspective(texture,H,(640,480),borderValue=220)
            path = cls.directory/f'{i:02d}.png'
            path.write_bytes(cv2.imencode('.png',frame)[1].tobytes()); cls.files.append(path)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_calibration(self,files):
        return calibrate(files,camera_id='synthetic-test',columns=7,rows=5,square_m=.03)

    def test_known_camera_from_distinct_training_and_holdout_images(self):
        result = self.run_calibration(self.files)
        self.assertEqual((result['training_views'],result['holdout_views']),(18,6))
        K = np.array(result['camera_matrix'])
        self.assertLess(abs(K[0,0]/500-1),.03)
        self.assertLess(abs(K[1,1]/500-1),.03)
        self.assertLess(max(result['holdout_rms_px']),.25)
        self.assertFalse(result['physical_depth_accuracy_verified'])
        saved = self.directory/'calibration.json'
        saved.write_text(json.dumps(result),encoding='utf8')
        self.assertEqual(PoseDepth(saved,.24,camera_id='synthetic-test').size,(640,480))

    def test_duplicate_images_do_not_count_as_distinct_views(self):
        with self.assertRaisesRegex(ValueError,'got 1'):
            self.run_calibration([self.files[0]]*20)

    def test_mixed_resolution_fails_before_calibration(self):
        frame = cv2.imread(str(self.files[1]));frame = cv2.resize(frame,(320,240))
        small = self.directory/'small.png'; small.write_bytes(cv2.imencode('.png',frame)[1].tobytes())
        with self.assertRaisesRegex(ValueError,'same resolution'):
            self.run_calibration([self.files[0],small])

    def test_invalid_units_and_incomplete_images_rejected(self):
        for size in (0,True,float('nan'),30):
            with self.assertRaises(ValueError):
                calibrate([],camera_id='synthetic',columns=7,rows=5,square_m=size)
        with self.assertRaisesRegex(ValueError,'at least 15'):
            self.run_calibration(self.files[:5])


if __name__ == '__main__':unittest.main()
