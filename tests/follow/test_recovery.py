from tests.helpers.FollowFixture import make_controller as FollowController
from follow.r3_follow.config import FollowConfig
from dataclasses import replace, asdict
import json
from pathlib import Path
import unittest

from follow.r3_follow.core import State
from follow.r3_follow.vision import MarkerTracker, synthetic_frame


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.c = FollowController(config=FollowConfig(horizontal_limit_rc=30.0, slew_rc_per_second=None), auto_recover=True)
        self.detector = MarkerTracker()
        self.seq = 0
        self.frame(0)
        self.c.calibrate(0)
        self.c.start(0)

    def frame(self, now, **options):
        self.seq += 1
        obs = self.detector.detect(synthetic_frame(x=.8, **options), self.seq, now, 'synthetic')
        self.c.observe(obs)
        return self.c.tick(now)

    def zero(self, req):
        self.assertEqual((req.yaw, req.forward), (0, 0))

    def lose(self):
        self.zero(self.frame(.1, visible=False))
        self.assertEqual(self.c.state, State.RECOVERING)

    def recover(self):
        self.zero(self.frame(.2))
        self.zero(self.frame(.3))
        return self.frame(.4)

    def test_short_loss_then_stable_target(self):
        self.lose()
        self.assertGreater(self.recover().yaw, 0)
        self.assertEqual(self.c.state, State.TRACKING)

    def test_one_frame_and_repeated_ticks_do_not_confirm(self):
        self.lose()
        self.frame(.2)
        for now in (.3, .4, .5):
            self.zero(self.c.tick(now))
        self.assertEqual(self.c.state, State.RECOVERING)

    def test_three_frames_also_require_elapsed_stability(self):
        self.lose()
        for t in (.2, .21, .22): self.zero(self.frame(t))
        self.assertEqual(self.c.state, State.RECOVERING)
        self.assertGreater(self.frame(.38).yaw, 0)

    def test_long_loss_recovers_after_stable_target_returns(self):
        self.lose()
        for t in (.3, .5, .71): self.zero(self.frame(t, visible=False))
        self.assertEqual(self.c.state, State.RECOVERING)
        self.zero(self.frame(.8))
        self.zero(self.frame(.9))
        self.assertGreater(self.frame(1.0).yaw,0)

    def test_stop_during_recovery_cancels_restart(self):
        self.lose()
        self.c.stop()
        self.recover()
        self.assertEqual(self.c.state, State.STOPPED)

    def test_pause_during_recovery_requires_explicit_resume(self):
        self.lose()
        self.c.pause()
        self.recover()
        self.assertEqual(self.c.state, State.PAUSED)

    def test_other_id_during_recovery_keeps_zero_and_allows_target_recovery(self):
        self.lose()
        self.zero(self.frame(.2, marker_id=1))
        self.assertEqual(self.c.observation.reason, 'not_found')
        self.assertEqual(self.c.state, State.RECOVERING)
        self.zero(self.frame(.3))
        self.zero(self.frame(.4))
        self.assertGreater(self.frame(.5).yaw, 0)

    def test_other_id_while_tracking_enters_zero_wait(self):
        self.zero(self.frame(.1, marker_id=1))
        self.assertEqual(self.c.state, State.RECOVERING)
        self.assertGreater(self.recover().yaw, 0)

    def test_duplicate_target_blocks_auto_restart(self):
        self.lose()
        ambiguous = replace(self.c.observation, frame_id=3, acquired_at=.2,
                            reason='ambiguous_duplicate_id')
        self.c.observe(ambiguous)
        self.zero(self.c.tick(.2))
        self.assertEqual(self.c.state, State.LOST)

    def test_frozen_stream_is_latched(self):
        self.lose()
        self.zero(self.c.tick(1.11))
        self.assertEqual(self.c.state, State.RECOVERING)
        self.zero(self.frame(1.15))

    def test_new_frame_cannot_bypass_watchdog(self):
        self.lose()
        self.zero(self.frame(1.15))
        self.assertEqual(self.c.state, State.RECOVERING)


    def test_invalid_data_cancels_recovery(self):
        self.lose()
        self.c.observe(None)
        self.zero(self.c.tick(.2))
        self.assertEqual(self.c.state, State.RECOVERING)
        self.recover()


if __name__ == '__main__':
    unittest.main()
