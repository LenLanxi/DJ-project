"""Actual reviewed upstream stub + explicit generated image fixture, no hardware."""
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from r3_follow.team2_adapter import Team2StubPort
from r3_follow.team2_bridge import Team3FollowBridge
from r3_follow.service_ports import FrameSnapshot
from r3_follow.vision import synthetic_frame

REPO=Path(__file__).resolve().parents[2]/'integrations'/'DJ-project'


class Team2IntegrationTests(unittest.TestCase):
    def setUp(self):
        if not REPO.is_dir():
            self.skipTest('Reviewed team2 checkout required: integrations/DJ-project')
        self.network=patch('socket.socket',side_effect=AssertionError('No networking'))
        self.network.start();self.addCleanup(self.network.stop)
        self.port=Team2StubPort(REPO)
        self.sdk=self.port.sdk
        owner=self.sdk.get_status().control_owner
        if owner not in ('none',None):self.sdk.release_control(owner)
        self.sdk.stop_follow()
        self.sdk.request_control('model')
        self.sdk.land('model')
        self.sdk.takeoff('model')
        self.sdk.release_control('model')
        self.now=10.
        self.seq=0
        self.packet=FrameSnapshot(synthetic_frame(),0,time.time())
        self.port.frame_provider=lambda:self.packet
        self.bridge=Team3FollowBridge(self.port)
        self.service=self.bridge.service
        self.service.clock=lambda:self.now
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.bridge.close()
        owner=self.sdk.get_status().control_owner
        if owner not in ('none',None):
            self.sdk.land(owner);self.sdk.release_control(owner)

    def frame(self, **kwargs):
        self.now+=.11;self.seq+=1
        self.packet=FrameSnapshot(synthetic_frame(**kwargs),self.seq,time.time())
        self.service.capture_once();self.service.control_once()

    def ready(self):
        self.frame()
        self.assertTrue(self.service.calibrate_reference().ok)
        self.assertTrue(self.bridge.start_follow().ok)
        self.frame(x=.8)
        self.assertTrue(any(e['event']=='velocity_call' and e['channels'][0]>0 for e in self.port.events))

    def test_raw_empty_video_blocks_start(self):
        self.port.frame_provider=None
        self.assertFalse(self.service.capture_once())
        self.assertFalse(self.service.calibrate_reference().ok)
        self.assertFalse(self.bridge.start_follow().ok)

    def test_upstream_empty_frame_refreshes_metadata(self):
        a,b=self.sdk.get_frame(),self.sdk.get_frame()
        self.assertIsNone(a.frame)
        self.assertFalse(a.stale)
        self.assertGreater(b.frame_id,a.frame_id)

    def test_actual_control_contention_and_bridge_start_stop(self):
        self.ready()
        self.assertEqual(self.sdk.request_control('model').reason,'busy')
        self.assertTrue(self.bridge.is_following())
        self.assertTrue(self.bridge.stop_follow().ok)
        self.assertEqual(self.sdk.get_status().control_owner,'none')
        zeros=[e for e in self.port.events if e['event']=='velocity_call' and e['channels']==(0,0,0,0)]
        self.assertTrue(zeros)
        self.frame(x=.8)
        self.assertFalse(self.bridge.is_following())

    def test_brief_loss_zero_then_recovery(self):
        self.ready();self.frame(visible=False)
        self.assertEqual(self.port.events[-1]['channels'],(0,0,0,0))
        self.assertFalse(self.bridge.is_following())
        for _ in range(3):self.frame(x=.8)
        self.assertTrue(self.bridge.is_following())

    def test_long_loss_requires_start(self):
        self.ready();self.frame(visible=False)
        for _ in range(29):self.frame(visible=False)
        self.frame(x=.8)
        self.assertFalse(self.bridge.is_following())

    def test_stale_and_duplicate_frames(self):
        self.ready()
        seq=self.service.last_frame_id
        self.now+=.6
        self.service.capture_once();self.service.control_once()
        self.assertEqual(self.service.last_frame_id,seq)
        self.assertFalse(self.bridge.is_following())
        self.assertEqual(self.sdk.get_status().control_owner,'none')

    def test_changed_cached_timestamp_is_rejected(self):
        self.ready()
        self.packet=FrameSnapshot(self.packet.frame,self.packet.frame_id,self.packet.timestamp+.01)
        self.assertFalse(self.service.capture_once())
        self.assertFalse(self.bridge.is_following())

    def test_handoff_waits_for_completed_and_returns_control(self):
        self.ready()
        result,ticket=self.bridge.pause_for_action();self.assertTrue(result.ok)
        self.assertTrue(self.sdk.request_control('model').ok)
        self.frame(x=.2)
        self.assertEqual(self.bridge.finish_action(ticket,'accepted').status,'accepted')
        self.assertFalse(self.bridge.is_following())
        self.assertEqual(self.sdk.move_forward('model',20).status,'completed')
        self.sdk.release_control('model')
        self.assertTrue(self.bridge.finish_action(ticket,'completed').ok)
        self.assertTrue(self.bridge.is_following())

    def test_explicit_stop_cancels_late_completion(self):
        self.ready();_,ticket=self.bridge.pause_for_action()
        self.bridge.stop_follow();self.frame(x=.8)
        self.assertFalse(self.bridge.finish_action(ticket,'completed').ok)
        self.assertFalse(self.bridge.is_following())

    def test_action_failure_never_recovers(self):
        self.ready();_,ticket=self.bridge.pause_for_action();self.frame(x=.8)
        self.assertFalse(self.bridge.finish_action(ticket,'failed').ok)
        self.assertFalse(self.bridge.is_following())

    def test_lost_owner_does_not_stop_new_owner(self):
        self.ready()
        self.sdk.release_control('follow');self.sdk.request_control('model')
        count=len(self.port.events)
        self.service.control_once()
        self.assertFalse(self.bridge.is_following())
        self.assertEqual(len(self.port.events),count)
        self.assertEqual(self.sdk.get_status().control_owner,'model')

    def test_old_lease_and_invalid_channels_rejected_locally(self):
        old=self.port.request_control('follow').lease
        self.port.release_control(old)
        new=self.port.request_control('follow').lease
        self.assertNotEqual(old,new)
        self.assertEqual(self.port.update_velocity(old,left_right=1,forward_back=0,up_down=0,yaw=0).reason,'not_owner')
        for value in (101,True,1.5,float('nan')):
            with self.subTest(value=value):
                self.assertEqual(self.port.update_velocity(new,left_right=value,forward_back=0,up_down=0,yaw=0).reason,'invalid_channels')
        self.port.release_control(new)

    def test_upstream_follow_is_placeholder(self):
        self.sdk.start_follow()
        self.assertTrue(self.sdk.is_following())
        self.assertEqual(self.sdk.get_status().control_owner,'none')
        self.sdk.stop_follow()

    def test_upstream_accepts_out_of_range_speed(self):
        self.sdk.request_control('follow')
        self.assertTrue(self.sdk.update_velocity('follow',101,0,0,0).ok)
        self.sdk.release_control('follow')

    def test_upstream_release_non_owner_acknowledges_none_incorrectly(self):
        self.sdk.request_control('model')
        result=self.sdk.release_control('follow')
        self.assertTrue(result.ok)
        self.assertEqual(result.current_owner,'none')
        self.assertEqual(self.sdk.get_status().control_owner,'model')


if __name__=='__main__':unittest.main()
