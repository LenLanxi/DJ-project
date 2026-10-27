"""Braking and control scheduling contracts; no hardware or UI."""
import math
import unittest
from unittest.mock import patch
from dataclasses import replace

from r3_follow.depth_brake import DepthBrake
from r3_follow.core import FollowController, MotionRequest, State
from r3_follow.service import FollowService
from r3_follow.mock_port import MockPort
from test_openpose_control import observation
from test_service import Clock, Detector


class TrendTests(unittest.TestCase):
    def trend(self, brake=None, sign=1., age=0.):
        b=brake or DepthBrake(.4)
        values=[]
        for i in range(5):
            t=i*.1
            values.append(b.limit(sign*.2,frame_id=i,captured_at=t,now=t+age,error=sign*(.3-.2*t)))
        return b,values

    def test_closing_distance_reduces_forward_before_reference_crossing(self):
        b,v=self.trend()
        self.assertLess(v[-1],.2)
        self.assertGreater(b.predicted_error,0)
        self.assertEqual(b.reason,'approach_braking')

    def test_retreat_brakes_symmetrically_when_spacing_opens(self):
        _,a=self.trend();_,b=self.trend(sign=-1)
        self.assertEqual(a,[-v for v in b])

    def test_delayed_valid_frame_adds_more_braking_than_fresh_frame(self):
        _,a=self.trend();_,b=self.trend(age=.3)
        self.assertLess(b[-1],a[-1])

    def test_receding_target_does_not_slow_chase(self):
        b=DepthBrake(.4)
        for i in range(5):
            out=b.limit(.2,frame_id=i,captured_at=i*.1,now=i*.1,error=.2+i*.03)
        self.assertGreaterEqual(out,.2)
        self.assertLessEqual(out,.4)

    def test_constant_distance_error_preserves_pid_request(self):
        b=DepthBrake(.4)
        for i in range(20):
            self.assertEqual(b.limit(.18,frame_id=i,captured_at=i*.07,now=i*.07+.3,error=.3),.18)

    def test_noise_does_not_become_consistent_velocity(self):
        b=DepthBrake(.4)
        for i in range(40):
            out=b.limit(.1,frame_id=i,captured_at=i*.04,now=i*.04,error=.3+(.004 if i%2 else -.004))
            self.assertEqual(out,.1)

    def test_single_size_jump_cannot_supply_a_trend(self):
        b=DepthBrake(.4)
        for i,e in enumerate((.3,.3,.3,-.2)):
            out=b.limit(.1,frame_id=i,captured_at=i*.1,now=i*.1,error=e)
        self.assertEqual(out,.1);self.assertIsNone(b.rate)

    def test_duplicate_frames_do_not_build_evidence_or_refresh_capture(self):
        b=DepthBrake(.4)
        for i in range(20):
            self.assertEqual(b.limit(.2,frame_id=1,captured_at=0.,now=i*.02,error=.3),.2)
        self.assertEqual(len(b.samples),1);self.assertIsNone(b.rate)

    def test_changed_duplicate_frame_is_rejected(self):
        b,_=self.trend()
        self.assertEqual(b.limit(.2,frame_id=4,captured_at=.4,now=.45,error=.9),0)
        self.assertEqual(b.reason,'changed_cached_measurement')

    def test_invalid_or_expired_input_clears_history(self):
        for changes in ({'error':None},{'error':float('nan')},{'now':float('inf')},
                        {'captured_at':True},{'request':True},{'now':1.0},{'now':.2},{'frame_id':False}):
            b,_=self.trend()
            args=dict(request=.2,frame_id=5,captured_at=.5,now=.5,error=.2);args.update(changes)
            if changes=={'now':1.0}:args['now']=1.00001
            self.assertEqual(b.limit(**args),0,changes)
            self.assertEqual(len(b.samples),0)

    def test_old_frames_and_nonadvancing_time_are_rejected(self):
        for fid,t in ((2,.2),(5,.3)):
            b,_=self.trend()
            self.assertEqual(b.limit(.2,frame_id=fid,captured_at=t,now=.4,error=.2),0)

    def test_long_capture_gap_needs_new_trend(self):
        b,_=self.trend()
        self.assertEqual(b.limit(.2,frame_id=6,captured_at=1.,now=1.,error=.1),.2)
        self.assertIsNone(b.rate)

    def test_short_intervals_cannot_amplify_differential_noise(self):
        b=DepthBrake(.4)
        for i in range(20):
            self.assertEqual(b.limit(.2,frame_id=i,captured_at=i*.001,now=i*.001,error=.3-i*.002),.2)

    def test_correction_and_final_request_obey_each_aircraft_cap(self):
        for cap in (.05,.3,.4,1.):
            b=DepthBrake(cap)
            for i in range(9):
                out=b.limit(cap,frame_id=i,captured_at=i*.05,now=i*.05+.4,error=1-i*.08)
                self.assertLessEqual(abs(out),cap)
                self.assertLessEqual(abs(b.correction),min(.2,cap*.5))
                self.assertTrue(math.isfinite(out))

    def test_explicit_reset_removes_all_prior_trend(self):
        b,_=self.trend();b.reset('paused')
        self.assertEqual(b.limit(.2,frame_id=6,captured_at=.6,now=.6,error=.2),.2)
        self.assertIsNone(b.rate)

    def test_tiny_reversals_zero_without_removing_small_initial_or_same_direction_requests(self):
        b=DepthBrake(.4)
        for i,(request,expected) in enumerate(((.02,.02),(-.01,0),(-.04,-.04),(-.01,-.01),(.02,0),(.04,.04))):
            self.assertEqual(b.limit(request,frame_id=i,captured_at=i*.1,now=i*.1,error=.3),expected)

    def test_size_plateaus_do_not_create_full_derivative_steps(self):
        b=DepthBrake(.4)
        for i,e in enumerate((.30,.25,.25,.20,.20,.15,.15)):
            out=b.limit(.1,frame_id=i,captured_at=i*.1,now=i*.1+.3,error=e)
            self.assertTrue(math.isfinite(out));self.assertLessEqual(abs(out),.4)
        self.assertIsNotNone(b.rate)
        self.assertLess(b.rate,0)

    def test_small_reversal_filter_clears_on_loss_reset(self):
        b=DepthBrake(.4)
        b.limit(.1,frame_id=0,captured_at=0.,now=0.,error=.3)
        b.reset('lost')
        self.assertEqual(b.limit(-.01,frame_id=1,captured_at=.1,now=.1,error=-.3),-.01)


class ControllerBrakingTests(unittest.TestCase):
    def ready(self):
        c=FollowController(control_profile='openpose_pid',distance_mode='relative_size',
            max_request_rate=.8,forward_limit=.4,require_geometry=True,recovery_window=3.)
        c.observe(observation(0,0));c.calibrate(0);c.start(0)
        return c

    def test_same_direction_deceleration_bypasses_ramp_but_acceleration_does_not(self):
        c=self.ready();c._motion_values=(0.,.3,0.,0.);c._motion_at=.0
        # Forward ramp tests require an actually farther target. At the
        # reference, the close-target guard correctly overrides a mocked PID.
        c.observe(observation(1,.01,size=.15))
        with patch.object(c._openpose,'update',return_value=(0.,.1,0.)),patch.object(c._depth_brake,'limit',side_effect=lambda request,**kw:request):
            self.assertEqual(c.tick(.02).forward,.1)
        with patch.object(c._openpose,'update',return_value=(0.,.3,0.)),patch.object(c._depth_brake,'limit',side_effect=lambda request,**kw:request):
            self.assertAlmostEqual(c.tick(.04).forward,.116)

    def test_predicted_reversal_has_zero_before_opposite_ramp(self):
        c=self.ready();c._motion_values=(0.,.2,0.,0.);c._motion_at=.0
        with patch.object(c._depth_brake,'limit',return_value=-.1):
            self.assertEqual(c.tick(.02).forward,0)
            self.assertAlmostEqual(c.tick(.04).forward,-.016)

    def test_shape_rejection_resets_trend_and_zeroes_depth(self):
        c=self.ready()
        for i in range(1,7):c.observe(observation(i,i*.06,size=.15+i*.004));c.tick(i*.06)
        self.assertGreater(len(c._depth_brake.samples),0)
        c.observe(observation(7,.42,narrow=True));r=c.tick(.42)
        self.assertEqual(r.forward,0);self.assertEqual(len(c._depth_brake.samples),0)

    def test_disabling_acceleration_limit_does_not_bypass_zero_crossing(self):
        c=self.ready();c.max_request_rate=None
        c.observe(observation(1,.01,size=.15))
        with patch.object(c._depth_brake,'limit',return_value=.2):self.assertEqual(c.tick(.02).forward,.2)
        with patch.object(c._depth_brake,'limit',return_value=-.1):
            self.assertEqual(c.tick(.04).forward,0)
            self.assertEqual(c.tick(.06).forward,-.1)

    def test_loss_stop_pause_stale_and_recalibration_do_not_keep_trend(self):
        for action in ('loss','stop','pause','stale','reference'):
            c=self.ready()
            for i in range(1,7):c.observe(observation(i,i*.06,size=.15+i*.004));c.tick(i*.06)
            if action=='loss':c.observe(observation(7,.42,found=False));c.tick(.42)
            elif action=='stop':c.stop()
            elif action=='pause':c.pause()
            elif action=='reference':c.pause();c.calibrate(.36)
            else:c.tick(.9)
            self.assertEqual(c.last_request.forward,0,action)
            self.assertEqual(len(c._depth_brake.samples),0,action)


class SendSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock();self.port=MockPort(self.clock.mono)
        self.s=FollowService(self.port,tracker=Detector(),control_profile='openpose_pid',
            forward_limit=.4,yaw_limit=.5,monotonic=self.clock.mono,
            wall_clock=self.clock.wall,log_directory=False)
        self.addCleanup(self.s.close)
        self.port.publish((True,320),0,self.clock.wall());self.s.capture_once()
        self.assertTrue(self.s.calibrate_reference().ok);self.assertTrue(self.s.start_follow().ok)
        self.start=self.clock.now+.1

    def send(self,dt,forward,yaw):
        self.clock.now=self.start+dt
        motion=MotionRequest(State.TRACKING,forward=forward,yaw=yaw)
        with patch.object(self.s.controller,'tick',return_value=motion):self.s.control_once()

    def test_smaller_forward_request_is_sent_before_regular_deadline(self):
        self.send(0,.3,.1);self.send(.02,.18,.3)
        self.assertEqual(self.port.velocity,(0,18,0,10))

    def test_repeated_braking_cannot_starve_yaw_regular_updates(self):
        self.send(0,.3,.1)
        for i in range(1,5):self.send(i*.02,.3-i*.02,.3)
        self.assertEqual(self.port.velocity[3],10)
        self.send(.1,.2,.3)
        self.assertEqual(self.port.velocity,(0,20,0,30))

    def test_reverse_and_acceleration_are_not_sent_early(self):
        self.send(0,.2,.1);self.send(.02,.3,-.1)
        self.assertEqual(self.port.velocity,(0,20,0,10))
        self.send(.04,-.1,-.1)
        self.assertEqual(self.port.velocity,(0,20,0,10))

    def test_total_zero_is_immediate_and_fresh_chase_resumes_at_regular_tick(self):
        self.send(0,.2,.1);self.send(.02,0,0)
        self.assertEqual(self.port.velocity,(0,0,0,0))
        self.send(.1,.2,.1)
        self.assertEqual(self.port.velocity,(0,20,0,10))

    def test_control_loss_blocks_even_an_urgent_deceleration(self):
        self.send(0,.2,.1)
        self.port.release_control(self.s.lease)
        other=self.port.request_control('model').lease
        self.port.update_velocity(other,left_right=7,forward_back=0,up_down=0,yaw=0)
        self.send(.02,.1,.1)
        self.assertEqual(self.port.velocity,(7,0,0,0))
        self.assertEqual(len(self.s.controller._depth_brake.samples),0)


if __name__=='__main__':unittest.main()
