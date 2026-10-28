"""Functional acceptance of vertical following, including the sending boundary.

These tests use actual marker pixels and simulated ports. They do not prove
altitude, braking, obstacle avoidance, or the response of a flying Tello.
"""
from dataclasses import replace
import math
import unittest
from unittest.mock import patch

from camera_pid_bench import make_service, phase_at, VERTICAL_PHASES
from r3_follow.config import FollowConfig, load_config
from r3_follow.core import FollowController, MotionRequest, State
from r3_follow.openpose_control import OpenPoseMarkerControl, THROTTLE_GAINS
from r3_follow._vendor.simple_pid import PID
from r3_follow.service_ports import Result
from r3_follow.tello_adapter import preview_rc
from r3_follow.vision import synthetic_frame
from test_horizontal_routing import observation


def values(request):
    return request.right, request.forward, request.up, request.yaw


def vertical_observation(seq, now, y=240, **kw):
    obs = observation(seq, now, **kw)
    if not obs.found:
        return obs
    return replace(obs, center_y=y,
                   corners=tuple((x, cy+y-240) for x, cy in obs.corners))


class VerticalCalculationTests(unittest.TestCase):
    def test_exact_upstream_throttle_equation_with_declared_cap(self):
        original = PID(.4, 0, 0, setpoint=0, sample_time=None, output_limits=(-80,100))
        self.assertEqual(THROTTLE_GAINS, (.4, 0, 0))
        c = OpenPoseMarkerControl(vertical_follow=True)
        for i, y in enumerate((240, 230, 200, 120, 20, 240, 260, 360, 460)):
            c.update(frame_id=i, acquired_at=i*.1, center_x=320, image_width=640,
                center_y=y, image_height=480, size_ratio=.2, reference_size=.2)
            upstream = int(-original(int(240-y), dt=.1))
            self.assertEqual(round(c.vertical_request*100), max(-20,min(20,upstream)))
        self.assertFalse(c.status()['vertical']['altitude_limits_enforced'])

    def test_resolution_normalization_and_image_midheight(self):
        for width,height in ((640,480),(960,720),(1280,960),(1280,720)):
            c=OpenPoseMarkerControl(vertical_follow=True)
            # Equal offset in the upstream 640-wide pixel space -> equal request.
            for i,offset in enumerate((-25,0,25)):
                c.update(frame_id=i, acquired_at=i*.1, center_x=width/2, image_width=width,
                    center_y=height/2-offset*width/640, image_height=height,
                    size_ratio=.2, reference_size=.2)
                self.assertAlmostEqual(c.vertical_request,offset*.4/100)

    def test_explicit_enable_and_legacy_config_compatibility(self):
        base=dict(instance_id='A',target_id=0,dictionary='DICT_4X4_50',control_profile='openpose_pid')
        self.assertFalse(load_config(base).vertical_follow)
        self.assertTrue(load_config(dict(base,vertical_follow=True)).vertical_follow)
        self.assertFalse(FollowConfig().vertical_follow)
        for bad in (None,1,'true',[],.2):
            with self.assertRaises(ValueError):load_config(dict(base,vertical_follow=bad))
            with self.assertRaises(ValueError):FollowController(vertical_follow=bad)
            with self.assertRaises(ValueError):OpenPoseMarkerControl(vertical_follow=bad)
        with self.assertRaises(ValueError):FollowController(vertical_follow=True)
        with self.assertRaises(ValueError):FollowConfig(vertical_follow=True)
        c=OpenPoseMarkerControl()
        c.update(frame_id=0,acquired_at=0.,center_x=400,image_width=640,
                 size_ratio=.15,reference_size=.2)
        self.assertEqual(c.vertical_request,0)

    def test_same_frame_is_not_recomputed_and_invalid_vertical_clears_all_history(self):
        c=OpenPoseMarkerControl(vertical_follow=True)
        args=dict(frame_id=0,acquired_at=0.,center_x=400,image_width=640,
                  center_y=180,image_height=480,size_ratio=.15,reference_size=.2)
        c.update(**args);self.assertGreater(c.vertical_request,0)
        c.update(**{**args,'center_y':300})
        self.assertGreater(c.vertical_request,0)
        for field,val in (('center_y',float('nan')),('center_y',-1),('center_y',480),
                          ('image_height',0),('center_y',True),('image_height',None)):
            with self.assertRaises(ValueError):c.update(**{**args,field:val})
            self.assertEqual(c.vertical_request,0)
            self.assertEqual(c.status()['raw_normalized_request'],(0,0,0))

    def test_ramp_direction_change_stop_and_faults_include_vertical(self):
        for mode in ('yaw','lateral','hybrid'):
            for end in ('stop','pause','stale','wrong_id','invalid'):
                with self.subTest(mode=mode,end=end):
                    c=FollowController(control_profile='openpose_pid',steering_mode=mode,
                        vertical_follow=True,max_request_rate=.8,require_geometry=True)
                    c.observe(vertical_observation(0,0));self.assertTrue(c.calibrate(0)[0])
                    c.start(0)
                    for seq in range(1,6):
                        c.observe(vertical_observation(seq,seq*.1,y=180,x=.7,size=.15))
                        c.tick(seq*.1)
                    self.assertGreater(c.last_request.up,0)
                    c.observe(vertical_observation(6,.6,y=300,x=.3,size=.3))
                    self.assertEqual(c.tick(.6).up,0) # brake before reversing
                    c.observe(vertical_observation(7,.7,y=300,x=.3,size=.3))
                    self.assertLess(c.tick(.7).up,0)
                    if end in ('stop','pause'):r=getattr(c,end)()
                    elif end=='stale':r=c.tick(1.71)
                    else:
                        c.observe(None if end=='invalid' else vertical_observation(8,.8,target_id=1))
                        r=c.tick(.8)
                    self.assertEqual(values(r),(0,0,0,0))
                    self.assertEqual(c.status()['openpose_control']['vertical']['raw_normalized_request'],0)

    def test_preview_transmits_up_and_rejects_nonfinite_up(self):
        request=MotionRequest(State.TRACKING,.1,-.2,'test',True,.15,.2)
        rc=preview_rc(request,full_scale=100)
        self.assertEqual((rc.left_right,rc.forward_backward,rc.up_down,rc.yaw),(10,-20,20,15))
        for v in (math.inf,math.nan,.31):
            with self.assertRaises(ValueError):preview_rc(replace(request,up=v),full_scale=100)
        self.assertEqual(preview_rc(replace(request,state=State.STOPPED),full_scale=100).up_down,0)

    def test_enabled_camera_test_has_both_vertical_directions_and_stops(self):
        self.assertEqual(sum(p[2] for p in VERTICAL_PHASES),40)
        self.assertIn('抬高',phase_at(25,True)[1])
        self.assertIn('放低',phase_at(30,True)[1])
        self.assertEqual(phase_at(45,True)[1],'完成')
        self.assertEqual(phase_at(35)[1],'完成')


class Clock:
    now=100.
    def mono(self):return self.now
    def wall(self):return 1800000000.+self.now

class Reader:
    latest=None
    def snapshot(self):return self.latest,''

class VerticalServiceTests(unittest.TestCase):
    def setUp(self):
        for name in ('socket.socket','cv2.VideoCapture','tkinter.Tk'):
            guard=patch(name,side_effect=AssertionError('external action forbidden'))
            guard.start();self.addCleanup(guard.stop)

    def instance(self,mode='hybrid',target_id=0,vertical=True):
        clock,reader=Clock(),Reader()
        port,service=make_service(reader,target_id,steering_mode=mode,vertical_follow=vertical,
                                 monotonic=clock.mono,wall=clock.wall)
        # Advance detector/control time together; don't turn host scheduling
        # jitter into a synthetic target loss in this channel-direction test.
        service.tracker._clock = clock.mono
        self.addCleanup(service.close)
        def frame(*,x=.5,y=.5,size=.25,visible=True,marker_id=target_id,dt=.1):
            clock.now+=dt
            seq=max(1,service.last_received_frame_id+1)
            reader.latest=(seq,clock.now,synthetic_frame(x,size,visible,marker_id,y=y))
            service.capture_once();service.control_once()
        frame();self.assertTrue(service.calibrate_reference().ok)
        self.assertTrue(service.start_follow().ok)
        return clock,reader,port,service,frame

    def test_pixels_to_all_four_channels_and_all_three_modes(self):
        for mode in ('yaw','lateral','hybrid'):
            clock,reader,port,service,frame=self.instance(mode)
            for _ in range(6):frame(x=.7,y=.3,size=.18)
            right,forward,up,yaw=port.velocity
            self.assertGreater(forward,0);self.assertGreater(up,0)
            self.assertGreater(right,0) if mode!='yaw' else self.assertEqual(right,0)
            self.assertGreater(yaw,0) if mode!='lateral' else self.assertEqual(yaw,0)
            self.assertLessEqual(abs(up),20);self.assertLessEqual(abs(forward),30)
            self.assertLessEqual(abs(right)+abs(yaw),20)
            for _ in range(8):frame(x=.3,y=.7,size=.32)
            self.assertLess(port.velocity[1],0);self.assertLess(port.velocity[2],0)
            self.assertLessEqual(port.velocity[0],0);self.assertLessEqual(port.velocity[3],0)
            self.assertTrue(service.get_follow_status()['vertical_follow'])
            events=[e for e in service.events if e['event']=='motion_request']
            self.assertTrue(any(e['channels'][2] for e in events))
            self.assertTrue(events[-1]['openpose_control']['vertical']['enabled'])
            self.assertTrue(service.get_follow_status()['altitude_limits_enforced'])

    def test_old_config_does_not_silently_enable_vertical(self):
        _,_,port,service,frame=self.instance(vertical=False)
        for _ in range(6):frame(y=.3,size=.18)
        self.assertGreater(port.velocity[1],0);self.assertEqual(port.velocity[2],0)
        self.assertFalse(service.get_follow_status()['vertical_follow'])

    def test_vertical_zero_bypasses_send_period_without_increasing_other_axes(self):
        clock,_,port,service,frame=self.instance()
        for _ in range(6):frame(y=.3,x=.6,size=.18)
        old=port.velocity;self.assertGreater(old[2],0)
        frame(y=.5,x=.8,size=.15,dt=.02)
        self.assertEqual(port.velocity[2],0)
        for i in (0,1,3):self.assertLessEqual(abs(port.velocity[i]),abs(old[i]))
        self.assertAlmostEqual(service.last_send,clock.now)

    def test_loss_zero_then_recovery_uses_new_vertical_direction(self):
        _,_,port,service,frame=self.instance()
        for _ in range(5):frame(y=.3)
        self.assertGreater(port.velocity[2],0)
        frame(visible=False);self.assertEqual(port.velocity,(0,0,0,0))
        self.assertTrue(service.get_follow_status()['recovering'])
        for _ in range(2):
            frame(y=.7);self.assertEqual(port.velocity,(0,0,0,0))
        for _ in range(5):frame(y=.7)
        self.assertTrue(service.is_following());self.assertLess(port.velocity[2],0)

    def test_stop_stale_long_loss_and_send_failure_latch_every_axis(self):
        for event in ('stop','stale','long_loss','send_failure'):
            with self.subTest(event=event):
                clock,_,port,service,frame=self.instance()
                for _ in range(5):frame(x=.7,y=.3,size=.18)
                self.assertGreater(port.velocity[2],0)
                if event=='stop':service.stop_follow()
                elif event=='stale':clock.now+=1.01;service.control_once()
                elif event=='long_loss':
                    for _ in range(34):frame(visible=False)
                else:
                    with patch.object(port,'update_velocity',return_value=Result('failed','test_send')):
                        frame(y=.3)
                self.assertEqual(port.velocity,(0,0,0,0))
                for _ in range(5):frame(y=.3)
                self.assertFalse(service.is_following())
                self.assertEqual(port.velocity,(0,0,0,0))

    def test_handoff_only_resumes_after_completed_with_fresh_target(self):
        _,_,port,service,frame=self.instance()
        for _ in range(5):frame(y=.3)
        result,ticket=service.pause_for_action();self.assertTrue(result.ok)
        self.assertEqual(port.velocity,(0,0,0,0))
        self.assertEqual(service.finish_action(ticket,'accepted').status,'accepted')
        for _ in range(4):frame(y=.7)
        self.assertEqual(port.velocity,(0,0,0,0))
        self.assertTrue(service.finish_action(ticket,'completed').ok)
        for _ in range(4):frame(y=.7)
        self.assertLess(port.velocity[2],0)

    def test_late_completion_after_stop_cannot_restart_vertical(self):
        _,_,port,service,frame=self.instance()
        for _ in range(5):frame(y=.3)
        _,ticket=service.pause_for_action()
        service.stop_follow()
        for _ in range(4):frame(y=.3)
        self.assertEqual(service.finish_action(ticket,'completed').reason,'obsolete_ticket')
        self.assertEqual(port.velocity,(0,0,0,0))

    def test_owner_revocation_preserves_new_owners_vertical_command(self):
        _,_,port,service,frame=self.instance()
        for _ in range(5):frame(y=.3)
        lease=port.revoke_for_test('model')
        port.update_velocity(lease,left_right=1,forward_back=2,up_down=-7,yaw=3)
        for _ in range(5):frame(y=.3)
        self.assertFalse(service.is_following())
        self.assertEqual(port.velocity,(1,2,-7,3))

    def test_three_ids_cannot_borrow_another_targets_height(self):
        for target_id in range(3):
            _,_,port,service,frame=self.instance(target_id=target_id)
            for _ in range(5):frame(y=.3)
            self.assertGreater(port.velocity[2],0)
            for _ in range(5):frame(y=.7,marker_id=(target_id+1)%3)
            self.assertFalse(service.get_follow_status()['target_visible'])
            self.assertEqual(port.velocity,(0,0,0,0))

    def test_duplicate_id_scene_does_not_authorize_height_follow(self):
        clock,reader,port,service,frame=self.instance()
        for _ in range(5):frame(y=.3)
        first=synthetic_frame(x=.25,y=.3,size=.15)
        second=synthetic_frame(x=.75,y=.7,size=.15)
        import numpy as np
        clock.now+=.1
        reader.latest=(service.last_received_frame_id+1,clock.now,np.minimum(first,second))
        service.capture_once();service.control_once()
        self.assertEqual(port.velocity,(0,0,0,0))
        self.assertFalse(service.is_following())


if __name__=='__main__':unittest.main()
