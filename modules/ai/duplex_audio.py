"""Independent device streams; API boundary stays PCM16 mono at 24kHz.

有线耳麦现场注意事项
--------------------
- PortAudio/sounddevice **不提供** NS/AEC 开关；这里的 noise_reduction 是
  云端 Realtime API 的近场降噪参数，不等于应用级回声消除。
- 设备索引必须显式指定，不从系统默认设备回退；配置由 audio_config.json 保存。
- 耳机被拔掉时 finished_callback / 2 秒无回调会置 fault，
  由 chunks() 抛错并安全退出，不自动切换设备。

正式实现位置：本文件已从 docs/v4-reference 迁入 modules/ai，
不再有跨目录 sys.path 依赖（audio_config / audio_devices 是同目录模块）。
"""
import array
import asyncio
import collections
import json
import math
import queue
import threading
import time

from audio_config import AudioConfig
from audio_streaming import CaptureQueue, PCMResampler, OutputAdapter
from audio_devices import RATE, describe_endpoints, resolve_endpoints


def output_pcm(mono, channels):
    """Keep the API/playback accounting mono; duplicate only at the device boundary."""
    if channels == 1:
        return mono
    stereo = bytearray(len(mono) * 2)
    stereo[0::4] = mono[0::2]
    stereo[1::4] = mono[1::2]
    stereo[2::4] = mono[0::2]
    stereo[3::4] = mono[1::2]
    return bytes(stereo)

class PlaybackBuffer:
    def __init__(self,rate=24000,max_seconds=15):
        self.rate=rate; self.limit=rate*2*max_seconds
        self.parts=collections.deque(); self.size=0
        self.lock=threading.Lock(); self.current=None
        self.played={}; self.generated={}; self.latency_ms=0; self.generation=0
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
            key=(self.parts[0][0] if self.parts else self.current)
            self.generation+=1
            self.parts.clear(); self.size=0
            if not key: return None
            # Conservative: subtract estimated device-buffer latency from submitted bytes.
            ms=max(0,int(self.played.get(key,0)*1000/(self.rate*2)-self.latency_ms))
            ms=min(ms,int(self.generated.get(key,0)*1000/(self.rate*2)))
            self.current=None
            return {'item_id':key[0],'content_index':key[1],'audio_end_ms':ms}

def pcm_rms(data):
    """16bit 单声道 PCM 的 RMS，用于打断门限判断。"""
    samples = array.array('h')
    try:
        samples.frombytes(bytes(data))
    except ValueError:
        return 0.0
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class DuplexAudio:
    def __init__(self,input_device=None,output_device=None,*,config=None):
        self.config=config or AudioConfig(input_device=input_device,output_device=output_device)
        self.input_device=self.config.input_device; self.output_device=self.config.output_device
        self.input=CaptureQueue(RATE,self.config.capture_buffer_ms); self.speaker=PlaybackBuffer()
        self.fault=None; self.streams=[]; self.endpoints=None
        self.closing=False; self.last_capture=0; self.last_render=0
        self.mic_rms=0.0
        self._level_lock=threading.Lock()
        self.on_input_gap=None

    def _update_level(self, data):
        level=pcm_rms(data)
        with self._level_lock:
            self.mic_rms=max(level, self.mic_rms*0.9)

    def level(self):
        with self._level_lock:
            return self.mic_rms

    def start(self):
        import sounddevice as sd
        if self.streams: raise RuntimeError('音频流已经启动')
        self.endpoints=resolve_endpoints(sd,self.config)
        describe_endpoints(self.endpoints)
        self.input=CaptureQueue(self.endpoints[0].rate,self.config.capture_buffer_ms)
        self.converter=PCMResampler(self.endpoints[0].rate,RATE)
        self.output_adapter=OutputAdapter(self.speaker,self.endpoints[1].rate)
        self._diagnostic_at=time.monotonic(); self._converted_generation=0
        self.closing=False; self.fault=None
        self.last_capture=self.last_render=time.monotonic()
        def finished():
            if not self.closing: self.fault='音频设备流已停止；请检查耳机连接并重新启动，不自动切换设备'
        def capture(data,frames,timing,status):
            self.last_capture=time.monotonic()
            try:
                raw=bytes(data)
                if len(raw)!=frames*2: raise ValueError('capture_size_mismatch')
                self._update_level(raw)
                before=self.loss_generation
                self.input.put(raw,overflow=bool(getattr(status,'input_overflow',False)))
                if self.loss_generation!=before and self.on_input_gap: self.on_input_gap()
            except Exception as exc: self.fault='麦克风读取失败：'+str(exc)[:200]
        def render(out,frames,timing,status):
            self.last_render=time.monotonic()
            if getattr(status,'output_underflow',False): self.input.output_underflows+=1
            try:
                pcm=self.output_adapter.render(frames,(timing.outputBufferDacTime-timing.currentTime)*1000)
                out[:]=output_pcm(pcm,self.endpoints[1].channels)
            except Exception as exc:
                out[:]=b'\0'*len(out); self.fault='音频回放失败：'+str(exc)[:200]
        try:
            # Append immediately so failure to open output also closes the input stream.
            self.streams.append(sd.RawInputStream(**self.endpoints[0].stream_kwargs(),
                blocksize=0,callback=capture,finished_callback=finished))
            self.streams.append(sd.RawOutputStream(**self.endpoints[1].stream_kwargs(),
                blocksize=0,callback=render,finished_callback=finished))
            for stream in self.streams: stream.start()
        except BaseException:
            self.close(); raise
    async def chunks(self):
        while True:
            if self.fault: raise RuntimeError(self.fault)
            if not self.streams or any(not stream.active for stream in self.streams):
                raise RuntimeError('音频流未运行；请检查耳机连接并重新启动')
            if time.monotonic()-min(self.last_capture,self.last_render)>2:
                raise RuntimeError('音频设备超过2秒无回调；请检查连接，不自动切换设备')
            if time.monotonic()-self._diagnostic_at>=self.config.diagnostics_interval_s:
                print('audio_diagnostics '+json.dumps(self.diagnostics(),ensure_ascii=False),flush=True)
                self._diagnostic_at=time.monotonic()
            try:
                raw,generation=self.input.get_nowait()
                if generation!=self._converted_generation:
                    self.converter.clear(); self._converted_generation=generation
                pcm=self.converter.process(raw)
                converted_at=time.monotonic()
                # Bound API packet size regardless of the host callback's block size.
                for offset in range(0,len(pcm),960):
                    if generation!=self.loss_generation: break
                    if time.monotonic()-converted_at>self.input.seconds:
                        with self.input.lock:
                            remaining=len(pcm)-offset
                            self.input._drop(int(remaining/2*self.input.rate/RATE)*2)
                        if self.on_input_gap: self.on_input_gap()
                        break
                    yield pcm[offset:offset+960]
            except queue.Empty:
                if self.on_input_gap: self.on_input_gap()
                await asyncio.sleep(0.005)
    @property
    def loss_generation(self):
        return self.input.loss_generation

    def diagnostics(self):
        return {**self.input.diagnostics(),
                'input_rate':self.endpoints[0].rate if self.endpoints else None,
                'output_rate':self.endpoints[1].rate if self.endpoints else None,'api_rate':RATE}

    def close(self):
        self.closing=True
        self.speaker.interrupt()
        for stream in self.streams:
            try: stream.abort()
            except Exception: pass
            try: stream.close()
            except Exception: pass
        self.streams=[]
