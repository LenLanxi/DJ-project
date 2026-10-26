"""Cancelled host workers must never re-enter a later SDK/follow lifecycle.

Actual SDK and follow service, synthetic marker frames, no aircraft/network.
"""
import asyncio
import threading
import json
import unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock

from RuntimeFixture import reset,SyntheticFeed
from SafetyTests import NoNetwork
from backends import SDKBackend,OPERATION
from gateway import Gateway
from drone_sdk import controller as ctl,_driver as drv

ROOT=Path(__file__).resolve().parents[1]

class OperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        NoNetwork.setUp(self)
        reset(flying=True)
        ctl.set_frame_provider(SyntheticFeed())
        self.releases=[]
    async def asyncTearDown(self):
        for event in self.releases:event.set()
        await asyncio.sleep(.03)
        ctl.stop_follow();reset()
    def backend(self):return SDKBackend(ROOT,allow_simulated=True)
    def gateway(self,backend):
        gateway=Gateway(backend);gateway.begin();return gateway
    async def cancel(self,gateway,task):
        result=await gateway.interrupt()
        self.assertTrue(result['ok'])
        task.cancel();await asyncio.gather(task,return_exceptions=True)
        await gateway.finish(False)
    def delay(self,base,name):
        entered,release,returned=threading.Event(),threading.Event(),threading.Event()
        self.releases.append(release)
        class Delayed(base):
            delayed=False
        original=getattr(base,name)
        def call(instance,*args):
            if OPERATION.get() is not None and not instance.delayed:
                instance.delayed=True
                entered.set();release.wait(3)
                try:return original(instance,*args)
                finally:returned.set()
            return original(instance,*args)
        setattr(Delayed,name,call)
        return Delayed(ROOT,allow_simulated=True),entered,release,returned
    async def new_move(self,backend):
        gateway=self.gateway(backend)
        await gateway.dispatch('move_forward',[30],{})
        await gateway.finish(True)
    async def new_follow(self,backend):
        gateway=self.gateway(backend)
        await gateway.dispatch('start_follow',[],{})
        await gateway.finish(True)
        self.assertTrue(ctl.is_following())

    async def test01_old_start_cannot_run_after_new_task_releases_model(self):
        backend,entered,release,returned=self.delay(SDKBackend,'start_follow')
        gateway=self.gateway(backend)
        task=asyncio.create_task(gateway.dispatch('start_follow',[],{}))
        self.assertTrue(await asyncio.to_thread(entered.wait,1))
        await self.cancel(gateway,task)
        await self.new_move(backend)
        release.set();self.assertTrue(await asyncio.to_thread(returned.wait,1))
        self.assertFalse(ctl.is_following())
        self.assertEqual(ctl.get_status().control_owner,'none')

    async def test02_old_stop_cannot_stop_replacement_follow(self):
        backend,entered,release,returned=self.delay(SDKBackend,'stop_follow')
        self.assertTrue(ctl.start_follow().ok)
        gateway=self.gateway(backend)
        task=asyncio.create_task(gateway.dispatch('stop_follow',[],{}))
        self.assertTrue(await asyncio.to_thread(entered.wait,1))
        await self.cancel(gateway,task)
        await self.new_follow(backend)
        release.set();self.assertTrue(await asyncio.to_thread(returned.wait,1))
        self.assertTrue(ctl.is_following())
        self.assertEqual(ctl.get_status().control_owner,'follow')

    async def test03_old_request_cannot_grant_itself_control_after_cancel(self):
        backend,entered,release,returned=self.delay(SDKBackend,'request_control')
        gateway=self.gateway(backend)
        task=asyncio.create_task(gateway.dispatch('move_forward',[20],{}))
        self.assertTrue(await asyncio.to_thread(entered.wait,1))
        await self.cancel(gateway,task)
        await self.new_move(backend)
        drv._events.clear()
        release.set();self.assertTrue(await asyncio.to_thread(returned.wait,1))
        self.assertEqual(ctl.get_status().control_owner,'none')
        self.assertFalse(any(event.get('param')==20 for event in drv.snapshot()['events']))

    async def test04_old_pause_cannot_pause_replacement_follow(self):
        self.assertTrue(ctl.start_follow().ok)
        backend,entered,release,returned=self.delay(SDKBackend,'pause_follow_for_action')
        gateway=self.gateway(backend)
        task=asyncio.create_task(gateway.dispatch('move_forward',[20],{}))
        self.assertTrue(await asyncio.to_thread(entered.wait,1))
        await self.cancel(gateway,task)
        await self.new_follow(backend)
        release.set();self.assertTrue(await asyncio.to_thread(returned.wait,1))
        self.assertTrue(ctl.is_following())

    async def test05_old_finish_cannot_resume_after_cancel_and_new_move(self):
        self.assertTrue(ctl.start_follow().ok)
        backend,entered,release,returned=self.delay(SDKBackend,'finish_follow_action')
        gateway=self.gateway(backend)
        await gateway.dispatch('move_forward',[20],{})
        task=asyncio.create_task(gateway.finish(True))
        self.assertTrue(await asyncio.to_thread(entered.wait,1))
        await self.cancel(gateway,task)
        await self.new_move(backend)
        release.set();self.assertTrue(await asyncio.to_thread(returned.wait,1))
        self.assertFalse(ctl.is_following())

    async def test06_new_begin_never_clears_old_worker_cancellation(self):
        backend=self.backend();gateway=self.gateway(backend)
        old=gateway.cancelled
        old.set();gateway.begin()
        self.assertIsNot(old,gateway.cancelled)
        self.assertTrue(old.is_set())
        token=OPERATION.set(old)
        try:result=await asyncio.to_thread(backend.request_control,'model')
        finally:OPERATION.reset(token)
        self.assertFalse(result['ok'])
        self.assertEqual(ctl.get_status().control_owner,'none')

    async def test07_failure_after_explicit_follow_stops_own_service(self):
        gateway=self.gateway(self.backend())
        await gateway.dispatch('start_follow',[],{})
        self.assertTrue(ctl.is_following())
        await gateway.finish(False)
        self.assertFalse(ctl.is_following())
        self.assertEqual(ctl.get_status().control_owner,'none')

    async def test08_old_failure_cleanup_does_not_stop_new_follow(self):
        backend=self.backend();old=self.gateway(backend)
        await old.dispatch('start_follow',[],{})
        old.cancelled.set()
        ctl.stop_follow()
        await self.new_follow(backend)
        await old.finish(False)
        self.assertTrue(ctl.is_following())

    async def test09_read_only_failure_does_not_stop_prior_follow(self):
        self.assertTrue(ctl.start_follow().ok)
        gateway=self.gateway(self.backend())
        await gateway.dispatch('get_status',[],{})
        await gateway.finish(False)
        self.assertTrue(ctl.is_following())

    async def test10_cancelled_program_still_cleans_its_own_model_grant(self):
        gateway=self.gateway(self.backend())
        await gateway.dispatch('move_forward',[20],{})
        gateway.cancelled.set()
        await gateway.finish(False)
        self.assertEqual(ctl.get_status().control_owner,'none')

    async def test11_program_timeout_after_start_follow_stops_its_service(self):
        from sandbox_runner import SandboxRunner
        from ContainerWireTests import Process
        gateway=self.gateway(self.backend())
        runner=SandboxRunner(gateway,timeout=1)
        process=Process()
        process.stdout=asyncio.StreamReader()
        process.stdout.feed_data(json.dumps({'type':'rpc','id':1,'method':'start_follow','args':[],'kwargs':{}}).encode()+b'\n')
        with patch.object(runner,'check',new=AsyncMock()),patch('sandbox_runner.asyncio.create_subprocess_exec',side_effect=[process,Process()]):
            result=await runner.run('drone.start_follow(); time.sleep(100)')
        self.assertEqual(result['status'],'timeout',result)
        self.assertTrue(result['journal'][0]['result']['ok'],result)
        self.assertFalse(ctl.is_following())
        self.assertEqual(ctl.get_status().control_owner,'none')

if __name__=='__main__':unittest.main(verbosity=2)
