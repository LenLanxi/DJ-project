"""Deterministic textured-object sequences. No devices, network or cloud calls.

Passing generated scenes does not establish real tent identity or field safety.
"""
from SceneFixture import scene
import copy
import sys
from pathlib import Path
import unittest

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
from navigation.TargetTracker import TargetTracker, TargetTrackingError


class Clock:
    def __init__(self,value=100.):self.value=value
    def __call__(self):return self.value




BOX=[170/640,120/360,150/640,120/360]


def packet(image,fid=1,stamp=100.):
    return {'frame':image,'frame_id':fid,'timestamp':stamp,'source':'sdk_camera','simulated':True}


def warp(image,dx=0,dy=0,scale=1.,angle=0.):
    matrix=cv2.getRotationMatrix2D((245,180),angle,scale)
    matrix[:,2]+=np.array([dx,dy])
    return cv2.warpAffine(image,matrix,(640,360),borderValue=(170,170,170))


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock();self.image=scene()
        self.tracker=TargetTracker(clock=self.clock)

    def advance(self,image,fid,stamp):
        self.clock.value=stamp
        return self.tracker.update(packet(image,fid,stamp))

    def test01_translation_uses_real_frame_identity_and_timestamp(self):
        self.tracker.seed(packet(self.image),BOX)
        for index in range(1,5):
            result=self.advance(warp(self.image,dx=4*index,dy=2*index),index+1,100.+index*.1)
        self.assertAlmostEqual(result['center_x'],(245+16)/640,delta=.003)
        self.assertEqual(result['frame_id'],5)
        self.assertEqual(result['timestamp'],100.4)
        self.assertEqual(result['source'],'sdk_camera')
        self.assertFalse(result['target_tracking']['identity_verified'])

    def test02_scale_uses_same_seed_template_without_metric_distance_claim(self):
        self.tracker.seed(packet(self.image),BOX)
        for index in range(1,5):
            result=self.advance(warp(self.image,scale=1.+index*.025),index+1,100.+index*.1)
        self.assertAlmostEqual(result['width_fraction'],BOX[2]*1.1,delta=.006)
        self.assertNotIn('distance_m',result)

    def test03_mild_perspective_remains_geometrically_consistent(self):
        self.tracker.seed(packet(self.image),BOX)
        src=np.float32([[170,120],[320,120],[320,240],[170,240]])
        dst=src+np.float32([[3,2],[-2,1],[1,-1],[2,-2]])
        transform=cv2.getPerspectiveTransform(src,dst)
        moved=cv2.warpPerspective(self.image,transform,(640,360),borderValue=(170,170,170))
        result=self.advance(moved,2,100.1)
        self.assertAlmostEqual(result['center_x'],np.mean(dst[:,0])/640,delta=.008)

    def test04_historical_replay_catches_up_without_refreshing_old_time(self):
        self.clock.value=102.
        seed=self.tracker.seed(packet(self.image,1,100.),BOX)
        self.assertFalse(seed['fresh'])
        for index in range(1,21):
            result=self.tracker.update(packet(warp(self.image,dx=index*.5),index+1,100.+index*.1))
        final=self.tracker.current()
        self.assertTrue(final['fresh'])
        self.assertEqual(final['timestamp'],102.)
        self.assertEqual(final['target_tracking']['semantic_timestamp'],100.)
        self.assertEqual(final['target_tracking']['seed_frame_id'],1)

    def test05_old_result_cannot_be_consumed_by_default_current(self):
        self.clock.value=102.
        self.tracker.seed(packet(self.image,1,100.),BOX)
        with self.assertRaisesRegex(TargetTrackingError,'current_frame_expired'):self.tracker.current()
        with self.assertRaises(TargetTrackingError):self.tracker.update(packet(self.image,2,102.))

    def test06_duplicate_identity_never_refreshes_time(self):
        self.tracker.seed(packet(self.image),BOX)
        self.clock.value=100.2
        duplicate=self.tracker.update(packet(self.image))
        self.assertEqual(duplicate['timestamp'],100.)
        self.assertEqual(duplicate['target_tracking']['updates'],0)
        self.clock.value=101.1
        with self.assertRaises(TargetTrackingError):self.tracker.current()

    def test07_duplicate_identity_with_new_timestamp_is_rejected(self):
        self.tracker.seed(packet(self.image),BOX)
        with self.assertRaisesRegex(TargetTrackingError,'duplicate_timestamp_changed'):
            self.advance(self.image,1,100.1)

    def test08_identity_and_timestamp_reversal_and_frame_gap_stop(self):
        for fid,stamp in [(0,100.1),(2,99.99),(2,100.31)]:
            with self.subTest(fid=fid,stamp=stamp):
                self.tracker.seed(packet(self.image),BOX)
                self.clock.value=max(100.,stamp)
                with self.assertRaises(TargetTrackingError):self.tracker.update(packet(self.image,fid,stamp))

    def test09_future_expired_and_stale_packets_cannot_seed(self):
        for p in [packet(self.image,1,101.),packet(self.image,1,90.),dict(packet(self.image),stale=True)]:
            with self.subTest(timestamp=p['timestamp']):
                with self.assertRaises(TargetTrackingError):self.tracker.seed(p,BOX)

    def test10_low_texture_target_fails_instead_of_following_background(self):
        blank=np.full_like(self.image,150)
        with self.assertRaisesRegex(TargetTrackingError,'texture_insufficient'):
            self.tracker.seed(packet(blank),BOX)

    def test11_large_occlusion_invalidates_and_next_frame_does_not_resurrect(self):
        self.tracker.seed(packet(self.image),BOX)
        occluded=self.image.copy();occluded[120:240,170:260]=20
        with self.assertRaises(TargetTrackingError):self.advance(occluded,2,100.1)
        with self.assertRaises(TargetTrackingError):self.advance(self.image,3,100.2)

    def test12_target_leaving_frame_does_not_jump_to_background(self):
        self.tracker.seed(packet(self.image),BOX)
        with self.assertRaises(TargetTrackingError):self.advance(warp(self.image,dx=-230),2,100.1)

    def test13_duplicate_textured_target_is_ambiguous_at_seed(self):
        duplicate=self.image.copy();duplicate[120:240,420:570]=self.image[120:240,170:320]
        with self.assertRaisesRegex(TargetTrackingError,'appearance_ambiguous'):
            self.tracker.seed(packet(duplicate),BOX)

    def test14_duplicate_appearing_later_stops_tracking(self):
        self.tracker.seed(packet(self.image),BOX)
        duplicate=self.image.copy();duplicate[120:240,420:570]=self.image[120:240,170:320]
        with self.assertRaisesRegex(TargetTrackingError,'appearance_ambiguous'):
            self.advance(duplicate,2,100.1)

    def test15_static_scene_with_new_capture_identity_remains_trackable(self):
        self.tracker.seed(packet(self.image),BOX)
        for index in range(1,11):
            result=self.advance(self.image,index+1,100.+index*.1)
        self.assertTrue(result['fresh'])
        self.assertEqual(result['frame_id'],11)
        self.assertEqual(result['target_tracking']['updates'],10)

    def test16_semantic_lifetime_expires_even_with_fresh_source(self):
        tracker=TargetTracker(clock=self.clock,max_semantic_age=.5)
        tracker.seed(packet(self.image),BOX)
        self.clock.value=100.6
        with self.assertRaisesRegex(TargetTrackingError,'semantic_expired'):tracker.current()

    def test17_sample_budget_cannot_be_extended_by_good_frames(self):
        tracker=TargetTracker(clock=self.clock,max_updates=2)
        tracker.seed(packet(self.image),BOX)
        for index in (1,2):
            self.clock.value=100.+index*.1
            tracker.update(packet(warp(self.image,dx=index*2),index+1,self.clock.value))
        self.clock.value=100.3
        with self.assertRaisesRegex(TargetTrackingError,'sample_budget'):
            tracker.update(packet(warp(self.image,dx=6),4,100.3))

    def test18_background_drift_or_changed_texture_does_not_reseed_itself(self):
        self.tracker.seed(packet(self.image),BOX)
        different=scene();different[120:240,170:320]=255-different[120:240,170:320]
        with self.assertRaises(TargetTrackingError):self.advance(different,2,100.1)

    def test19_source_change_and_changed_duplicate_content_are_rejected(self):
        for changed in [dict(packet(self.image,2,100.1),source='another_camera'),
                        packet(warp(self.image,dx=2),1,100.)]:
            self.tracker.seed(packet(self.image),BOX)
            self.clock.value=100.1
            with self.assertRaises(TargetTrackingError):self.tracker.update(changed)

    def test20_bbox_validation_and_invalid_id_do_not_reach_opencv(self):
        for box in [[-.1,0,.2,.2],[.9,.9,.2,.2],[0,0,0,.1],[0,0,float('nan'),.2],['x',0,.1,.1]]:
            with self.assertRaises(TargetTrackingError):self.tracker.seed(packet(self.image),box)
        with self.assertRaises(TargetTrackingError):self.tracker.seed(packet(self.image,True),BOX)

    def test21_explicit_reseed_creates_new_track_identity(self):
        first=self.tracker.seed(packet(self.image),BOX)
        self.clock.value=101.1
        with self.assertRaises(TargetTrackingError):self.tracker.current()
        second=self.tracker.seed(packet(warp(self.image,dx=2),2,101.1),BOX)
        self.assertNotEqual(first['target_tracking']['track_id'],second['target_tracking']['track_id'])
        self.assertEqual(second['target_tracking']['semantic_timestamp'],101.1)


if __name__=='__main__':unittest.main()
