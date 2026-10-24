"""Contract checks added after the teacher's review; never execute generated code on host."""
import asyncio
import unittest
from unittest.mock import patch
from program_runner import Runner
from gateway import Gateway
from backends import MockBackend
from realtime import RealtimeSession

class SandboxContract(unittest.TestCase):
    def test_accepted_program_does_not_mean_executed_without_docker(self):
        r=Runner(allow_programs=True)
        with patch('sandbox_runner.shutil.which',return_value=None), patch('builtins.exec') as host_exec:
            result=r.run('drone.takeoff()\ndrone.land()')
        self.assertTrue(result['accepted'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'],'unavailable')
        self.assertFalse(r.demo.drone.flying)
        host_exec.assert_not_called()

class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_noise_and_cooldown_ignore_but_loud_speech_interrupts(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock,MagicMock
        runner=SimpleNamespace(cancel=MagicMock(),running=True,
                               gateway=SimpleNamespace(interrupt=AsyncMock(return_value={'ok':True})))
        audio=SimpleNamespace(level=lambda:0,speaker=SimpleNamespace(interrupt=MagicMock(return_value=None)))
        s=RealtimeSession(runner,audio)
        s.barge_rms_min=100; s.barge_cooldown=0
        await s.barge_in()
        runner.cancel.assert_not_called()
        audio.level=lambda:200
        await s.barge_in()
        await asyncio.gather(*list(s.tool_tasks))
        runner.cancel.assert_called_once()
        self.assertTrue(runner.gateway.interrupt.called)
        s.barge_cooldown=100
        await s.barge_in()
        self.assertEqual(runner.cancel.call_count,1)

    async def test_no_static_image_navigation_on_real_backend(self):
        b=MockBackend(); b.simulated=False; b.flying=True
        from types import SimpleNamespace
        g=Gateway(b,SimpleNamespace(observe=lambda _:dict(source='static_test_image',frame_id=1)))
        g.begin()
        await g.dispatch('observe',['tent'],{})
        with self.assertRaisesRegex(RuntimeError,'live_camera'):
            await g.dispatch('move_forward',[20],{})
        self.assertNotIn(('move_forward',(20,)),b.calls)
        await g.finish(False)
