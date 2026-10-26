"""Behavioral tests migrated from the retired docs/v4-reference runtime."""
import asyncio
import base64
import json
import os
import shutil
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock
from Support import MockBackend
from Fixtures import FixtureVision
from backends import SDKBackend
from gateway import Gateway
from sandbox_runner import SandboxRunner
from DuplexAudio import PlaybackBuffer
from vision import Vision
class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend=MockBackend(); self.g=Gateway(self.backend,vision=FixtureVision()); self.g.begin()
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
        self.assertEqual(self.backend.owner,'model')  # priority stop retains a NEW grant
        self.assertFalse(self.backend.flying)
    async def test_interrupt_does_not_wait_for_blocking_action(self):
        entered=threading.Event(); released=threading.Event()
        class Blocking(MockBackend):
            def priority_stop(self,land=False):
                released.set()
                return super().priority_stop(land)
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
            
            with self.assertRaises(RuntimeError): await asyncio.wait_for(motion,1)
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


class ConfigTests(unittest.TestCase):
    def test_container_boundary(self):
        c=SandboxRunner.command('image','name')
        for flag in ('--network=none','--read-only','--cap-drop=ALL','--pull=never','--memory=128m','--pids-limit=64'):
            self.assertIn(flag,c)
        self.assertNotIn('-v',c); self.assertNotIn('--privileged',c)
        self.assertNotIn('-e',c)


@unittest.skipUnless(os.getenv('RUN_DOCKER_TESTS')=='1' and shutil.which('docker'),'requires explicit RUN_DOCKER_TESTS=1 and built Docker image')
class ContainerIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_multi_step_full_python(self):
        b=MockBackend(); g=Gateway(b,vision=FixtureVision()); r=SandboxRunner(g,timeout=15)
        code=(Path(__file__).resolve().parent/'ContainerScenario.py').read_text(encoding='utf-8')
        result=await r.run(code)
        self.assertTrue(result['ok'],result)
        self.assertGreaterEqual(len(result['journal']),6)
        self.assertIn('达到演示',result['debug'])
    async def test_host_secrets_and_network_unavailable(self):
        code="""import os,socket
assert os.getenv('AI_API_KEY') is None
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

