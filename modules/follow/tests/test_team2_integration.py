from simulation_test_support import settle_landing, settle_workers
"""Actual reviewed upstream stub + explicit generated image fixture, no hardware."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from r3_follow.team2_adapter import Team2StubPort, HASHES, load_reviewed_stub
from r3_follow.team2_bridge import Team3FollowBridge
from r3_follow.service_ports import FrameSnapshot
from r3_follow.vision import synthetic_frame

ROOT = Path(__file__).resolve().parents[1]
REPO = (ROOT.parents[1] if (ROOT.parents[1] / 'drone_sdk').is_dir()
        else ROOT.parent / 'tmp' / 'team2_b9f37f9')


class Team2IntegrationTests(unittest.TestCase):
    def setUp(self):
        if not REPO.is_dir():
            self.skipTest('Reviewed team2 snapshot required: tmp/team2_b9f37f9')
        self.network=patch('socket.socket',side_effect=AssertionError('No networking'))
        self.network.start();self.addCleanup(self.network.stop)
        self.port=Team2StubPort(REPO)
        self.sdk=self.port.sdk
        owner=self.sdk.get_status().control_owner
        if owner not in ('none',None):self.sdk.release_control(owner)
        self.sdk.stop_follow()
        self.sdk.set_frame_provider(None)
        self.sdk.request_control('model')
        self.sdk.land('model')
        settle_landing(self.sdk)
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
        self.sdk.stop_follow()
        self.sdk.set_frame_provider(None)
        self.bridge.close()
        owner=self.sdk.get_status().control_owner
        if owner not in ('none',None):
            self.sdk.land(owner);settle_landing(self.sdk);self.sdk.release_control(owner)

    def frame(self, **kwargs):
        self.now+=.11;self.seq+=1
        self.packet=FrameSnapshot(synthetic_frame(**kwargs),self.seq,time.time())
        self.service.capture_once();self.service.control_once()

    def ready(self):
        self.frame()
        self.assertTrue(self.service.calibrate_reference().ok)
        self.assertTrue(self.bridge.start_follow().ok)
        self.frame(x=.8)
        self.assertTrue(any(e['event']=='velocity_call' and e['channels'][3]>0 for e in self.port.events))

    def test_raw_empty_video_blocks_start(self):
        self.port.frame_provider=None
        self.assertFalse(self.service.capture_once())
        self.assertFalse(self.service.calibrate_reference().ok)
        self.assertFalse(self.bridge.start_follow().ok)

    def test_empty_frame_is_stale_and_preserves_metadata(self):
        a,b=self.sdk.get_frame(),self.sdk.get_frame()
        self.assertIsNone(a.frame)
        self.assertTrue(a.stale)
        self.assertEqual((b.frame_id,b.timestamp),(a.frame_id,a.timestamp))

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
        self.now+=1.1
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
        self.sdk.set_frame_provider(generated)
        return position

    def test_upstream_follow_without_frame_source_refuses_start(self):
        result = self.sdk.start_follow()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'follow_no_frame_source')
        self.assertFalse(self.sdk.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, 'none')

    def test_upstream_follow_delegates_and_stop_clears_requests_and_workers(self):
        position = self.sdk_feed()
        result = self.sdk.start_follow()
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(self.sdk.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, 'follow')
        service = self.sdk._follow._bridge.service
        workers = tuple(service.workers)
        self.assertTrue(all(w.is_alive() for w in workers))
        # A repeat start must keep the same service, not create another sender.
        self.assertTrue(self.sdk.start_follow().ok)
        self.assertIs(self.sdk._follow._bridge.service, service)
        position[0] = .8
        self.wait_until(lambda: any(e.get('channels', (0,0,0,0))[3] > 0
                                  for e in self.sdk._follow.events()))
        self.assertTrue(self.sdk.stop_follow().ok)
        self.assertFalse(self.sdk.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, 'none')
        self.assertTrue(any(e.get('channels') == (0, 0, 0, 0)
                            for e in self.sdk._follow.events()))
        self.wait_until(lambda:all(not w.is_alive() for w in workers))

    def test_upstream_follow_busy_keeps_model_owner(self):
        self.sdk_feed()
        self.assertTrue(self.sdk.request_control('model').ok)
        result = self.sdk.start_follow()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'busy')
        self.assertEqual(self.sdk.get_status().control_owner, 'model')
        self.assertFalse(self.sdk.is_following())
        self.assertFalse(any(e.get('channels') and any(e['channels'])
                             for e in self.sdk._follow.events()))

    def test_upstream_follow_wrong_marker_cannot_start(self):
        self.sdk_feed(marker_id=1)
        result = self.sdk.start_follow()
        self.assertFalse(result.ok)
        self.assertFalse(self.sdk.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, 'none')

    def test_upstream_frame_provider_switch_and_stale_frame_zero(self):
        self.sdk_feed()
        self.assertTrue(self.sdk.start_follow().ok)
        seq = 10000
        frame = synthetic_frame(x=.2)
        def replacement():
            nonlocal seq
            seq += 1
            return self.sdk.FrameResult(frame, seq, time.time(), False)
        self.sdk.set_frame_provider(replacement)
        self.wait_until(lambda: any(e.get('channels', (0,0,0,0))[3] < 0
                                  for e in self.sdk._follow.events()))
        stale = self.sdk.FrameResult(frame, seq + 10000, time.time() - 1, True)
        self.sdk.set_frame_provider(lambda: stale)
        self.wait_until(lambda: not self.sdk.is_following())
        self.wait_until(lambda: self.sdk.get_status().control_owner == 'none')
        velocities = [e for e in self.sdk._follow.events() if e['event'] == 'velocity_call']
        self.assertEqual(velocities[-1]['channels'], (0, 0, 0, 0))

    def test_new_follow_dependency_must_match_reviewed_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            sdk = Path(folder) / 'drone_sdk'
            sdk.mkdir()
            for name in (p.name for p in (REPO/'drone_sdk').glob('*.py')):
                (sdk / name).write_bytes((REPO / 'drone_sdk' / name).read_bytes())
            with (sdk / 'follow_integration.py').open('a', encoding='utf8') as f:
                f.write('\n# Unreviewed change\n')
            with self.assertRaisesRegex(ValueError, 'follow_integration.py'):
                load_reviewed_stub(folder)

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
