import unittest
import numpy as np
from video_replay import crop_frame, gap_statistics, paired_summary


class ReplayMetricTests(unittest.TestCase):
    def test_irregular_timestamps_use_elapsed_time_and_include_tail(self):
        rows = [dict(pts=t, found=hit) for t,hit in [(0,False),(.1,True),(.4,False),(.9,False)]]
        stats = gap_statistics(rows, "found", 1.1)
        self.assertEqual(stats["gap_count"], 2)
        self.assertAlmostEqual(stats["longest_gap_seconds"], .7)
        self.assertAlmostEqual(stats["total_not_detected_seconds"], .8)
        self.assertEqual(stats["found"], 1)
        self.assertTrue(all(r["edge"] for r in stats["runs"]))

    def test_paired_counts_are_partition_not_independent_totals(self):
        rows = [dict(pts=i*.1, baseline_found=a, current_found=b)
                for i,(a,b) in enumerate([(True,True),(True,False),(False,True),(False,False)])]
        stats=paired_summary(rows,.4)
        self.assertEqual([stats[k] for k in ("both_detected","regressions","newly_detected","both_missing")], [1,1,1,1])

    def test_duplicate_pts_cannot_silently_make_zero_duration(self):
        with self.assertRaises(ValueError):
            gap_statistics([dict(pts=0, found=False),dict(pts=0, found=True)],"found",1)

    def test_roi_outside_frame_is_an_error_not_silent_partial_crop(self):
        frame=np.zeros((480,640,3), np.uint8)
        with self.assertRaises(ValueError):
            crop_frame(frame,(24,175,960,720))

    def test_zero_hits_and_all_hits(self):
        for value in (True,False):
            result=gap_statistics([dict(pts=i*.1, found=value) for i in range(3)],"found",.3)
            self.assertEqual(result["found"],3 if value else 0)
            self.assertAlmostEqual(result["longest_gap_seconds"],0 if value else .3)


if __name__ == "__main__":
    unittest.main()
