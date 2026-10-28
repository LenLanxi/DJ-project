"""Natural-region image -> delayed semantic seed -> local tracker -> gateway.

Generated fabric is not a fiducial marker. Cloud output is explicitly a test
fixture; passing proves software propagation/authority, not real tent arrival.
"""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import unittest
import pytest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
from NavigationFixture import CameraBackend, BOX
from Support import MockBackend
from Fixtures import FixtureVision
from navigation.vision import Vision, NavigationFrames
from execution.Host import Host, NavigationGuard
from execution.sandbox_runner import SandboxRunner
from ExecutionFixture import run_program, runner_for






class SemanticClient:
    def __init__(self, backend, delay=.65, *, ambiguous=False):
        self.backend,self.delay,self.ambiguous = backend,delay,ambiguous
        self.calls = 0
        self.following_during_inference = None
    def observe_image(self,jpeg,target,*,timeout):
        self.calls += 1
        self.following_during_inference = self.backend.following
        time.sleep(self.delay)
        return json.dumps({'visible':True,'ambiguous':self.ambiguous,'candidate_count':2 if self.ambiguous else 1,
                           'bbox':BOX,'bbox_format':'xyxy','description':'synthetic textured test region'})


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    async def build(self, **options):
        backend = CameraBackend(); backend.flying=True; backend.start_follow()
        client = SemanticClient(backend,**options)
        visual = Vision(client,backend); gateway = Host(backend, backend, visual)
        self.addCleanup(visual.close)
        return gateway,visual,client,backend

    async def test01_delayed_semantics_replays_real_images_to_fresh_bbox(self):
        gateway,visual,client,backend = await self.build()
        observation = await gateway.dispatch('observe',['red tent'],{})
        self.assertFalse(client.following_during_inference)
        self.assertFalse(backend.following)
        self.assertEqual(backend.owner,'model')
        self.assertTrue(observation['visible'])
        self.assertGreater(observation['frame_id'],observation['target_tracking']['seed_frame_id'])
        self.assertGreater(time.time()-observation['target_tracking']['semantic_timestamp'],.5)
        self.assertLessEqual(time.time()-observation['timestamp'],.5)
        self.assertAlmostEqual(observation['width_fraction'],150/640,delta=.005)
        self.assertAlmostEqual(observation['bbox'][2],.5,delta=.005)
        self.assertEqual(observation['bbox_format'],'xyxy')
        await gateway.dispatch('move_forward',[20],{})
        self.assertEqual(backend.motion_observations,[{'is_navigation_guard':True,'expired':False}])
        await gateway.dispatch('rotate',[360],{})
        await gateway.finish(True)
        self.assertTrue(backend.following)
        self.assertIsNone(visual._frames)

    async def test02_next_observe_updates_local_target_without_another_cloud_request(self):
        gateway,visual,client,_ = await self.build(delay=.1)
        first = await gateway.dispatch('observe',['red tent'],{})
        second = await gateway.dispatch('observe',['red tent'],{})
        self.assertEqual(client.calls,1)
        self.assertEqual(first['target_tracking']['track_id'],second['target_tracking']['track_id'])
        self.assertGreater(second['frame_id'],first['frame_id'])
        await gateway.finish(True)

    async def test03_multiple_targets_do_not_authorize_a_translation(self):
        gateway,_,_,backend = await self.build(delay=.01,ambiguous=True)
        observation = await gateway.dispatch('observe',['red tent'],{})
        self.assertFalse(observation['visible'])
        with self.assertRaisesRegex(RuntimeError,'not_visible'):
            await gateway.dispatch('move_forward',[20],{})
        self.assertFalse(any(name=='move_forward' for name,_ in backend.calls))
        await gateway.finish(False)

    async def test04_source_freeze_during_cloud_cannot_be_retimed_to_fresh(self):
        gateway,_,client,backend = await self.build()
        client.delay = 1.2
        original = client.observe_image
        def freeze(jpeg,target,*,timeout):
            captured = backend.get_frame()
            backend.get_frame = lambda: captured
            return original(jpeg,target,timeout=timeout)
        client.observe_image = freeze
        with self.assertRaisesRegex(RuntimeError,'expired'):
            await gateway.dispatch('observe',['red tent'],{})
        self.assertFalse(any(name=='move_forward' for name,_ in backend.calls))
        await gateway.finish(False)

    async def test05_capture_identity_fault_stays_invalid_until_explicit_new_observe(self):
        backend = CameraBackend(); frames = NavigationFrames(backend)
        try:
            for _ in range(100):
                if frames.frames: break
                await asyncio.sleep(.01)
            packet = backend.get_frame()
            backend.get_frame = lambda: replace(packet,frame_id=0,timestamp=time.time())
            for _ in range(100):
                if frames.stopped.is_set(): break
                await asyncio.sleep(.01)
            self.assertTrue(frames.stopped.is_set())
            with self.assertRaisesRegex(RuntimeError,'identity_changed'): frames.snapshot()
        finally: frames.close()

    async def test06_expiry_after_control_wait_cannot_borrow_new_collector_frame(self):
        backend = MockBackend(); backend.flying=True
        gateway = Host(backend, backend, FixtureVision())
        clock = [100.]
        gateway.observation = {'source':'sdk_camera','frame_id':1,'timestamp':100.,'visible':True}
        original = gateway.acquire
        async def delayed_acquire():
            await original(); clock[0] = 101.1
        with patch('execution.Host.time.time',side_effect=lambda:clock[0]), patch('execution.Host.time.monotonic',side_effect=lambda:clock[0]), patch.object(gateway,'acquire',side_effect=delayed_acquire):
            with self.assertRaisesRegex(RuntimeError,'expired'): await gateway.dispatch('move_forward',[20],{})
        self.assertFalse(any(name=='move_forward' for name,_ in backend.calls))
        await gateway.finish(False)

    async def test07_guard_cannot_extend_when_new_frames_exist(self):
        clock = [100.]; event = threading.Event()
        guard = NavigationGuard(event,100.,clock=lambda:clock[0],wall_clock=lambda:clock[0])
        deadline = guard.deadline
        clock[0] += 1.1
        self.assertTrue(guard.is_set())
        self.assertEqual(guard.deadline,deadline)

    async def test09_observed_steps_are_20cm_but_explicit_unobserved_distance_is_preserved(self):
        backend = MockBackend(); backend.flying=True
        gateway = Host(backend, backend, FixtureVision())
        gateway.observation = {'visible':True,'source':'scripted_fixture','frame_id':1,'timestamp':time.time()}
        with self.assertRaisesRegex(ValueError,'20_cm'): await gateway.dispatch('move_forward',[100],{})
        self.assertEqual(backend.calls,[])
        gateway.observation = None
        result = await gateway.dispatch('move_forward',[100],{})
        self.assertTrue(result['ok'])
        self.assertEqual(backend.calls,[('move_forward',(100,))])
        await gateway.finish(True)

    async def test10_negative_observation_normal_exit_does_not_resume_without_explicit_request(self):
        for explicit_request in (False,True):
            with self.subTest(explicit_request=explicit_request):
                gateway,_,_,backend = await self.build(delay=.01,ambiguous=True)
                result = await gateway.dispatch('observe',['red tent'],{})
                self.assertFalse(result['visible'])
                self.assertFalse(gateway.resume)
                if explicit_request:
                    deferred = await gateway.dispatch('start_follow',[],{})
                    self.assertTrue(deferred['deferred'])
                # A model may print the negative branch and return normally.
                # That normal exit alone does not mean its target task worked.
                await gateway.finish(True)
                self.assertEqual(backend.following,explicit_request)

    async def test_duplicate_live_frame_cannot_authorize_another_motion(self):
        backend = MockBackend()
        backend.flying = True
        visual = FixtureVision()
        visual.observe = lambda _target: dict(visible=True, source='sdk_camera', frame_id=1,
            timestamp=time.time(), center_x=.5, width_fraction=.2)
        host = Host(backend, backend, visual)
        await host.dispatch('observe', ['target'], {})
        await host.dispatch('move_forward', [20], {})
        with self.assertRaisesRegex(RuntimeError, 'new_frame'):
            await host.dispatch('observe', ['target'], {})
        with self.assertRaisesRegex(RuntimeError, 'new_observation'):
            await host.dispatch('move_forward', [20], {})
        self.assertEqual(sum(name == 'move_forward' for name, _ in backend.calls), 1)
        await host.finish(False)


@pytest.mark.docker
@unittest.skipUnless(os.getenv('RUN_DOCKER_TESTS')=='1' and shutil.which('docker'),'explicit real Docker opt-in required')
class ContainerNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test08_real_docker_calls_actual_vision_tracker_move_rotate_and_follow_resume(self):
        backend = CameraBackend(); backend.flying=True; backend.start_follow()
        client = SemanticClient(backend,delay=.65)
        visual = Vision(client,backend)
        runner = runner_for(backend, backend, vision=visual, timeout=10)
        try:
            result = await run_program(runner, "o=drone.observe('red tent')\nassert o['visible']\ndrone.move_forward(20)\ndrone.rotate(360)\nprint('short fixture step and rotation complete; no arrival claim')")
            self.assertTrue(result['ok'],result)
            self.assertEqual(client.calls,1)
            self.assertEqual([entry['method'] for entry in result['journal']],['observe','move_forward','rotate'])
            obs = result['journal'][0]['result']
            self.assertGreater(obs['timestamp'],obs['target_tracking']['semantic_timestamp'])
            self.assertEqual(backend.motion_observations,[{'is_navigation_guard':True,'expired':False}])
            self.assertTrue(backend.following)
            self.assertEqual(result['follow_cleanup']['status'],'follow_resumed')
            self.assertTrue(runner.last_cleanup['confirmed'])
            self.assertIsNone(visual._frames)
        finally: visual.close()


if __name__ == '__main__': unittest.main(verbosity=2)
