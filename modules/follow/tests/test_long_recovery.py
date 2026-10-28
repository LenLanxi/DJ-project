import unittest
from r3_follow.core import FollowController, Observation, State


class LongRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.c = FollowController(recovery_window=1.5)
        self.seq = 0
        self.frame(0)
        self.c.calibrate(0)
        self.c.start(0)

    def frame(self, t, found=True, reason=''):
        self.seq += 1
        self.c.observe(Observation(self.seq,t,'test',640,480,0,found,
            500 if found else None,240 if found else None,.25 if found else None,reason=reason))
        return self.c.tick(t)

    def test_one_second_loss_recovers_only_after_stable_new_frames(self):
        for t in (.1,.3,.5,.7,.9,1.1):
            self.assertEqual(self.frame(t,False).right,0)
        for t in (1.2,1.3):
            self.assertEqual(self.frame(t).right,0)
        self.assertGreater(self.frame(1.4).right,0)
        self.assertEqual(self.c.state,State.TRACKING)

    def test_longer_than_deadline_cannot_restart(self):
        for t in (.1,.3,.5,.7,.9,1.1,1.3,1.61): self.frame(t,False)
        self.assertEqual(self.c.state,State.LOST)
        for t in (1.7,1.8,1.9): self.assertEqual(self.frame(t).right,0)

    def test_long_window_does_not_extend_frame_expiry(self):
        self.frame(.1,False)
        self.assertEqual(self.c.tick(1.11).right,0)
        self.assertEqual(self.c.state,State.STALE)
        for t in (1.2,1.3,1.4): self.assertEqual(self.frame(t).right,0)

    def test_stop_cancels_long_window(self):
        self.frame(.1,False)
        self.c.stop()
        for t in (.2,.3,.4): self.assertEqual(self.frame(t).right,0)
        self.assertEqual(self.c.state,State.STOPPED)

    def test_wrong_target_cancels_long_window(self):
        self.frame(.1,False)
        self.frame(.2,False,'wrong_target')
        for t in (.3,.4,.5): self.assertEqual(self.frame(t).right,0)
        self.assertEqual(self.c.state,State.LOST)

    def test_flicker_cannot_extend_deadline(self):
        self.frame(.1,False)
        for t in (.2,.4,.6,.8,1.0,1.2,1.4):
            self.frame(t)
            self.frame(t+.05,False)
        self.frame(1.61)
        self.assertEqual(self.c.state,State.LOST)
