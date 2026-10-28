from tests.helpers.FollowFixture import make_controller as FollowController
"""RC 预算（速度上限）与高速度下的安全兜底。

默认值必须与历史审查一致：yaw_limit 0.20 / forward_limit 0.30 → (20, 30, 0, 20)。
放大预算时，"算法自身输出上限" 与 "服务层钳位" 必须同时抬高，否则会被静默夹回。
"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))             # repository root
FOLLOW = os.path.join(REPO, "follow")
for path in (REPO,):
    if path not in sys.path:
        sys.path.insert(0, path)

from follow.r3_follow.config import FollowConfig, load_config
from follow.r3_follow.core import MotionRequest, Observation, State
from tests.helpers.FollowFixture import FakeDevice
from follow.r3_follow import FollowService


def service(**kwargs):
    kwargs.setdefault('log_directory', False)
    kwargs.setdefault('tracker', _NullTracker())
    config = FollowConfig(**{key: kwargs.pop(key) for key in tuple(kwargs)
                              if key in FollowConfig.__dataclass_fields__})
    return FollowService(FakeDevice(), config=config, **kwargs)


def tracking(servo):
    """让 control_once 走到"下发速度"这一步（状态机 + 控制权 + 一条假观测）。"""
    result = servo.device.request_control('follow')
    servo.grant = dict(control_epoch=result.control_epoch, command_epoch=result.command_epoch)
    servo.controller.state = State.TRACKING
    servo.controller.observation = Observation(
        1, servo.clock(), 'budget-test', 640, 480, 0, True, 320, 240, .2, ())
    servo.controller.tick = lambda now: MotionRequest(State.TRACKING, right=.9,
                                                      forward=.9, reason='budget', yaw=.9)
    servo.control_once()
    return servo.device.velocity


class _NullTracker:
    target_id = 0


class BudgetTests(unittest.TestCase):
    def test_defaults_unchanged(self):
        controller = FollowController(config=FollowConfig(max_request_rate=None))
        self.assertEqual((controller.yaw_limit, controller.forward_limit), (.20, .30))
        self.assertEqual(service().rc_caps, (20, 30, 0, 20))

    def test_raised_budget_reaches_the_port(self):
        servo = service(yaw_limit=.5, forward_limit=1.0)
        self.assertEqual(servo.rc_caps, (50, 100, 0, 50))
        self.assertEqual(tracking(servo), (50, 90, 0, 50))

    def test_default_budget_still_clamps_to_30_20(self):
        self.assertEqual(tracking(service()), (20, 30, 0, 20))

    def test_altitude_channel_is_never_commanded(self):
        for yaw_limit, forward_limit in ((.2, .3), (.5, 1.0), (1.0, 1.0)):
            caps = service(yaw_limit=yaw_limit, forward_limit=forward_limit).rc_caps
            self.assertEqual(caps[2], 0, caps)
            self.assertLessEqual(max(caps), 100, caps)

    def test_limits_are_validated(self):
        for bad in (0, -0.1, 1.5, float('nan'), 'x', True):
            with self.assertRaises(ValueError):
                FollowController(config=FollowConfig(forward_limit=bad, max_request_rate=None))
            with self.assertRaises(ValueError):
                FollowController(config=FollowConfig(yaw_limit=bad, max_request_rate=None))

    def test_config_accepts_and_defaults_the_budget(self):
        self.assertEqual(FollowConfig(max_request_rate=None).yaw_limit, .20)
        self.assertEqual(FollowConfig(max_request_rate=None).forward_limit, .30)
        self.assertEqual(FollowConfig().max_request_rate, .8)
        config = load_config({'instance_id': 'A', 'target_id': 0,
                              'dictionary': 'DICT_4X4_50',
                              'control_profile': 'openpose_pid',
                              'yaw_limit': .5, 'forward_limit': 1.0,
                              'max_request_rate': 2.0})
        self.assertEqual((config.yaw_limit, config.forward_limit), (.5, 1.0))
        self.assertEqual(config.max_request_rate, 2.0)
        with self.assertRaises(ValueError):
            load_config({'instance_id': 'A', 'target_id': 0, 'dictionary': 'DICT_4X4_50',
                         'control_profile': 'openpose_pid', 'yaw_limit': 2.0})
        with self.assertRaises(ValueError):
            load_config({'instance_id': 'A', 'target_id': 0, 'dictionary': 'DICT_4X4_50',
                         'control_profile': 'openpose_pid', 'max_request_rate': 0})

    def test_service_applies_the_configured_ramp(self):
        self.assertEqual(service().controller.max_request_rate, .8)
        self.assertEqual(service(max_request_rate=2.0).controller.max_request_rate, 2.0)
        self.assertIsNone(service(max_request_rate=None).controller.max_request_rate)

    def test_shipped_yaw_config_is_full_budget(self):
        config = load_config(os.path.join(FOLLOW, 'configs', 'aircraft_A_ID0.json'))
        self.assertEqual((config.steering_mode, config.control_profile), ('yaw', 'openpose_pid'))
        # yaw 拉满到 ±50 RC；前后按 1 m/s 的安全目标收到 ±40 RC；斜坡 2.0/s
        self.assertEqual((config.yaw_limit, config.forward_limit), (.5, .4))
        self.assertEqual(config.max_request_rate, 2.0)

    def test_yaw_gains_are_detuned_and_damped(self):
        from follow.r3_follow.openpose_control import YAW_DERIVATIVE_MIN_DT, YAW_GAINS
        self.assertEqual(YAW_GAINS, (0.16, 0.0, 0.03))
        self.assertLess(YAW_GAINS[0], 0.25)          # 低于上游 0.25，饱和点后移
        self.assertGreater(YAW_GAINS[2], 0.0)        # 有阻尼
        self.assertGreaterEqual(YAW_DERIVATIVE_MIN_DT, .01)

    def test_derivative_guard_keeps_a_1ms_jitter_frame_in_check(self):
        """1ms 帧间隔 + 少量像素抖动 不能把 yaw 打成满量程（D 项间隔下限）。"""
        control = FollowController(config=FollowConfig(target_id=0, steering_mode='yaw', yaw_limit=.5, control_profile='openpose_pid', max_request_rate=None), require_geometry=True)
        t = 100.0
        control.observe(Observation(1, t, 'bench', 960, 720, 0, True, 480 + 120, 360,
                                    .2, ((420, 300), (540, 300), (540, 420), (420, 420))))
        self.assertTrue(control.calibrate(t)[0])
        self.assertTrue(control.start(t)[0])
        t += .033
        control.observe(Observation(2, t, 'bench', 960, 720, 0, True, 480 + 121, 360,
                                    .2, ((421, 300), (541, 300), (541, 420), (421, 420))))
        request = control.tick(t)
        jitter = abs(request.yaw * 100)
        t += .001                                   # 只有 1ms 间隔的下一帧
        control.observe(Observation(3, t, 'bench', 960, 720, 0, True, 480 + 122, 360,
                                    .2, ((422, 300), (542, 300), (542, 420), (422, 420))))
        request = control.tick(t)
        self.assertLess(abs(request.yaw * 100), 20,
                        'jitter should stay small: %s vs %s' % (jitter, request.yaw))


class HighSpeedSafetyTests(unittest.TestCase):
    """高速度下必须仍然有：独立看门狗 + 优先通道急停/降落。"""

    def setUp(self):
        from drone_sdk import controller
        self.ctl = controller
        self.ctl._driver.set_simulated_delay(0)
        owner = self.ctl.get_status().control_owner
        if owner != 'none':
            self.ctl.release_control(owner)
        self.addCleanup(self._restore)

    def _restore(self):
        owner = self.ctl.get_status().control_owner
        if owner != 'none':
            self.ctl.release_control(owner)
        self.ctl._flight_state = 'landed'
        self.ctl._driver.set_velocity((0, 0, 0, 0), 'test_cleanup')

    def _full_speed(self):
        grant = self.ctl.request_control('follow')
        self.assertTrue(grant.ok, grant.reason)
        self.ctl._flight_state = 'flying'
        sent = self.ctl.update_velocity('follow', 100, 100, 0, 50,
                                        control_epoch=grant.control_epoch,
                                        command_epoch=grant.command_epoch)
        self.assertTrue(sent.ok, sent.reason)
        self.assertEqual(self.ctl._driver.snapshot()['velocity'], (100, 100, 0, 50))
        return grant

    def test_watchdog_runs_in_its_own_thread_and_zeroes_full_speed(self):
        watchdog = self.ctl._watchdog          # controller 层独立守护线程
        self.assertTrue(watchdog.is_alive())
        self.assertTrue(watchdog.daemon)
        self.assertEqual(self.ctl.VELOCITY_WATCHDOG_S, 0.3)
        self._full_speed()
        time.sleep(0.2)       # 0.3s 门限内：必须还在动（证明门限确实收紧到 0.3）
        self.assertEqual(self.ctl._driver.snapshot()['velocity'], (100, 100, 0, 50))
        time.sleep(0.15)      # 累计 0.35s > 0.3s：必须已归零
        self.assertEqual(self.ctl._driver.snapshot()['velocity'], (0, 0, 0, 0))

    def test_priority_stop_is_immediate_at_full_speed(self):
        self._full_speed()
        started = time.perf_counter()
        result = self.ctl.stop_motion('model', priority=True)
        elapsed = (time.perf_counter() - started) * 1000
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.ctl._driver.snapshot()['velocity'], (0, 0, 0, 0))
        self.assertLess(elapsed, 50)
        self.assertEqual(self.ctl.get_status().control_owner, 'model')

    def test_priority_land_bypasses_the_normal_gates(self):
        self._full_speed()
        result = self.ctl.land('model', priority=True)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.ctl._driver.snapshot()['velocity'], (0, 0, 0, 0))

    def test_ground_lock_still_blocks_full_speed(self):
        grant = self.ctl.request_control('follow')
        self.assertTrue(grant.ok, grant.reason)
        self.ctl._flight_state = 'landed'
        blocked = self.ctl.update_velocity('follow', 100, 100, 0, 50,
                                           control_epoch=grant.control_epoch,
                                           command_epoch=grant.command_epoch)
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.reason, 'is_landed')
        self.assertEqual(self.ctl._driver.snapshot()['velocity'], (0, 0, 0, 0))


if __name__ == '__main__':
    unittest.main()
