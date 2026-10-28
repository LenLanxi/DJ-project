import base64
import json
import time
import unittest
import threading
from unittest.mock import patch
from vision import Vision, OBSERVATION_SCHEMA
from Support import MockBackend
from gateway import Gateway

class VisionTests(unittest.IsolatedAsyncioTestCase):
    def test_navigation_uses_structured_method_and_requires_explicit_xyxy(self):
        import numpy as np
        class Frames:
            stopped=threading.Event()
            def close(self): self.stopped.set()
        class Client:
            def __init__(self): self.calls=[]
            def complete(self,messages): raise AssertionError('weaker text path must not run')
            def complete_structured(self,messages,schema):
                self.calls.append((messages,schema))
                return json.dumps({'visible':False,'ambiguous':False,'candidate_count':0,
                    'bbox_format':'xyxy','bbox':None,'description':'not visible'})
        client=Client(); visual=Vision(client,MockBackend())
        visual._frames=Frames()
        visual._stable={'frame':np.zeros((20,30,3),np.uint8),'source':'sdk_camera',
            'simulated':True,'frame_id':1,'timestamp':time.time()}
        try:
            self.assertFalse(visual.observe('red tent')['visible'])
            self.assertEqual(len(client.calls),1)
            self.assertEqual(client.calls[0][1],OBSERVATION_SCHEMA)
            self.assertIn('0到1000',client.calls[0][0][0]['content'])
            with patch.object(client,'complete_structured',side_effect=RuntimeError('HTTP 400 structured unsupported')):
                with self.assertRaisesRegex(RuntimeError,'HTTP 400'):
                    visual.observe('red tent')
        finally: visual.close()

    def test_schema_tuple_and_host_validation_do_not_guess_coordinate_units(self):
        schema=OBSERVATION_SCHEMA
        self.assertEqual(set(schema['required']),set(schema['properties']))
        self.assertEqual(schema['properties']['bbox_format']['enum'],['xyxy'])
        array=schema['properties']['bbox']['anyOf'][1]
        self.assertEqual([item['type'] for item in array['prefixItems']],['number']*4)
        self.assertIs(array['items'],False)
        base={'visible':True,'ambiguous':False,'candidate_count':1,'bbox_format':'xyxy',
              'bbox':[.2,.25,.7,.8],'description':'red tent'}
        self.assertAlmostEqual(Vision.parse_observation(json.dumps(base),require_format=True)['width_fraction'],.5)
        for box in ([200,250,700,800],[.2,.3,.4],[.2,.3,.4,.5,.6],
                    [.7,.2,.3,.8],[-.1,.2,.7,.8],[True,.2,.7,.8]):
            with self.subTest(box=box),self.assertRaisesRegex(ValueError,'invalid_visual_bbox'):
                Vision.parse_observation(json.dumps(dict(base,bbox=box)),require_format=True)
        for format in ('xywh','pixels',None):
            with self.subTest(format=format),self.assertRaisesRegex(ValueError,'bbox_format'):
                Vision.parse_observation(json.dumps(dict(base,bbox_format=format)),require_format=True)
        del base['bbox_format']
        with self.assertRaisesRegex(ValueError,'bbox_format'):
            Vision.parse_observation(json.dumps(base),require_format=True)
        # Compatibility test doubles may omit the field, but explicit wrong
        # conventions never become implicit conversions on either route.
        self.assertTrue(Vision.parse_observation(json.dumps(base))['visible'])

    def test_description_remains_plain_text_not_structured_navigation(self):
        class Client:
            def complete_structured(self,*args): raise AssertionError('read-only question is not navigation')
            def complete(self,messages): return '红色帐篷'
        visual=Vision(Client(),MockBackend())
        with patch.object(visual,'frame',return_value={'image_url':'data:image/jpeg;base64,eA==',
                'timestamp':time.time(),'frame_id':1,'source':'sdk_camera','simulated':True}):
            self.assertEqual(visual.describe('what is visible?')['answer'],'红色帐篷')

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
            def complete(self,messages): return json.dumps({'visible':True,'ambiguous':False,'candidate_count':1,'bbox':[.15,.2,.25,.4],'description':'drone.takeoff()'})
        v=Vision(Client(),MockBackend())
        result=v.parse_observation(Client().complete([]))
        self.assertEqual(result['center_x'],0.2)
        self.assertIn('drone.takeoff()',result['description'])
        self.assertFalse(v.backend.flying)
    async def test_invalid_visual_coordinates_rejected(self):
        class Client:
            def complete(self,messages): return '{"visible":true,"center_x":NaN,"width_fraction":0.1}'
        with self.assertRaises(ValueError): Vision.parse_observation(Client().complete([]))
