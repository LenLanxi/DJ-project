import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import threading
import unittest
from unittest.mock import patch
from backends import MockBackend, SDKBackend
from gateway import Gateway
from sandbox_runner import SandboxRunner
from duplex_audio import PlaybackBuffer
from realtime import RealtimeSession,session_config
from vision import Vision

class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend=MockBackend(); self.g=Gateway(self.backend,fixture_vision=True); self.g.begin()
    async def test_multi_step_and_follow_resume(self):
        await self.g.dispatch('takeoff',[],{})
        await self.g.finish(True)
        self.backend.start_follow(); self.g.begin()
        for n in range(4):
            obs=await self.g.dispatch('observe',['红色帐篷'],{})
            if obs['width_fraction']>=0.35: break
            await self.g.dispatch('move_forward',[20],{})
        await self.g.finish(True)
        self.assertTrue(self.backend.following)
        self.assertEqual(self.backend.owner,'follow')
        self.assertEqual(obs['source'],'scripted_fixture')
    async def test_stop_never_resumes(self):
        self.backend.flying=True; self.backend.start_follow()
        await self.g.dispatch('stop_motion',[],{})
        await self.g.finish(True)
        self.assertFalse(self.backend.following)
        self.assertIsNone(self.backend.owner)
    async def test_unknown_host_function_rejected(self):
        for method in ('__class__','__dict__','system','read_file','request_control'):
            with self.assertRaises(ValueError): await self.g.dispatch(method,[],{})
    async def test_sdk_units_only(self):
        for args in ([True],[10],[501],['30']):
            with self.assertRaises(ValueError): await self.g.dispatch('move_forward',args,{})
        self.assertEqual(self.backend.calls,[])
    async def test_cancel_invalidates_new_motion(self):
        await self.g.interrupt()
        with self.assertRaises(RuntimeError): await self.g.dispatch('takeoff',[],{})
        await self.g.finish(False)
        self.assertIsNone(self.backend.owner)
    async def test_interrupt_does_not_wait_for_blocking_action(self):
        entered=threading.Event(); released=threading.Event()
        class Blocking(MockBackend):
            def action(self,name,*args):
                if name=='move_forward':
                    entered.set(); released.wait(3)
                if name=='stop_motion': released.set()
                return super().action(name,*args)
        b=Blocking(); b.flying=True
        g=Gateway(b); g.begin()
        motion=asyncio.create_task(g.dispatch('move_forward',[20],{}))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait,1))
            result=await asyncio.wait_for(g.interrupt(),0.5)
            self.assertTrue(result['ok'])
            await asyncio.wait_for(motion,1)
        finally:
            released.set(); await g.finish(False)
    async def test_failed_action_not_success(self):
        with self.assertRaises(RuntimeError): await self.g.dispatch('move_forward',[20],{})
        await self.g.finish(False)
        self.assertFalse(self.backend.following)
    async def test_no_fake_camera(self):
        with self.assertRaises(RuntimeError): await self.g.dispatch('get_frame',[],{})
    async def test_read_only_does_not_acquire(self):
        await self.g.dispatch('get_status',[],{})
        await self.g.finish(True)
        self.assertIsNone(self.backend.owner)
    async def test_busy_manual_owner(self):
        self.backend.owner='manual'
        with self.assertRaises(RuntimeError): await self.g.dispatch('takeoff',[],{})
        self.assertFalse(self.backend.flying)
    async def test_no_docker_no_host_execution(self):
        runner=SandboxRunner(self.g)
        with patch('sandbox_runner.shutil.which',return_value=None):
            result=await runner.run("raise RuntimeError('must not execute')")
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(self.backend.calls,[])
        self.assertFalse(runner.running)

class PlaybackTests(unittest.TestCase):
    def test_interrupt_drops_unplayed_audio(self):
        p=PlaybackBuffer(); p.append('a',0,b'\x01\x00'*4800)
        self.assertEqual(p.consume(2400),b'\x01\x00'*2400)
        cut=p.interrupt()
        self.assertEqual(cut['audio_end_ms'],100)
        self.assertEqual(p.consume(100),b'\x00'*200)
    def test_dac_latency_subtracted(self):
        p=PlaybackBuffer(); p.latency_ms=30
        p.append('a',0,b'\x00\x00'*2400); p.consume(2400)
        self.assertEqual(p.interrupt()['audio_end_ms'],70)
    def test_unplayed_item_truncates_to_zero(self):
        p=PlaybackBuffer(); p.append('a',0,b'\x00\x00'*100)
        self.assertEqual(p.interrupt()['audio_end_ms'],0)
    def test_overflow_is_explicit(self):
        p=PlaybackBuffer(rate=1,max_seconds=1)
        with self.assertRaises(RuntimeError): p.append('a',0,b'\x00'*4)
    def test_pcm_alignment(self):
        with self.assertRaises(ValueError): PlaybackBuffer().append('a',0,b'x')

class EventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runner=SandboxRunner(Gateway(MockBackend()))
        class Audio:
            speaker=PlaybackBuffer()
        self.audio=Audio(); self.events=[]
        self.session=RealtimeSession(self.runner,self.audio,self.events.append)
        class Socket:
            def __init__(self): self.sent=[]
            async def send(self,m): self.sent.append(json.loads(m))
        self.ws=Socket(); self.session.ws=self.ws
    async def test_barge_in_discards_late_audio(self):
        await self.session.handle({'type':'response.created','response':{'id':'r1'}})
        event={'type':'response.output_audio.delta','response_id':'r1','item_id':'a','delta':base64.b64encode(b'\x01\x00'*100).decode()}
        await self.session.handle(event)
        await self.session.handle({'type':'input_audio_buffer.speech_started'})
        await self.session.handle(event)
        self.assertEqual(self.audio.speaker.size,0)
        self.assertEqual(self.ws.sent[0]['type'],'conversation.item.truncate')
    async def test_tool_result_returns_actual_failure(self):
        with patch('sandbox_runner.shutil.which',return_value=None):
            await self.session.call_tool({'name':'run_program','call_id':'c1','arguments':'{"code":"drone.takeoff()"}'},0)
        result=json.loads(self.ws.sent[0]['item']['output'])
        self.assertFalse(result['ok']); self.assertEqual(result['status'],'unavailable')
        self.assertFalse(self.runner.gateway.backend.flying)
    async def test_stale_tool_never_starts(self):
        self.session.epoch=2
        await self.session.call_tool({'name':'run_program','call_id':'c1','arguments':'{"code":"drone.takeoff()"}'},1)
        result=json.loads(self.ws.sent[0]['item']['output'])
        self.assertEqual(result['reason'],'stale_model_turn')
    async def test_tool_deduplicated(self):
        await self.session.handle({'type':'response.created','response':{'id':'r'}})
        e={'type':'response.output_item.done','response_id':'r','item':{'type':'function_call','name':'interrupt','call_id':'same','arguments':'{"land":false}'}}
        await self.session.handle(e); await self.session.handle(e)
        await asyncio.gather(*list(self.session.tool_tasks))
        self.assertEqual(sum(1 for x in self.ws.sent if x['type']=='conversation.item.create'),1)
    async def test_older_response_done_does_not_clear_current(self):
        await self.session.handle({'type':'response.created','response':{'id':'old'}})
        await self.session.handle({'type':'response.created','response':{'id':'new'}})
        await self.session.handle({'type':'response.done','response':{'id':'old'}})
        self.assertTrue(self.session.response_active)
    async def test_pending_response_waits_for_done(self):
        await self.session.handle({'type':'response.created','response':{'id':'r'}})
        await self.session.request_response()
        self.assertEqual(self.ws.sent,[])
        await self.session.handle({'type':'response.done','response':{'id':'r'}})
        self.assertEqual(self.ws.sent[-1]['type'],'response.create')
    async def test_input_stream_does_not_wait_for_model_response(self):
        class Audio:
            speaker=PlaybackBuffer()
            async def chunks(self):
                yield b'\x00\x00'*480
                yield b'\x01\x00'*480
        self.session.audio=Audio(); self.session.ready.set(); self.session.response_active=True
        await self.session.stream_input()
        self.assertEqual(len(self.ws.sent),2)
        self.assertTrue(all(e['type']=='input_audio_buffer.append' for e in self.ws.sent))

class ConfigTests(unittest.TestCase):
    def test_container_boundary(self):
        c=SandboxRunner.command('image','name')
        for flag in ('--network=none','--read-only','--cap-drop=ALL','--pull=never','--memory=128m','--pids-limit=64'):
            self.assertIn(flag,c)
        self.assertNotIn('-v',c); self.assertNotIn('--privileged',c)
        self.assertNotIn('-e',c)
    def test_realtime_has_vad_no_transcription_chain(self):
        s=session_config('configured-model','marin')['session']
        self.assertEqual(s['output_modalities'],['audio'])
        self.assertTrue(s['audio']['input']['turn_detection']['interrupt_response'])
        self.assertNotIn('transcription',s['audio']['input'])

@unittest.skipUnless(os.getenv('RUN_DOCKER_TESTS')=='1' and shutil.which('docker'),'requires explicit RUN_DOCKER_TESTS=1 and built Docker image')
class ContainerIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_multi_step_full_python(self):
        b=MockBackend(); g=Gateway(b,fixture_vision=True); r=SandboxRunner(g,timeout=15)
        code=(Path(__file__).resolve().parents[1]/'examples/red_tent.py').read_text()
        result=await r.run(code)
        self.assertTrue(result['ok'],result)
        self.assertGreaterEqual(len(result['journal']),6)
        self.assertIn('达到演示',result['debug'])
    async def test_host_secrets_and_network_unavailable(self):
        code="""import os,socket
assert os.getenv('OPENAI_API_KEY') is None
assert not os.path.exists('/var/run/docker.sock')
assert not os.path.exists('/workspace')
s=socket.socket()
s.settimeout(0.5)
try:
 s.connect(('1.1.1.1',443))
except OSError:
 pass
else:
 raise RuntimeError('unexpected network access')
"""
        result=await SandboxRunner(Gateway(MockBackend()),timeout=15).run(code)
        self.assertTrue(result['ok'],result)
    async def test_infinite_loop_timeout(self):
        result=await SandboxRunner(Gateway(MockBackend()),timeout=2).run('while True: pass')
        self.assertEqual(result['status'],'timeout')

if __name__=='__main__': unittest.main()
