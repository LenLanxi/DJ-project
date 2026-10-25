"""Bounded capture backlog and stateful PCM16 sample-rate conversion."""
import collections
import math
import queue
import threading
import time

class CaptureQueue:
    """Mono PCM queue, bounded by samples AND enqueue age rather than callback count."""
    def __init__(self, rate, milliseconds=200):
        self.rate=rate; self.limit=int(rate*milliseconds/1000)*2
        self.seconds=milliseconds/1000; self.parts=collections.deque(); self.size=0
        self.lock=threading.Lock(); self.dropped_samples=0; self.loss_generation=0
        self.callbacks=0; self.max_gap_ms=0.; self.last_at=None
        self.input_overflows=0; self.output_underflows=0
    def _drop(self,n):
        self.dropped_samples+=n//2; self.loss_generation+=1
    def put(self, data, *, now=None, overflow=False):
        now=time.monotonic() if now is None else now
        with self.lock:
            self.callbacks+=1
            if self.last_at is not None: self.max_gap_ms=max(self.max_gap_ms,(now-self.last_at)*1000)
            self.last_at=now
            if overflow:
                self.input_overflows+=1; self.loss_generation+=1
            self.parts.append([now,bytes(data)]); self.size+=len(data)
            excess=max(0,self.size-self.limit)
            while excess:
                _,old=self.parts[0]; count=min(excess,len(old))
                self.parts[0][1]=old[count:]; self.size-=count; excess-=count
                self._drop(count)
                if not self.parts[0][1]: self.parts.popleft()
    def get_nowait(self, *, now=None):
        now=time.monotonic() if now is None else now
        with self.lock:
            while self.parts and now-self.parts[0][0]>self.seconds:
                _,old=self.parts.popleft(); self.size-=len(old); self._drop(len(old))
            if not self.parts: raise queue.Empty
            _,data=self.parts.popleft(); self.size-=len(data)
            return data,self.loss_generation
    def diagnostics(self):
        with self.lock:
            return dict(callbacks=self.callbacks,queued_ms=round(self.size/2/self.rate*1000,2),
                        dropped_samples=self.dropped_samples,
                        dropped_ms=round(self.dropped_samples/self.rate*1000,2),
                        loss_generation=self.loss_generation,max_callback_gap_ms=round(self.max_gap_ms,2),
                        input_overflows=self.input_overflows,output_underflows=self.output_underflows)

class PCMResampler:
    """Persistent band-limited SoXR; never resample each chunk as an independent signal."""
    def __init__(self,source,target):
        self.source,self.target=source,target; self.engine=None
        if source!=target:
            import soxr
            self.engine=soxr.ResampleStream(source,target,1,dtype='float32',quality='MQ')
    def process(self,pcm,*,last=False):
        if self.engine is None: return pcm
        import numpy as np
        data=np.frombuffer(pcm,dtype='<i2').astype(np.float32)/32768
        result=self.engine.resample_chunk(data,last=last)
        return np.clip(np.rint(result*32768),-32768,32767).astype('<i2').tobytes()
    def clear(self):
        if self.engine is not None: self.engine.clear()
    def delay_ms(self):
        return self.engine.delay()/self.target*1000 if self.engine is not None else 0.

class OutputAdapter:
    def __init__(self,speaker,rate):
        self.speaker=speaker; self.rate=rate; self.pending=bytearray()
        self.resampler=PCMResampler(24000,rate); self.generation=speaker.generation
    def render(self,frames,dac_latency_ms):
        # Called by one output callback; only it owns conversion state.
        if self.generation!=self.speaker.generation:
            self.pending.clear(); self.resampler.clear(); self.generation=self.speaker.generation
        needed=frames*2
        for _ in range(64):
            if len(self.pending)>=needed: break
            count=max(1,math.ceil((needed-len(self.pending))/2*24000/self.rate))
            self.pending.extend(self.resampler.process(self.speaker.consume(count)))
        if len(self.pending)<needed: raise RuntimeError('output_resampler_stalled')
        output=bytes(self.pending[:needed]); del self.pending[:needed]
        self.speaker.latency_ms=max(0,dac_latency_ms)+self.resampler.delay_ms()+len(self.pending)/2/self.rate*1000
        return output
