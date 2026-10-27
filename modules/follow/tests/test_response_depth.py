"""Measurement/control failure cases; no camera, aircraft or network."""
from dataclasses import replace
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import threading

import cv2
import numpy as np

from r3_follow.config import FollowConfig
from r3_follow.core import FollowController, Observation, State
from r3_follow.openpose_control import OpenPoseMarkerControl
from r3_follow.pose_depth import PoseDepth
from r3_follow.service import FollowService
from r3_follow.service_ports import DeviceStatus
from r3_follow.mock_port import MockPort
from test_horizontal_routing import observation
from test_service import Clock, Detector


def calibration(path):
    value = dict(schema='r3_camera_calibration_v1', camera_id='synthetic-camera',
                 image_size=[640,480], camera_matrix=[[500,0,320],[0,500,240],[0,0,1]],
                 dist_coeffs=[0,0,0,0,0], rms_px=0., source='synthetic_not_hardware')
    path.write_text(json.dumps(value),encoding='utf8')
    return value


def projected(seq, t, depth=2., angle=0., x=0., y=0.):
    side=.24; h=side/2
    points=np.array([[-h,h,0],[h,h,0],[h,-h,0],[-h,-h,0]])
    a=math.radians(angle)
    R=np.array([[math.cos(a),0,math.sin(a)],[0,1,0],[-math.sin(a),0,math.cos(a)]])@np.diag([1.,-1.,-1.])
    corners=cv2.projectPoints(points,cv2.Rodrigues(R)[0],np.array([x,y,depth]),
        np.array([[500.,0,320],[0,500.,240],[0,0,1]]),np.zeros(5))[0].reshape(4,2)
    size=float(np.linalg.norm(np.roll(corners,-1,axis=0)-corners,axis=1).mean()/640)
    center=corners.mean(axis=0)
    return Observation(seq,t,'synthetic',640,480,0,True,float(center[0]),float(center[1]),size,
                       tuple(map(tuple,corners.tolist())))


class RelativeDepthTests(unittest.TestCase):
    def test_equal_relative_distance_has_equal_response_at_different_reference_sizes(self):
        outputs=[]
        for reference in (.08,.12,.18,.25):
            c=FollowController(control_profile='openpose_pid',distance_mode='relative_size')
            c.observe(observation(0,0,size=reference));self.assertTrue(c.calibrate(0)[0]);c.start(0)
            series=[]
            for seq in range(1,21):
                c.observe(observation(seq,seq*.1,size=reference*2/3))
                series.append(c.tick(seq*.1).forward)
            outputs.append(series)
        np.testing.assert_allclose(outputs,[outputs[0]]*4,atol=.01)
        self.assertGreater(outputs[0][-1],.2)

    def test_far_near_reference_loss_and_stop(self):
        c=FollowController(control_profile='openpose_pid',distance_mode='relative_size',max_request_rate=2.)
        c.observe(observation(0,0));c.calibrate(0);c.start(0)
        for seq,size in enumerate((.15,.15,.15,.3,.3,.3,.2),1):
            c.observe(observation(seq,seq*.1,size=size));r=c.tick(seq*.1)
            self.assertTrue(math.isfinite(r.forward) and abs(r.forward)<=.3)
            if seq==3:self.assertGreater(r.forward,0)
            if seq==6:self.assertLess(r.forward,0)
        self.assertEqual(r.forward,0)
        self.assertEqual(c.stop().forward,0)
        c.observe(observation(8,.8,size=.12));self.assertEqual(c.tick(.8).forward,0)

    def test_legacy_pixel_calculation_is_still_available(self):
        c=OpenPoseMarkerControl()
        c.update(frame_id=0,acquired_at=0.,center_x=320,image_width=640,
                 size_ratio=.1,reference_size=.2)
        self.assertIsNone(c.status()['relative_depth_error'])
        for error in (math.nan,math.inf,True,'1'):
            with self.assertRaises(ValueError):
                c.update(frame_id=1,acquired_at=.1,center_x=320,image_width=640,
                    size_ratio=.1,reference_size=.2,depth_error=error)
            self.assertEqual(c.status()['raw_normalized_request'],(0,0,0))

    def test_resolution_change_requires_new_session(self):
        c=FollowController(control_profile='openpose_pid',distance_mode='relative_size')
        c.observe(observation(0,0));c.calibrate(0);c.start(0)
        self.assertFalse(c.observe(replace(observation(1,.1),width=1280,height=960)))
        self.assertEqual(c.tick(.1).state,State.INVALID)
        self.assertEqual(c.last_request.forward,0)

    def test_low_rate_fresh_frames_can_recover_but_real_gap_stops(self):
        for interval in (.1,.2,.3,.4):
            c=FollowController(control_profile='openpose_pid',distance_mode='relative_size',
                               require_geometry=True,recovery_window=3.)
            c.observe(observation(0,0));c.calibrate(0);c.start(0)
            c.observe(observation(1,.1,found=False));self.assertEqual(c.tick(.1).forward,0)
            for seq in (2,3,4):
                t=.1+(seq-1)*interval;c.observe(observation(seq,t,size=.15));c.tick(t)
                if seq<4:self.assertEqual(c.last_request.forward,0)
            self.assertEqual(c.state,State.TRACKING)
            self.assertGreater(c.last_request.forward,0)
            c.observe(observation(5,t+.51,size=.15));self.assertEqual(c.tick(t+.51).state,State.STALE)
            self.assertEqual(c.last_request.forward,0)


class PoseDepthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.file=Path(self.temp.name)/'camera.json';self.data=calibration(self.file)
        self.pose=PoseDepth(self.file,.24,camera_id='synthetic-camera')

    def test_side_turn_does_not_look_farther_and_real_depth_change_remains_visible(self):
        for depth in (1.,2.,3.):
            for angle in (0,30,45,60,-45):
                with self.subTest(depth=depth,angle=angle):
                    estimate=self.pose.estimate(projected(0,0,depth,angle))
                    self.assertTrue(estimate.valid,estimate.reason)
                    self.assertAlmostEqual(estimate.depth_m,depth,places=5)

    def test_closed_controller_moves_forward_while_sideways_and_zero_at_reference(self):
        c=FollowController(control_profile='openpose_pid',distance_mode='pose',
                           pose_estimator=self.pose,require_geometry=True)
        c.observe(projected(0,0));self.assertTrue(c.calibrate(0)[0]);c.start(0)
        for seq in range(1,5):
            c.observe(projected(seq,seq*.1,2,45));r=c.tick(seq*.1)
            self.assertEqual(r.forward,0)
        c.observe(projected(5,.5,2.5,45));self.assertGreater(c.tick(.5).forward,0)
        c.observe(projected(6,.6,1.5,45))
        # Reversing a depth command now sends zero even without a configured
        # acceleration ramp; the next control tick may request retreat.
        self.assertEqual(c.tick(.6).forward,0)
        self.assertLess(c.tick(.62).forward,0)

    def test_no_fallback_to_apparent_size_when_pose_fails(self):
        c=FollowController(control_profile='openpose_pid',distance_mode='pose',
                           pose_estimator=self.pose,require_geometry=True)
        c.observe(projected(0,0));c.calibrate(0);c.start(0)
        c.observe(projected(1,.1,2.5));self.assertGreater(c.tick(.1).forward,0)
        c.observe(replace(projected(2,.2,2.5),corners=()))
        self.assertEqual(c.tick(.2).forward,0)
        self.assertFalse(c.distance_status()['forward_allowed'])

    def test_reject_too_small_edge_distorted_size_mismatch_and_hidden(self):
        observations=[projected(1,.1,8), projected(1,.1,2,85),
            replace(projected(1,.1),width=1280),replace(projected(1,.1),found=False),
            replace(projected(1,.1),corners=((1,1),(50,1),(50,50),(1,50))),
            replace(projected(1,.1),corners=((10,10),(100,100),(100,10),(10,100)))]
        for obs in observations:self.assertFalse(self.pose.estimate(obs).valid)

    def test_noise_is_not_reported_as_exact_and_failed_solver_stops(self):
        rng=np.random.default_rng(13)
        errors=[]
        for seq in range(100):
            obs=projected(seq,seq*.1,2.,45)
            noisy=np.array(obs.corners)+rng.normal(0,.4,(4,2))
            estimate=self.pose.estimate(replace(obs,corners=tuple(map(tuple,noisy.tolist()))))
            if estimate.valid:errors.append(abs(estimate.depth_m-2.))
        self.assertGreater(len(errors),90)
        self.assertLess(np.percentile(errors,95),.08) # synthetic noise only

    def test_calibration_identity_numeric_and_quality_errors_rejected(self):
        with self.assertRaises(ValueError):PoseDepth(self.file,.24,camera_id='other-camera')
        for field,bad in [('rms_px',5),('rms_px',True),('image_size',[640,True]),
                          ('camera_matrix',[[0,0,320],[0,500,240],[0,0,1]]),
                          ('dist_coeffs',[float('nan')]*5)]:
            self.file.write_text(json.dumps({**self.data,field:bad}))
            with self.assertRaises(ValueError):PoseDepth(self.file,.24,camera_id='synthetic-camera')

    def test_pose_config_requires_complete_explicit_calibration(self):
        with self.assertRaises(ValueError):FollowConfig(control_profile='openpose_pid',distance_mode='pose')
        with self.assertRaises(ValueError):FollowConfig(camera_id='unused')
        for value in (None,False,'bad'):
            with self.assertRaises(ValueError):FollowConfig(distance_mode=value)


class ResponseServiceTests(unittest.TestCase):
    def test_camera_bench_records_effective_delivered_configuration(self):
        from camera_pid_bench import make_service
        from r3_follow.config import load_config
        class Reader:
            def snapshot(self):return None,''
        config=load_config(Path(__file__).resolve().parents[1]/'configs/aircraft_C_ID2.json')
        port,s=make_service(Reader(),config=config)
        self.addCleanup(s.close)
        record=next(v for v in s.events if v['event']=='camera_bench_context')
        self.assertEqual(record['effective_config']['target_id'],2)
        self.assertTrue(record['vertical_follow'])
        self.assertEqual(record['effective_config']['forward_limit'],config.forward_limit)
        self.assertEqual(s.config,config)

    def test_real_height_needs_acquisition_time_not_query_time(self):
        from r3_follow.team2_adapter import Team2StubPort
        port = Team2StubPort.__new__(Team2StubPort)
        port.lock = threading.RLock();port.epoch=0;port.lease=None
        status = SimpleNamespace(control_owner='none',connected=True,flight_state='flying',
            timestamp=100.,height_cm=120,simulated=False)
        port.sdk = SimpleNamespace(get_status=lambda: status)
        with patch('r3_follow.team2_adapter.time.time',return_value=100.):
            self.assertFalse(port.get_status().height_fresh)
            for stamp,valid in ((99.6,True),(99.4,False),(101,False),(True,False),(float('nan'),False)):
                status.height_timestamp=stamp
                self.assertEqual(port.get_status().height_fresh,valid)
            status.simulated=True
            self.assertTrue(port.get_status().height_fresh)
            status.connected=False
            self.assertFalse(port.get_status().height_fresh)

    def service(self):
        clock=Clock();port=MockPort(clock.mono)
        s=FollowService(port,tracker=Detector(),monotonic=clock.mono,wall_clock=clock.wall,
            recovery_window=3,control_profile='openpose_pid',distance_mode='relative_size',
            vertical_follow=True,log_directory=False)
        self.addCleanup(s.close)
        def frame(seq,found=True):
            clock.now+=.1;port.publish((found,400),seq,clock.wall());s.capture_once();s.control_once()
        frame(1);self.assertTrue(s.calibrate_reference().ok);self.assertTrue(s.start_follow().ok)
        return s,port,clock,frame

    def test_height_guard_rejects_unknown_and_only_blocks_outward_direction(self):
        s,port,clock,frame=self.service()
        for height,fresh,up,expected in [(None,False,20,0),(120,False,-20,0),
                (190,True,20,0),(190,True,-20,-20),(60,True,-20,0),(60,True,20,20),
                (120,True,-20,-20),(120,True,20,20),(math.nan,True,20,0)]:
            status=DeviceStatus(True,True,port.lease,height,fresh)
            self.assertEqual(s._guard_vertical((5,8,up,7),status),(5,8,expected,7))

    def test_recovering_handoff_preserves_intent_and_requires_new_valid_target(self):
        s,port,clock,frame=self.service();frame(2,False)
        self.assertEqual(s.controller.state,State.RECOVERING)
        self.assertFalse(s.is_following());self.assertTrue(s.get_follow_status()['handoff_required'])
        result,ticket=s.pause_for_action();self.assertTrue(result.ok);self.assertTrue(ticket.was_following)
        self.assertIsNone(port.lease);self.assertEqual(port.velocity,(0,0,0,0))
        self.assertEqual(s.finish_action(ticket,'accepted').status,'accepted')
        frame(3,True)
        self.assertTrue(s.finish_action(ticket,'completed').ok)
        self.assertEqual(s.controller.state,State.TRACKING)

    def test_stale_or_busy_resume_never_steals_or_sends_old_motion(self):
        for outcome in ('busy','stale','failed','stopped'):
            with self.subTest(outcome=outcome):
                s,port,clock,frame=self.service();_,ticket=s.pause_for_action();frame(2)
                if outcome=='busy':
                    lease=port.request_control('model').lease
                    port.update_velocity(lease,left_right=9,forward_back=0,up_down=0,yaw=0)
                if outcome=='stale':clock.now+=.6
                if outcome=='stopped':s.stop_follow()
                r=s.finish_action(ticket,'failed' if outcome=='failed' else 'completed')
                self.assertFalse(r.ok)
                self.assertEqual(port.velocity,(9,0,0,0) if outcome=='busy' else (0,0,0,0))

    def test_horizontal_rc_cap_matches_lateral_controller_budget(self):
        s=FollowService(MockPort(),control_profile='openpose_pid',steering_mode='lateral',
                        yaw_limit=.5,forward_limit=.3,log_directory=False)
        self.addCleanup(s.close)
        self.assertEqual(s.rc_caps,(50,30,0,50))
