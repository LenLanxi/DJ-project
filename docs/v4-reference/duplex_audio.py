"""Independent microphone and speaker streams; PCM16 mono at 24kHz.

有线耳麦现场注意事项
--------------------
- PortAudio/sounddevice **不提供** NS/AEC 开关。系统级降噪与回声消除必须在
  操作系统或声卡驱动里开启（Windows：声音设置 -> 输入设备 -> 增强 -> 噪声抑制；
  或在 Realtek/Intel 音频控制面板打开"回声消除"）。本模块只能做软件侧的门限
  与半双工保护。
- 设备编号可用 AI_INPUT_DEVICE / AI_OUTPUT_DEVICE 环境变量强制指定，
  设备名或默认设备变动时不必改代码。
- 耳机被拔掉时回调会报告异常：这里记录 fault 并由 chunks() 抛错，
  由实时会话安全退出，不会让音频线程崩溃整个进程。
"""
import array
import asyncio
import collections
import math
import os
import queue
import threading

INPUT_DEVICE_ENV = 'AI_INPUT_DEVICE'
OUTPUT_DEVICE_ENV = 'AI_OUTPUT_DEVICE'


def resolve_device(value, env_name):
    """显式参数优先；否则读环境变量里的设备编号；都没有则用系统默认。"""
    if value is not None:
        return value
    raw = (os.getenv(env_name) or '').strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError('%s 必须是设备编号整数，当前为 %r' % (env_name, raw)) from None


def pcm_rms(data):
    """16bit 单声道 PCM 的 RMS。用于判断麦克风是否真的有声音。"""
    samples = array.array('h')
    try:
        samples.frombytes(bytes(data))
    except ValueError:
        return 0.0
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


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
        self.input_device=resolve_device(input_device, INPUT_DEVICE_ENV)
        self.output_device=resolve_device(output_device, OUTPUT_DEVICE_ENV)
        self.input=queue.Queue(maxsize=100); self.speaker=PlaybackBuffer()
        self.fault=None; self.streams=[]
        self.mic_rms=0.0          # 最近麦克风电平，供打断防抖判断
        self.mic_peak=0.0
        self._level_lock=threading.Lock()

    def _update_level(self, data):
        level=pcm_rms(data)
        with self._level_lock:
            # 轻微衰减，避免单个尖峰长期把门限撑高
            self.mic_rms=max(level, self.mic_rms*0.9)
            self.mic_peak=max(level, self.mic_peak*0.95)

    def level(self):
        with self._level_lock:
            return self.mic_rms

    def start(self):
        import sounddevice as sd
        def capture(data,frames,timing,status):
            try:
                if status: self.fault='麦克风音频丢帧：'+str(status)
                self._update_level(data)
                self.input.put_nowait(bytes(data))
            except queue.Full:
                self.fault='麦克风发送积压，停止会话以避免延迟继续增加'
            except Exception as exc:      # 设备被拔掉时 PortAudio 会在这里抛错
                self.fault='麦克风读取失败：'+str(exc)[:200]
        def render(out,frames,timing,status):
            try:
                if status: self.fault='耳机音频丢帧：'+str(status)
                self.speaker.latency_ms=max(0,(timing.outputBufferDacTime-timing.currentTime)*1000)
                out[:]=self.speaker.consume(frames)
            except Exception as exc:
                self.fault='耳机写入失败：'+str(exc)[:200]
                out[:]=b'\x00'*(frames*2)
        try:
            self.streams=[sd.RawInputStream(samplerate=24000,blocksize=480,channels=1,dtype='int16',
                           device=self.input_device,callback=capture),
                          sd.RawOutputStream(samplerate=24000,blocksize=480,channels=1,dtype='int16',
                           device=self.output_device,callback=render)]
            for stream in self.streams: stream.start()
        except BaseException as exc:
            self.close()
            raise RuntimeError('音频设备打开失败：%s（可用 python tools/check_audio.py 查看设备编号）'
                               % str(exc)[:200]) from None

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
