"""Explicit horizontal RC routing; no claim of physical flight equivalence."""
from dataclasses import FrozenInstanceError, replace
import math
import random
import unittest

from r3_follow.config import FollowConfig, load_config
from r3_follow.core import FollowController, Observation, State
from r3_follow.openpose_control import OpenPoseMarkerControl, YAW_DERIVATIVE_LIMIT

MODES = ("yaw", "lateral", "hybrid")

def observation(seq, now, *, x=.5, size=.2, target_id=0, found=True, narrow=False):
    cx, cy, side = x*640, 240, size*640
    half_x = side*(.25 if narrow else .5)
    corners = ((cx-half_x, cy-side/2), (cx+half_x, cy-side/2),
               (cx+half_x, cy+side/2), (cx-half_x, cy+side/2))
    return Observation(seq, now, f"camera-{target_id}", 640, 480, target_id, found,
        cx if found else None, cy if found else None, size if found else None,
        corners if found else (), "" if found else "not_found")

def ready(mode, target_id=0, rate=None):
    controller = FollowController(target_id=target_id, control_profile="openpose_pid",
        steering_mode=mode, recovery_window=3, require_geometry=True, max_request_rate=rate)
    controller.observe(observation(0, 0., target_id=target_id))
    assert controller.calibrate(0.)[0]
    assert controller.start(0.)[0]
    return controller

def sample(controller, seq, now, **kwargs):
    controller.observe(observation(seq, now, target_id=controller.target_id, **kwargs))
    return controller.tick(now)

def values(request):
    return request.right, request.forward, request.yaw

class HorizontalRoutingTests(unittest.TestCase):
    def call(self, adapter, seq, time, *, cx=400., size=.15, allowed=True):
        return adapter.update(frame_id=seq, acquired_at=time, center_x=cx,
            image_width=640, size_ratio=size, reference_size=.2, forward_allowed=allowed)

    def test_one_pid_value_is_routed_without_changing_depth_or_budget(self):
        controllers = {m:OpenPoseMarkerControl(steering_mode=m) for m in MODES}
        # Include odd integer RC numbers; round(half)+round(half) must not amplify.
        for seq, cx in enumerate(range(220, 421, 4)):
            actual = {m:self.call(c, seq, seq*.04, cx=cx) for m,c in controllers.items()}
            h = round(actual["yaw"][2]*100)
            self.assertEqual(actual["lateral"], (actual["yaw"][2], actual["yaw"][1], 0))
            self.assertEqual(actual["hybrid"][1], actual["yaw"][1])
            right, _, yaw = actual["hybrid"]
            self.assertEqual(round(right*100)+round(yaw*100), h)
            self.assertEqual(abs(round(right*100))+abs(round(yaw*100)), abs(h))
            self.assertLessEqual(abs(right)+abs(yaw), .2+1e-12)

    def test_center_and_signs_and_quantization_at_one_rc_unit(self):
        # P=0.12 后量化步长变成 xoff≈±9 才 1 RC（旧 P=0.25 是 ±4）；每个点用新适配器，
        # 避免 D 的历史干扰这一条"单 RC 量化"检查。
        for mode in MODES:
            for cx in (280, 316, 320, 324, 360):
                c = OpenPoseMarkerControl(steering_mode=mode)
                r = self.call(c, 0, 0., cx=cx, size=.2)
                self.assertEqual(r[1], 0)
                self.assertTrue(all(v*(cx-320) >= 0 for v in (r[0], r[2])))
                if cx == 320:
                    self.assertEqual(r, (0, 0, 0))
                if mode == "hybrid" and cx in (316, 324):
                    # ±4 的等价偏移在新增益下不足 1 RC，量化到 0
                    self.assertEqual(r[0], 0)
                    self.assertEqual(r[2], 0)
            # 明确量化步长：P=0.16 时 xoff=±7 → 1 RC，±6 → 0（三种路由合计值）
            c = OpenPoseMarkerControl(steering_mode=mode)
            routed = self.call(c, 0, 0., cx=327, size=.2)
            self.assertEqual(abs(round((routed[0]+routed[2])*100)), 1)
            c = OpenPoseMarkerControl(steering_mode=mode)
            routed = self.call(c, 0, 0., cx=326, size=.2)
            self.assertEqual(routed[0]+routed[2], 0)

    def test_shape_block_keeps_selected_horizontal_axis_on_same_frame(self):
        for mode in MODES:
            c=OpenPoseMarkerControl(steering_mode=mode)
            first=self.call(c,1,.1)
            blocked=self.call(c,1,.1,allowed=False)
            self.assertEqual(blocked,(first[0],0,first[2]))
            self.assertEqual(c.status()["pitch_components"],(0,0,0))

    def test_modes_are_constructor_only_and_bad_combinations_are_rejected(self):
        for mode in MODES:
            c=ready(mode)
            with self.assertRaises(AttributeError):c.steering_mode="yaw"
            with self.assertRaises(AttributeError):c._openpose.steering_mode="yaw"
        for mode in (None,False,"mix",""):
            with self.assertRaises(ValueError):OpenPoseMarkerControl(steering_mode=mode)
            with self.assertRaises(ValueError):FollowController(steering_mode=mode)
        with self.assertRaises(ValueError):FollowController(control_profile="legacy",steering_mode="hybrid")
        self.assertEqual(FollowController().steering_mode,"lateral")
        self.assertEqual(FollowController(control_profile="openpose_pid").steering_mode,"yaw")

    def test_config_default_old_mapping_and_explicit_modes(self):
        base=dict(instance_id="A",target_id=0,dictionary="DICT_4X4_50",control_profile="openpose_pid")
        self.assertEqual(load_config(base).steering_mode,"yaw")
        for mode in MODES:
            conf=load_config(dict(base,steering_mode=mode))
            self.assertEqual(conf.to_dict()["steering_mode"],mode)
            with self.assertRaises(FrozenInstanceError):conf.steering_mode="yaw"
        with self.assertRaises(ValueError):FollowConfig(control_profile="legacy",steering_mode="hybrid")
        for mode in (None,False,"mix",""):
            with self.assertRaises(ValueError):load_config(dict(base,steering_mode=mode))

    def test_outer_ramp_bounded_with_irregular_dt_and_abrupt_reversals(self):
        rng=random.Random(4321)
        for mode in MODES:
            c=ready(mode,rate=.8);now=0
            for seq in range(1,601):
                now+=rng.choice((.001,.02,.07,.12,.19))
                x=rng.choice((.25,.49,.5,.51,.75))
                r=sample(c,seq,now,x=x,size=.16)
                self.assertTrue(all(math.isfinite(v) for v in values(r)))
                self.assertLessEqual(abs(r.right)+abs(r.yaw),.2+1e-12)
                self.assertLessEqual(abs(round(r.right*100))+abs(round(r.yaw*100)),20)
                self.assertLessEqual(abs(r.forward),.3+1e-12)
                # D 会在急反转的瞬间刹车（甚至短暂反向），幅度被 YAW_DERIVATIVE_LIMIT 限制
                braking = YAW_DERIVATIVE_LIMIT + 1e-12
                self.assertGreaterEqual((x-.5)*r.right,-braking)
                self.assertGreaterEqual((x-.5)*r.yaw,-braking)
                if mode=="yaw":self.assertEqual(r.right,0)
                if mode=="lateral":self.assertEqual(r.yaw,0)

    def test_stop_pause_stale_and_wrong_id_zero_every_axis_without_ramp_delay(self):
        for mode in MODES:
            for action in ("stop","pause","stale","wrong_id","invalid"):
                with self.subTest(mode=mode,action=action):
                    c=ready(mode,rate=.8)
                    for seq in range(1,8):sample(c,seq,seq*.1,x=.75,size=.15)
                    self.assertTrue(any(values(c.last_request)))
                    if action in ("stop","pause"):r=getattr(c,action)()
                    elif action=="stale":r=c.tick(1.201)
                    else:
                        c.observe(None if action=="invalid" else observation(8,.8,target_id=1))
                        r=c.tick(.8)
                    self.assertEqual(values(r),(0,0,0))
                    self.assertEqual(c.status()["openpose_control"]["raw_normalized_request"],(0,0,0))
                    self.assertEqual(c.status()["openpose_control"]["yaw_components"],(0,0,0))

    def test_short_loss_stable_recovery_has_no_old_direction(self):
        for mode in MODES:
            c=ready(mode,rate=.8)
            for seq in range(1,8):sample(c,seq,seq*.1,x=.75,size=.15)
            self.assertEqual(values(sample(c,8,.8,found=False)),(0,0,0))
            for seq,now in ((9,.9),(10,1.0)):
                self.assertEqual(values(sample(c,seq,now,x=.25,size=.15)),(0,0,0))
            r=sample(c,11,1.1,x=.25,size=.15)
            self.assertEqual(c.state,State.TRACKING)
            self.assertLessEqual(r.right,0);self.assertLessEqual(r.yaw,0)
            r=sample(c,12,1.2,x=.25,size=.15)
            self.assertLess(r.right+r.yaw,0)

    def test_long_loss_timeout_does_not_restart_when_target_returns(self):
        for mode in MODES:
            c=ready(mode)
            sample(c,1,.1,x=.75,size=.15)
            sample(c,2,.2,found=False)
            for seq in range(3,34):
                self.assertEqual(values(sample(c,seq,seq*.1,found=False)),(0,0,0))
            self.assertEqual(c.state,State.LOST)
            self.assertEqual(values(sample(c,34,3.4,x=.25,size=.15)),(0,0,0))
            self.assertEqual(c.state,State.LOST)

    def test_three_target_instances_keep_modes_and_histories_separate(self):
        cs=[ready(mode,target_id=i,rate=.8) for i,mode in enumerate(MODES)]
        for seq in range(1,8):
            for c,x in zip(cs,(.75,.25,.75)):sample(c,seq,seq*.1,x=x,size=.15)
        before=[c.status()["openpose_control"].copy() for c in cs]
        cs[1].observe(observation(8,.8,target_id=0))
        self.assertEqual(values(cs[1].tick(.8)),(0,0,0))
        for i in (0,2):
            self.assertEqual(cs[i].status()["openpose_control"],before[i])
            self.assertTrue(any(values(cs[i].last_request)))
        self.assertEqual([c.target_id for c in cs],[0,1,2])
        self.assertEqual([c.steering_mode for c in cs],list(MODES))

if __name__=="__main__":unittest.main()
