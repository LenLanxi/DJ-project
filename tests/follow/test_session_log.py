from follow.r3_follow.config import load_config
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from follow.r3_follow.session_log import SessionLog
from follow.r3_follow import FollowService
from tests.helpers.FollowFixture import FakeDevice
from FollowFixtures import GeometryDetector
from app.SessionLog import SessionLog as RuntimeLog


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
            rows = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
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
            self.assertIn('"race"', log.path.read_text(encoding="utf-8"))
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
            port = FakeDevice(lambda:t[0])
            service = FollowService(port, tracker=GeometryDetector(), log_directory=directory,
                monotonic=lambda:t[0], wall_clock=lambda:1800000000+t[0], config=load_config())
            port.publish((True,320,160),1,1800000010.)
            self.assertTrue(service.capture_once())
            self.assertTrue(service.calibrate_reference().ok)
            self.assertTrue(service._start_tracking().ok)
            t[0] += .1
            port.publish((True,550,160),2,1800000000+t[0])
            self.assertTrue(service.capture_once())
            service.control_once()
            service.close()
            self.assertTrue(service.session_log.finished.wait(1))
            rows = [json.loads(line) for line in service.session_log.path.read_text(encoding="utf-8").splitlines()]
            events = {row["event"] for row in rows}
            self.assertTrue({"observation","motion_request","cleanup_completed","stop"} <= events)
            self.assertTrue(any(row.get("channels",[0,0,0,0])[3]>0 for row in rows))
            self.assertTrue(all("frame" not in row and "image" not in row for row in rows))
            self.assertEqual(service.status()["log"]["session_id"], rows[0]["session_id"])

    def test_log_failure_does_not_prevent_stop_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory)/"file"
            bad.write_text("not a directory")
            port = FakeDevice()
            service = FollowService(port, log_directory=bad, config=load_config())
            self.assertTrue(service.session_log.finished.wait(1))
            service.grant = (port.request_control("follow"), port.lease)[1]
            port.update_velocity(service.grant,left_right=0,forward_back=20,up_down=0,yaw=20)
            self.assertTrue(service.close().ok)
            self.assertIsNone(port.lease)
            self.assertEqual(port.velocity,(0,0,0,0))
            self.assertIsNotNone(service.status()["log"]["error"])

    def test_async_follow_records_share_runtime_retention_without_closing_sink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Runtime.jsonl'
            runtime = RuntimeLog(path, max_bytes=8000, segment_bytes=1200)
            for index in range(25):
                runtime({'event': 'application', 'index': index, 'detail': 'x' * 300})
            sink_threads = []
            def sink(record):
                sink_threads.append(threading.current_thread().name)
                runtime(record)
            now = [10.]
            port = FakeDevice(lambda: now[0])
            unused_directory = Path(directory) / 'Unused'
            service = FollowService(port, tracker=GeometryDetector(), log_sink=sink,
                log_directory=unused_directory, monotonic=lambda: now[0],
                wall_clock=lambda: 1800000000 + now[0], config=load_config())
            try:
                port.publish((True, 320, 160), 1, 1800000010.)
                self.assertTrue(service.capture_once())
                self.assertTrue(service.calibrate_reference().ok)
                self.assertTrue(service._start_tracking().ok)
                now[0] += .1
                port.publish((True, 550, 160), 2, 1800000000 + now[0])
                self.assertTrue(service.capture_once())
                service.control_once()
                self.assertTrue(service.close().ok)
                self.assertTrue(service.session_log.finished.wait(1))
                self.assertTrue(service.session_log.status()['flush_complete'])
                self.assertTrue(service.session_log.status()['shared_sink'])
                self.assertFalse(unused_directory.exists())
                self.assertTrue(sink_threads)
                self.assertEqual(set(sink_threads), {'follow-log'})
                runtime({'event': 'application_after_follow_close'})
            finally:
                service.close()
                runtime.close()
            files = list(Path(directory).glob('*.jsonl'))
            self.assertLessEqual(sum(candidate.stat().st_size for candidate in files), 8000)
            rows = [json.loads(line) for candidate in files
                    for line in candidate.read_text(encoding='utf-8').splitlines()]
            follow_rows = [row for row in rows if 'follow_session_id' in row]
            self.assertTrue({'observation', 'motion_request', 'cleanup_completed', 'session_end'}
                            <= {row['event'] for row in follow_rows})
            self.assertEqual({row['session_id'] for row in follow_rows}, {runtime.session_id})
            self.assertEqual({row['follow_session_id'] for row in follow_rows},
                             {service.session_log.session_id})
            self.assertTrue(any(row['event'] == 'application_after_follow_close' for row in rows))

    def test_shared_sink_failure_is_visible_but_does_not_prevent_control_cleanup(self):
        def failed_sink(record):
            raise OSError('runtime disk failed')
        port = FakeDevice()
        service = FollowService(port, log_sink=failed_sink, config=load_config())
        self.assertTrue(service.session_log.finished.wait(1))
        service.grant = (port.request_control('follow'), port.lease)[1]
        port.update_velocity(service.grant, left_right=0, forward_back=20, up_down=0, yaw=20)
        self.assertTrue(service.close().ok)
        self.assertIsNone(port.lease)
        self.assertEqual(port.velocity, (0, 0, 0, 0))
        status = service.status()['log']
        self.assertIn('runtime disk failed', status['error'])
        self.assertFalse(status['flush_complete'])
        self.assertTrue(status['incomplete'])


if __name__ == "__main__":
    unittest.main()
