import unittest
from r3_follow.core import FollowController, Observation, State


class RequestRampTests(unittest.TestCase):
    def sample(self, c, seq, now, found=True, x=600, size=.25, reason=""):
        c.observe(Observation(seq, now, "test", 640, 480, 0, found,
                              x if found else None, 240 if found else None,
                              size if found else None, reason=reason))
        return c.tick(now)

    def ready(self):
        c=FollowController(recovery_window=3,max_request_rate=.8)
        self.sample(c,0,0)
        c.calibrate(0)
        c.start(0)
        return c

    def test_rate_bound_after_start_and_sustained_direction(self):
        c=self.ready()
        previous=c.last_request.right
        self.assertEqual(previous,0)
        for i in range(1,11):
            req=self.sample(c,i,i*.05)
            self.assertLessEqual(req.right-previous,.040001)
            self.assertLessEqual(abs(req.right),.3)
            previous=req.right
        self.assertAlmostEqual(previous,.3)

    def test_loss_and_stop_zero_immediately(self):
        for action in ("loss","stop","pause","invalid"):
            c=self.ready()
            for i in range(1,9):self.sample(c,i,i*.05)
            self.assertAlmostEqual(c.last_request.right,.3)
            if action=="loss":self.sample(c,9,.45,False)
            elif action=="invalid":c.observe(None);c.tick(.45)
            else:getattr(c,action)()
            self.assertEqual((c.last_request.right,c.last_request.forward),(0,0))

    def test_recovery_restarts_ramp_from_zero(self):
        c=self.ready()
        for i in range(1,9):self.sample(c,i,i*.05)
        self.sample(c,9,.45,False)
        for seq,t in ((10,.5),(11,.6),(12,.7)):
            self.sample(c,seq,t)
        self.assertEqual(c.state,State.TRACKING)
        self.assertEqual(c.last_request.right,0)
        self.assertAlmostEqual(self.sample(c,13,.75).right,.04)

    def test_direction_reversal_brakes_before_ramping_other_way(self):
        c=self.ready()
        for i in range(1,9):self.sample(c,i,i*.05)
        self.assertEqual(self.sample(c,9,.45,x=20).right,0)
        self.assertAlmostEqual(self.sample(c,10,.5,x=20).right,-.04)

    def test_deadband_zero_does_not_keep_old_motion(self):
        c=self.ready()
        for i in range(1,9):self.sample(c,i,i*.05)
        self.assertEqual(self.sample(c,9,.45,x=320).right,0)

    def test_stale_frame_and_repeated_tick_dont_bypass_zero(self):
        c=self.ready()
        self.sample(c,1,.1)
        old=c.last_request.right
        self.assertEqual(c.tick(.1).right,old)
        self.assertEqual(c.tick(.61).right,0)
        self.sample(c,2,.7)
        self.assertEqual(c.last_request.right,0)

    def test_rate_validation_and_default_compatibility(self):
        self.assertIsNone(FollowController().max_request_rate)
        for value in (0,-1,float("nan"),float("inf"),"fast",True):
            with self.assertRaises(ValueError):FollowController(max_request_rate=value)

    def test_forward_axis_is_limited_too(self):
        c=self.ready()
        req=self.sample(c,1,.05,size=.1)
        self.assertAlmostEqual(req.forward,.04)
        self.assertEqual(self.sample(c,2,.1,size=.25).forward,0)
