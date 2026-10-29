"""Actual textured-image tracking and its independent failure boundaries."""
import unittest
import cv2
import numpy as np
from SceneFixture import scene
from navigation.TargetTracker import TargetTracker, TargetTrackingError

BOX=[170/640,120/360,320/640,240/360]

def packet(image,fid=1,stamp=100.):
    return dict(frame=image,frame_id=fid,timestamp=stamp,source='sdk_camera',simulated=True)

def warp(image,dx=0,dy=0,scale=1.):
    matrix=cv2.getRotationMatrix2D((245,180),0,scale)
    matrix[:,2]+=np.array([dx,dy])
    return cv2.warpAffine(image,matrix,(640,360),borderValue=(170,170,170))

class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.now=100.
        self.image=scene()
        self.tracker=TargetTracker(clock=lambda:self.now)

    def test_motion_and_scale_use_xyxy_and_real_capture_identity(self):
        self.tracker.seed(packet(self.image),BOX)
        for step in range(1,5):
            self.now=100.+step*.1
            result=self.tracker.update(packet(warp(self.image,dx=4*step,scale=1+step*.025),step+1,self.now))
        left,top,right,bottom=result['bbox']
        self.assertAlmostEqual((left+right)/2,(245+16)/640,delta=.004)
        self.assertAlmostEqual(right-left,150/640*1.1,delta=.006)
        self.assertEqual((result['frame_id'],result['timestamp']),(5,100.4))
        self.assertEqual(result['target_tracking']['semantic_timestamp'],100.)

    def test_historical_replay_preserves_semantic_time_and_freshness(self):
        self.now=102.
        self.assertFalse(self.tracker.seed(packet(self.image),BOX)['fresh'])
        with self.assertRaisesRegex(TargetTrackingError,'current_frame_expired'):
            self.tracker.current()
        self.tracker.seed(packet(self.image),BOX)
        for step in range(1,21):
            self.tracker.update(packet(warp(self.image,dx=step*.5),step+1,100.+step*.1))
        result=self.tracker.current()
        self.assertTrue(result['fresh'])
        self.assertEqual(result['timestamp'],102.)
        self.assertEqual(result['target_tracking']['semantic_timestamp'],100.)
        self.now=106.01
        with self.assertRaisesRegex(TargetTrackingError,'semantic_expired'):
            self.tracker.current()

    def test_bad_timing_cannot_seed_or_advance(self):
        for stamp in (101.,90.):
            with self.subTest(seed_time=stamp),self.assertRaises(TargetTrackingError):
                self.tracker.seed(packet(self.image,1,stamp),BOX)
        for stamp in (99.99,100.31):
            self.tracker.seed(packet(self.image),BOX)
            self.now=max(100.,stamp)
            with self.subTest(next_time=stamp),self.assertRaises(TargetTrackingError):
                self.tracker.update(packet(self.image,2,stamp))

    def test_low_texture_and_duplicate_candidates_reject_seed(self):
        duplicate=self.image.copy()
        duplicate[120:240,420:570]=self.image[120:240,170:320]
        for image in (np.full_like(self.image,150),duplicate):
            with self.assertRaises(TargetTrackingError):self.tracker.seed(packet(image),BOX)

    def test_occlusion_changed_texture_departure_and_new_duplicate_invalidate(self):
        occluded=self.image.copy();occluded[120:240,170:260]=20
        changed=self.image.copy();changed[120:240,170:320]=255-changed[120:240,170:320]
        duplicate=self.image.copy();duplicate[120:240,420:570]=self.image[120:240,170:320]
        for image in (occluded,changed,warp(self.image,dx=-230),duplicate):
            self.now=100.
            self.tracker.seed(packet(self.image),BOX)
            self.now=100.1
            with self.assertRaises(TargetTrackingError):self.tracker.update(packet(image,2,self.now))
            with self.assertRaises(TargetTrackingError):self.tracker.update(packet(self.image,3,100.2))

    def test_static_scene_with_new_capture_remains_trackable(self):
        self.tracker.seed(packet(self.image),BOX)
        for step in range(1,11):
            self.now=100.+step*.1
            result=self.tracker.update(packet(self.image,step+1,self.now))
        self.assertTrue(result['fresh'])
        self.assertEqual(result['frame_id'],11)
