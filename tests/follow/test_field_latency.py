from tests.helpers.FollowFixture import make_controller as FollowController
from follow.r3_follow.config import load_config
from follow.r3_follow.config import FollowConfig
"""Field-derived latency, late-result and honest status regressions, no device."""
import threading
import time
import unittest
from dataclasses import replace

from follow.r3_follow.core import Observation, FollowController
from follow.r3_follow import FollowService
from tests.helpers.FollowFixture import FakeDevice
from follow.r3_follow.vision import MarkerTracker
from follow.r3_follow.notices import ConsoleNotices
from drone_sdk.contract import CommandResult
from unittest.mock import patch


class Clock:
    now = 10.
    def mono(self): return self.now
    def wall(self): return 1800000000.+self.now


class Detector:
    def __init__(self, clock):
        self.clock, self.cost = clock, 0.
        self.calls = 0

    def detect(self, frame, frame_id, acquired_at, source):
        self.calls += 1
        self.clock.now += self.cost
        reason, x = frame
        corners = ((x-80,160),(x+80,160),(x+80,320),(x-80,320)) if not reason else ()
        return Observation(frame_id, acquired_at, source, 640,480,0,not reason,
                           x if not reason else None,240 if not reason else None,
                           .25 if not reason else None,corners=corners,reason=reason)


class FieldLatencyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.port = FakeDevice(self.clock.mono)
        self.detector = Detector(self.clock)
        self.service = FollowService(self.port, tracker=self.detector,
            monotonic=self.clock.mono, wall_clock=self.clock.wall,
            recovery_window=3, log_directory=False, config=load_config())
        self.seq = 0
        self.addCleanup(self.service.close)
        self.frame()
        self.assertTrue(self.service.calibrate_reference().ok)
        self.assertTrue(self.service._start_tracking().ok)
        self.frame(x=500)
        self.assertGreater(self.port.velocity[3],0)

    def frame(self, reason="", x=320, dt=.1):
        self.seq += 1
        self.clock.now += dt
        self.port.publish((reason,x),self.seq,self.clock.wall())
        result=self.service.capture_once()
        self.service.control_once()
        return result

    def test_budget_failure_zero_and_three_new_frames_restore(self):
        self.frame("detection_budget_exceeded")
        s=self.service.status()
        self.assertTrue(s['recovering'])
        self.assertFalse(s['following'])
        self.assertEqual(self.port.velocity,(0,0,0,0))
        self.assertIn('识别处理超时',s['status_message'])
        for _ in range(2):
            self.frame(x=500)
            self.assertEqual(self.port.velocity,(0,0,0,0))
        self.frame(x=500)
        self.assertTrue(self.service.is_following())
        self.frame(x=500)
        self.assertGreater(self.port.velocity[3],0)

    def test_budget_and_not_found_share_deadline_without_renewal(self):
        self.frame('detection_budget_exceeded')
        began=self.service.controller._loss_at
        for i in range(14):
            self.frame('not_found' if i%2 else 'detection_budget_exceeded',dt=.2)
            self.assertEqual(self.service.controller._loss_at,began)
        self.frame('detection_budget_exceeded',dt=.21)
        self.assertFalse(self.service.status()['active'])
        self.assertEqual(self.port.velocity,(0,0,0,0))
        self.assertEqual(self.service.last_halt_reason,'recovery_timeout_requires_start')
        self.frame(x=500)
        self.assertFalse(self.service.is_following())

    def test_duplicate_does_not_decode_or_count_as_recovery_confirmation(self):
        self.frame('detection_budget_exceeded')
        self.frame(x=500)
        count=self.detector.calls
        for _ in range(10):
            self.service.capture_once()
            self.service.control_once()
        self.assertEqual(self.detector.calls,count)
        self.assertFalse(self.service.is_following())
        self.assertEqual(self.port.velocity,(0,0,0,0))

    def test_stale_while_budget_wait_is_latched(self):
        self.frame('detection_budget_exceeded')
        self.clock.now += 1.01
        self.service.control_once()
        self.frame(x=500)
        s=self.service.status()
        self.assertEqual(s['state'],'frame_stale')
        self.assertTrue(s['target_visible'])
        self.assertFalse(s['active'])
        self.assertTrue(s['restart_required'])
        self.assertIn('目标现在可见，但跟随没有运行',s['status_message'])
        self.assertIn('重新开始',s['status_message'])

    def test_budget_result_already_old_cannot_enter_recovery(self):
        self.detector.cost=1.01
        self.assertFalse(self.frame('detection_budget_exceeded'))
        s=self.service.status()
        self.assertEqual(s['state'],'frame_stale')
        self.assertFalse(s['active'])
        self.assertFalse(s['observation_fresh'])
        self.assertEqual(s['last_halt_reason'],'vision_result_expired')

    def test_completion_never_refreshes_capture_timestamp(self):
        self.detector.cost=.12
        self.frame('detection_budget_exceeded')
        s=self.service.status()
        self.assertAlmostEqual(s['observation_age_ms'],120,places=4)
        e=[e for e in self.service.events() if e['event']=='observation'][-1]
        self.assertAlmostEqual(e['result_age_ms'],120,places=4)
        self.assertEqual(e['image_width'],640)
        self.assertIn('corners',e)

    def test_duplicate_id_during_budget_recovery_stops_without_substitution(self):
        self.frame('detection_budget_exceeded')
        self.frame('ambiguous_duplicate_id')
        s=self.service.status()
        self.assertFalse(s['active'])
        self.assertEqual(s['last_halt_reason'],'ambiguous_duplicate_id')

    def test_budget_cannot_resume_after_control_loss(self):
        self.frame('detection_budget_exceeded')
        other=self.port.revoke_for_test('model')
        self.port.update_velocity(other,left_right=0,forward_back=5,up_down=0,yaw=0)
        for _ in range(4): self.frame(x=500)
        self.assertFalse(self.service.is_following())
        self.assertEqual(self.port.velocity,(0,5,0,0))

    def test_frame_gap_reason_survives_inactive_ticks_and_status_queries(self):
        self.frame(dt=1.1)
        for _ in range(3):
            s=self.service.status()
            self.assertEqual(s['last_halt_reason'],'frame_gap')
            self.assertIn('画面间隔过长',s['status_message'])

    def test_tracking_at_zero_is_not_reported_stopped(self):
        self.frame()
        s=self.service.status()
        self.assertTrue(s['following'])
        self.assertEqual(s['requested_channels'],(0,0,0,0))
        self.assertIn('跟随已开启',s['status_message'])
        self.assertFalse(s['restart_required'])

    def test_stop_status_persists_after_fresh_green_detection(self):
        self.service._stop_tracking()
        self.frame(x=500)
        s=self.service.status()
        self.assertTrue(s['target_visible'])
        self.assertFalse(s['following'])
        self.assertIn('已收到停止指令',s['status_message'])
        self.assertEqual(s['requested_channels'],(0,0,0,0))
        self.assertFalse(s['physical_hover_confirmed'])

    def test_status_notice_not_repeated_for_unchanged_queries(self):
        first=self.service.status()['status_notice_seq']
        for _ in range(100): self.service.status()
        self.assertEqual(first,self.service.status()['status_notice_seq'])

    def test_remaining_age_restricts_native_detector_budget(self):
        clock=self.clock
        class NativeSpy(MarkerTracker):
            def detect(inner,frame,frame_id,acquired_at,source,*,processing_budget_ms=None):
                inner.received_budget=processing_budget_ms
                return Detector(clock).detect(frame,frame_id,acquired_at,source)
        spy=NativeSpy()
        self.service.tracker=spy
        self.service.pause_if_active()
        self.clock.now+=.95
        self.seq+=1
        self.port.publish(('',500),self.seq,self.clock.wall()-.9)
        self.service.capture_once()
        self.assertAlmostEqual(spy.received_budget,80,places=3)

    def test_previous_observation_deadline_also_restricts_budget(self):
        clock=self.clock
        class NativeSpy(MarkerTracker):
            def detect(inner,frame,frame_id,acquired_at,source,*,processing_budget_ms=None):
                inner.received_budget=processing_budget_ms
                return Detector(clock).detect(frame,frame_id,acquired_at,source)
        spy=NativeSpy()
        self.service.tracker=spy
        self.clock.now+=.95
        self.seq+=1
        self.port.publish(('',500),self.seq,self.clock.wall())
        self.service.capture_once()
        self.assertAlmostEqual(spy.received_budget,30,places=3)

    def test_detector_cannot_forge_new_frame_time(self):
        detect=self.detector.detect
        self.detector.detect=lambda *a,**kw: replace(detect(*a,**kw),acquired_at=self.clock.now+.01)
        self.assertFalse(self.frame(x=500))
        self.assertFalse(self.service.is_following())
        self.assertIn('detector_changed_frame_identity_or_time',self.service.last_reason)

    def test_new_id_cannot_reuse_previous_capture_time(self):
        self.seq+=1
        self.port.publish(('',100),self.seq,self.port.snapshot.timestamp)
        self.assertFalse(self.service.capture_once())
        self.assertFalse(self.service.is_following())
        self.assertIn('new_frame_timestamp_not_advancing',self.service.last_reason)

    def test_revoked_failed_cleanup_does_not_poison_later_start_message(self):
        with patch.object(self.port,'release_control',return_value=CommandResult(False, 'failed','injected')):
            self.service._stop_tracking()
        other=self.port.revoke_for_test('model')
        self.service.control_once()
        self.assertFalse(self.service.cleanup_unconfirmed)
        self.port.release_control(other)
        self.frame()
        self.assertTrue(self.service._start_tracking().ok)
        self.assertIn('跟随已开启',self.service.status()['status_message'])

    def test_stop_is_prompt_even_while_service_notice_output_blocks(self):
        entered,release=threading.Event(),threading.Event()
        emitted=[]
        def sink(text):
            entered.set();release.wait(2);emitted.append(text)
        self.service.notifier=ConsoleNotices(sink)
        n=self.service.notifier
        try:
            n.start();n.submit('带时间的旧状态 t=0')
            self.assertTrue(entered.wait(1))
            self.frame('detection_budget_exceeded')
            before=time.monotonic()
            self.service._stop_tracking()
            self.assertLess(time.monotonic()-before,.2)
            self.assertEqual(self.port.velocity,(0,0,0,0))
            release.set();n.close()
            self.assertIn('已收到停止指令',emitted[-1])
            self.assertIn('状态#',emitted[-1])
            self.assertLessEqual(len(emitted),2)
        finally:
            release.set();n.close()

    def run_late_detection(self, transition, fail=False):
        entered,release=threading.Event(),threading.Event()
        detect=self.detector.detect
        def blocked(*a,**kw):
            entered.set()
            release.wait(2)
            if fail: raise RuntimeError('old detection')
            return detect(*a,**kw)
        self.detector.detect=blocked
        old=self.service.last_frame_id
        self.clock.now+=.01
        self.seq+=1
        self.port.publish(('',100),self.seq,self.clock.wall())
        worker=threading.Thread(target=self.service.capture_once)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            transition()
        finally:
            release.set();worker.join(1)
            self.detector.detect=detect
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.service.last_frame_id,old)
        self.assertTrue(any(e['event']=='observation_discarded' for e in self.service.events()))

    def test_pre_handoff_detection_cannot_supply_recovery_frame(self):
        tickets=[]
        self.run_late_detection(lambda:tickets.append(self.service.pause_if_active()))
        # Re-reading the discarded cached frame must not relabel it as new.
        before=self.detector.calls
        self.assertFalse(self.service.capture_once())
        self.assertEqual(self.detector.calls,before)
        result=self.service.finish_action(tickets[0],True)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason,'resume_requires_prior_follow_and_new_frame')
        self.assertFalse(self.service.is_following())

    def test_pre_handoff_fetch_cannot_supply_recovery_frame(self):
        original=self.port.get_frame
        entered,release=threading.Event(),threading.Event()
        self.clock.now+=.01;self.seq+=1
        self.port.publish(('',100),self.seq,self.clock.wall())
        def blocked_fetch():
            packet=original();entered.set();release.wait(2);return packet
        self.port.get_frame=blocked_fetch
        worker=threading.Thread(target=self.service.capture_once)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            ticket=self.service.pause_if_active()
        finally:
            release.set();worker.join(1);self.port.get_frame=original
        self.assertFalse(self.service.capture_once())
        self.assertFalse(self.service.finish_action(ticket,True).ok)

    def test_old_result_cannot_overwrite_stop_then_new_start(self):
        def transition():
            self.service._stop_tracking()
            self.assertTrue(self.service._start_tracking().ok)
        self.run_late_detection(transition)
        self.assertTrue(self.service.is_following())
        self.assertEqual(self.service.controller.observation.center_x,500)

    def test_old_exception_cannot_fault_new_start(self):
        def transition():
            self.service._stop_tracking();self.assertTrue(self.service._start_tracking().ok)
        self.run_late_detection(transition,fail=True)
        self.assertTrue(self.service.is_following())


class NoticeThreadTests(unittest.TestCase):
    def test_blocked_output_does_not_block_submit_or_close(self):
        entered,release=threading.Event(),threading.Event()
        def sink(text): entered.set();release.wait(2)
        n=ConsoleNotices(sink)
        try:
            n.start();n.submit('first')
            self.assertTrue(entered.wait(1))
            before=time.monotonic()
            for _ in range(100):n.submit('new status')
            n.close(wait=False)
            self.assertLess(time.monotonic()-before,.2)
            self.assertGreater(n.dropped,0)
        finally:
            release.set();n.close()

    def test_sink_exception_does_not_kill_worker(self):
        seen=threading.Event()
        def sink(text): seen.set();raise OSError('closed stdout')
        n=ConsoleNotices(sink)
        n.start();n.submit('stop')
        self.assertTrue(seen.wait(1));n.close()
        self.assertEqual(n.errors,1)


class CoreCaptureTimeTests(unittest.TestCase):
    def test_new_id_with_equal_capture_time_invalidates_motion(self):
        for profile in ('openpose_pid',):
            with self.subTest(profile=profile):
                controller=FollowController(config=FollowConfig(steering_mode='yaw', control_profile=profile, max_request_rate=None))
                obs=Observation(1,10.,'test',640,480,0,True,500,240,.2)
                self.assertTrue(controller.observe(obs))
                self.assertTrue(controller.calibrate(10.)[0])
                self.assertTrue(controller.start(10.)[0])
                self.assertGreater(controller.tick(10.).yaw,0)
                self.assertFalse(controller.observe(replace(obs,frame_id=2,center_x=100)))
                request=controller.tick(10.)
                self.assertEqual((request.right,request.forward,request.yaw),(0,0,0))
                self.assertEqual(str(controller.state),'invalid_input')


if __name__=='__main__':unittest.main()
