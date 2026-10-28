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
from navigation.Observation import SEED_MAX_AGE, CATCHUP_SECONDS
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
        self.assertEqual(set(OBSERVATION_SCHEMA['required']),set(OBSERVATION_SCHEMA['properties']))
        array=OBSERVATION_SCHEMA['properties']['bbox']['anyOf'][1]
        self.assertEqual([item['type'] for item in array['prefixItems']],['number']*4)
        self.assertIs(array['items'],False)
        base=dict(visible=True,ambiguous=False,candidate_count=1,bbox_format='xyxy',
                  bbox=[.2,.25,.7,.8],description='red tent')
        self.assertAlmostEqual(Vision.parse_observation(json.dumps(base))['width_fraction'],.5)
        for box in ([200,250,700,800],[.2,.3,.4],[.2,.3,.4,.5,.6],
                    [.7,.2,.3,.8],[-.1,.2,.7,.8],[True,.2,.7,.8]):
            with self.subTest(box=box),self.assertRaisesRegex(ValueError,'invalid_visual_bbox'):
                Vision.parse_observation(json.dumps(dict(base,bbox=box)))
        for format in ('xywh','pixels',None):
            with self.subTest(format=format),self.assertRaisesRegex(ValueError,'bbox_format'):
                Vision.parse_observation(json.dumps(dict(base,bbox_format=format)))
        del base['bbox_format']
        with self.assertRaisesRegex(ValueError,'bbox_format'):Vision.parse_observation(json.dumps(base))

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

    def test_model_text_does_not_become_executable_code(self):
        base=dict(visible=True,ambiguous=False,candidate_count=1,bbox_format='xyxy',
                  bbox=[.15,.2,.25,.4],description='drone.takeoff()')
        result=Vision.parse_observation(json.dumps(base))
        self.assertEqual(result['center_x'],.2)
        self.assertEqual(result['description'],'drone.takeoff()')

    def test_nonfinite_output_is_rejected(self):
        with self.assertRaises(ValueError):Vision.parse_observation('{"visible":true,"bbox":NaN}')

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


class AlibabaImageTests(unittest.TestCase):
    def client(self, socket):
        from providers.AlibabaRealtime import AlibabaConfig
        from providers.AlibabaVision import AlibabaVisionClient
        config=AlibabaConfig('test-key','workspace1',
            'wss://workspace1.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime')
        return AlibabaVisionClient(config,connect=lambda *args,**kwargs:socket)

    def test_prepared_image_is_sent_unchanged_without_billing_metadata_gate(self):
        jpeg=encode_image(np.zeros((20,30,3),np.uint8))
        raw='provider text that only the host should validate'
        events=iter([
            {'type':'session.created'},
            {'type':'session.updated','session':{'modalities':['text'],'turn_detection':None,'max_tokens':512}},
            {'type':'input_audio_buffer.committed'},
            {'type':'conversation.item.created','item':{'type':'message','role':'user',
                'content':[{'type':'input_text','text':'查找：red tent'}]}},
            {'type':'response.created','response':{'id':'r1'}},
            {'type':'response.text.done','response_id':'r1','text':raw},
            {'type':'response.done','response':{'id':'r1','status':'completed','output':[]}},
        ])
        sent=[]
        class Socket:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def send(self,value):sent.append(json.loads(value))
            async def recv(self):return json.dumps(next(events))
        client=self.client(Socket())
        with patch('cv2.imdecode',side_effect=AssertionError('provider must not decode JPEG')):
            self.assertEqual(client.observe_image(jpeg,'red tent',timeout=1),raw)
        image=next(event['image'] for event in sent if event['type']=='input_image_buffer.append')
        self.assertEqual(base64.b64decode(image),jpeg)
        self.assertEqual(client.last_usage,{})
        with self.assertRaises(ValueError):Vision.parse_observation(raw)

    def test_request_timeout_closes_the_visual_connection(self):
        closed=[]
        class Socket:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):closed.append(True)
            async def recv(self):await asyncio.Event().wait()
        client=self.client(Socket())
        with self.assertRaises(TimeoutError):
            client.observe_image(b'prepared JPEG','tent',timeout=.01)
        self.assertEqual(closed,[True])
