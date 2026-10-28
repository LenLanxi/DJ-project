"""Capture identity must survive decoder -> SDK -> cloud consumers unchanged."""
import json
import threading
import time
import unittest
from unittest.mock import patch
import numpy as np

from drone_sdk import controller, _driver as driver
from drone_sdk.contract import FrameResult
from navigation.vision import Vision


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.previous = driver._video
        self.decoder = driver._VideoDecoder('offline-only')
        driver._video = self.decoder
        self.frame = np.zeros((24,32,3),dtype=np.uint8)
        self.decoder._frame = self.frame
        self.decoder.count = 12
        self.decoder.last_frame_timestamp = time.time()
        self.decoder.last_frame_at = time.monotonic()

    def tearDown(self):
        driver._video = self.previous

    def test_decoder_identity_and_timestamp_are_not_recreated(self):
        first = controller.get_frame()
        packet = controller.get_frame()
        self.assertEqual((packet.frame_id,packet.timestamp),(12,self.decoder.last_frame_timestamp))
        self.assertEqual((first.frame_id,first.timestamp),(packet.frame_id,packet.timestamp))
        self.assertIsNot(packet.frame,self.frame)

    def test_missing_metadata_is_not_fake_fresh_evidence(self):
        self.decoder.count = 0
        self.assertTrue(controller.get_frame().stale)
        self.assertIsNone(controller.get_frame().frame)

    def test_expired_capture_is_stale_even_when_wall_clock_is_recent(self):
        self.decoder.last_frame_at -= 2
        packet=controller.get_frame()
        self.assertTrue(packet.stale)
        self.assertIsNone(packet.frame)

    def test_consumer_mutation_does_not_change_the_source_image(self):
        packet = controller.get_frame()
        packet.frame[:] = 255
        self.assertFalse(controller.get_frame().frame.any())

    def test_no_decoder_has_no_synthetic_frame(self):
        driver._video = None
        packet = controller.get_frame()
        self.assertEqual((packet.frame_id,packet.timestamp),(0,0.))
        self.assertTrue(packet.stale)
        self.assertIsNone(packet.frame)

    def test_concurrent_reads_keep_image_identity_and_timestamp_together(self):
        started, done = threading.Event(), threading.Event()
        def publish():
            for identity in range(1, 101):
                with self.decoder._lock:
                    self.decoder._frame = np.full((24,32,3), identity % 255, dtype=np.uint8)
                    self.decoder.count = identity
                    self.decoder.last_frame_timestamp = identity + .25
                    self.decoder.last_frame_at = time.monotonic()
                started.set()
                time.sleep(0)
            done.set()
        worker = threading.Thread(target=publish)
        worker.start()
        try:
            self.assertTrue(started.wait(1))
            while not done.is_set():
                packet = controller.get_frame()
                self.assertTrue((packet.frame == packet.frame_id % 255).all())
                self.assertEqual(packet.timestamp, packet.frame_id + .25)
        finally:
            worker.join(2)
        self.assertFalse(worker.is_alive())


class ReadOnlyVisionTests(unittest.TestCase):
    def test_slow_description_keeps_capture_age_and_does_not_claim_motion(self):
        class Client:
            def describe_image(self,jpeg,question):
                clock[0]=103.
                return 'A red tent is visible.'
        clock=[100.]
        vision=Vision(Client(),None)
        with patch.object(vision,'_capture',return_value=({'source':'sdk_camera','frame_id':8,'timestamp':100.,'simulated':True},b'jpeg')),patch('navigation.vision.time.time',side_effect=lambda:clock[0]):
            value=vision.describe('What is visible?')
        self.assertEqual(value['age_seconds'],3.)
        self.assertTrue(value['read_only'])
        self.assertNotIn('image_url',value)

    def test_slow_navigation_result_remains_rejected(self):
        class Client:
            def observe_image(self,jpeg,target,*,timeout):
                clock[0]=103.
                return json.dumps({'visible':True,'center_x':.5,'width_fraction':.2})
        clock=[100.]
        vision=Vision(Client(),None)
        with patch.object(vision,'frame',return_value={'source':'sdk_camera','frame_id':8,'timestamp':100.,'image_url':'data:omitted'}),patch('navigation.vision.time.time',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(RuntimeError,'expired'):
                vision.observe('red tent')

    def test_read_only_description_also_requires_fresh_input(self):
        class SDK:
            def get_frame(self):return FrameResult(None,1,time.time()-2,True)
        class Client:
            def describe_image(self,*args):raise AssertionError('stale image reached cloud')
        with self.assertRaises(RuntimeError):Vision(Client(),SDK()).describe('What is visible?')
