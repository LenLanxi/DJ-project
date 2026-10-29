import asyncio
import base64
import json
import threading
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from navigation.vision import Vision, OBSERVATION_SCHEMA, encode_image
from navigation.Observation import Observation, SEED_MAX_AGE, CATCHUP_SECONDS
from drone_sdk.contract import FrameResult


def observation():
    return dict(visible=False, ambiguous=False, candidate_count=0,
                bbox_format='xyxy', bbox=None, description='not visible')


class VisionTests(unittest.TestCase):
    def prepared(self, client):
        class Frames:
            def __init__(self): self.stopped=threading.Event()
            def close(self): self.stopped.set()
        visual=Vision(client,None)
        visual._frames=Frames()
        visual._stable={'frame':np.zeros((20,30,3),np.uint8),'source':'sdk_camera',
                        'simulated':True,'frame_id':1,'timestamp':time.time()}
        self.addCleanup(visual.close)
        return visual

    def test_navigation_uses_only_the_explicit_image_observation_contract(self):
        calls=[]
        class Client:
            def observe_image(self,jpeg,target,*,timeout):
                calls.append((jpeg,target,timeout))
                return json.dumps(observation())
        visual=self.prepared(Client())
        with patch('navigation.vision.encode_image',wraps=encode_image) as encode:
            self.assertFalse(visual.observe('red tent')['visible'])
        self.assertEqual(encode.call_count,1)
        self.assertTrue(calls[0][0].startswith(b'\xff\xd8'))
        self.assertEqual(calls[0][1],'red tent')
        self.assertTrue(0<calls[0][2]<=SEED_MAX_AGE-CATCHUP_SECONDS)
        with patch.object(visual.client,'observe_image',side_effect=RuntimeError('provider unsupported')) as request:
            with self.assertRaisesRegex(RuntimeError,'provider unsupported'):visual.observe('red tent')
            request.assert_called_once()

    def test_schema_and_host_validation_do_not_guess_coordinate_units(self):
        base=dict(visible=True,ambiguous=False,candidate_count=1,bbox_format='xyxy',
                  bbox=[.2,.25,.7,.8],description='red tent')
        self.assertAlmostEqual(Observation.parse(json.dumps(base)).to_dict()['width_fraction'],.5)
        for box in ([200,250,700,800], [.7,.2,.3,.8], [True,.2,.7,.8]):
            with self.subTest(box=box),self.assertRaisesRegex(ValueError,'invalid_visual_bbox'):
                Observation.parse(json.dumps(dict(base,bbox=box)))
        with self.assertRaisesRegex(ValueError, 'bbox_format'):
            Observation.parse(json.dumps(dict(base, bbox_format='xywh')))
        with self.assertRaises(ValueError):
            Observation.parse('{"visible":true,"bbox":NaN}')

    def test_invisible_result_discards_unused_coordinates(self):
        raw = dict(observation(), bbox=[.1,.2,.3,.4], center_x=.2, candidate_count=1)
        result = Observation.parse(json.dumps(raw)).to_dict()
        self.assertFalse(result['visible'])
        self.assertIsNone(result['bbox'])
        self.assertIsNone(result['center_x'])
        self.assertEqual(result['candidate_count'], 0)

    def test_first_cached_refreshed_and_negative_observations_share_one_contract(self):
        from SceneFixture import scene
        calls=[]
        class Client:
            def observe_image(self, jpeg, target, *, timeout):
                calls.append(target)
                return json.dumps(dict(visible=target=='tent', ambiguous=False,
                    candidate_count=1 if target=='tent' else 0, bbox_format='xyxy',
                    bbox=[170/640,120/360,320/640,240/360] if target=='tent' else None,
                    description='visible evidence'))
        visual=self.prepared(Client())
        visual._stable['frame']=scene()
        visual._frames.snapshot=lambda:[visual._stable]
        first=visual.observe('tent')
        cached=visual.observe('tent')
        refreshed=visual.refresh_observation(first)
        negative=visual.observe('missing')
        for result in (cached,refreshed,negative):
            self.assertEqual(set(first),set(result))
            self.assertEqual(result['bbox_format'],'xyxy')
            self.assertEqual(result['description'],'visible evidence')
        self.assertEqual(calls,['tent','missing'])
        self.assertEqual(first['bbox'],cached['bbox'])
        self.assertAlmostEqual(first['width_fraction'],150/640,places=5)
        self.assertAlmostEqual(first['center_x'],245/640,places=5)
        self.assertFalse(negative['visible'])
        self.assertIsNone(negative['bbox'])
        self.assertIsNone(negative['center_x'])

    def test_description_is_read_only_and_uses_prepared_jpeg(self):
        class Client:
            def describe_image(self,jpeg,question):
                self.input=(jpeg,question)
                return '红色帐篷'
        client=Client(); visual=Vision(client,None)
        metadata={'timestamp':time.time(),'frame_id':1,'source':'sdk_camera','simulated':True}
        with patch.object(visual,'_capture',return_value=(metadata,b'prepared JPEG')):
            result=visual.describe('what is visible?')
        self.assertEqual(result['answer'],'红色帐篷')
        self.assertTrue(result['read_only'])
        self.assertEqual(client.input,(b'prepared JPEG','what is visible?'))
        self.assertNotIn('image_url',result)

    def test_missing_or_stale_frame_never_reaches_model(self):
        class SDK:
            def get_status(self):return SimpleNamespace(simulated=True)
            def get_frame(self):return FrameResult(None,1,time.time()-2,True)
        class Client:
            def describe_image(self,*args):raise AssertionError('must not call model')
        with self.assertRaisesRegex(RuntimeError,'取消本次观察'):Vision(Client(),SDK()).describe('tent')

    def test_static_png_remains_available_to_read_only_description(self):
        import cv2
        class Client:
            def describe_image(self,jpeg,question):
                self.jpeg=jpeg
                return 'static picture'
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'Image.png'
            path.write_bytes(cv2.imencode('.png',np.zeros((20,30,3),np.uint8))[1].tobytes())
            client=Client();visual=Vision(client,None,str(path))
            result=visual.describe('what is visible?')
        self.assertEqual(result['source'],'static_test_image')
        self.assertTrue(result['read_only'])
        self.assertTrue(client.jpeg.startswith(b'\xff\xd8'))

    def test_capture_preserves_each_provider_resolution_with_one_jpeg_encode(self):
        import cv2
        class Device:
            def get_frame(self):return FrameResult(np.zeros((720,960,3),np.uint8),1,time.time(),False)
            def get_status(self):return SimpleNamespace(simulated=True)
        for bounds,shape in (((640,480),(480,640)),(None,(720,960))):
            with self.subTest(bounds=bounds):
                visual=Vision(SimpleNamespace(image_bounds=bounds),Device())
                with patch('cv2.imencode',wraps=cv2.imencode) as encode:
                    _,jpeg=visual._capture()
                self.assertEqual(encode.call_count,1)
                self.assertEqual(cv2.imdecode(np.frombuffer(jpeg,np.uint8),cv2.IMREAD_COLOR).shape[:2],shape)

    def test_semantic_request_deadline_rejects_a_late_provider_result(self):
        class Client:
            def observe_image(self,*args,**kwargs):
                clock[0]+=SEED_MAX_AGE
                return json.dumps(observation())
        clock=[100.]
        visual=self.prepared(Client());visual._stable['timestamp']=100.
        with patch('navigation.vision.time.time',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(RuntimeError,'semantic_request_expired'):visual.observe('tent')


class DoubaoImageTests(unittest.TestCase):
    def client(self, *, content='visible evidence', error=None, audit=None):
        from providers.DoubaoVision import DoubaoVisionClient
        calls, closed = [], []
        class SDK:
            def __init__(self, **kwargs):
                calls.append(('client', kwargs))
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            def __enter__(self): return self
            def __exit__(self, *args): closed.append(True)
            def create(self, **kwargs):
                calls.append(('request', kwargs))
                if error: raise error
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                                       usage=None)
        config = SimpleNamespace(vision_key='test-secret', vision_model='test-model')
        client = DoubaoVisionClient(config, on_usage=audit, client_factory=SDK)
        return client, calls, closed

    def test_observation_uses_prepared_jpeg_schema_and_no_retry(self):
        jpeg = encode_image(np.zeros((20,30,3),np.uint8))
        raw = 'provider text that only the host should validate'
        client, calls, closed = self.client(content=raw)
        with patch('cv2.imdecode', side_effect=AssertionError('must not decode JPEG')):
            self.assertEqual(client.observe_image(jpeg, 'red tent', timeout=1), raw)
        self.assertEqual(calls[0][1], dict(api_key='test-secret', timeout=1, max_retries=0))
        body = calls[1][1]
        image = body['messages'][1]['content'][1]['image_url']['url']
        self.assertEqual(base64.b64decode(image.split(',',1)[1]), jpeg)
        schema = body['response_format']['json_schema']['schema']
        self.assertEqual(schema['required'], OBSERVATION_SCHEMA['required'])
        self.assertEqual(schema['properties']['bbox']['anyOf'][1],
                         {'type':'array', 'items':{'type':'number'}})
        self.assertEqual(body['thinking'], {'type':'disabled'})
        self.assertNotIn('max_tokens', body)
        self.assertEqual(closed, [True])
        self.assertEqual(client.last_usage, {})
        with self.assertRaises(ValueError): Observation.parse(raw)

    def test_failure_closes_sdk_and_records_single_failed_request(self):
        audit = []
        client, calls, closed = self.client(error=TimeoutError('deadline'), audit=audit.append)
        with self.assertRaises(TimeoutError): client.describe_image(b'prepared JPEG','tent')
        self.assertEqual(len(calls), 2)
        self.assertEqual(closed, [True])
        self.assertEqual([row['event'] for row in audit], ['model_request_start','model_request_end'])
        self.assertEqual(audit[-1]['status'], 'failed')
        self.assertEqual(audit[-1]['error_type'], 'TimeoutError')
        self.assertNotIn('test-secret', json.dumps(audit))

    def test_text_plan_preserves_messages_and_requests_json(self):
        raw = json.dumps({'code':'print(drone.get_status())', 'explanation':'query'})
        client, calls, _ = self.client(content=raw)
        messages = [{'role':'system','content':'generate Python JSON'},
                    {'role':'user','content':'status'}]
        self.assertEqual(client.complete(messages), raw)
        body = calls[1][1]
        self.assertEqual(body['messages'], messages)
        self.assertEqual(body['response_format'], {'type':'json_object'})
