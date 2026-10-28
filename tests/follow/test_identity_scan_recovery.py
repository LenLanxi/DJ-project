from follow.r3_follow.config import FollowConfig
"""Field failure: incomplete duplicate scan is zero-and-retry, not a conflict."""
import unittest
import cv2
from follow.r3_follow.core import Observation
from follow.r3_follow import FollowService
from tests.helpers.FollowFixture import FakeDevice
from follow.r3_follow.vision import MarkerTracker, synthetic_frame


class Clock:
    now = 10.
    def mono(self): return self.now
    def wall(self): return 1800000000.+self.now


class Detector:
    def detect(self, frame, frame_id, acquired_at, source):
        reason, x = frame
        return Observation(frame_id, acquired_at, source, 640, 480, 0, not reason,
            x if not reason else None, 240 if not reason else None,
            .16 if not reason else None,
            corners=((x-51,189),(x+51,189),(x+51,291),(x-51,291)) if not reason else (),
            reason=reason)


class IdentityRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock(); self.port=FakeDevice(self.clock.mono)
        self.service=FollowService(self.port, config=FollowConfig(control_profile='openpose_pid', max_request_rate=.8),
                         tracker=Detector(),
                         monotonic=self.clock.mono,
                         wall_clock=self.clock.wall,
                         recovery_window=3,
                         log_directory=False)
        self.addCleanup(self.service.close);self.seq=0
        self.frame()
        self.assertTrue(self.service.calibrate_reference().ok)
        self.assertTrue(self.service._start_tracking().ok)

    def frame(self,reason='',x=480,dt=.1):
        self.seq+=1;self.clock.now+=dt
        self.port.publish((reason,x),self.seq,self.clock.wall())
        self.service.capture_once();self.service.control_once()

    def assert_waiting(self):
        s=self.service.status()
        self.assertTrue(s['active']);self.assertTrue(s['recovering'])
        self.assertFalse(s['restart_required']);self.assertFalse(s['following'])
        self.assertEqual(self.port.velocity,(0,0,0,0))

    def test_incomplete_scan_immediately_after_start_recovers_on_three_new_frames(self):
        self.frame('ambiguous_scan_budget_exceeded',dt=.02)
        self.assert_waiting()
        self.assertIn('唯一性检查未完成',self.service.status()['status_message'])
        for _ in range(2):self.frame();self.assert_waiting()
        self.frame();self.assertTrue(self.service.is_following())
        self.frame();self.assertGreater(self.port.velocity[3],0)

    def test_many_cached_reads_cannot_confirm_identity(self):
        self.frame('ambiguous_scan_budget_exceeded');self.frame()
        for _ in range(20):self.service.capture_once();self.service.control_once()
        self.assert_waiting()

    def test_mixed_incomplete_and_occluded_frames_cannot_extend_timeout(self):
        self.frame('ambiguous_scan_budget_exceeded')
        began=self.service.controller._loss_at
        for i in range(14):
            self.frame(('not_found','ambiguous_scan_budget_exceeded','detection_budget_exceeded')[i%3],dt=.2)
            self.assertEqual(began,self.service.controller._loss_at);self.assert_waiting()
        self.frame('ambiguous_scan_budget_exceeded',dt=.21)
        self.assertFalse(self.service.status()['active'])
        self.assertEqual(self.service.last_halt_reason,'recovery_timeout_requires_start')
        for _ in range(4):self.frame()
        self.assertFalse(self.service.is_following());self.assertEqual(self.port.velocity,(0,0,0,0))

    def test_confirmed_same_id_conflict_still_requires_restart(self):
        self.frame('ambiguous_scan_budget_exceeded');self.frame('ambiguous_duplicate_id')
        for _ in range(4):self.frame()
        self.assertFalse(self.service.is_following())
        self.assertEqual(self.service.last_halt_reason,'ambiguous_duplicate_id')

    def test_unknown_miss_is_not_implicitly_recoverable(self):
        self.frame('unrecognized_fault')
        for _ in range(4):self.frame()
        self.assertFalse(self.service.is_following())

    def test_expired_frame_during_identity_wait_is_latched(self):
        self.frame('ambiguous_scan_budget_exceeded')
        self.clock.now+=1.01;self.service.control_once()
        for _ in range(4):self.frame()
        self.assertFalse(self.service.is_following())
        self.assertEqual(self.service.status()['state'],'frame_stale')

    def test_explicit_stop_during_identity_wait_cannot_auto_resume(self):
        self.frame('ambiguous_scan_budget_exceeded');self.service._stop_tracking()
        for _ in range(4):self.frame()
        self.assertFalse(self.service.is_following());self.assertEqual(self.port.velocity,(0,0,0,0))

    def test_control_handoff_never_stops_or_overwrites_new_owner(self):
        self.frame('ambiguous_scan_budget_exceeded')
        other=self.port.revoke_for_test('model')
        self.port.update_velocity(other,left_right=0,forward_back=5,up_down=0,yaw=0)
        for _ in range(4):self.frame()
        self.assertFalse(self.service.is_following());self.assertEqual(self.port.velocity,(0,5,0,0))

    def test_zero_recovery_window_remains_an_explicit_no_auto_resume_policy(self):
        self.service.controller.recovery_window=0
        self.frame('ambiguous_scan_budget_exceeded')
        self.assertFalse(self.service.status()['active'])
        self.assertIn('唯一性检查未完成',self.service.status()['status_message'])

    def test_real_detector_small_clutter_continues_but_incomplete_scan_still_waits(self):
        self.service._stop_tracking();self.service.tracker=MarkerTracker(processing_budget_ms=None)
        # Nine decorative squares produce >8 small ROIs, now checked in full.
        def image(clutter=False):
            a=synthetic_frame(x=.65,size=.19)
            if clutter:
                for k in range(9):
                    x=20+k*60;cv2.rectangle(a,(x,20),(x+24,44),(0,0,0),-1)
            return a
        def publish(a):
            self.seq+=1;self.clock.now+=.1
            self.port.publish(a,self.seq,self.clock.wall())
            self.service.capture_once();self.service.control_once()
        publish(image());self.assertTrue(self.service.calibrate_reference().ok)
        self.assertTrue(self.service._start_tracking().ok)
        publish(image(True));self.assertTrue(self.service.is_following())
        completed=self.service.tracker.last_diagnostics['duplicate_check']
        self.assertGreater(completed['roi_count'],8)
        self.assertTrue(completed['scan_complete'])
        self.assertGreater(self.port.velocity[3],0)
        # Force a genuinely incomplete scan; fail-closed recovery is unchanged.
        self.service.tracker.DUPLICATE_MAX_ROIS=8
        publish(image(True));self.assert_waiting()
        event=[e for e in self.service.events() if e['event']=='observation'][-1]
        self.assertEqual(event['reason'],'ambiguous_scan_budget_exceeded')
        self.assertEqual(event['duplicate_check']['budget_reason'],'roi_count')
        self.assertGreater(event['duplicate_check']['candidate_count'],8)
        self.assertIsNone(event['center_x'])
        for _ in range(4):publish(image())
        self.assertTrue(self.service.is_following());self.assertGreater(self.port.velocity[3],0)


if __name__=='__main__':unittest.main()
