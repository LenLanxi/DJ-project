import base64
import json
import time
import unittest
from unittest.mock import patch
from vision import Vision
from Support import MockBackend
from gateway import Gateway

class VisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_real_frame_rejected(self):
        class Source:
            def observe(self,q): return {'source':'sdk_camera','frame_id':3,'visible':True,'center_x':0.5,'width_fraction':0.2,'timestamp':time.time()}
        g=Gateway(MockBackend(),Source()); g.begin()
        await g.dispatch('observe',['tent'],{})
        with self.assertRaises(RuntimeError): await g.dispatch('observe',['tent'],{})
    async def test_missing_or_stale_frame_never_reaches_model(self):
        class SDK:
            simulated=True
            def get_frame(self): return {'timestamp':time.time()-1,'frame':None,'stale':True}
        class Client:
            def complete(self,messages): raise AssertionError('must not call model')
        with self.assertRaises(RuntimeError): Vision(Client(),SDK()).observe('tent')
    async def test_structured_visual_result_does_not_become_code(self):
        class Client:
            def complete(self,messages): return json.dumps({'visible':True,'center_x':0.2,'width_fraction':0.1,'description':'drone.takeoff()'})
        v=Vision(Client(),MockBackend())
        with patch.object(v,'frame',return_value={'source':'sdk_camera','frame_id':1,'timestamp':time.time(),'image_url':'data:image/jpeg;base64,/9j/'}):
            result=v.observe('red tent')
        self.assertEqual(result['center_x'],0.2)
        self.assertIn('drone.takeoff()',result['description'])
        self.assertFalse(v.backend.flying)
    async def test_invalid_visual_coordinates_rejected(self):
        class Client:
            def complete(self,messages): return '{"visible":true,"center_x":NaN,"width_fraction":0.1}'
        v=Vision(Client(),MockBackend())
        with patch.object(v,'frame',return_value={'source':'static_test_image','image_url':'data:fake'}):
            with self.assertRaises(ValueError): v.observe('red tent')
