import io
import json
import threading
import unittest
from unittest.mock import patch
from audio_io import wav_bytes, decode_wav
from voice_service import VoiceService, multipart
from pipeline import VoicePipeline
from ai_agent import Agent
from demo_ai import ScriptedClient
from session import Session
from vision_service import validate_frame
from model_client import ModelClient
from http_transport import post

class IndependentTests(unittest.TestCase):
    def test_visual_intent_is_read_only(self):
        class Client:
            def complete(self,m): return '{"kind":"vision","question":"有什么"}'
        seen=[]
        a=Agent(Client(),vision=lambda q:seen.append(q) or '一只杯子')
        result=a.handle('看看面前')
        self.assertIn('一只杯子',result['reply'])
        self.assertEqual(seen,['有什么'])
        self.assertFalse(a.runner.demo.drone.flying)
        self.assertFalse(result['execution']['accepted'])

    def test_wav_roundtrip(self):
        pcm = b'\x00\x00' * 160
        self.assertEqual(decode_wav(wav_bytes(pcm)), (pcm, 16000, 1))

    def test_multipart(self):
        body, mime = multipart({'model':'test'}, b'RIFFtest')
        self.assertIn(b'name="model"\r\n\r\ntest', body)
        self.assertIn(b'filename="input.wav"', body)
        self.assertIn(b'RIFFtest', body)
        self.assertTrue(body.endswith(('--' + mime.split('boundary=')[1] + '--\r\n').encode()))

    def test_voice_transcription_and_tts_payload(self):
        with patch.dict('os.environ', {'AI_STT_MODEL':'stt','AI_TTS_MODEL':'tts','AI_TTS_VOICE':'voice'}):
            v = VoiceService()
        wav = wav_bytes(b'\x00\x00' * 160)
        with patch('voice_service.post', return_value=b'{"text":"hello"}'):
            self.assertEqual(v.transcribe(wav), 'hello')
        with patch('voice_service.post', return_value=wav) as request:
            self.assertEqual(v.synthesize('hi'), wav)
            payload = json.loads(request.call_args.args[2])
            self.assertEqual(payload['response_format'], 'wav')
        with patch('voice_service.post', return_value=b'{"text":""}'):
            with self.assertRaises(ValueError): v.transcribe(wav)

    def test_voice_pipeline(self):
        a = Agent(ScriptedClient())
        class Voice:
            def transcribe(self, wav): return '起飞'
            def synthesize(self, text): return b'sound'
        played=[]
        p = VoicePipeline(a, Voice(), lambda:b'input', played.append)
        self.assertTrue(p.turn(lambda t:False)['cancelled'])
        self.assertFalse(a.runner.demo.drone.flying)
        result = p.turn(lambda t:True)
        self.assertTrue(a.runner.demo.drone.flying)
        self.assertEqual(played, [b'sound'])
        self.assertIn('timing_ms', result)

    def test_speech_failure_does_not_repeat_action(self):
        a=Agent(ScriptedClient())
        class Voice:
            def transcribe(self, wav): return '起飞'
            def synthesize(self, text): raise RuntimeError('tts failed')
        p=VoicePipeline(a, Voice(), lambda:b'input', lambda x:None)
        result=p.turn(lambda t:True)
        self.assertIn('speech_error',result)
        self.assertIn('模拟起飞完成',result['reply'])
        self.assertTrue(a.runner.demo.drone.flying)

    def test_history_contains_true_results(self):
        a=Agent(ScriptedClient())
        a.handle('起飞')
        self.assertIn('模拟起飞完成', a.history[-1]['content'])
        for _ in range(10): a.handle('还有多少电')
        self.assertEqual(len(a.history),12)

    def test_cancel_late_model_program(self):
        entered=threading.Event(); release=threading.Event()
        class Slow:
            def complete(self, messages):
                entered.set(); release.wait(3)
                return '{"kind":"program","code":"drone.takeoff()"}'
        a=Agent(Slow()); s=Session(a)
        try:
            future=s.submit('起飞')
            self.assertTrue(entered.wait(1))
            with self.assertRaises(RuntimeError): s.submit('起飞')
            interrupt=s.interrupt(land=True)
            self.assertTrue(interrupt.result(1)['accepted'])
            release.set()
            self.assertTrue(future.result(1)['cancelled'])
            self.assertFalse(a.runner.demo.drone.flying)
            self.assertEqual(a.history,[])
        finally:
            release.set(); s.close()

    def test_frame_threshold_and_duplicate(self):
        snap={'frame':object(),'timestamp':100.0,'frame_id':7,'stale':False}
        self.assertEqual(validate_frame(snap, now=100.5),7)
        for kwargs in ({'now':100.51},{'now':99.9},{'now':100.1,'previous_id':7}):
            with self.assertRaises(ValueError): validate_frame(snap,**kwargs)
        snap['stale']=True
        with self.assertRaises(ValueError): validate_frame(snap,now=100.1)

    def test_transport_rejects_plain_http_and_embedded_key(self):
        for url in ('http://example.com', 'https://u:p@example.com', 'https://example.com/#frag'):
            with self.assertRaises(ValueError): post(url,'key',b'')

    def test_model_parser_and_usage(self):
        with patch.dict('os.environ',{'AI_CHAT_URL':'https://example.com/chat','AI_API_KEY':'fake','AI_MODEL':'test'}):
            client=ModelClient()
        with patch('model_client.post_json',return_value={'choices':[{'message':{'content':'answer'}}],'usage':{'total_tokens':10}}):
            self.assertEqual(client.complete([]),'answer')
            self.assertEqual(client.last_usage['total_tokens'],10)
        with patch('model_client.post_json',return_value={'choices':[]}):
            with self.assertRaises(RuntimeError): client.complete([])

if __name__=='__main__': unittest.main()
