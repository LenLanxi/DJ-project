import json
from pathlib import Path
import tempfile
import unittest

from app.ReplayCapture import ReplayCapture
from app.SessionLog import SessionLog


class ReplayCaptureTests(unittest.TestCase):
    def test_original_video_and_rtc_jpeg_are_preserved_with_timestamps(self):
        with tempfile.TemporaryDirectory() as folder:
            capture = ReplayCapture(Path(folder) / 'Test')
            capture.submit('video_packet', b'originalh264', stream_open=1)
            capture.submit('rtc_frame', b'originaljpeg', frame_id=7, decoded_at=123.5)
            summary = capture.close()
            self.assertFalse(summary['error'])
            self.assertEqual(summary['dropped'], 0)
            self.assertEqual((capture.root / 'Drone.h264').read_bytes(), b'originalh264')
            self.assertEqual((capture.root / 'Frames' / 'Frame0000000007.jpg').read_bytes(), b'originaljpeg')
            rows = [json.loads(line) for line in (capture.root / 'Media.jsonl').read_text().splitlines()]
            self.assertEqual(rows[1]['decoded_at'], 123.5)
            self.assertGreater(rows[0]['timestamp'], 0)

    def test_evidence_log_neither_truncates_nor_recycles_segments(self):
        with tempfile.TemporaryDirectory() as folder:
            log = SessionLog(Path(folder) / 'Runtime.jsonl', max_bytes=100, segment_bytes=50, retain_all=True)
            for _ in range(3):
                self.assertTrue(log({'text': 'x' * 17000, 'password': 'private'}))
            log.close()
            rows = [json.loads(line) for file in Path(folder).glob('*.jsonl') for line in file.read_text().splitlines()]
            self.assertEqual(len(rows), 3)
            self.assertEqual(len(rows[0]['text']), 17000)
            self.assertEqual(rows[0]['password'], '[redacted]')
