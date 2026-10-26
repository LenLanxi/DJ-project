"""Independent microphone and speaker streams; PCM16 mono at 24kHz.

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
import math
import queue
import threading
import time

from AudioConfig import AudioConfig
from AudioDevices import BLOCK_FRAMES, RATE, describe_endpoints, resolve_endpoints


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
            key=(self.parts[0][0] if self.parts else self.current)
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
        self.input=queue.Queue(maxsize=100); self.speaker=PlaybackBuffer()
        self.fault=None; self.streams=[]; self.endpoints=None
        self.closing=False; self.last_capture=0; self.last_render=0
        self.mic_rms=0.0
        self._level_lock=threading.Lock()

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
        self.closing=False; self.fault=None
        self.last_capture=self.last_render=time.monotonic()
        def finished():
            if not self.closing: self.fault='音频设备流已停止；请检查耳机连接并重新启动，不自动切换设备'
        def capture(data,frames,timing,status):
            self.last_capture=time.monotonic()
            if status: self.fault='麦克风音频丢帧：'+str(status)
            try:
                self._update_level(data)
                self.input.put_nowait(bytes(data))
            except queue.Full: self.fault='麦克风发送积压，停止会话以避免延迟继续增加'
            except Exception as exc: self.fault='麦克风读取失败：'+str(exc)[:200]
        def render(out,frames,timing,status):
            self.last_render=time.monotonic()
            if status: self.fault='耳机音频丢帧：'+str(status)
            self.speaker.latency_ms=max(0,(timing.outputBufferDacTime-timing.currentTime)*1000)
            out[:]=output_pcm(self.speaker.consume(frames),self.endpoints[1].channels)
        try:
            # Append immediately so failure to open output also closes the input stream.
            self.streams.append(sd.RawInputStream(**self.endpoints[0].stream_kwargs(),
                blocksize=BLOCK_FRAMES,callback=capture,finished_callback=finished))
            self.streams.append(sd.RawOutputStream(**self.endpoints[1].stream_kwargs(),
                blocksize=BLOCK_FRAMES,callback=render,finished_callback=finished))
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
            try: yield self.input.get_nowait()
            except queue.Empty: await asyncio.sleep(0.005)
    def close(self):
        self.closing=True
        self.speaker.interrupt()
        for stream in self.streams:
            try: stream.abort()
            except Exception: pass
            try: stream.close()
            except Exception: pass
        self.streams=[]
