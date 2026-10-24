import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from r3_follow.session_log import SessionLog
from r3_follow.service import FollowService
from r3_follow.mock_port import MockPort
from test_service_lifecycle_fix import GeometryDetector


class SessionLogTests(unittest.TestCase):
    def test_session_identity_utf8_path_and_jsonl_footer(self):
        with tempfile.TemporaryDirectory(prefix="三组 日志 ") as directory:
            log = SessionLog(directory, {"version":"test", "hardware_verified":False})
            self.assertTrue(log.submit({"event":"motion_request", "channels":[0,0,0,20]}))
            log.close()
            self.assertTrue(log.finished.wait(1))
            rows = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["session_id"], log.session_id)
            self.assertEqual(rows[-1]["event"], "session_end")
            self.assertTrue(rows[-1]["complete"])
            self.assertEqual(rows[1]["channels"], [0,0,0,20])
            self.assertTrue(log.status()["flush_complete"])

    def test_slow_writer_does_not_block_producer_and_overflow_is_reported(self):
        gate = threading.Event()
        original = SessionLog._write
        def blocked(log):
            gate.wait(2)
            original(log)
        with tempfile.TemporaryDirectory() as directory, patch.object(SessionLog, "_write", blocked):
            log = SessionLog(directory, {}, capacity=2)
            try:
                self.assertTrue(log.submit({"event":"first"}))
                self.assertFalse(log.submit({"event":"dropped"}))
                self.assertEqual(log.dropped, 1)
                log.close(wait=False)
                self.assertFalse(log.finished.is_set())
            finally:
                gate.set()
                self.assertTrue(log.finished.wait(2))
            rows = [json.loads(line) for line in log.path.read_text().splitlines()]
            self.assertFalse(rows[-1]["complete"])
            self.assertTrue(log.status()["incomplete"])

    def test_close_cannot_finish_between_acceptance_and_enqueue(self):
        entered, release, closing, closed = [threading.Event() for _ in range(4)]
        with tempfile.TemporaryDirectory() as directory:
            log = SessionLog(directory, {})
            original = log.queue.put_nowait
            def gated(record):
                entered.set()
                release.wait(2)
                original(record)
            with patch.object(log.queue, "put_nowait", gated):
                writer = threading.Thread(target=lambda: log.submit({"event":"race"}))
                def close():
                    closing.set()
                    log.close()
                    closed.set()
                closer = threading.Thread(target=close)
                writer.start()
                self.assertTrue(entered.wait(1))
                closer.start()
                self.assertTrue(closing.wait(1))
                self.assertFalse(closed.wait(.03))
                release.set()
                writer.join(1)
                closer.join(1)
            self.assertTrue(log.finished.wait(1))
            self.assertEqual(log.queue.qsize(), 0)
            self.assertIn('"race"', log.path.read_text())
            self.assertFalse(log.submit({"event":"too_late"}))
            self.assertEqual(log.rejected_after_close, 1)

    def test_unwritable_location_and_size_limit_report_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory)/"file"
            bad.write_text("not a directory")
            log = SessionLog(bad, {})
            self.assertTrue(log.finished.wait(1))
            self.assertIsNotNone(log.status()["error"])
            self.assertTrue(log.status()["incomplete"])
            log.close()
            limited = SessionLog(directory, {}, max_bytes=1)
            self.assertTrue(limited.finished.wait(1))
            self.assertEqual(limited.error, "log_size_limit_reached")
            self.assertTrue(limited.status()["incomplete"])
            limited.close()

    def test_service_records_detection_control_cleanup_without_image_content(self):
        with tempfile.TemporaryDirectory() as directory:
            t = [10.]
            port = MockPort(lambda:t[0])
            service = FollowService(port, tracker=GeometryDetector(), log_directory=directory,
                monotonic=lambda:t[0], wall_clock=lambda:1800000000+t[0])
            port.publish((True,320,160),1,1800000010.)
            self.assertTrue(service.capture_once())
            self.assertTrue(service.calibrate_reference().ok)
            self.assertTrue(service.start_follow().ok)
            t[0] += .1
            port.publish((True,550,160),2,1800000000+t[0])
            self.assertTrue(service.capture_once())
            service.control_once()
            service.close()
            self.assertTrue(service.session_log.finished.wait(1))
            rows = [json.loads(line) for line in service.session_log.path.read_text().splitlines()]
            events = {row["event"] for row in rows}
            self.assertTrue({"observation","motion_request","cleanup_completed","stop"} <= events)
            self.assertTrue(any(row.get("channels",[0,0,0,0])[3]>0 for row in rows))
            self.assertTrue(all("frame" not in row and "image" not in row for row in rows))
            self.assertEqual(service.get_follow_status()["log"]["session_id"], rows[0]["session_id"])

    def test_log_failure_does_not_prevent_stop_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory)/"file"
            bad.write_text("not a directory")
            port = MockPort()
            service = FollowService(port, log_directory=bad)
            self.assertTrue(service.session_log.finished.wait(1))
            service.lease = port.request_control("follow").lease
            port.update_velocity(service.lease,left_right=0,forward_back=20,up_down=0,yaw=20)
            self.assertTrue(service.close().ok)
            self.assertIsNone(port.lease)
            self.assertEqual(port.velocity,(0,0,0,0))
            self.assertIsNotNone(service.get_follow_status()["log"]["error"])


if __name__ == "__main__":
    unittest.main()
