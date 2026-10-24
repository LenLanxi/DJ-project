"""Bounded, asynchronous JSONL diagnostics. No frames or audio are stored."""
import json
from pathlib import Path
from queue import Queue, Empty, Full
import threading
import time
import uuid


class SessionLog:
    def __init__(self, directory, metadata, *, capacity=2048, max_bytes=16*1024*1024):
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

    def _write(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("x", encoding="utf-8", newline="\n") as stream:
                while not self.stopping.is_set() or not self.queue.empty():
                    try:
                        record = self.queue.get(timeout=.05)
                    except Empty:
                        continue
                    line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
                    size = len(line.encode("utf-8"))
                    if self.bytes_written + size > self.max_bytes:
                        self.error = "log_size_limit_reached"
                        self.dropped += 1 + self.queue.qsize()
                        break
                    stream.write(line)
                    stream.flush()
                    self.bytes_written += size
                    self.written += 1
                if self.error is None:
                    footer = json.dumps(dict(event="session_end", session_id=self.session_id,
                        dropped_records=self.dropped, complete=self.dropped == 0)) + "\n"
                    size = len(footer.encode("utf-8"))
                    if self.bytes_written + size <= self.max_bytes:
                        stream.write(footer)
                        self.bytes_written += size
                        self.written += 1
                    else:
                        self.error = "log_size_limit_reached"
        except Exception as exc:
            self.error = type(exc).__name__ + ": " + str(exc)
        finally:
            self.finished.set()

    def close(self, wait=True):
        # Serialize closure with acceptance, never with filesystem operations.
        with self.submit_lock:
            self.stopping.set()
        if wait and self.worker is not threading.current_thread():
            self.worker.join(timeout=.3)

    def status(self):
        return dict(session_id=self.session_id, path=str(self.path),
                    written_records=self.written, dropped_records=self.dropped,
                    error=self.error, writer_finished=self.finished.is_set(),
                    pending_records=self.queue.qsize(),
                    rejected_after_close=self.rejected_after_close,
                    flush_complete=self.finished.is_set() and self.queue.empty() and self.error is None,
                    incomplete=self.dropped > 0 or self.error is not None)
