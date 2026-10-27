"""Causal timeline and actual service guards; no UI, camera or device."""
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import cv2

from timeline_flight_check import Timeline
from r3_follow.core import FollowController, State
from test_horizontal_routing import observation
from test_response_depth import calibration, projected
from r3_follow.pose_depth import PoseDepth
from check_follow_config import check_files
from calibrate_follow_camera import view_conditioning


class TimelineTests(unittest.TestCase):
    def test_control_keeps_ticking_during_real_detector_work_on_virtual_timeline(self):
        w=Timeline(costs=[.10]);result,_=w.run(3.)
        self.assertTrue(result['started'])
        self.assertGreater(result['control_ticks_while_vision_busy'],50)
        self.assertTrue(all(j['completed_at']-j['started_at'] >= .10-1e-9 for j in w.jobs))
        busy=[r for r in w.rows if r['vision_busy'] and r['published_frame_id'] is not None]
        self.assertTrue(any(r['latest_capture_id'] > r['published_frame_id'] for r in busy))

    def test_detector_stall_expires_old_motion_without_waiting_for_return(self):
        w=Timeline(case='cpu_spike',costs=[.03]);r,events=w.run(5.)
        stalled=next(j for j in w.jobs if j['scheduled_cost_ms']>500)
        halt=next(e for e in events if e['event']=='tracking_halted')
        self.assertLess(halt['time'],stalled['completed_at'])
        self.assertEqual(r['last_state'],'frame_stale')
        self.assertTrue(all(not any(row[k] for k in ('rc_right','rc_forward','rc_up','rc_yaw'))
                            for row in w.rows if row['t']>=halt['time']))

    def test_stop_in_flight_of_detector_cannot_be_undone_by_late_result(self):
        w=Timeline(case='explicit_stop',costs=[.08]);r,events=w.run(7.)
        self.assertTrue(any(j['started_at'] < 6 < j['completed_at'] for j in w.jobs))
        self.assertEqual(r['last_state'],'stopped')
        self.assertTrue(any(e['event']=='observation_discarded' for e in events))
        self.assertTrue(all(row['rc_forward']==0 for row in w.rows if row['t']>=6.))

    def test_delay_plus_work_expired_result_never_starts(self):
        w=Timeline(costs=[.12],delay=.4);r,_=w.run(2.)
        self.assertFalse(r['started'])
        self.assertEqual(r['last_state'],'frame_stale')
        self.assertTrue(all(row['rc_forward']==0 for row in w.rows))

    def test_roundoff_at_half_second_does_not_latch_but_long_gap_does(self):
        w=Timeline(costs=[.10],delay=.2);r,_=w.run(4.)
        self.assertEqual(r['last_state'],'tracking')
        c=FollowController(control_profile='openpose_pid',distance_mode='relative_size')
        c.observe(observation(0,0));c.calibrate(0);c.start(0)
        self.assertEqual(c.tick(.5000002).state,State.TRACKING)
        self.assertEqual(c.tick(.50001).state,State.STALE)
        self.assertEqual(c.last_request.forward,0.)

    def test_scheduled_work_cannot_bypass_detector_budget_even_if_result_fresh(self):
        w=Timeline(costs=[.13]);r,_=w.run(2.)
        self.assertFalse(r['started'])
        self.assertGreater(r['budget_exceeded_jobs'],0)
        self.assertTrue(all(not j['found'] for j in w.jobs))

    def test_wrong_marker_and_loss_zero_then_recover_only_after_new_frames(self):
        for case in ('wrong_id','loss'):
            w=Timeline(case=case,costs=[.03]);r,_=w.run(6.)
            self.assertEqual(r['last_state'],'tracking')
            missing=[row for row in w.rows if row['state']=='recovering_target']
            self.assertGreater(len(missing),10)
            self.assertTrue(all(row['rc_forward']==row['rc_yaw']==0 for row in missing))

    def test_identical_cost_trace_repeats_control_and_physics(self):
        values=[]
        for _ in range(2):
            w=Timeline(costs=[.02,.07,.04]);w.run(4.)
            values.append([(r['t'],r['rc_forward'],r['virtual_depth_m']) for r in w.rows])
        self.assertEqual(values[0],values[1])

    def test_reversal_sends_zero_before_negative_and_explicit_stop_does_not_ramp(self):
        c=FollowController(control_profile='openpose_pid',distance_mode='relative_size',max_request_rate=2.)
        c.observe(observation(0,0));c.calibrate(0);c.start(0)
        for i in range(1,21):
            c.observe(observation(i,i*.03,size=.12));c.tick(i*.03)
        self.assertGreater(c.last_request.forward,0)
        c.observe(observation(21,.63,size=.3));self.assertEqual(c.tick(.63).forward,0)
        self.assertLess(c.tick(.65).forward,0)
        self.assertEqual(c.stop().forward,0)


class ConfigurationCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.root=Path(__file__).resolve().parents[1]
        self.paths=[self.root/'configs'/f'aircraft_{name}_ID{i}.json' for i,name in enumerate('ABC')]

    def save(self,value,name='config.json'):
        p=self.directory/name;p.write_text(json.dumps(value),encoding='utf8');return p

    def test_explicit_three_configs_ids_actual_constructor_and_limits(self):
        report=check_files(self.paths,expected_ids=[0,1,2])
        self.assertTrue(report['ok'],report['errors'])
        self.assertEqual([c['actual_service_rc_caps'] for c in report['configs']],
                         [[50,40,20,50],[20,30,20,20],[20,30,20,20]])
        self.assertFalse(report['hardware_verified'])

    def test_missing_environment_does_not_silently_approve_legacy(self):
        with patch.dict('os.environ',{},clear=True):self.assertFalse(check_files()['ok'])

    def test_environment_selects_same_explicit_file(self):
        with patch.dict('os.environ',{'R3_FOLLOW_CONFIG':str(self.paths[1])}):
            result=check_files(expected_ids=[1]);self.assertTrue(result['ok'])

    def test_duplicate_ids_and_instance_names_fail(self):
        report=check_files([self.paths[0],self.paths[0]],expected_ids=[0,1,2])
        self.assertFalse(report['ok']);self.assertEqual(len(report['errors']),3)

    def test_missing_file_invalid_json_duplicate_field_and_invalid_number(self):
        paths=[self.directory/'missing.json',self.directory/'bad.json',self.directory/'duplicate.json']
        paths[1].write_text('{',encoding='utf8')
        paths[2].write_text('{"target_id":0,"target_id":1}',encoding='utf8')
        for path in paths:self.assertFalse(check_files([path])['ok'])
        value=json.loads(self.paths[0].read_text());value['forward_limit']=float('nan')
        self.assertFalse(check_files([self.save(value)])['ok'])

    def test_pose_rejects_synthetic_or_mismatching_camera_and_resolution(self):
        p=self.directory/'calib.json';calibration(p)
        value=json.loads(self.paths[0].read_text());value.update(distance_mode='pose',
            camera_calibration=str(p),camera_id='synthetic-camera',marker_side_m=.24)
        report=check_files([self.save(value)],image_size=(1280,720),camera_id='different')
        self.assertFalse(report['ok']);self.assertEqual(len(report['errors']),3)

    def test_legacy_selection_fails_candidate_preflight_without_changing_runtime_default(self):
        from r3_follow.config import load_config
        self.assertEqual(load_config.__module__,'r3_follow.config')
        value=dict(instance_id='test',target_id=0,dictionary='DICT_4X4_50',control_profile='legacy')
        self.assertFalse(check_files([self.save(value)])['ok'])


class PoseAndCalibrationStressTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'camera.json';self.data=calibration(self.path)
        self.pose=PoseDepth(self.path,.24,camera_id='synthetic-camera')

    def test_many_front_parallel_views_do_not_prove_focal_length(self):
        board=np.zeros((35,3),np.float32);board[:,:2]=np.mgrid[0:7,0:5].T.reshape(-1,2)*.03
        samples=[(board[:,:2]*(1000+i*5)+[50+(i%5)*40,40+(i//5)*50]).reshape(-1,1,2) for i in range(20)]
        with self.assertRaisesRegex(ValueError,'tilted views'):view_conditioning(board,samples,(640,480))

    def test_correlated_corner_translation_does_not_change_depth_on_side_turn(self):
        for angle in (0,30,45,60):
            obs=projected(0,0,2.,angle)
            for shift in ((2,1),(-2,-1)):
                corners=np.array(obs.corners)+shift
                result=self.pose.estimate(replace(obs,corners=tuple(map(tuple,corners.tolist()))))
                self.assertTrue(result.valid,result.reason)
                self.assertLess(abs(result.depth_m-2.),.03)

    def test_near_edge_extreme_tilt_and_bad_corner_cannot_fall_back_to_size(self):
        obs=projected(0,0,2.,45)
        warped=list(obs.corners);warped[1]=(warped[1][0]+30,warped[1][1]-25)
        for bad in (replace(obs,corners=tuple(warped)),projected(0,0,2.,80),projected(0,0,2.,0,x=1.25)):
            result=self.pose.estimate(bad);self.assertFalse(result.valid,result)

    def test_wrong_measured_marker_size_bias_is_documented_not_hidden_by_low_fit_error(self):
        wrong=PoseDepth(self.path,.30,camera_id='synthetic-camera')
        result=wrong.estimate(projected(0,0,2.,45))
        self.assertTrue(result.valid)
        self.assertAlmostEqual(result.depth_m,2.5,places=5)
        self.assertLess(result.reprojection_px,.001)
        # Establish why the field ruler check is mandatory: reprojection alone
        # cannot detect a wrong physical side length. No guessed correction.

    def test_calibration_with_failed_view_quality_is_not_loaded(self):
        for value in (.0001, True, float('nan'), None, [.1]):
            self.data['view_conditioning']={'intrinsic_constraint_rank5_ratio':value}
            self.path.write_text(json.dumps(self.data),encoding='utf8')
            with self.assertRaisesRegex(ValueError,'conditioning'):
                PoseDepth(self.path,.24,camera_id='synthetic-camera')

    def test_corner_noise_reports_acceptance_and_error_not_a_perfect_detection_claim(self):
        rng=np.random.default_rng(211);errors=[];rejections=0
        for i in range(80):
            obs=projected(i,i*.1,2.,45)
            pixels=np.array(obs.corners)+rng.normal(0,1.5,(4,2))
            est=self.pose.estimate(replace(obs,corners=tuple(map(tuple,pixels.tolist()))))
            if est.valid:errors.append(abs(est.depth_m-2.))
            else:rejections+=1
        self.assertGreater(rejections,0);self.assertGreater(len(errors),0)
        self.assertLess(max(errors),.3)


if __name__=='__main__':unittest.main()
