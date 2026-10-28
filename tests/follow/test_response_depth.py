from tests.helpers.FollowFixture import make_controller as FollowController
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

from follow.r3_follow.config import FollowConfig
from follow.r3_follow.core import Observation, State
from follow.r3_follow.openpose_control import OpenPoseMarkerControl
from follow.r3_follow.pose_depth import PoseDepth
from follow.r3_follow import FollowService
from tests.helpers.FollowFixture import FakeDevice
from FollowFixtures import horizontal_observation as observation
from FollowFixtures import Clock, Detector


from FollowFixtures import calibration


from FollowFixtures import projected


class RelativeDepthTests(unittest.TestCase):
    def test_equal_relative_distance_has_equal_response_at_different_reference_sizes(self):
        outputs=[]
        for reference in (.08,.12,.18,.25):
            c=FollowController(config=FollowConfig(control_profile='openpose_pid', distance_mode='relative_size', max_request_rate=None))
            c.observe(observation(0,0,size=reference));self.assertTrue(c.calibrate(0)[0]);c.start(0)
            series=[]
            for seq in range(1,21):
                c.observe(observation(seq,seq*.1,size=reference*2/3))
                series.append(c.tick(seq*.1).forward)
            outputs.append(series)
        np.testing.assert_allclose(outputs,[outputs[0]]*4,atol=.01)
        self.assertGreater(outputs[0][-1],.2)

    def test_far_near_reference_loss_and_stop(self):
        c=FollowController(config=FollowConfig(control_profile='openpose_pid', distance_mode='relative_size', max_request_rate=2.))
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
        c=OpenPoseMarkerControl(FollowConfig())
        c.update(frame_id=0,acquired_at=0.,center_x=320,image_width=640,
                 size_ratio=.1,reference_size=.2)
        self.assertIsNone(c.status()['relative_depth_error'])
        for error in (math.nan,math.inf,True,'1'):
            with self.assertRaises(ValueError):
                c.update(frame_id=1,acquired_at=.1,center_x=320,image_width=640,
                    size_ratio=.1,reference_size=.2,depth_error=error)
            self.assertEqual(c.status()['raw_normalized_request'],(0,0,0))

    def test_resolution_change_requires_new_session(self):
        c=FollowController(config=FollowConfig(control_profile='openpose_pid', distance_mode='relative_size', max_request_rate=None))
        c.observe(observation(0,0));c.calibrate(0);c.start(0)
        self.assertFalse(c.observe(replace(observation(1,.1),width=1280,height=960)))
        self.assertEqual(c.tick(.1).state,State.INVALID)
        self.assertEqual(c.last_request.forward,0)

    def test_low_rate_fresh_frames_can_recover_but_real_gap_stops(self):
        for interval in (.1,.2,.3,.4):
            c=FollowController(config=FollowConfig(control_profile='openpose_pid', distance_mode='relative_size', max_request_rate=None), require_geometry=True, recovery_window=3.)
            c.observe(observation(0,0));c.calibrate(0);c.start(0)
            c.observe(observation(1,.1,found=False));self.assertEqual(c.tick(.1).forward,0)
            for seq in (2,3,4):
                t=.1+(seq-1)*interval;c.observe(observation(seq,t,size=.15));c.tick(t)
                if seq<4:self.assertEqual(c.last_request.forward,0)
            self.assertEqual(c.state,State.TRACKING)
            self.assertGreater(c.last_request.forward,0)
            c.observe(observation(5,t+1.01,size=.15));self.assertEqual(c.tick(t+1.01).state,State.STALE)
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
        c=FollowController(config=FollowConfig(distance_mode='pose', camera_calibration=str(self.file), camera_id='synthetic-camera', marker_side_m=.24, max_request_rate=None), pose_estimator=self.pose, require_geometry=True)
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
        c=FollowController(config=FollowConfig(distance_mode='pose', camera_calibration=str(self.file), camera_id='synthetic-camera', marker_side_m=.24, max_request_rate=None), pose_estimator=self.pose, require_geometry=True)
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
        with self.assertRaises(ValueError):FollowConfig(control_profile='openpose_pid',distance_mode='pose', max_request_rate=None)
        with self.assertRaises(ValueError):FollowConfig(camera_id='unused', max_request_rate=None)
        for value in (None,False,'bad'):
            with self.assertRaises(ValueError):FollowConfig(distance_mode=value, max_request_rate=None)


class ResponseServiceTests(unittest.TestCase):
    def test_real_height_needs_acquisition_time_not_query_time(self):
        status = SimpleNamespace(control_owner='none',connected=True,flight_state='flying',
            timestamp=100.,height_cm=120,simulated=False,height_timestamp=None)
        service = FollowService(SimpleNamespace(get_status=lambda: status), config=FollowConfig(max_request_rate=.8), wall_clock=lambda:100.)
        with service.lock:
            self.assertFalse(service._height_fresh(status))
            for stamp,valid in ((99.6,True),(99.4,True),(99.,True),(98.99,False),(101,False),(True,False),(float('nan'),False)):
                status.height_timestamp=stamp
                self.assertEqual(service._height_fresh(status),valid)
            status.simulated=True
            self.assertTrue(service._height_fresh(status))
            status.connected=False
            self.assertFalse(service._height_fresh(status))

    def service(self):
        clock=Clock();port=FakeDevice(clock.mono)
        s=FollowService(port, config=FollowConfig(control_profile='openpose_pid', distance_mode='relative_size', vertical_follow=True, max_request_rate=.8),
              tracker=Detector(),
              monotonic=clock.mono,
              wall_clock=clock.wall,
              recovery_window=3,
              log_directory=False)
        self.addCleanup(s.close)
        def frame(seq,found=True):
            clock.now+=.1;port.publish((found,400),seq,clock.wall());s.capture_once();s.control_once()
        frame(1);self.assertTrue(s.calibrate_reference().ok);self.assertTrue(s._start_tracking().ok)
        return s,port,clock,frame

    def test_height_guard_rejects_unknown_and_only_blocks_outward_direction(self):
        s,port,clock,frame=self.service()
        for height,fresh,up,expected in [(None,False,20,0),(120,False,-20,0),
                (190,True,20,0),(190,True,-20,-20),(60,True,-20,0),(60,True,20,20),
                (120,True,-20,-20),(120,True,20,20),(math.nan,True,20,0)]:
            port.height_cm,port.height_fresh=height,fresh
            status=port.get_status()
            self.assertEqual(s._guard_vertical((5,8,up,7),status),(5,8,expected,7))

    def test_recovering_handoff_preserves_intent_and_requires_new_valid_target(self):
        s,port,clock,frame=self.service();frame(2,False)
        self.assertEqual(s.controller.state,State.RECOVERING)
        self.assertFalse(s.is_following());self.assertTrue(s.status()['handoff_required'])
        ticket=s.pause_if_active();self.assertIsNotNone(ticket)
        self.assertIsNone(port.lease);self.assertEqual(port.velocity,(0,0,0,0))
        frame(3,True)
        self.assertTrue(s.finish_action(ticket,True).ok)
        self.assertEqual(s.controller.state,State.TRACKING)

    def test_stale_or_busy_resume_never_steals_or_sends_old_motion(self):
        for outcome in ('busy','stale','failed','stopped'):
            with self.subTest(outcome=outcome):
                s,port,clock,frame=self.service();ticket=s.pause_if_active();frame(2)
                if outcome=='busy':
                    lease=(port.request_control('model'), port.lease)[1]
                    port.update_velocity(lease,left_right=9,forward_back=0,up_down=0,yaw=0)
                if outcome=='stale':clock.now+=1.1
                if outcome=='stopped':s._stop_tracking()
                r=s.finish_action(ticket,outcome!='failed')
                self.assertFalse(r.ok)
                self.assertEqual(port.velocity,(9,0,0,0) if outcome=='busy' else (0,0,0,0))

    def test_horizontal_rc_cap_matches_lateral_controller_budget(self):
        s=FollowService(FakeDevice(), config=FollowConfig(control_profile='openpose_pid', steering_mode='lateral', yaw_limit=0.5, forward_limit=0.3, max_request_rate=.8),
              log_directory=False)
        self.addCleanup(s.close)
        self.assertEqual(s.rc_caps,(50,30,0,50))
