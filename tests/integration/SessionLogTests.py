"""Small byte budgets exercise retention without cloud, devices or GB files."""
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.SessionLog import SessionLog

MODULE = importlib.import_module('app.SessionLog')


class RuntimeSessionLogTests(unittest.TestCase):
    def test_default_budget_is_shared_and_recycles_oldest_closed_segments(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(MODULE, 'ROOT', Path(folder)):
            first = SessionLog(max_bytes=650, segment_bytes=200)
            for index in range(4):
                first({'event': '中文', 'index': index})
            first.close()
            old_files = set(first.path.parent.glob('*.jsonl'))
            second = SessionLog(max_bytes=650, segment_bytes=200)
            for index in range(5):
                second({'event': '新记录', 'index': index})
            second.close()
            files = list(second.path.parent.glob('*.jsonl'))
            self.assertLessEqual(sum(path.stat().st_size for path in files), 650)
            self.assertTrue(old_files - set(files))
            records = [json.loads(line) for path in files
                       for line in path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(max(row['index'] for row in records
                                 if row['session_id'] == second.session_id), 4)

    def test_active_log_and_foreign_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(MODULE, 'ROOT', Path(folder)):
            active = SessionLog(max_bytes=400, segment_bytes=200)
            active({'event': 'active'})
            directory = active.path.parent
            foreign = directory / 'Notes.jsonl'
            foreign.write_text('user material', encoding='utf-8')
            nested = directory / 'Keep'
            nested.mkdir()
            nested.joinpath('Document.jsonl').write_text('nested material', encoding='utf-8')
            second = SessionLog(max_bytes=400, segment_bytes=200)
            try:
                for index in range(8):
                    second({'event': 'rolling', 'index': index})
                self.assertEqual(json.loads(active.path.read_text(encoding='utf-8'))['event'], 'active')
                self.assertEqual(foreign.read_text(encoding='utf-8'), 'user material')
                self.assertEqual(nested.joinpath('Document.jsonl').read_text(encoding='utf-8'), 'nested material')
            finally:
                second.close()
                active.close()

    def test_active_or_legacy_bytes_exhausting_budget_drop_without_deletion(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(MODULE, 'ROOT', Path(folder)):
            first = SessionLog(max_bytes=250)
            first({'event': 'active'})
            legacy = first.path.parent / '20261028T1234561234abcd.jsonl'
            legacy.write_text('x' * 180, encoding='utf-8')
            second = SessionLog(max_bytes=250)
            try:
                self.assertFalse(second({'event': 'cannot fit'}))
                self.assertIn('保留预算不足', second.error)
                self.assertEqual(second.dropped_records, 1)
                self.assertTrue(first.path.exists())
                self.assertEqual(legacy.read_text(encoding='utf-8'), 'x' * 180)
                self.assertEqual(second.path.read_text(encoding='utf-8'), '')
            finally:
                second.close()
                first.close()

    def test_explicit_path_rotates_only_its_own_namespace(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'Session.jsonl'
            foreign = Path(folder) / 'Another.jsonl'
            foreign.write_text('x' * 1000, encoding='utf-8')
            log = SessionLog(path, max_bytes=400, segment_bytes=180)
            for index in range(10):
                log({'event': 'rotate', 'index': index})
            log.close()
            files = [candidate for candidate in path.parent.iterdir() if candidate != foreign]
            self.assertLessEqual(sum(candidate.stat().st_size for candidate in files), 400)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['index'], 9)
            self.assertEqual(foreign.stat().st_size, 1000)
            log.close()

    def test_same_explicit_path_cannot_be_opened_by_another_active_logger(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'Session.jsonl'
            first = SessionLog(path)
            try:
                with self.assertRaises(FileExistsError):
                    SessionLog(path)
                first({'event': 'still active'})
            finally:
                first.close()
            second = SessionLog(path)
            second.close()

    def test_large_record_and_write_failures_are_reported_without_interrupting_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            log = SessionLog(Path(folder) / 'Session.jsonl', max_bytes=250)
            try:
                self.assertFalse(log({'event': 'x' * 1000}))
                self.assertIn('单条', log.error)
                log._retry_at = 0
                with patch.object(log, 'handle', wraps=log.handle) as handle:
                    handle.closed = False
                    handle.tell.return_value = 0
                    handle.write.side_effect = OSError('disk failed')
                    self.assertFalse(log({'event': 'write failure'}))
                    self.assertIn('disk failed', log.error)
                self.assertEqual(log.dropped_records, 2)
            finally:
                log.close()

    def test_redaction_is_preserved_after_rotation(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'TEST_API_KEY': 'PrivateSecret123'}):
            log = SessionLog(Path(folder) / 'Session.jsonl', max_bytes=1000, segment_bytes=200)
            for _ in range(3):
                log({'event': 'PrivateSecret123', 'image_url': 'data:image/secret',
                     'usage': {'input_tokens': 2}, 'authorization': 'Bearer abc'})
            log.close()
            text = ''.join(path.read_text(encoding='utf-8') for path in Path(folder).glob('*.jsonl'))
            self.assertNotIn('PrivateSecret123', text)
            self.assertNotIn('data:image', text)
            self.assertNotIn('Bearer abc', text)
            self.assertIn('"input_tokens": 2', text)

    def test_capacity_scan_is_not_repeated_for_each_event(self):
        with tempfile.TemporaryDirectory() as folder:
            log = SessionLog(Path(folder) / 'Session.jsonl')
            try:
                with patch.object(log, '_files', wraps=log._files) as files:
                    for index in range(20):
                        self.assertTrue(log({'event': 'sample', 'index': index}))
                    self.assertEqual(files.call_count, 1)
            finally:
                log.close()
