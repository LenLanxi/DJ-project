"""Bounded, asynchronous JSONL diagnostics. No frames or audio are stored."""
import json
from pathlib import Path
from queue import Queue, Empty, Full
import threading
import time
import uuid


class SessionLog:
    def __init__(self, directory, metadata, *, capacity=2048, max_bytes=16*1024*1024, max_segments=8):
        self.session_id = "follow_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:10]
        self.path = Path(directory).absolute() / (self.session_id + ".jsonl")
        self.queue = Queue(maxsize=capacity)
        self.stopping = threading.Event()
        self.submit_lock = threading.Lock()
        self.finished = threading.Event()
        self.error = None
        self.dropped = 0
        self.rejected_after_close = 0
        self.written = 0
        self.bytes_written = 0
        self.max_bytes = max_bytes
        self.max_segments = max_segments
        self.paths = [self.path]
        self._detail_at = {}
        self.submit(dict(event="session_start", session_id=self.session_id,
                         unix_time=time.time(), **metadata))
        self.worker = threading.Thread(target=self._write, name="follow-log", daemon=True)
        self.worker.start()

    def submit(self, record):
        # Never wait for disk/serialization on the capture or control thread.
        with self.submit_lock:
            if self.stopping.is_set():
                self.rejected_after_close += 1
                return False
            if self.error is not None:
                self.dropped += 1
                return False
            try:
                self.queue.put_nowait(record)
                return True
            except Full:
                self.dropped += 1
                return False

    def _compact(self, record):
        # Keep every timestamp/result/channel. Sample only bulky diagnostic
        # internals once a second and whenever the reason/state changes.
        event = record.get('event')
        if event not in ('observation', 'motion_request'):
            return record
        stamp = record.get('time', 0.)
        key = (record.get('reason'), record.get('state'), record.get('vertical_guard'))
        previous_at, previous_key = self._detail_at.get(event, (-float('inf'), None))
        if stamp-previous_at >= 1 or key != previous_key:
            self._detail_at[event] = stamp, key
            return dict(record, diagnostic_detail=True)
        result = dict(record, diagnostic_detail=False)
        bulky = ('recognition_attempts', 'rejected_quads_sample', 'openpose_control')
        for field in bulky:
            result.pop(field, None)
        return result

    def _write(self):
        stream = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stream = self.path.open('x', encoding='utf-8', newline='\n')
            segment_bytes = 0
            last_flush = time.monotonic()
            while not self.stopping.is_set() or not self.queue.empty():
                try:
                    record = self.queue.get(timeout=.05)
                except Empty:
                    stream.flush()
                    continue
                line = json.dumps(self._compact(record), ensure_ascii=False, allow_nan=False) + '\n'
                size = len(line.encode('utf-8'))
                if size > self.max_bytes:
                    self.error = 'log_size_limit_reached'
                    self.dropped += 1 + self.queue.qsize()
                    break
                if segment_bytes + size > self.max_bytes:
                    if len(self.paths) >= self.max_segments:
                        self.error = 'log_size_limit_reached'
                        self.dropped += 1 + self.queue.qsize()
                        break
                    stream.close()
                    path = self.path.with_name(self.path.stem + '.part%02d.jsonl' % (len(self.paths)+1))
                    stream = path.open('x', encoding='utf-8', newline='\n')
                    self.paths.append(path)
                    segment_bytes = 0
                stream.write(line)
                segment_bytes += size
                self.bytes_written += size
                self.written += 1
                if time.monotonic()-last_flush >= .2:
                    stream.flush()
                    last_flush = time.monotonic()
            if self.error is None:
                footer = json.dumps(dict(event='session_end', session_id=self.session_id,
                    segments=[p.name for p in self.paths], dropped_records=self.dropped,
                    complete=self.dropped == 0)) + '\n'
                # Reserve a bounded footer; per-segment byte cap excludes footer.
                stream.write(footer)
                self.bytes_written += len(footer.encode('utf-8'))
                self.written += 1
        except Exception as exc:
            self.error = type(exc).__name__ + ": " + str(exc)
        finally:
            if stream is not None:
                stream.close()
            self.finished.set()

    def close(self, wait=True):
        # Serialize closure with acceptance, never with filesystem operations.
        with self.submit_lock:
            self.stopping.set()
        if wait and self.worker is not threading.current_thread():
            self.worker.join(timeout=.3)

    def status(self):
        return dict(session_id=self.session_id, path=str(self.path),
                    segments=[str(p) for p in self.paths],
                    written_records=self.written, dropped_records=self.dropped,
                    error=self.error, writer_finished=self.finished.is_set(),
                    pending_records=self.queue.qsize(),
                    rejected_after_close=self.rejected_after_close,
                    flush_complete=self.finished.is_set() and self.queue.empty() and self.error is None,
                    incomplete=self.dropped > 0 or self.error is not None)
