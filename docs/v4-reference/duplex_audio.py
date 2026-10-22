"""Independent microphone and speaker streams; PCM16 mono at 24kHz."""
import asyncio
import collections
import queue
import threading

class PlaybackBuffer:
    def __init__(self,rate=24000,max_seconds=15):
        self.rate=rate; self.limit=rate*2*max_seconds
        self.parts=collections.deque(); self.size=0
        self.lock=threading.Lock(); self.current=None
        self.played={}; self.generated={}; self.latency_ms=0
    def append(self,item,index,data):
        if len(data)%2: raise ValueError('odd PCM byte count')
        key=(item,index)
        with self.lock:
            if self.size+len(data)>self.limit: raise RuntimeError('音频输出队列超限')
            self.parts.append((key,bytearray(data))); self.size+=len(data)
            self.generated[key]=self.generated.get(key,0)+len(data)
    def consume(self,frames):
        need=frames*2; output=bytearray()
        with self.lock:
            while self.parts and len(output)<need:
                key,data=self.parts[0]; n=min(need-len(output),len(data))
                output+=data[:n]; del data[:n]; self.size-=n
                self.current=key; self.played[key]=self.played.get(key,0)+n
                if not data: self.parts.popleft()
        return bytes(output)+b'\x00'*(need-len(output))
    def interrupt(self):
        with self.lock:
            key=self.current or (self.parts[0][0] if self.parts else None)
            self.parts.clear(); self.size=0
            if not key: return None
            # Conservative: subtract estimated device-buffer latency from submitted bytes.
            ms=max(0,int(self.played.get(key,0)*1000/(self.rate*2)-self.latency_ms))
            ms=min(ms,int(self.generated.get(key,0)*1000/(self.rate*2)))
            self.current=None
            return {'item_id':key[0],'content_index':key[1],'audio_end_ms':ms}

class DuplexAudio:
    def __init__(self,input_device=None,output_device=None):
        self.input_device=input_device; self.output_device=output_device
        self.input=queue.Queue(maxsize=100); self.speaker=PlaybackBuffer()
        self.fault=None; self.streams=[]
    def start(self):
        import sounddevice as sd
        def capture(data,frames,timing,status):
            if status: self.fault='麦克风音频丢帧：'+str(status)
            try: self.input.put_nowait(bytes(data))
            except queue.Full: self.fault='麦克风发送积压，停止会话以避免延迟继续增加'
        def render(out,frames,timing,status):
            if status: self.fault='耳机音频丢帧：'+str(status)
            self.speaker.latency_ms=max(0,(timing.outputBufferDacTime-timing.currentTime)*1000)
            out[:]=self.speaker.consume(frames)
        try:
            self.streams=[sd.RawInputStream(samplerate=24000,blocksize=480,channels=1,dtype='int16',
                           device=self.input_device,callback=capture),
                          sd.RawOutputStream(samplerate=24000,blocksize=480,channels=1,dtype='int16',
                           device=self.output_device,callback=render)]
            for stream in self.streams: stream.start()
        except BaseException:
            self.close(); raise
    async def chunks(self):
        while True:
            if self.fault: raise RuntimeError(self.fault)
            try: yield self.input.get_nowait()
            except queue.Empty: await asyncio.sleep(0.005)
    def close(self):
        self.speaker.interrupt()
        for stream in self.streams:
            try: stream.abort(); stream.close()
            except Exception: pass
        self.streams=[]
