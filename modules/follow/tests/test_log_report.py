import json
from pathlib import Path
import tempfile
import unittest

from log_report import diagnostic_report, frame_stats, latest_session, session_report, write_report


def frame(seq, t, found=True, event="frame", state="tracking", **extra):
    return {"event": event, "time_monotonic": t,
            "observation": {"frame_id": seq, "found": found, "size_ratio": .2, "width": 640,
                            "reason": "" if found else "not_found"},
            "request": {"state": state, "right": 0, "forward": 0}, **extra}


def transition(t, old, new, reason=""):
    return {"event": "state_transition", "time_monotonic": t, "previous_state": old,
            "request": {"state": new, "right": 0, "forward": 0, "reason": reason},
            "last_stop_reason": reason}


class LogReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def save(self, rows, name="demo_1.jsonl"):
        path = self.folder / name
        path.write_text("".join(json.dumps(r)+"\n" for r in rows), encoding="utf-8")
        return path

    def test_deduplicates_frames_not_ticks_or_transitions(self):
        path = self.save([frame(1, 0), frame(1, .05, event="tick"),
                          frame(1, .06, event="state_transition"), frame(1, .07),
                          frame(2, .1, False), frame(3, .3)])
        r = session_report(path)
        self.assertEqual(r["all_frames"]["processed_frames"], 3)
        self.assertEqual(r["all_frames"]["found_frames"], 2)
        self.assertAlmostEqual(r["all_frames"]["longest_miss_seconds"], .2)
        self.assertIn("重复帧", r["warnings"][0])

    def test_explicit_transitions_count_once_and_manual_start_not_recovery(self):
        recovered = transition(.3, "recovering_target", "tracking")
        path = self.save([frame(1, 0), transition(.1, "tracking", "recovering_target"),
                          recovered, recovered, transition(.6, "tracking", "target_lost",
                          "recovery_timeout_requires_start"),
                          frame(2, .7, event="tick", last_stop_reason="recovery_timeout_requires_start"),
                          transition(.8, "target_lost", "tracking")])
        r = session_report(path)
        self.assertEqual(r["automatic_recoveries"], 1)
        self.assertEqual(r["stop_events"], {"recovery_timeout_requires_start": 1})

    def test_incomplete_line_does_not_modify_raw_or_mark_closed(self):
        path = self.save([frame(1, 0)])
        with path.open("a", encoding="utf-8") as f:
            f.write('{"event":')
        original = path.read_bytes()
        report = write_report(path)
        self.assertEqual(original, path.read_bytes())
        r = json.loads(report.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertFalse(r["session_closed"])
        self.assertTrue(any("第2行" in x for x in r["warnings"]))
        self.assertIsNone(r["automatic_recoveries"])

    def test_short_phases_separate_from_session_and_aborted_not_completed(self):
        path = self.save([frame(1, 0), {"event": "short_test_start",
                                      "diagnostic_log": "diagnostic_2.jsonl"}])
        self.save([{"frame_id": 10, "elapsed_seconds": 0, "phase": "站立不动",
                    "found": True, "test_context": {"session_log": path.name}},
                   {"frame_id": 11, "elapsed_seconds": 10, "phase": "缓慢左右移动",
                    "found": False}], "diagnostic_2.jsonl")
        (self.folder/"diagnostic_2.txt").write_text("20秒人体佩戴标记短测\n提前结束\n", encoding="utf-8")
        r = session_report(path)
        self.assertEqual(r["all_frames"]["processed_frames"], 1)
        short = r["short_tests"][0]
        self.assertEqual(short["completion"], "aborted")
        self.assertEqual(short["phases"]["站立不动"]["detection_rate"], 1)
        self.assertEqual(short["phases"]["缓慢左右移动"]["detection_rate"], 0)
        self.assertEqual(short["phases"]["缓慢左右移动"]["longest_miss_seconds"], 0)

    def test_completed_phase_tail_uses_known_phase_boundary(self):
        path = self.save([{"frame_id": 1, "elapsed_seconds": 19.5, "phase": "缓慢左右移动",
                           "found": False}], "diagnostic_2.jsonl")
        path.with_suffix(".txt").write_text("20秒人体佩戴标记短测\n测试结束\n", encoding="utf-8")
        r = diagnostic_report(path)["phases"]["缓慢左右移动"]
        self.assertEqual(r["longest_miss_seconds"], .5)
        self.assertTrue(r["open_miss_run"])

    def test_new_input_error_counts_as_miss_without_observation(self):
        path = self.save([{"event": "frame", "time_monotonic": 1, "observation": None,
                          "frame_diagnostic": {"frame_id": 1, "found": False,
                                               "reason": "input_error:ValueError"}}])
        stats = session_report(path)["all_frames"]
        self.assertEqual(stats["missed_frames"], 1)
        self.assertEqual(stats["miss_reasons"], {"input_error:ValueError": 1})

    def test_end_of_session_does_not_extend_last_miss_to_close(self):
        path = self.save([frame(1, 0, False), frame(2, .2, False),
                          {"event": "session_summary", "time_monotonic": 50}])
        r = session_report(path)
        self.assertTrue(r["session_closed"])
        self.assertEqual(r["all_frames"]["longest_miss_seconds"], .2)
        self.assertTrue(r["all_frames"]["open_miss_run"])

    def test_nontracking_nonzero_records_flagged_without_transition_duplicate(self):
        bad = frame(1, .1, state="paused")
        bad["request"]["right"] = .1
        path = self.save([{**bad, "event": "state_transition"}, bad])
        self.assertEqual(session_report(path)["nontracking_nonzero_log_records"], 1)

    def test_latest_prefers_camera_excludes_auto_and_uses_creation_order(self):
        def start(source, auto=False):
            return [{"event": "session_start", "source": source, "automated_gui_check": auto}]
        camera = self.save(start("电脑摄像头"), "demo_20.jsonl")
        self.save(start("生成画面"), "demo_21.jsonl")
        self.save(start("电脑摄像头", True), "demo_22.jsonl")
        self.save(start("电脑摄像头"), "demo_10.jsonl")  # later modification, older creation
        self.assertEqual(latest_session(self.folder), camera)

    def test_legacy_auto_flag_from_sidecar(self):
        camera = self.save([{"event": "session_start", "source": "电脑摄像头"}], "demo_10.jsonl")
        auto = self.save([{"event": "session_start", "source": "电脑摄像头"}], "demo_20.jsonl")
        auto.with_suffix(".summary.json").write_text('{"automated_gui_check": true}', encoding="utf-8")
        self.assertEqual(latest_session(self.folder), camera)

    def test_empty_frames_are_unknown_not_zero_or_perfect(self):
        path = self.save([{"event": "session_start"}])
        self.assertIsNone(session_report(path)["all_frames"]["detection_rate"])
        self.assertIsNone(frame_stats([])["detection_rate"])

    def test_missing_child_and_cross_session_child_are_not_silently_merged(self):
        path = self.save([{"event": "short_test_start", "diagnostic_log": "diagnostic_1.jsonl"},
                          {"event": "short_test_start", "diagnostic_log": "diagnostic_2.jsonl"}])
        self.save([{"frame_id": 1, "found": True, "elapsed_seconds": 0,
                    "test_context": {"session_log": "demo_other.jsonl"}}], "diagnostic_2.jsonl")
        r = session_report(path)
        self.assertEqual(r["short_tests"], [])
        self.assertEqual(len([w for w in r["warnings"] if "短测" in w]), 2)


if __name__ == "__main__":
    unittest.main()
