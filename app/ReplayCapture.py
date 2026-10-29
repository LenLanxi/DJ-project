"""Opt-in evidence archive. The flight/video threads never wait for disk writes."""
import json
from pathlib import Path
import queue
import shutil
import threading
import time
import zipfile


class ReplayCapture:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=False)
        (self.root / 'Frames').mkdir()
        self.pending = queue.Queue(maxsize=512)
        self.dropped = 0
        self.error = None
        self.written = {'video_packet': 0, 'rtc_frame': 0}
        self.closed = False
        self.thread = threading.Thread(target=self.write, name='replay-writer', daemon=True)
        self.thread.start()

    def submit(self, kind, data, **metadata):
        if self.closed:
            return
        row = dict(kind=kind, timestamp=time.time(), monotonic=time.monotonic(), **metadata)
        try:
            self.pending.put_nowait((row, data))
        except queue.Full:
            self.dropped += 1

    def write(self):
        try:
            with (self.root / 'Drone.h264').open('wb') as video, \
                    (self.root / 'Media.jsonl').open('w', encoding='utf-8') as index:
                while True:
                    item = self.pending.get()
                    if item is None:
                        break
                    row, data = item
                    if row['kind'] == 'video_packet':
                        row.update(offset=video.tell(), bytes=len(data))
                        video.write(data)
                        video.flush()
                    elif row['kind'] == 'rtc_frame':
                        name = 'Frame%010d.jpg' % row['frame_id']
                        (self.root / 'Frames' / name).write_bytes(data)
                        row['file'] = 'Frames/' + name
                    index.write(json.dumps(row, ensure_ascii=False) + '\n')
                    index.flush()
                    self.written[row['kind']] += 1
        except Exception as exc:
            self.error = repr(exc)

    def snapshot_source(self, project):
        project = Path(project)
        with zipfile.ZipFile(self.root / 'Source.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
            for folder in ('app', 'drone_sdk', 'execution', 'providers', 'navigation', 'follow', 'web'):
                for file in (project / folder).rglob('*'):
                    if any(part in ('node_modules', '__pycache__', 'logs', 'Build') for part in file.parts):
                        continue
                    if file.is_file() and file.suffix in ('.py', '.js', '.json', '.html'):
                        archive.write(file, file.relative_to(project))
            for name in ('pyproject.toml', 'uv.lock', 'start_flight.ps1'):
                archive.write(project / name, name)

    def close(self):
        self.closed = True
        if self.thread.is_alive():
            try:
                self.pending.put(None, timeout=5)
            except queue.Full:
                self.error = self.error or 'recording_queue_did_not_drain'
            self.thread.join(10)
        for folder in (self.root / 'Audio').glob('*'):
            parts = sorted(folder.glob('video*.webm'))
            if parts:
                try:
                    with (folder / 'Video.webm').open('wb') as output:
                        for part in parts:
                            with part.open('rb') as source:
                                shutil.copyfileobj(source, output)
                except OSError as exc:
                    self.error = self.error or repr(exc)
        audio = {kind: len(list((self.root / 'Audio').glob('*/' + kind + '*.wav')))
                 for kind in ('mic', 'model')}
        summary = dict(closed_at=time.time(), writer_finished=not self.thread.is_alive(),
                       dropped=self.dropped, error=self.error, queued=self.pending.qsize(),
                       written=self.written, audio_segments=audio)
        (self.root / 'CaptureEnd.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        return summary
