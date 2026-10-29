"""Test the host supervisor with a simulated byte channel; never exec model code on the host."""
from ContainerFixture import Process
import asyncio
import json
import unittest
from unittest.mock import patch,AsyncMock
from Support import MockBackend
from Fixtures import FixtureVision
from execution.Host import Host
from execution.sandbox_runner import SandboxRunner
from ExecutionFixture import run_program, runner_for



class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def run_with(self,messages):
        b=MockBackend(); r=runner_for(b, b, vision=FixtureVision())
        proc=Process(messages)
        with patch.object(r,'check',new=AsyncMock()),patch('execution.sandbox_runner.asyncio.create_subprocess_exec',side_effect=[proc,Process()]):
            result=await run_program(r, 'arbitrary Python is passed unchanged, not parsed on host')
        return result,b,proc
    async def test_multiple_rpc_calls_and_journal(self):
        result,b,proc=await self.run_with([
            {'type':'rpc','id':1,'method':'takeoff','args':[],'kwargs':{}},
            {'type':'rpc','id':2,'method':'observe','args':['red tent'],'kwargs':{}},
            {'type':'rpc','id':3,'method':'move_forward','args':[20],'kwargs':{}},
            {'type':'rpc','id':4,'method':'stop_motion','args':[],'kwargs':{}},
            {'type':'done','ok':True}])
        self.assertTrue(result['ok'],result)
        self.assertEqual(len(result['journal']),4)
        self.assertIn(('move_forward',(20,)),b.calls)
        self.assertIsNone(b.owner)
        self.assertIn(b'arbitrary Python',proc.stdin.data)
    async def test_recovered_program_keeps_interface_error_in_journal(self):
        result,b,_=await self.run_with([
            {'type':'rpc','id':1,'method':'unknown_method','args':[],'kwargs':{}},
            {'type':'done','ok':True}])
        self.assertTrue(result['ok'], result)
        self.assertIn('error', result['journal'][0])
        self.assertEqual(b.calls, [])
    async def test_missing_final_result_is_failure(self):
        result,_,_=await self.run_with([])
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'],'sandbox_closed_without_result')
    async def test_unknown_host_call_has_no_effect(self):
        result,b,_=await self.run_with([
            {'type':'rpc','id':1,'method':'system','args':['echo unsafe'],'kwargs':{}},
            {'type':'done','ok':False,'error':'RPC rejected'}])
        self.assertFalse(result['ok']); self.assertEqual(b.calls,[])
