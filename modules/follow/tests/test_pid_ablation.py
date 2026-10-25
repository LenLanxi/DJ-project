"""Check comparisons exercise isolated settings and the production stop rule."""
import unittest
from unittest.mock import patch
from pid_ablation_check import factory,run_samples,VARIANTS
from pid_adaptation_check import synthetic_samples,virtual_step
from r3_follow.openpose_control import OpenPoseMarkerControl


class AblationTests(unittest.TestCase):
    def test_each_setting_changes_only_declared_knobs(self):
        baseline=factory('marker_v2')
        expected={'marker_v2':((.5,.01,.06),.08,True),
            'upstream_reference':((.5,.04,.3),0.,False),
            'marker_no_filter':((.5,.01,.06),0.,True),
            'marker_upstream_D':((.5,.01,.3),.08,True),
            'marker_upstream_I':((.5,.04,.06),.08,True),
            'adapted_without_integral_guards':((.5,.01,.06),.08,False),
            'marker_no_I':((.5,0.,.06),.08,True)}
        for name,values in expected.items():
            a=factory(name)
            self.assertEqual((a.pitch_pid.tunings,a.depth_filter_tau,a.tuning=='marker_v2'),values)
            self.assertEqual(a.pitch_pid.output_limits,baseline.pitch_pid.output_limits)
            self.assertEqual(a.yaw_pid.tunings,baseline.yaw_pid.tunings)
        self.assertEqual(OpenPoseMarkerControl().pitch_pid.tunings,(.5,.01,.06))

    def test_default_comparison_is_exact_runtime_calculation(self):
        samples=synthetic_samples('irregular_noise')
        got=run_samples(samples,'marker_v2');control=OpenPoseMarkerControl()
        for sample,row in zip(samples,got):
            command=control.update(frame_id=sample['frame_id'],acquired_at=sample['timestamp'],
                image_width=640,center_x=sample['center_x'],size_ratio=sample['size_ratio'],reference_size=.2)
            self.assertEqual((row['forward_rc'],row['yaw_rc']),(command[1]*100,command[2]*100))

    def test_all_variants_reset_on_missing_target_and_keep_same_caps(self):
        for name in VARIANTS:
            rows=run_samples(synthetic_samples('loss_and_shape'),name)
            for row in rows:
                self.assertLessEqual(abs(row['forward_rc']),30)
                self.assertLessEqual(abs(row['yaw_rc']),20)
                if not row['found']:self.assertEqual((row['forward_rc'],row['yaw_rc']),(0,0))

    def test_virtual_ramp_does_not_delay_a_zero_command(self):
        class CommandSequence:
            def update(self,**kw):return (0.,.3 if kw['acquired_at']<3 else 0.,0.)
            def reset(self):pass
        with patch('pid_adaptation_check.OpenPoseMarkerControl',return_value=CommandSequence()):
            rows,_=virtual_step('marker_v2')
        self.assertGreater(next(r for r in rows if r['t']==2.98)['request_rc'],0)
        self.assertEqual(next(r for r in rows if r['t']==3.)['request_rc'],0)


if __name__=='__main__':unittest.main()
