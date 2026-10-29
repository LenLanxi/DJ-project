"""Repeated frames cannot make the host retain an unbounded media journal."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from ContainerFixture import Process
from execution.sandbox_runner import SandboxRunner, SandboxOutcome


class PayloadTests(unittest.IsolatedAsyncioTestCase):
    async def run_channel(self, value, count):
        gateway=SimpleNamespace(cancel=lambda:None, close_admission=lambda:None, dispatch=AsyncMock(return_value=value),cancelled=threading.Event(),simulated=True,
            cleanup={'status':'no_control'})
        runner=SandboxRunner()
        runner.container, runner.created = 'companion-fixture', True
        requests=[{'type':'rpc','id':i+1,'method':'get_frame','args':[],'kwargs':{}} for i in range(count)]
        requests.append({'type':'done','ok':True})
        proc=Process(requests)
        with patch.object(runner,'_reset',new=AsyncMock()),patch('execution.sandbox_runner.asyncio.create_subprocess_exec',side_effect=[proc,Process()]):
            outcome = SandboxOutcome()
            await runner.run('frame=drone.get_frame()', gateway, outcome)
            return outcome.result,proc

    async def test_worker_receives_image_but_journal_only_keeps_metadata(self):
        result,process=await self.run_channel({'frame_id':7,'timestamp':100.,'image_url':'data:image/jpeg;base64,testOnly'},1)
        self.assertTrue(result['ok'],result)
        self.assertIn(b'data:image/jpeg',process.stdin.data)
        self.assertEqual(result['journal'][0]['result']['image_url'],'[payload omitted]')
        self.assertEqual(result['journal'][0]['result']['frame_id'],7)

    async def test_one_oversized_response_is_rejected_before_pipe_write(self):
        result,process=await self.run_channel({'image_url':'x'*8_000_001},1)
        self.assertFalse(result['ok'])
        self.assertIn('response budget',result['reason'])
        self.assertLess(len(process.stdin.data),1000)

    async def test_total_response_budget_limits_many_individually_valid_frames(self):
        result,process=await self.run_channel({'image_url':'x'*6_000_000},3)
        self.assertFalse(result['ok'])
        self.assertIn('response budget',result['reason'])
        self.assertLess(len(process.stdin.data),16_000_000)
