from FollowSupport import settle_landing, settle_workers
"""Repository SDK + explicit generated image fixture, no hardware."""
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from drone_sdk import controller as Device
from follow.r3_follow import FollowService
from follow.r3_follow.config import load_config
from follow.r3_follow.config import load_config
from follow.r3_follow import FollowService
from drone_sdk.contract import FrameResult
from follow.r3_follow.vision import synthetic_frame

SOURCE = Path(__file__).resolve().parents[2] / 'follow'
REPO = Path(__file__).resolve().parents[2]


class Team2IntegrationTests(unittest.TestCase):
    def setUp(self):
        if not REPO.is_dir():
            self.skipTest('Team2 SDK snapshot required: tmp/team2_b9f37f9')
        self.network=patch('socket.socket',side_effect=AssertionError('No networking'))
        self.network.start();self.addCleanup(self.network.stop)
        self.port=Device
        self.sdk=self.port
        self.follow = FollowService(self.sdk, config=load_config())
        owner=self.sdk.get_status().control_owner
        if owner not in ('none',None):self.sdk.release_control(owner)
        self.follow.stop()
        self.follow.set_frame_provider(None)
        self.sdk.request_control('model')
        self.sdk.land('model')
        settle_landing(self.sdk)
        self.sdk.takeoff('model')
        self.sdk.release_control('model')
        self.now=10.
        self.wall_base=time.time()-self.now
        self.seq=0
        self.packet=FrameResult(synthetic_frame(),0,self.wall_base+self.now,False)
        self.service=FollowService(self.port, config=load_config(), frame_provider=lambda:self.packet, recovery_window=3)
        self.service.clock=lambda:self.now
        # Step both clocks together; Windows time.time() may repeat between frames.
        self.service.wall=lambda:self.wall_base+self.now
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.follow.close(wait=True)
        self.follow.set_frame_provider(None)
        self.service.close()
        owner=self.sdk.get_status().control_owner
        if owner not in ('none',None):
            self.sdk.land(owner);settle_landing(self.sdk);self.sdk.release_control(owner)

    def frame(self, **kwargs):
        self.now+=.11;self.seq+=1
        self.packet=FrameResult(synthetic_frame(**kwargs),self.seq,self.wall_base+self.now,False)
        self.service.capture_once();self.service.control_once()

    def ready(self):
        self.frame()
        self.assertTrue(self.service.calibrate_reference().ok)
        self.assertTrue(self.service._start_tracking().ok)
        self.frame(x=.8)
        self.assertTrue(any(e['event']=='motion_request' and e['channels'][3]>0 for e in self.service.events()))

    def test_raw_empty_video_blocks_start(self):
        self.service.frame_provider=None
        self.assertFalse(self.service.capture_once())
        self.assertFalse(self.service.calibrate_reference().ok)
        self.assertFalse(self.service._start_tracking().ok)

    def test_empty_frame_is_stale_and_preserves_metadata(self):
        a,b=self.sdk.get_frame(),self.sdk.get_frame()
        self.assertIsNone(a.frame)
        self.assertTrue(a.stale)
        self.assertEqual((b.frame_id,b.timestamp),(a.frame_id,a.timestamp))

    def test_actual_control_contention_and_bridge_start_stop(self):
        self.ready()
        self.assertEqual(self.sdk.request_control('model').reason,'busy')
        self.assertTrue(self.service.is_following())
        self.assertTrue(self.service._stop_tracking().ok)
        self.assertEqual(self.sdk.get_status().control_owner,'none')
        zeros=[e for e in self.service.events() if e['event']=='motion_request' and e['channels']==(0,0,0,0)]
        self.assertTrue(zeros)
        self.frame(x=.8)
        self.assertFalse(self.service.is_following())

    def test_brief_loss_zero_then_recovery(self):
        self.ready();self.frame(visible=False)
        self.assertEqual(self.sdk.get_status().velocity,(0,0,0,0))
        self.assertFalse(self.service.is_following())
        for _ in range(3):self.frame(x=.8)
        self.assertTrue(self.service.is_following())

    def test_long_loss_requires_start(self):
        self.ready();self.frame(visible=False)
        for _ in range(29):self.frame(visible=False)
        self.frame(x=.8)
        self.assertFalse(self.service.is_following())

    def test_stale_and_duplicate_frames(self):
        self.ready()
        seq=self.service.last_frame_id
        self.now+=1.1
        self.service.capture_once();self.service.control_once()
        self.assertEqual(self.service.last_frame_id,seq)
        self.assertFalse(self.service.is_following())
        self.assertEqual(self.sdk.get_status().control_owner,'none')

    def test_changed_cached_timestamp_is_rejected(self):
        self.ready()
        self.packet=FrameResult(self.packet.frame,self.packet.frame_id,self.packet.timestamp+.01,False)
        self.assertFalse(self.service.capture_once())
        self.assertFalse(self.service.is_following())

    def test_handoff_waits_for_completed_and_returns_control(self):
        self.ready()
        ticket=self.service.pause_if_active();self.assertIsNotNone(ticket)
        self.assertTrue(self.sdk.request_control('model').ok)
        self.frame(x=.2)
        self.assertFalse(self.service.is_following())
        self.assertEqual(self.sdk.move_forward('model',20).status,'completed')
        self.sdk.release_control('model')
        self.assertTrue(self.service.finish_action(ticket,True).ok)
        self.assertTrue(self.service.is_following())

    def test_explicit_stop_cancels_late_completion(self):
        self.ready();ticket=self.service.pause_if_active()
        self.service._stop_tracking();self.frame(x=.8)
        self.assertFalse(self.service.finish_action(ticket,True).ok)
        self.assertFalse(self.service.is_following())

    def test_action_failure_never_recovers(self):
        self.ready();ticket=self.service.pause_if_active();self.frame(x=.8)
        self.assertFalse(self.service.finish_action(ticket,False).ok)
        self.assertFalse(self.service.is_following())

    def test_lost_owner_does_not_stop_new_owner(self):
        self.ready()
        self.sdk.release_control('follow'); self.sdk.request_control('model')
        self.sdk.update_velocity('model', 5, 0, 0, 0)
        self.service.control_once()
        self.assertFalse(self.service.is_following())
        self.assertEqual(self.sdk.get_status().velocity, (5, 0, 0, 0))
        self.assertEqual(self.sdk.get_status().control_owner, 'model')

    def test_old_authorization_and_invalid_channels_rejected(self):
        old=self.sdk.request_control('follow')
        self.sdk.release_control('follow', control_epoch=old.control_epoch)
        new=self.sdk.request_control('follow')
        self.assertNotEqual(old.control_epoch,new.control_epoch)
        self.assertEqual(self.sdk.update_velocity('follow',1,0,0,0,control_epoch=old.control_epoch).reason,'obsolete_control')
        for value in (101,True,1.5,float('nan')):
            with self.subTest(value=value):
                self.assertEqual(self.sdk.update_velocity('follow',value,0,0,0,control_epoch=new.control_epoch).reason,'invalid_channels')
        self.sdk.release_control('follow',control_epoch=new.control_epoch)

    def wait_until(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail('Upstream follow condition did not become true')

    def sdk_feed(self, visible=True, marker_id=0):
        seq = 0
        position = [.5]
        def generated():
            nonlocal seq
            seq += 1
            return self.sdk.FrameResult(
                synthetic_frame(x=position[0], visible=visible, marker_id=marker_id),
                seq, time.time(), False)
        self.follow.set_frame_provider(generated)
        return position

    def test_upstream_follow_without_frame_source_refuses_start(self):
        result = self.follow.start()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'follow_no_frame_source')
        self.assertFalse(self.follow.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, 'none')

    def test_upstream_follow_delegates_and_stop_clears_requests_and_workers(self):
        position = self.sdk_feed()
        result = self.follow.start()
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(self.follow.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, 'follow')
        service = self.follow
        workers = tuple(service.workers)
        self.assertTrue(all(w.is_alive() for w in workers))
        # A repeat start must keep the same service, not create another sender.
        self.assertTrue(self.follow.start().ok)
        self.assertIs(self.follow, service)
        position[0] = .8
        self.wait_until(lambda: any(e.get('channels', (0,0,0,0))[3] > 0
                                  for e in self.follow.events()))
        self.assertTrue(self.follow.stop().ok)
        self.assertFalse(self.follow.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, 'none')
        self.assertTrue(any(e.get('channels') == (0, 0, 0, 0)
                            for e in self.follow.events()))
        self.assertTrue(all(w.is_alive() for w in workers))
        self.follow.close(wait=True)
        self.wait_until(lambda: all(not w.is_alive() for w in workers))

    def test_upstream_follow_busy_keeps_model_owner(self):
        self.sdk_feed()
        self.assertTrue(self.sdk.request_control('model').ok)
        result = self.follow.start()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'busy')
        self.assertEqual(self.sdk.get_status().control_owner, 'model')
        self.assertFalse(self.follow.is_following())
        self.assertFalse(any(e.get('channels') and any(e['channels'])
                             for e in self.follow.events()))

    def test_upstream_follow_wrong_marker_cannot_start(self):
        self.sdk_feed(marker_id=1)
        result = self.follow.start()
        self.assertFalse(result.ok)
        self.assertFalse(self.follow.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, 'none')

    def test_upstream_frame_provider_switch_and_stale_frame_zero(self):
        self.sdk_feed()
        self.assertTrue(self.follow.start().ok)
        seq = 10000
        frame = synthetic_frame(x=.2)
        def replacement():
            nonlocal seq
            seq += 1
            return self.sdk.FrameResult(frame, seq, time.time(), False)
        self.follow.set_frame_provider(replacement)
        self.wait_until(lambda: any(e.get('channels', (0,0,0,0))[3] < 0
                                  for e in self.follow.events()))
        stale = self.sdk.FrameResult(frame, seq + 10000, time.time() - 1, True)
        self.follow.set_frame_provider(lambda: stale)
        self.wait_until(lambda: not self.follow.is_following())
        self.wait_until(lambda: self.sdk.get_status().control_owner == 'none')
        velocities = [e for e in self.follow.events() if e['event'] == 'velocity_call']
        self.assertEqual(self.sdk.get_status().velocity, (0, 0, 0, 0))

    def test_upstream_rejects_out_of_range_speed(self):
        self.sdk.request_control('follow')
        self.assertFalse(self.sdk.update_velocity('follow',101,0,0,0).ok)
        self.sdk.release_control('follow')

    def test_upstream_release_non_owner_is_rejected(self):
        self.sdk.request_control('model')
        result=self.sdk.release_control('follow')
        self.assertFalse(result.ok)
        self.assertEqual(result.current_owner,'model')
        self.assertEqual(self.sdk.get_status().control_owner,'model')


if __name__=='__main__':unittest.main()
