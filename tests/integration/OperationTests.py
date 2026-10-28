"""Cancelled host workers must never re-enter a later SDK/follow lifecycle.

Actual SDK and follow service, synthetic marker frames, no aircraft/network.
"""
import asyncio
import threading
import json
import unittest
from ExecutionFixture import run_program, runner_for
from pathlib import Path
from unittest.mock import patch,AsyncMock

from RuntimeFixture import reset,SyntheticFeed,wait_until
from SafetyFixture import NoNetwork
from execution.Host import Host
from drone_sdk import controller as ctl,_driver as drv
from drone_sdk.contract import CommandResult

ROOT=Path(__file__).resolve().parents[2]

class OperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        NoNetwork.setUp(self)
        self.follow = reset(flying=True)
        self.follow.set_frame_provider(SyntheticFeed())
        self.releases=[]
    async def asyncTearDown(self):
        for event in self.releases:event.set()
        await asyncio.sleep(.03)
        self.follow.close(wait=True);reset()
    def backend(self):return ctl
    def gateway(self,backend):
        return Host(backend,self.follow)
    async def cancel(self,gateway,task):
        result=await gateway.interrupt()
        self.assertTrue(result['ok'])
        task.cancel();await asyncio.gather(task,return_exceptions=True)
        try:
            await gateway.finish(False)
        except RuntimeError as exc:
            self.assertEqual(str(exc), 'control_acquire_pending')
            self.assertTrue(gateway.cleanup_pending)
    def delay(self,name):
        entered,release,returned=threading.Event(),threading.Event(),threading.Event()
        self.releases.append(release)
        class Device:
            def __getattr__(self, attr): return getattr(ctl,attr)
        device = Device()
        target = device if name == 'request_control' else self.follow
        method = {'start_follow':'start', 'stop_follow':'stop',
                  'pause_follow_for_action':'pause_if_active',
                  'finish_follow_action':'finish_action'}.get(name,name)
        original = getattr(target,method)
        delayed = False
        def call(*args,**kwargs):
            nonlocal delayed
            if kwargs.get('cancelled') is not None and not delayed:
                delayed=True
                entered.set();release.wait(3)
                try:return original(*args,**kwargs)
                finally:returned.set()
            return original(*args,**kwargs)
        setattr(target,method,call)
        self.addCleanup(setattr,target,method,original)
        return device,entered,release,returned
    async def new_move(self,backend):
        gateway=self.gateway(backend)
        await gateway.dispatch('move_forward',[30],{})
        await gateway.finish(True)
    async def new_follow(self,backend):
        gateway=self.gateway(backend)
        await gateway.dispatch('start_follow',[],{})
        await gateway.finish(True)
        self.assertTrue(self.follow.is_following())

    async def test01_old_start_cannot_run_after_new_task_releases_model(self):
        backend,entered,release,returned=self.delay('start_follow')
        gateway=self.gateway(backend)
        task=asyncio.create_task(gateway.dispatch('start_follow',[],{}))
        self.assertTrue(await asyncio.to_thread(entered.wait,1))
        await self.cancel(gateway,task)
        await self.new_move(backend)
        release.set();self.assertTrue(await asyncio.to_thread(returned.wait,1))
        self.assertFalse(self.follow.is_following())
        self.assertEqual(ctl.get_status().control_owner,'none')

    async def test02_old_stop_cannot_stop_replacement_follow(self):
        backend,entered,release,returned=self.delay('stop_follow')
        self.assertTrue(self.follow.start().ok)
        gateway=self.gateway(backend)
        task=asyncio.create_task(gateway.dispatch('stop_follow',[],{}))
        self.assertTrue(await asyncio.to_thread(entered.wait,1))
        await self.cancel(gateway,task)
        await self.new_follow(backend)
        release.set();self.assertTrue(await asyncio.to_thread(returned.wait,1))
        self.assertTrue(self.follow.is_following())
        self.assertEqual(ctl.get_status().control_owner,'follow')

    async def test03_old_request_cannot_grant_itself_control_after_cancel(self):
        backend,entered,release,returned=self.delay('request_control')
        gateway=self.gateway(backend)
        task=asyncio.create_task(gateway.dispatch('move_forward',[20],{}))
        self.assertTrue(await asyncio.to_thread(entered.wait,1))
        await self.cancel(gateway,task)
        await self.new_move(backend)
        drv._events.clear()
        release.set();self.assertTrue(await asyncio.to_thread(returned.wait,1))
        self.assertEqual(ctl.get_status().control_owner,'none')
        self.assertFalse(any(event.get('param')==20 for event in drv.snapshot()['events']))
        await asyncio.sleep(.01)
        await gateway.finish(False)
        self.assertFalse(gateway.cleanup_pending)

    async def test04_old_pause_cannot_pause_replacement_follow(self):
        self.assertTrue(self.follow.start().ok)
        backend,entered,release,returned=self.delay('pause_follow_for_action')
        gateway=self.gateway(backend)
        task=asyncio.create_task(gateway.dispatch('move_forward',[20],{}))
        self.assertTrue(await asyncio.to_thread(entered.wait,1))
        await self.cancel(gateway,task)
        await self.new_follow(backend)
        release.set();self.assertTrue(await asyncio.to_thread(returned.wait,1))
        self.assertTrue(self.follow.is_following())

    async def test05_old_finish_cannot_resume_after_cancel_and_new_move(self):
        self.assertTrue(self.follow.start().ok)
        backend,entered,release,returned=self.delay('finish_follow_action')
        gateway=self.gateway(backend)
        await gateway.dispatch('move_forward',[20],{})
        task=asyncio.create_task(gateway.finish(True))
        self.assertTrue(await asyncio.to_thread(entered.wait,1))
        await self.cancel(gateway,task)
        await self.new_move(backend)
        release.set();self.assertTrue(await asyncio.to_thread(returned.wait,1))
        self.assertFalse(self.follow.is_following())

    async def test06_new_begin_never_clears_old_worker_cancellation(self):
        backend=self.backend();gateway=self.gateway(backend)
        old=gateway.cancelled
        gateway.cancel()
        replacement=self.gateway(backend)
        self.assertIsNot(old,replacement.cancelled)
        self.assertTrue(old.is_set())
        result=await asyncio.to_thread(backend.request_control,'model',cancelled=old)
        self.assertFalse(result.ok)
        self.assertEqual(ctl.get_status().control_owner,'none')

    async def test07_failure_after_explicit_follow_stops_own_service(self):
        gateway=self.gateway(self.backend())
        await gateway.dispatch('start_follow',[],{})
        self.assertTrue(self.follow.is_following())
        await gateway.finish(False)
        self.assertFalse(self.follow.is_following())
        self.assertEqual(ctl.get_status().control_owner,'none')

    async def test08_old_failure_cleanup_does_not_stop_new_follow(self):
        backend=self.backend();old=self.gateway(backend)
        await old.dispatch('start_follow',[],{})
        old.cancelled.set()
        self.follow.stop()
        await self.new_follow(backend)
        await old.finish(False)
        self.assertTrue(self.follow.is_following())

    async def test09_read_only_failure_does_not_stop_prior_follow(self):
        self.assertTrue(self.follow.start().ok)
        gateway=self.gateway(self.backend())
        await gateway.dispatch('get_status',[],{})
        await gateway.finish(False)
        self.assertTrue(self.follow.is_following())

    async def test10_cancelled_program_still_cleans_its_own_model_grant(self):
        gateway=self.gateway(self.backend())
        await gateway.dispatch('move_forward',[20],{})
        gateway.cancelled.set()
        await gateway.finish(False)
        self.assertEqual(ctl.get_status().control_owner,'none')

    async def test11_program_timeout_after_start_follow_stops_its_service(self):
        from execution.sandbox_runner import SandboxRunner
        from ContainerFixture import Process
        gateway=self.gateway(self.backend())
        runner=runner_for(gateway.device,gateway.follow,timeout=1)
        process=Process()
        process.stdout=asyncio.StreamReader()
        process.stdout.feed_data(json.dumps({'type':'rpc','id':1,'method':'start_follow','args':[],'kwargs':{}}).encode()+b'\n')
        with patch.object(runner,'check',new=AsyncMock()),patch('execution.sandbox_runner.asyncio.create_subprocess_exec',side_effect=[process,Process()]):
            result=await run_program(runner, 'drone.start_follow(); time.sleep(100)')
        self.assertEqual(result['status'],'timeout',result)
        self.assertTrue(result['journal'][0]['result']['ok'],result)
        self.assertFalse(self.follow.is_following())
        self.assertEqual(ctl.get_status().control_owner,'none')

    async def test12_cancel_during_sdk_grant_preserves_lease_for_cleanup(self):
        gateway = self.gateway(self.backend())
        zero = ctl._zero
        def cancelling_zero(reason):
            result = zero(reason)
            if reason == 'owner_granted':
                gateway.cancel()
            return result
        with patch.object(ctl, '_zero', side_effect=cancelling_zero):
            with self.assertRaisesRegex(RuntimeError, 'program_cancelled'):
                await gateway.dispatch('move_forward', [20], {})
        self.assertTrue(gateway.acquired)
        self.assertEqual(ctl.get_status().control_owner, 'model')
        await gateway.finish(False)
        self.assertFalse(gateway.cleanup_pending)
        self.assertEqual(ctl.get_status().control_owner, 'none')

    async def test13_task_cancel_during_sdk_grant_retains_late_lease(self):
        gateway = self.gateway(self.backend())
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        zero = ctl._zero
        def delayed_zero(reason):
            if reason == 'owner_granted':
                entered.set()
                release.wait(3)
            return zero(reason)
        try:
            with patch.object(ctl, '_zero', side_effect=delayed_zero):
                task = asyncio.create_task(gateway.dispatch('move_forward', [20], {}))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                with self.assertRaisesRegex(RuntimeError, 'control_acquire_pending'):
                    await gateway.finish(False)
                self.assertTrue(gateway.cleanup_pending)
                release.set()
                await wait_until(lambda: gateway.acquired)
            self.assertEqual(ctl.get_status().control_owner, 'model')
            await gateway.finish(False)
            self.assertFalse(gateway.cleanup_pending)
            self.assertEqual(ctl.get_status().control_owner, 'none')
        finally:
            release.set()

    async def test14_successful_finish_closes_business_admission(self):
        self.assertTrue(self.follow.start().ok)
        gateway = self.gateway(self.backend())
        await gateway.dispatch('move_forward', [20], {})
        await gateway.finish(True)
        self.assertFalse(gateway.cancelled.is_set(), 'success must preserve follow operation provenance')
        self.assertFalse(gateway.cleanup_pending)
        for method, args in [('get_status', []), ('move_forward', [20])]:
            with self.assertRaisesRegex(RuntimeError, 'execution_closed'):
                await gateway.dispatch(method, args, {})
        await gateway.finish(False)  # A confirmed cleanup is idempotent.
        self.assertFalse(gateway.cancelled.is_set())
        self.assertTrue(self.follow.is_following())
        self.assertEqual(ctl.get_status().control_owner, 'follow')

    async def test15_failed_follow_stop_retains_context_without_model_lease(self):
        backend = self.backend()
        gateway = self.gateway(backend)
        await gateway.dispatch('start_follow', [], {})
        self.assertFalse(gateway.acquired)
        with patch.object(self.follow, 'stop', return_value=CommandResult(False, reason='stop_failed')):
            with self.assertRaisesRegex(RuntimeError, 'cancelled_follow_cleanup_failed'):
                await gateway.finish(False)
        self.assertTrue(gateway.cleanup_pending)
        self.assertFalse(gateway.acquired)
        await gateway.finish(False)
        self.assertFalse(gateway.cleanup_pending)
        self.assertFalse(self.follow.is_following())
        self.assertEqual(ctl.get_status().control_owner, 'none')

if __name__=='__main__':unittest.main(verbosity=2)
