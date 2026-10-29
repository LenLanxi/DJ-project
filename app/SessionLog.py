"""Redacted JSONL with a shared decimal 1 GB runtime retention budget.

Only closed segments made by this logger are recycled. Active segments and old
pre-rotation logs count toward the budget but are never removed. A crashed
Active segment is deliberately retained. Capacity is reconciled every five
seconds or at rotation; concurrent writers can briefly exceed the shared budget.
"""
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
_FILES_LOCK = threading.Lock()
_SESSION = r'\d{8}T\d{6}[0-9a-f]{8}'


class SessionLog:
    """Best-effort diagnostics; logging failures never stop a running task."""

    def __init__(self, path=None, *, max_bytes=1_000_000_000, segment_bytes=16_000_000):
        if type(max_bytes) is not int or type(segment_bytes) is not int \
                or max_bytes <= 0 or segment_bytes <= 0:
            raise ValueError('log byte budgets must be positive integers')
        self.session_id = time.strftime('%Y%m%dT%H%M%S') + uuid.uuid4().hex[:8]
        self.max_bytes, self.segment_bytes = max_bytes, min(segment_bytes, max_bytes)
        self.lock = threading.Lock()
        self.sequence = 0
        self.finalized = False
        self.dropped_records = 0
        self.error = None
        self._remaining = 0
        self._checked_at = 0.
        self._retry_at = 0.
        self.explicit = path is not None
        self.path = Path(path) if self.explicit else ROOT / 'logs' / 'Runtime' / \
            ('Session' + self.session_id + 'Active.jsonl')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.prefix = self.path.stem + 'Part' if self.explicit else 'Session'
        suffix = re.escape(self.path.suffix)
        self.closed_pattern = re.compile(re.escape(self.prefix) + _SESSION + r'Part\d{6}' + suffix)
        self.owned_pattern = self.closed_pattern if self.explicit else re.compile(
            r'(?:Session' + _SESSION + r'(?:Active|Part\d{6})|' + _SESSION + r')\.jsonl')
        self.marker = self.path.with_name(self.path.name + '.active') if self.explicit else None
        with _FILES_LOCK:
            if self.marker is not None:
                # Exclusive creation rejects two writers to the same explicit path.
                self.marker.touch(exist_ok=False)
            try:
                self.handle = self.path.open('a' if self.explicit else 'x', encoding='utf-8',
                                             buffering=1, newline='\n')
            except BaseException:
                if self.marker is not None:
                    self.marker.unlink()
                raise
        self.secrets = tuple(v for k, v in os.environ.items() if v and len(v) >= 8
                             and any(w in k.upper() for w in ('_KEY', 'TOKEN', 'SECRET', 'PASSWORD')))

    def clean(self, value, key=''):
        if any(w in key.lower() for w in ('api_key', 'authorization', 'password', 'secret')) \
                or ('token' in key.lower() and isinstance(value, str)):
            return '[redacted]'
        if key.lower() in ('image_url', 'audio', 'pcm', 'frame'):
            return '[payload omitted]'
        if isinstance(value, dict):
            return {str(k): self.clean(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value[:3000]]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, '[redacted]')
            value = re.sub(r'(?i)(bearer\s+)[^\s\"\']+', r'\1[redacted]', value)
            value = re.sub(r'(?i)([?&](?:token|key|access_token)=)[^&#\s]+', r'\1[redacted]', value)
            if value.startswith('data:'):
                return '[payload omitted]'
            return value if len(value) <= 16000 else value[:16000] + '[truncated]'
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self.clean(str(value))

    def _files(self):
        for path in self.path.parent.iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            if path == self.path or self.owned_pattern.fullmatch(path.name):
                try:
                    yield path, path.stat()
                except FileNotFoundError:  # Another process may have recycled it.
                    continue

    def _reserve(self, size):
        now = time.monotonic()
        if size <= self._remaining and now-self._checked_at < 5:
            self._remaining -= size
            return
        files = list(self._files())
        total = sum(info.st_size for _, info in files)
        closed = sorted(((info.st_mtime_ns, path) for path, info in files
                         if self.closed_pattern.fullmatch(path.name)), key=lambda item: item[0])
        for _, path in closed:
            if total + size <= self.max_bytes:
                break
            try:
                length = path.stat().st_size
                path.unlink()
                total -= length
            except OSError:
                continue  # A busy file must not prevent trying older closed files.
        if total + size > self.max_bytes:
            raise RuntimeError('运行日志保留预算不足；活动日志或历史文件不能自动删除')
        self._remaining = self.max_bytes-total-size
        self._checked_at = now

    def _rotate(self):
        self._remaining = 0
        self.handle.close()
        self.sequence += 1
        closed = self.path.with_name(self.prefix + self.session_id +
                                     f'Part{self.sequence:06d}' + self.path.suffix)
        self.path.rename(closed)
        self.handle = self.path.open('x', encoding='utf-8', buffering=1, newline='\n')

    def _failed(self, exc):
        self.error = self.clean(type(exc).__name__ + ': ' + str(exc))
        self._retry_at = time.monotonic() + 5

    def __call__(self, event):
        with self.lock:
            if self.finalized or time.monotonic() < self._retry_at:
                self.dropped_records += 1
                return False
            try:
                record = self.clean(dict(event, timestamp=time.time(), session_id=self.session_id))
                line = json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n'
                size = len(line.encode('utf-8'))
                if size > self.max_bytes:
                    raise ValueError('单条运行日志超过保留预算')
                with _FILES_LOCK:
                    if self.handle.tell() and self.handle.tell() + size > self.segment_bytes:
                        self._rotate()
                    self._reserve(size)
                    self.handle.write(line)
                self.error = None
                return True
            except Exception as exc:
                self.dropped_records += 1
                self._failed(exc)
                return False

    def close(self):
        with self.lock, _FILES_LOCK:
            if self.finalized:
                return
            try:
                self.handle.close()
                if self.marker is not None:
                    self.marker.unlink()
                else:
                    self.sequence += 1
                    closed = self.path.with_name(self.prefix + self.session_id +
                                                 f'Part{self.sequence:06d}' + self.path.suffix)
                    self.path.rename(closed)
                    self.path = closed
            except OSError as exc:
                self._failed(exc)
            self.finalized = True
