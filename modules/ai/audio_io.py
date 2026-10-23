"""Optional PortAudio I/O. Imports happen only when hardware is requested."""
import io
import math
import os
import queue
import wave
import time

# 现场可调：门限= max(绝对下限, 底噪 + 余量)；笔记本阵列麦底噪常在 -55dBFS 左右
VAD_MIN_DBFS = float(os.getenv('AI_VAD_MIN_DBFS', '-55'))
VAD_MARGIN_DB = float(os.getenv('AI_VAD_MARGIN_DB', '6'))

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

def _open_input(sd, device, rate, block_ms, callback):
    """按目标采样率开流；设备不支持时退回它的默认采样率。返回 (stream, rate, block)。

    必须用 callback 而不是阻塞式 read()：Windows 的 WDM-KS 设备不支持 PortAudio
    阻塞 API（PaErrorCode -9999 'Blocking API not supported yet'）。
    """
    for candidate in (rate, None):
        actual = candidate
        if actual is None:
            try:
                info = (sd.query_devices(device, 'input') if device is not None
                        else sd.query_devices(kind='input'))
                actual = int(info['default_samplerate'])
            except Exception:
                actual = 16000
        block = max(1, int(actual * block_ms / 1000))
        try:
            stream = sd.InputStream(samplerate=actual, channels=1, dtype='int16',
                                    blocksize=block, device=device, callback=callback)
            stream.start()
            return stream, actual, block
        except Exception:
            if candidate is None:
                raise
    raise RuntimeError('无法打开麦克风')

def listen_once(device=None, rate=16000, block_ms=50, silence_s=1.5, max_s=10.0,
                wait_s=0.0, min_dbfs=None, margin_db=None, onset_ms=150,
                preroll_ms=300, tail_pad_ms=300, pause=None):
    """流式监听一句话（免按键）：检测到人声自动开录，静音 silence_s 秒后收尾。

    门限是自适应的：噪声底噪随环境缓慢跟随，实际门限 = max(min_dbfs, 底噪+margin_db)，
    所以安静的耳机和嘈杂的教室都不用手工调阈值。

    pause 事件被置位时立刻放弃本次采集（用于 TTS 播报 / 动作执行期间闭嘴）。
    返回 (wav 或 None, 统计信息)；无语音时 wav 为 None，reason 说明原因。
    """
    sd, np = dependencies()
    min_dbfs = VAD_MIN_DBFS if min_dbfs is None else min_dbfs
    margin_db = VAD_MARGIN_DB if margin_db is None else margin_db
    info = {'reason': 'idle', 'speech_s': 0.0, 'peak_dbfs': -120.0,
            'floor_dbfs': -60.0, 'gate_dbfs': min_dbfs, 'rate': rate}
    stream = None
    blocks = queue.Queue(maxsize=200)

    def _on_audio(indata, frames, time_info, status):
        try:
            blocks.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    try:
        stream, rate, block = _open_input(sd, device, rate, block_ms, _on_audio)
        info['rate'] = rate
        block_s = block / float(rate)
        onset_blocks = max(1, int(round(onset_ms / (block_s * 1000))))
        preroll = []
        preroll_limit = max(1, int(round(preroll_ms / (block_s * 1000))))
        floor = -60.0
        speaking = False
        captured = []
        above = 0
        silence_run = 0
        started = time.monotonic()
        last_data = started
        while True:
            if pause is not None and pause.is_set():
                info['reason'] = 'paused'
                return None, info
            try:
                samples = blocks.get(timeout=0.2)
            except queue.Empty:
                now = time.monotonic()
                # 设备掉线/回调停摆时也要能收尾，不能靠"块计数"死等
                if speaking and now - last_data >= silence_s:
                    break
                if wait_s and not speaking and now - started >= wait_s:
                    info['reason'] = 'timeout'
                    return None, info
                continue
            last_data = time.monotonic()
            if samples.size == 0:
                continue
            rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
            dbfs = 20.0 * math.log10(rms / 32768.0) if rms > 0 else -120.0
            info['peak_dbfs'] = max(info['peak_dbfs'], dbfs)
            gate = max(min_dbfs, floor + margin_db)
            info['gate_dbfs'], info['floor_dbfs'] = gate, floor
            if not speaking:
                if dbfs >= gate:
                    above += 1
                    preroll.append(samples.copy())
                    if len(preroll) > preroll_limit:
                        preroll.pop(0)
                    if above >= onset_blocks:
                        speaking = True
                        captured = preroll[-preroll_limit:]
                        preroll = []
                        silence_run = 0
                else:
                    above = 0
                    # 底噪：向更安静的一侧立刻跟随，向更吵的一侧每帧最多 +0.5dB
                    floor = min(dbfs, floor + 0.5)
                    floor = max(floor, -90.0)
                    preroll.append(samples.copy())
                    if len(preroll) > preroll_limit:
                        preroll.pop(0)
                    if wait_s and time.monotonic() - started >= wait_s:
                        info['reason'] = 'timeout'
                        return None, info
            else:
                captured.append(samples.copy())
                if dbfs >= gate:
                    silence_run = 0
                    last_voice = len(captured)
                else:
                    silence_run += 1
                    if silence_run * block_s >= silence_s:
                        break
                if len(captured) * block_s >= max_s:
                    break
        info['reason'] = 'ok'
        # 尾部只留 300ms 余音：1.5s 静音是为了"判定说完了"，不是要送给 whisper
        pad_blocks = max(1, int(round(tail_pad_ms / (block_s * 1000))))
        keep = min(len(captured), max(1, last_voice) + pad_blocks)
        captured = captured[:keep]
        info['speech_s'] = round(len(captured) * block_s, 2)
        pcm = np.concatenate(captured).astype('<i2').tobytes()
        return wav_bytes(pcm, rate), info
    finally:
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

def play(data, device=None, cancel=None):
    pcm, rate, channels = decode_wav(data)
    sd, np = dependencies()
    samples = np.frombuffer(pcm, dtype='<i2').reshape(-1, channels)
    try:
        sd.play(samples, samplerate=rate, device=device, blocking=False)
        stream = sd.get_stream()
        while stream is not None and stream.active:
            if cancel is not None and cancel.is_set():
                return False
            time.sleep(0.02)
        if sd.get_status():
            raise RuntimeError('播放丢帧，请检查输出设备')
        return True
    finally:        sd.stop()


def close():
    """释放音频设备。异常路径与退出路径都要调用，避免流泄漏。"""
    try:
        import sounddevice as sd
    except (ImportError, OSError):
        return
    try:
        sd.stop()
    except (RuntimeError, OSError):
        pass
