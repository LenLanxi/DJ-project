"""Optional PortAudio I/O. Imports happen only when hardware is requested."""
import io
import wave
import time

def wav_bytes(pcm, rate=16000, channels=1):
    output = io.BytesIO()
    with wave.open(output, 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return output.getvalue()

def decode_wav(data):
    with wave.open(io.BytesIO(data), 'rb') as w:
        if w.getsampwidth() != 2 or w.getcomptype() != 'NONE':
            raise ValueError('仅支持16位未压缩PCM WAV')
        return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()

def dependencies():
    try:
        import sounddevice as sd
        import numpy as np
        return sd, np
    except (ImportError, OSError):
        raise RuntimeError('音频组件不可用，请安装 requirements-audio.txt 并检查PortAudio') from None

def devices():
    sd, _ = dependencies()
    return str(sd.query_devices())

def record(seconds=5, device=None, rate=16000):
    if not 1 <= seconds <= 20:
        raise ValueError('录音长度须为1至20秒')
    sd, _ = dependencies()
    try:
        data = sd.rec(int(seconds * rate), samplerate=rate, channels=1,
                      dtype='int16', device=device, blocking=True)
        if sd.get_status():
            raise RuntimeError('录音丢帧，请更换设备或采样率后重试')
        return wav_bytes(data.tobytes(), rate)
    finally:
        sd.stop()

def play(data, device=None, cancel=None):
    pcm, rate, channels = decode_wav(data)
    sd, np = dependencies()
    samples = np.frombuffer(pcm, dtype='<i2').reshape(-1, channels)
    try:
        sd.play(samples, samplerate=rate, device=device, blocking=False)
        while sd.get_stream().active:
            if cancel is not None and cancel.is_set():
                return False
            time.sleep(0.02)
        if sd.get_status():
            raise RuntimeError('播放丢帧，请检查输出设备')
        return True
    finally:
        sd.stop()
