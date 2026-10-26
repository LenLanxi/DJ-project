"""Capture identity must survive decoder -> SDK -> cloud consumers unchanged."""
import json
import time
import unittest
from unittest.mock import patch
import numpy as np

from drone_sdk.FrameBridge import FrameBridge
from drone_sdk import controller
from drone_sdk.contract import FrameResult
from vision import Vision


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.previous = controller._frame
        controller._frame = FrameResult(None,0,0.,True)
        self.frame = np.zeros((24,32,3),dtype=np.uint8)

    def tearDown(self):
        controller._frame = self.previous

    def test_decoder_identity_and_timestamp_are_not_recreated(self):
        bridge=FrameBridge()
        stamp=time.time()
        with patch('drone_sdk.FrameBridge.driver.get_frame',return_value=self.frame), patch('drone_sdk.FrameBridge.driver.capture_metadata',return_value=(12,stamp)):
            self.assertTrue(bridge.publish_once())
            self.assertTrue(bridge.publish_once())
        packet=controller.get_frame()
        self.assertEqual((packet.frame_id,packet.timestamp),(12,stamp))
        self.assertIsNot(packet.frame,self.frame)

    def test_missing_metadata_is_not_fake_fresh_evidence(self):
        for metadata in (None,False):
            with self.subTest(metadata=metadata), patch('drone_sdk.FrameBridge.driver.get_frame',return_value=self.frame), patch('drone_sdk.FrameBridge.driver.capture_metadata',return_value=metadata):
                self.assertFalse(FrameBridge().publish_once())
                self.assertTrue(controller.get_frame().stale)

    def test_expired_capture_is_stale_even_when_published_now(self):
        with patch('drone_sdk.FrameBridge.driver.get_frame',return_value=self.frame), patch('drone_sdk.FrameBridge.driver.capture_metadata',return_value=(1,time.time()-2)):
            self.assertTrue(FrameBridge().publish_once())
        packet=controller.get_frame()
        self.assertTrue(packet.stale)
        self.assertIsNone(packet.frame)

    def test_repeated_identity_cannot_refresh_timestamp(self):
        stamp=time.time()
        controller.publish_frame(FrameResult(self.frame,7,stamp,False))
        with self.assertRaisesRegex(ValueError,'timestamp_changed'):
            controller.publish_frame(FrameResult(self.frame,7,stamp+.1,False))

    def test_regression_and_malformed_metadata_rejected(self):
        controller.publish_frame(FrameResult(self.frame,7,time.time(),False))
        for identity,stamp in ((6,time.time()),(True,time.time()),(8,float('nan'))):
            with self.subTest(identity=identity),self.assertRaises(ValueError):
                controller.publish_frame(FrameResult(self.frame,identity,stamp,False))


class ReadOnlyVisionTests(unittest.TestCase):
    def test_slow_description_keeps_capture_age_and_does_not_claim_motion(self):
        class Client:
            def complete(self,messages):
                clock[0]=103.
                return 'A red tent is visible.'
        clock=[100.]
        vision=Vision(Client(),None)
        with patch.object(vision,'frame',return_value={'source':'sdk_camera','frame_id':8,'timestamp':100.,'simulated':True,'image_url':'data:omitted'}),patch('vision.time.time',side_effect=lambda:clock[0]):
            value=vision.describe('What is visible?')
        self.assertEqual(value['age_seconds'],3.)
        self.assertTrue(value['read_only'])
        self.assertNotIn('image_url',value)

    def test_slow_navigation_result_remains_rejected(self):
        class Client:
            def complete(self,messages):
                clock[0]=103.
                return json.dumps({'visible':True,'center_x':.5,'width_fraction':.2})
        clock=[100.]
        vision=Vision(Client(),None)
        with patch.object(vision,'frame',return_value={'source':'sdk_camera','frame_id':8,'timestamp':100.,'image_url':'data:omitted'}),patch('vision.time.time',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(RuntimeError,'expired'):
                vision.observe('red tent')

    def test_read_only_description_also_requires_fresh_input(self):
        class SDK:
            simulated=True
            def get_frame(self):return {'timestamp':time.time()-2,'stale':True,'frame':None}
        class Client:
            def complete(self,_):raise AssertionError('stale image reached cloud')
        with self.assertRaises(RuntimeError):Vision(Client(),SDK()).describe('What is visible?')
