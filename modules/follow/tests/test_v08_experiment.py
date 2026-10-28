import tempfile
import unittest
from pathlib import Path
from r3_follow.core import FollowController, Observation, State
from r3_follow.diagnostics import ExperimentSession, follow_hint


class ExperimentTests(unittest.TestCase):
    def frame(self, c, seq, t, found=True, reason=""):
        c.observe(Observation(seq, t, "test", 640, 480, 0, found,
                              450 if found else None, 240 if found else None,
                              .25 if found else None, reason=reason))
        return c.tick(t)

    def armed(self, window):
        c = FollowController(recovery_window=window)
        self.frame(c, 0, 0)
        c.calibrate(0)
        c.start(0)
        return c

    def test_two_second_gap_compares_windows(self):
        for window, expected in ((1.5, State.LOST), (3, State.TRACKING)):
            c = self.armed(window)
            for seq in range(1, 22):
                req = self.frame(c, seq, seq/10, False)
                self.assertEqual((req.right, req.forward), (0, 0))
            for seq in range(22, 26):
                self.frame(c, seq, seq/10)
            self.assertEqual(c.state, expected)

    def test_three_seconds_flicker_cannot_extend_deadline(self):
        c = self.armed(3)
        for seq in range(1, 33):
            self.frame(c, seq, seq/10, found=(seq % 2 == 0))
        self.assertEqual(c.state, State.LOST)
        self.assertEqual(c.last_stop_reason, "recovery_timeout_requires_start")
        self.frame(c, 33, 3.3)
        self.assertEqual(c.last_stop_reason, "recovery_timeout_requires_start")
        self.assertIn("超过恢复等待时间", follow_hint(c, True))

    def test_three_seconds_does_not_relax_watchdog_or_old_frame(self):
        c = self.armed(3)
        self.frame(c, 1, .1, False)
        self.frame(c, 1, .6, True)  # duplicate sequence cannot refresh
        c.tick(1.11)
        self.assertEqual(c.state, State.STALE)
        self.assertEqual(c.last_request.right, 0)
        self.frame(c, 2, 1.2)
        self.assertEqual(c.state, State.STALE)

    def test_duplicate_target_latches_and_retains_reason(self):
        c = self.armed(3)
        self.frame(c, 1, .1, False, "ambiguous_duplicate_id")
        self.frame(c, 2, .2)
        self.assertEqual(c.state, State.LOST)
        self.assertEqual(c.last_stop_reason, "ambiguous_duplicate_id")

    def test_pause_stop_cancel_recovery(self):
        for action, expected in (("pause", State.PAUSED), ("stop", State.STOPPED)):
            c = self.armed(3)
            self.frame(c, 1, .1, False)
            getattr(c, action)()
            for seq in range(2, 7):
                self.frame(c, seq, seq/10)
            self.assertEqual(c.state, expected)
            self.assertEqual(c.last_request.right, 0)

    def test_short_test_locks_actions_aborts_and_unlocks(self):
        with tempfile.TemporaryDirectory() as folder:
            c = self.armed(3)
            session = ExperimentSession(c)
            self.assertTrue(session.begin(Path(folder)/"test.jsonl", 1))
            self.assertEqual(c.state, State.STOPPED)
            for action in ("start", "calibrate", "pause", "resume"):
                self.assertFalse(session.allowed(action))
            self.assertTrue(session.allowed("stop"))
            self.assertFalse(session.set_recovery(1.5))
            self.assertFalse(session.begin(Path(folder)/"second.jsonl", 2))
            report = session.finish(2, aborted=True)
            self.assertIn("未完成", report.read_text(encoding="utf-8-sig"))
            self.assertTrue(session.allowed("start"))
            self.assertEqual(c.state, State.STOPPED)
            self.assertTrue(session.set_recovery(1.5))

    def test_completed_short_test_does_not_start_following(self):
        with tempfile.TemporaryDirectory() as folder:
            c = self.armed(3)
            session = ExperimentSession(c)
            session.begin(Path(folder)/"test.jsonl", 0)
            report = session.finish(25)
            self.assertEqual(c.state, State.STOPPED)
            self.assertIn("无可用画面", report.read_text(encoding="utf-8-sig"))

    def test_setting_locked_while_following_and_core_default_unchanged(self):
        self.assertEqual(FollowController().recovery_window, 0)
        c = self.armed(3)
        session = ExperimentSession(c)
        self.assertFalse(session.set_recovery(1.5))
        c.stop()
        for value in (-1, 0, 2, float("nan")):
            self.assertFalse(session.set_recovery(value))
        self.assertTrue(session.set_recovery(1.5))


if __name__ == "__main__":
    unittest.main()
