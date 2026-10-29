"""Structured observations can be consumed by sandbox Python branches/loops."""
import base64
import math
import time
import threading
from collections import deque
from pathlib import Path
from navigation.Observation import OBSERVATION_SCHEMA, SEED_MAX_AGE, CATCHUP_SECONDS, Observation


def encode_image(frame, bounds=None):
    """Encode once; navigation already bounds its frames before tracking."""
    import cv2
    if bounds is not None:
        height,width = frame.shape[:2]
        scale=min(1.,bounds[0]/width,bounds[1]/height)
        if scale < 1:frame=cv2.resize(frame,(round(width*scale),round(height*scale)),interpolation=cv2.INTER_AREA)
    ok,encoded = cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,80])
    if not ok: raise RuntimeError('navigation_image_encoding_failed')
    return encoded.tobytes()


class NavigationFrames:
    """Bounded decoder history; polling never invents capture identities."""
    def __init__(self, backend):
        self.backend = backend
        self.simulated = backend.get_status().simulated
        self.frames = deque(maxlen=110)
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.run,daemon=True,name='navigation-frames')
        self.thread.start()
    def run(self):
        import cv2
        while not self.stopped.is_set():
            try:
                packet = self.backend.get_frame()
                identity, stamp = packet.frame_id,packet.timestamp
                if type(identity) is not int or identity < 0 or type(stamp) not in (int,float) or not math.isfinite(stamp):
                    raise ValueError('navigation_source_identity_invalid')
                if (not 0 <= time.time()-stamp <= 1.0
                        or packet.stale or packet.frame is None):
                    raise RuntimeError('navigation_source_frame_expired')
                with self.lock:
                    previous = self.frames[-1] if self.frames else None
                    if previous is not None:
                        if identity < previous['frame_id'] or identity == previous['frame_id'] and stamp != previous['timestamp'] or identity > previous['frame_id'] and stamp <= previous['timestamp']:
                            self.stopped.set()  # A new source invalidates existing target seeds.
                            raise ValueError('navigation_source_identity_changed')
                        repeated = identity == previous['frame_id']
                    else: repeated = False
                if repeated:
                    self.stopped.wait(.1)
                    continue
                frame = packet.frame
                height,width = frame.shape[:2]
                scale = min(1.,480/max(height,width))
                frame = cv2.resize(frame,(round(width*scale),round(height*scale)),interpolation=cv2.INTER_AREA) if scale<1 else frame.copy()
                value = {'frame':frame,'frame_id':identity,'timestamp':stamp,'source':'sdk_camera',
                         'simulated':self.simulated}
                with self.lock:
                    if not self.stopped.is_set(): self.frames.append(value)
                    self.error = None
            except Exception as exc:
                with self.lock: self.error = type(exc).__name__+': '+str(exc)[:150]
            self.stopped.wait(.1)
    def snapshot(self):
        with self.lock:
            if self.error is not None or self.stopped.is_set(): raise RuntimeError(self.error or 'navigation_capture_stopped')
            values = list(self.frames)
        if not values or not 0 <= time.time()-values[-1]['timestamp'] <= 1.0:
            raise RuntimeError('navigation_source_frame_expired')
        return values
    def close(self): self.stopped.set()

class Vision:
    def __init__(self,client,backend,test_image=None):
        self.client,self.backend=client,backend
        self.test_image=test_image
        self._frames = self._tracker = self._stable = self._target = self._observation = None
        self._generation = 0
        self._lock = threading.RLock()
    def close(self):
        with self._lock:
            self._generation += 1
            if self._frames is not None: self._frames.close()
            self._frames = self._tracker = self._stable = self._target = self._observation = None
    reset = close
    def _context(self):
        with self._lock:
            if self._frames is None or self._frames.stopped.is_set():
                if self.backend is None: raise RuntimeError('navigation_source_frame_expired')
                self._generation += 1
                self._tracker = self._stable = self._target = self._observation = None
                self._frames = NavigationFrames(self.backend)
            return self._frames,self._generation
    def _current(self, frames, generation):
        if frames.stopped.is_set() or self._frames is not frames or self._generation != generation:
            raise RuntimeError('navigation_context_cancelled')
    def prepare_navigation(self, not_before, cancelled):
        """Only image stability is measured; this cannot prove physical hover."""
        import cv2
        import numpy as np
        frames,generation = self._context()
        deadline = time.monotonic()+1.8
        previous = None; stable_since = None; count = 0
        while time.monotonic() < deadline:
            self._current(frames,generation)
            if cancelled.is_set(): raise RuntimeError('program_cancelled')
            try: values = frames.snapshot()
            except RuntimeError:
                time.sleep(.02); continue
            for packet in values:
                if packet['timestamp'] <= not_before or previous is not None and packet['frame_id'] <= previous['frame_id']: continue
                gray = cv2.cvtColor(cv2.resize(packet['frame'],(160,120)),cv2.COLOR_BGR2GRAY).astype(np.float32)
                settled = False
                if previous is not None:
                    before = previous['gray']
                    if before.std()<2 and gray.std()<2: settled = float(np.abs(before-gray).mean()) <= 2
                    else:
                        shift,quality = cv2.phaseCorrelate(before,gray)
                        settled = quality >= .2 and math.hypot(*shift) <= 1.5 and float(np.abs(before-gray).mean()) <= 12
                if settled: count += 1
                else: stable_since = packet['timestamp']; count = 1
                previous = dict(packet,gray=gray)
                if count >= 3 and packet['timestamp']-stable_since >= .15:
                    with self._lock:
                        self._current(frames,generation)
                        self._stable = packet
                    return {'frame_id':packet['frame_id'],'timestamp':packet['timestamp'],'image_stable':True,
                            'physical_hover_verified':False}
            time.sleep(.015)
        raise RuntimeError('navigation_image_stability_not_confirmed')
    def frame(self):
        if self.test_image:
            path=Path(self.test_image)
            data=path.read_bytes()
            if not 0<len(data)<=5_000_000: raise ValueError('图片须小于5MB')
            if data.startswith(b'\x89PNG\r\n\x1a\n'): mime='image/png'
            elif data.startswith(b'\xff\xd8\xff'): mime='image/jpeg'
            else: raise ValueError('仅支持PNG/JPEG')
            return {'source':'static_test_image','simulated':True,'frame_id':str(path.stat().st_mtime_ns),
                    'timestamp':path.stat().st_mtime,'image_url':'data:'+mime+';base64,'+base64.b64encode(data).decode()}
        metadata,jpeg = self._capture()
        return dict(metadata,image_url='data:image/jpeg;base64,'+base64.b64encode(jpeg).decode())
    def _capture(self):
        if self.test_image:
            import cv2
            import numpy as np
            metadata=self.frame()
            data=base64.b64decode(metadata.pop('image_url').split(',',1)[1])
            frame=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
            if frame is None: raise ValueError('invalid_static_image')
            return metadata,encode_image(frame,getattr(self.client,'image_bounds',None))
        snap=self.backend.get_frame()
        if snap.stale or snap.frame is None or not 0<=time.time()-snap.timestamp<=1.0:
            raise RuntimeError('真实画面缺失或超过1秒，取消本次观察')
        metadata={'source':'sdk_camera','simulated':self.backend.get_status().simulated,
                  'frame_id':snap.frame_id,'timestamp':snap.timestamp}
        return metadata,encode_image(snap.frame,getattr(self.client,'image_bounds',None))
    def describe(self,question):
        """Read-only answers can tolerate inference delay, never authorize motion."""
        if self.client is None: raise RuntimeError('未配置视觉模型API')
        frame,jpeg=self._capture()
        answer=self.client.describe_image(jpeg,question)
        if not isinstance(answer,str) or not answer.strip() or len(answer)>12000:
            raise ValueError('视觉回答格式无效')
        return {'answer':answer,'age_seconds':max(0,time.time()-frame['timestamp']),
                'read_only':True,**frame}
    def _advance(self, frames, generation):
        self._current(frames,generation)
        tracker = self._tracker
        current = tracker.current(require_fresh=False)
        age = time.time()-current['target_tracking']['semantic_timestamp']
        deadline = time.monotonic()+min(CATCHUP_SECONDS,max(0.,SEED_MAX_AGE-age))
        while True:
            values = frames.snapshot()
            for packet in values:
                if packet['frame_id'] > current['frame_id']:
                    if time.monotonic() >= deadline: raise RuntimeError('navigation_tracker_catchup_deadline')
                    current = tracker.update(packet,require_fresh=False)
            if 0 <= time.time()-current['timestamp'] <= 1.0:
                result = tracker.current(require_fresh=True)
                break
            if time.monotonic() >= deadline: raise RuntimeError('navigation_tracker_catchup_deadline')
            self._current(frames,generation)
        self._current(frames,generation)
        return self._observation.tracked(result).to_dict()
    def refresh_observation(self, observation):
        with self._lock:
            frames,generation = self._frames,self._generation
            if frames is None or self._tracker is None: raise RuntimeError('navigation_target_not_seeded')
            expected = observation.get('target_tracking',{}).get('track_id')
            if expected is None: raise RuntimeError('navigation_tracking_identity_missing')
            result = self._advance(frames,generation)
            if result['target_tracking']['track_id'] != expected: raise RuntimeError('navigation_tracking_identity_changed')
            return result
    def observe(self,target):
        if self.client is None: raise RuntimeError('未配置用于结构化观察的视觉模型API')
        frames,generation = self._context()
        if self._stable is None: self.prepare_navigation(time.time(),threading.Event())
        with self._lock:
            self._current(frames,generation)
            if self._target == target and self._tracker is not None:
                try: return self._advance(frames,generation)
                except RuntimeError: self._tracker = None
            packet = self._stable
            if packet is None or not 0 <= time.time()-packet['timestamp'] <= 1.0: raise RuntimeError('navigation_source_frame_expired')
        jpeg = encode_image(packet['frame'])
        # Leave time to propagate the semantic seed through recorded frames.
        timeout = SEED_MAX_AGE-CATCHUP_SECONDS-(time.time()-packet['timestamp'])
        if timeout <= 0: raise RuntimeError('navigation_source_frame_expired')
        raw = self.client.observe_image(jpeg,target,timeout=timeout)
        if not 0 <= time.time()-packet['timestamp'] <= SEED_MAX_AGE-CATCHUP_SECONDS:
            raise RuntimeError('navigation_semantic_request_expired')
        obs = Observation.parse(raw, **{key:packet[key] for key in
            ('source','simulated','frame_id','timestamp')}, fresh=True)
        with self._lock:
            self._current(frames,generation)
            self._observation = obs
            if not obs.visible:
                self._tracker = None; self._target = target
                return obs.to_dict()
            from navigation.TargetTracker import TargetTracker
            tracker = TargetTracker()
            tracker.seed(packet, obs.bbox, require_fresh=False)
            self._tracker,self._target = tracker,target
            return self._advance(frames,generation)
