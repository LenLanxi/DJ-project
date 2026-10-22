import json
from pathlib import Path
import tempfile
import unittest

from r3_follow.core import FollowController, Observation
from r3_follow.diagnostics import ShortTest, follow_hint


class DiagnosticsTests(unittest.TestCase):
    def test_reacquired_hint_does_not_claim_tracking(self):
        c = FollowController()
        c.observe(Observation(1, 10, 'test', 640, 480, 0, True, 320, 240, .25))
        c.calibrate(10)
        c.start(10)
        c.observe(Observation(2, 10.1, 'test', 640, 480, 0, False))
        c.tick(10.1)
        c.observe(Observation(3, 10.2, 'test', 640, 480, 0, True, 320, 240, .25))
        self.assertIn('已重新识别', follow_hint(c, True))
        self.assertIn('跟随仍停止', follow_hint(c, True))
        self.assertNotIn('已重新识别', follow_hint(c, False))

    def test_short_test_boundaries_duplicates_and_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            test = ShortTest(Path(folder)/'diagnostic.jsonl', 100)
            self.assertEqual(test.phase(104)[0], '准备')
            self.assertEqual(test.phase(105)[0], '站立不动')
            self.assertEqual(test.phase(115)[0], '缓慢左右移动')
            self.assertEqual(test.phase(125)[0], '完成')
            def record(t, seq, found=True):
                test.record(t, frame_id=seq, found=found, reason='detected' if found else 'not_found',
                            size_px=55 if found else None, detection_ms=2, frame_age_ms=30, state='stopped')
            record(104, 1)
            record(105, 2)
            record(106, 2)
            record(115, 5, False)
            record(125, 6)
            report = test.finish(125)
            rows = [json.loads(x) for x in test.path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]['skipped_capture_frames'], 2)
            self.assertIn('识别到1帧', report.read_text(encoding='utf-8-sig'))
            self.assertIn('漏检1帧', report.read_text(encoding='utf-8-sig'))
            self.assertEqual({p.suffix for p in Path(folder).iterdir()}, {'.jsonl', '.txt'})
            self.assertEqual(test.finish(126), report)

    def test_empty_aborted_test_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as folder:
            test = ShortTest(Path(folder)/'empty.jsonl', 0)
            report = test.finish(2, aborted=True).read_text(encoding='utf-8-sig')
            self.assertIn('提前结束', report)
            self.assertIn('无可用画面', report)


if __name__ == '__main__':
    unittest.main()
