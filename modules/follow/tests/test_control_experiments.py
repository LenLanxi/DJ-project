import math
import unittest
from r3_follow.core import FollowController, Observation, State
from continuous_follow_sim import SCENARIOS, Scenario, run


def request(width, error, size_error):
    c = FollowController()
    c.observe(Observation(0, 0, 'test', width, 480, 0, True, width/2, 240, .2))
    c.calibrate(0)
    c.start(0)
    c.observe(Observation(1, .1, 'test', width, 480, 0, True,
                          width*(1+error)/2, 240, .2*(1-size_error)))
    return c.tick(.1)


class CalculationProperties(unittest.TestCase):
    def test_resolution_invariance(self):
        for error in (-.9, -.3, -.05, .05, .3, .9):
            results = [request(w, error, error/2) for w in (320, 640, 1280, 1920)]
            for result in results[1:]:
                self.assertAlmostEqual(result.right, results[0].right)
                self.assertAlmostEqual(result.forward, results[0].forward)

    def test_sign_symmetry_monotonicity_and_bounds(self):
        previous = -1
        for i in range(-99, 100):
            error = i/100
            r = request(640, error, error)
            opposite = request(640, -error, -error)
            self.assertAlmostEqual(r.right, -opposite.right)
            self.assertAlmostEqual(r.forward, -opposite.forward)
            self.assertGreaterEqual(r.right + 1e-12, previous)
            self.assertLessEqual(max(abs(r.right), abs(r.forward)), .3)
            previous = r.right

    def test_small_static_noise_inside_deadzone_produces_zero(self):
        for i in range(100):
            r = request(640, .08*math.sin(i), .08*math.cos(i))
            self.assertEqual((r.right, r.forward), (0, 0))

    def test_axes_do_not_leak_into_each_other(self):
        self.assertEqual(request(640, .7, 0).forward, 0)
        self.assertEqual(request(640, 0, .7).right, 0)


class ClosedLoopExperiments(unittest.TestCase):
    def test_stationary_target_converges_into_deadzone(self):
        rows, report = run(Scenario('stationary'))
        self.assertLess(report['tail_mean_abs_x'], .125)
        self.assertLess(report['tail_mean_abs_size'], .125)
        self.assertLess(abs(rows[-1]['x_error']), abs(rows[0]['x_error']))

    def test_every_scenario_keeps_limits_and_inactive_zero(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario.name):
                rows, report = run(scenario)
                self.assertEqual(report['nontracking_nonzero'], 0)
                self.assertLessEqual(report['max_request'], .3)
                self.assertTrue(all(math.isfinite(r['distance']) for r in rows))

    def test_short_loss_recovers_only_after_zero_wait(self):
        rows, _ = run(Scenario('brief', loss_start=5, loss_end=6))
        self.assertTrue(any(r['state'] == State.RECOVERING for r in rows))
        self.assertTrue(all(r['right'] == r['forward'] == 0 for r in rows if 5 <= r['time'] < 6))
        self.assertEqual(rows[-1]['state'], State.TRACKING)

    def test_long_loss_latches(self):
        rows, _ = run(Scenario('long', loss_start=5, loss_end=9))
        self.assertEqual(rows[-1]['state'], State.LOST)

    def test_frozen_frames_stop_and_do_not_restart(self):
        rows, _ = run(Scenario('freeze', freeze_start=5, freeze_end=7))
        self.assertTrue(all(r['right'] == r['forward'] == 0 for r in rows if r['time'] >= 5.5))
        self.assertEqual(rows[-1]['state'], State.STALE)

    def test_explicit_stop_cancels_follow(self):
        rows, _ = run(Scenario('stop', stop_at=5))
        self.assertTrue(all(r['state'] == State.STOPPED for r in rows if r['time'] >= 5))

    def test_excessive_delay_fails_closed(self):
        rows, _ = run(Scenario('delay', delay=.6))
        self.assertEqual(rows[-1]['state'], State.STALE)
        self.assertTrue(all(r['right'] == r['forward'] == 0 for r in rows))

    def test_repeatable(self):
        scenario = Scenario('noise', noise=.025, motion=True)
        self.assertEqual(run(scenario), run(scenario))


if __name__ == '__main__':
    unittest.main()
