"""Current gateway/SDK/follow desired behaviors, using generated images and RPC.

This is a diagnostic test runner, not another application. No model, Docker,
camera, desktop or aircraft is used. Source hash review remains mandatory.
"""
import argparse
import asyncio
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
BASE = Path(__file__).resolve().parent
REPO = BASE.parents[1]
sys.path.insert(0, str(REPO/'tests'))
from RuntimeFixture import SyntheticFeed, container, driver, reset, runtime, sdk, settle, wait_until
from drone_sdk.contract import CommandResult, FrameResult
MEASUREMENTS = {}


class PublicPathChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset(flying=True)
        self.sdk = sdk
        self.feed = SyntheticFeed(position=.65)
        self.app = runtime()
    def tearDown(self): reset()

    async def following(self):
        sdk.set_frame_provider(self.feed)
        with container([('start_follow', [])]):
            result = await self.app.run('drone.start_follow()')
        self.assertTrue(result['ok'], result)
        self.assertTrue(sdk.is_following())

    async def action(self):
        with container([('move_forward', [30])]):
            return await self.app.run('drone.move_forward(30)')

    async def test_failed_start_cleans_workers(self):
        result = sdk.start_follow(timeout=.1)
        self.assertFalse(result.ok)
        bridge = sdk._follow._bridge
        alive = [] if bridge is None else [t.name for t in bridge.service.workers if t.is_alive()]
        MEASUREMENTS['failed_start_live_workers'] = alive
        self.assertEqual(alive, [])

    async def test_stop_cancels_waiting_start(self):
        waiting = threading.Event()
        def no_frame():
            waiting.set()
            return FrameResult(None, 0, time.time(), True)
        sdk.set_frame_provider(no_frame)
        worker = asyncio.create_task(asyncio.to_thread(sdk.start_follow, timeout=.6))
        await wait_until(waiting.is_set)
        began = time.perf_counter()
        stopped = await asyncio.to_thread(sdk.stop_follow)
        elapsed = time.perf_counter()-began
        result = await worker
        self.assertTrue(stopped.ok)
        self.assertFalse(result.ok)
        self.assertLess(elapsed,.3)
        self.assertFalse(sdk.is_following())
        MEASUREMENTS['stop_during_start_wait_ms'] = elapsed*1000

    async def blocked_action(self):
        await self.following()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def block(*args, **kwargs):
            entered.set()
            if not release.wait(3): raise RuntimeError('test barrier timeout')
            finished.set()
            return True
        with patch.object(driver,'execute',side_effect=block):
            task = asyncio.create_task(self.action())
            try:
                await wait_until(entered.is_set)
                began = time.perf_counter()
                stopped = await asyncio.wait_for(self.app.interrupt(), .5)
                self.assertTrue(stopped['ok'], stopped)
                self.assertFalse(finished.is_set(), 'driver must still be blocked')
                self.assertFalse(sdk.is_following())
                MEASUREMENTS['interrupt'] = dict(dispatch_ms=(time.perf_counter()-began)*1000,
                    action_was_still_blocked=True, physical_stop_verified=False)
            finally:
                release.set()
                result = await asyncio.wait_for(task, 3)
        self.assertFalse(result['ok'])
        self.assertFalse(sdk.is_following())

    async def test_session_stop_reaches_sdk_while_action_is_blocked(self):
        await self.blocked_action()

    async def test_late_action_does_not_restart_after_explicit_stop(self):
        await self.blocked_action()
        self.feed.position = .8
        await asyncio.sleep(.1)
        self.assertFalse(sdk.is_following())

    async def test_resume_keeps_original_reference_size(self):
        await self.following()
        before = sdk._follow._bridge.service.controller.reference_size
        original = driver.execute
        def moved(*args, **kwargs):
            self.feed.size = .35
            time.sleep(.1)
            return original(*args, **kwargs)
        with patch.object(driver,'execute',side_effect=moved): result = await self.action()
        self.assertTrue(result['ok'],result)
        self.assertTrue(sdk.is_following())
        after = sdk._follow._bridge.service.controller.reference_size
        self.assertAlmostEqual(before,after,places=6)
        MEASUREMENTS['reference_size'] = dict(before=before,after=after)

    async def test_target_visible_is_actual_detection(self):
        await self.following()
        self.feed.visible = False
        service = sdk._follow._bridge.service
        await wait_until(lambda: service.controller.observation is not None
                         and not service.controller.observation.found)
        self.assertFalse(service.controller.observation.found)

    async def test_async_land_ack_does_not_claim_landed(self):
        self.app.gateway.begin()
        with patch.object(sdk,'land',return_value=CommandResult(True,'accepted')), \
             patch('gateway.LAND_CONFIRM_TIMEOUT_S', .08):
            with self.assertRaisesRegex(RuntimeError,'landing_confirmation_timeout'):
                await self.app.gateway.dispatch('land',[],{})
        self.assertNotEqual(sdk.get_status().flight_state,'landed')
        await self.app.gateway.finish(False)

    async def test_accepted_normal_action_never_resumes_follow(self):
        await self.following()
        with patch.object(sdk,'move_forward',return_value=CommandResult(True,'accepted')):
            result = await self.action()
        self.assertFalse(result['ok'],result)
        self.assertFalse(sdk.is_following())
        self.assertEqual(sdk.get_status().control_owner,'none')

    async def test_target_lost_during_action_cannot_resume(self):
        await self.following()
        def moved(*args,**kwargs):
            self.feed.visible=False
            time.sleep(.12)
            return True
        with patch.object(driver,'execute',side_effect=moved): result=await self.action()
        self.assertFalse(result['ok'],result)
        self.assertFalse(sdk.is_following())
        self.assertEqual(sdk.get_status().control_owner,'none')

    async def test_frozen_frame_during_action_cannot_resume(self):
        await self.following()
        self.feed.frozen=self.feed()
        with patch.object(driver,'execute',side_effect=lambda *a,**kw: (time.sleep(.6),True)[1]):
            result=await self.action()
        self.assertFalse(result['ok'],result)
        self.assertFalse(sdk.is_following())
        self.assertEqual(sdk.get_status().control_owner,'none')

    async def test_action_exception_does_not_resume(self):
        await self.following()
        with patch.object(driver,'execute',side_effect=RuntimeError('injected_failure')):
            result=await self.action()
        self.assertFalse(result['ok'])
        self.assertFalse(sdk.is_following())
        self.assertEqual(sdk.get_status().control_owner,'none')

    async def test_busy_start_cannot_release_model_control(self):
        sdk.set_frame_provider(self.feed)
        self.assertTrue(sdk.request_control('model').ok)
        result=sdk.start_follow()
        self.assertFalse(result.ok)
        self.assertEqual(sdk.get_status().control_owner,'model')

    async def test_pending_landing_blocks_other_motion_until_fresh_status(self):
        self.app.gateway.begin()
        with patch.object(sdk,'_landing_delay',.5):
            task=asyncio.create_task(self.app.gateway.dispatch('land',[],{}))
            await wait_until(lambda: sdk.get_status().flight_state=='landing')
            for method,args in [('move_forward',[30]),('takeoff',[]),('start_follow',[])]:
                with self.subTest(method=method), self.assertRaises(RuntimeError):
                    await self.app.gateway.dispatch(method,args,{})
            result=await task
        self.assertTrue(result['ok'])
        self.assertEqual(sdk.get_status().flight_state,'landed')

    async def test_stop_during_start_cannot_be_undone_by_late_frame(self):
        waiting=threading.Event()
        def empty():
            waiting.set()
            return FrameResult(None,0,time.time(),True)
        sdk.set_frame_provider(empty)
        worker=asyncio.create_task(asyncio.to_thread(sdk.start_follow,timeout=.3))
        await wait_until(waiting.is_set)
        await asyncio.to_thread(sdk.stop_follow)
        sdk.set_frame_provider(self.feed)
        self.assertFalse((await worker).ok)
        self.assertFalse(sdk.is_following())
        self.assertEqual(sdk.get_status().control_owner,'none')


def main():
    global REPO
    parser=argparse.ArgumentParser()
    parser.add_argument('--repository',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    REPO=args.repository.resolve()
    if REPO!=BASE.parents[1]:
        raise SystemExit('Mixed checkouts are not supported; run the reviewed checkout itself')
    from joint_integration_check import reviewed_sdk
    reviewed_sdk(REPO)
    args.output.mkdir(parents=True,exist_ok=True)
    console,tests=io.StringIO(),io.StringIO()
    with contextlib.redirect_stdout(console):
        result=unittest.TextTestRunner(stream=tests,verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(PublicPathChecks))
    report=dict(tests_run=result.testsRun,passed=result.testsRun-len(result.failures)-len(result.errors),
        failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped),successful=result.wasSuccessful(),
        measurements=MEASUREMENTS,unresolved=[dict(test=t.id(),detail=d) for t,d in result.failures+result.errors],
        scope='Current UnifiedRuntime/SandboxRunner scripted container RPC/Gateway/SDK/actual detector',
        generated_images=True,real_camera_or_flight=False,real_model_used=False,real_docker_used=False)
    (args.output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (args.output/'tests.txt').write_text(tests.getvalue(),encoding='utf-8')
    (args.output/'console.txt').write_text(console.getvalue(),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('tests_run','passed','failures','errors')},ensure_ascii=True))
    return 0 if result.wasSuccessful() else 1

if __name__=='__main__': raise SystemExit(main())
