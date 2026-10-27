"""Opt-in Plan A control, original SDK boundaries, clock ordering; no devices."""
from dataclasses import replace
import math
from pathlib import Path
import unittest

from r3_follow.config import FollowConfig, load_config
from r3_follow.core import FollowController, Observation, State
from r3_follow.mock_port import MockPort
from r3_follow.openpose_control import OpenPoseMarkerControl
from r3_follow.service import FollowService
from r3_follow.presentation import motion_text
from r3_follow.depth_brake import DepthBrake

CONFIG = Path(__file__).resolve().parents[1]/'configs/aircraft_A_ID0_plan_a.json'


def obs(seq, t, x=320, size=.2, found=True):
    s=size*640/2
    return Observation(seq,t,'test',640,480,0,found,x,240,size,
        ((x-s,240-s),(x+s,240-s),(x+s,240+s),(x-s,240+s)),
        '' if found else 'not_found')


def pid_call(c,seq=0,t=0.,x=360,depth=.2):
    return c.update(frame_id=seq,acquired_at=t,center_x=x,image_width=640,
        size_ratio=.2/(1+depth),reference_size=.2,depth_error=depth)


class PlanAControlTests(unittest.TestCase):
    def test_small_offset_is_lateral_primary_and_edge_preserves_full_yaw(self):
        for x in (280,360):
            right,_,yaw=pid_call(OpenPoseMarkerControl(steering_mode='adaptive'),x=x)
            self.assertGreater(abs(right),abs(yaw))
            self.assertGreater(right*(x-320),0)
            self.assertGreater(yaw*(x-320),0)
        for x in (100,540):
            actual=pid_call(OpenPoseMarkerControl(steering_mode='adaptive',yaw_limit=.5),x=x)
            yaw=pid_call(OpenPoseMarkerControl(steering_mode='yaw',yaw_limit=.5),x=x)
            self.assertEqual(actual,yaw)

    def test_adaptive_budget_and_depth_match_unsplit_output_for_full_image(self):
        for x in range(0,640,3):
            a=pid_call(OpenPoseMarkerControl(steering_mode='adaptive',yaw_limit=.5),x=x)
            b=pid_call(OpenPoseMarkerControl(steering_mode='yaw',yaw_limit=.5),x=x)
            self.assertAlmostEqual(a[0]+a[2],b[2])
            self.assertLessEqual(abs(a[0])+abs(a[2]),.5+1e-12)
            self.assertEqual(a[1],b[1])
            self.assertGreaterEqual(a[0]*a[2],0)

    def test_midrange_chase_is_stronger_but_same_cap(self):
        for error in (-.3,-.2,.1,.2,.3):
            old=pid_call(OpenPoseMarkerControl(forward_limit=.4),depth=error)[1]
            new=pid_call(OpenPoseMarkerControl(forward_limit=.4,tuning='responsive_v1'),depth=error)[1]
            self.assertGreater(abs(new),abs(old))
            self.assertGreater(new*error,0)
        for error in (-.8,2.,4.):
            self.assertLessEqual(abs(pid_call(OpenPoseMarkerControl(
                forward_limit=.4,tuning='responsive_v1'),depth=error)[1]),.4)

    def test_response_gain_also_updates_braking_strength(self):
        c=FollowController(control_profile='openpose_pid',distance_mode='relative_size',
                           pid_tuning='responsive_v1',forward_limit=.4)
        b=c._depth_brake
        self.assertAlmostEqual(b.proportional_scale,.96)
        for i in range(5):
            result=b.limit(.2,frame_id=i,captured_at=i*.1,now=i*.1+.2,error=.3-i*.02)
        self.assertLess(result,.2)
        self.assertEqual(b.reason,'approach_braking')

    def test_extra_lead_brakes_closing_motion_without_amplifying_departing_target(self):
        for approaching in (True,False):
            old=DepthBrake(.4,proportional_scale=.96)
            new=DepthBrake(.4,proportional_scale=.96,braking_allowance=.40)
            for i in range(5):
                kw=dict(frame_id=i,captured_at=i*.1,now=i*.1+.2,
                        error=.3+(-1 if approaching else 1)*i*.02)
                a=old.limit(.2,**kw);b=new.limit(.2,**kw)
            if approaching:self.assertLess(b,a)
            else:self.assertEqual(b,a)

    def test_bad_braking_settings_fail_before_control(self):
        for value in (True,float('nan'),float('inf'),0,.8):
            with self.assertRaises(ValueError):DepthBrake(braking_allowance=value)

    def test_reference_noise_and_changed_setpoint_do_not_retain_accumulated_chase(self):
        c=OpenPoseMarkerControl(tuning='responsive_v1')
        for i in range(30):pid_call(c,i,i*.05,x=320,depth=1.)
        self.assertEqual(pid_call(c,31,1.55,x=320,depth=0.),(0.,0.,0.))
        result=c.update(frame_id=32,acquired_at=1.60,center_x=320,image_width=640,
            size_ratio=.1,reference_size=.1,depth_error=0.)
        self.assertEqual(result,(0.,0.,0.))
        self.assertEqual(c.reason,'reference_changed_wait_new_frame')

    def test_old_defaults_and_invalid_combinations_remain_explicit(self):
        self.assertEqual(FollowConfig().pid_tuning,'marker_v2')
        for kw in (dict(pid_tuning='fast'),dict(pid_tuning='responsive_v1'),
                   dict(steering_mode='adaptive')):
            with self.assertRaises(ValueError):FollowConfig(**kw)
        c=load_config(CONFIG)
        self.assertEqual(c.steering_mode,'adaptive')
        self.assertIn('横移',motion_text((10,20,0,3),'adaptive'))

    def test_plan_a_service_losses_stop_recovery_and_height_guard(self):
        now=[10.]
        port=MockPort(lambda:now[0]);port.height_fresh=False
        class Detector:
            def detect(self,frame,frame_id,acquired_at,source):
                return replace(frame,frame_id=frame_id,acquired_at=acquired_at,source=source)
        svc=FollowService(port,tracker=Detector(),monotonic=lambda:now[0],
            wall_clock=lambda:1800000000+now[0],recovery_window=30,
            log_directory=False,**load_config(CONFIG).to_dict())
        self.addCleanup(svc.close)
        def send(seq,**kw):
            now[0]=10+seq*.1
            port.publish(obs(seq,now[0],**kw),seq,1800000000+now[0])
            self.assertTrue(svc.capture_once());svc.control_once()
        send(0)
        self.assertTrue(svc.calibrate_reference().ok);self.assertTrue(svc.start_follow().ok)
        for i in range(1,7):send(i,x=360,size=.16)
        self.assertGreater(port.velocity[0],0);self.assertGreater(port.velocity[1],0)
        self.assertEqual(port.velocity[2],0)
        self.assertEqual(svc.vertical_guard_reason,'height_unknown_or_stale')
        send(7,found=False)
        self.assertEqual(port.velocity,(0,0,0,0))
        self.assertEqual(svc.controller.state,State.RECOVERING)
        for i in range(8,15):send(i,x=280,size=.16)
        self.assertEqual(svc.controller.state,State.TRACKING)
        self.assertLess(port.velocity[0],0)
        self.assertTrue(svc.stop_follow().ok)
        for i in range(15,19):send(i,x=400,size=.12)
        self.assertEqual(port.velocity,(0,0,0,0))


class CaptureMappingTests(unittest.TestCase):
    def make(self):
        t=[100.04];epoch=1800000000.
        mono=lambda:math.floor(t[0]/.015625)*.015625
        wall=lambda:epoch+t[0]
        port=MockPort(mono)
        class Detector:
            def detect(self,frame,frame_id,acquired_at,source):
                return replace(obs(frame_id,acquired_at),source=source)
        svc=FollowService(port,tracker=Detector(),monotonic=mono,wall_clock=wall,log_directory=False)
        self.addCleanup(svc.close)
        return t,epoch,mono,wall,port,svc

    def test_coarse_tick_mapping_discards_sample_without_fault_or_refresh(self):
        t,epoch,mono,wall,port,svc=self.make()
        old_mapped=[];new_mapped=[]
        for i,(read,capture) in enumerate(((100.04,100.02),(100.045,100.021),(100.06,100.04))):
            t[0]=read;port.publish(object(),i,epoch+capture)
            old_mapped.append(mono()-(wall()-port.snapshot.timestamp))
            accepted=svc.capture_once()
            self.assertEqual(accepted,i!=1)
            new_mapped.append(svc.controller.observation.acquired_at)
        self.assertLess(old_mapped[1],old_mapped[0], 'Must reproduce old mapping defect')
        self.assertEqual(new_mapped[0],new_mapped[1])
        self.assertGreater(new_mapped[2],new_mapped[1])
        self.assertFalse(any(e['event']=='fault' for e in svc.events))
        self.assertTrue(any(e.get('reason')=='nonadvancing_clock_mapping' for e in svc.events))

    def test_clock_handling_does_not_accept_future_or_expired_timestamps(self):
        for stamp_offset in (.01,-.6):
            t,epoch,mono,wall,port,svc=self.make()
            port.publish(object(),0,wall()-.02);self.assertTrue(svc.capture_once())
            t[0]+=.7;port.publish(object(),1,wall()+stamp_offset)
            self.assertFalse(svc.capture_once());self.assertFalse(svc.frame_ready)

    def test_observation_rejection_records_specific_reason_and_metadata(self):
        t,epoch,mono,wall,port,svc=self.make()
        port.publish(object(),0,wall()-.02);self.assertTrue(svc.capture_once())
        original=svc.tracker.detect
        svc.tracker.detect=lambda *a,**kw:replace(original(*a,**kw),center_x=-1.)
        t[0]+=.1;port.publish(object(),1,wall()-.02)
        self.assertFalse(svc.capture_once())
        record=next(e for e in svc.events if e['event']=='observation_rejected')
        self.assertEqual(record['reason'],'invalid_fields_or_target')
        self.assertEqual(record['previous_frame_id'],0)
        self.assertIn('center_x=-1.0',record['rejected_observation'])


if __name__=='__main__':unittest.main()
