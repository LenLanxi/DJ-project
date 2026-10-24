"""Offline 3-second headset loopback test; uses the same streams as main.py.

用法（项目根目录）：
    .venv\\Scripts\\python.exe test_audio_device.py --list
    .venv\\Scripts\\python.exe test_audio_device.py --input-device 1 --output-device 3 --save-config audio_config.json
    .venv\\Scripts\\python.exe test_audio_device.py --noise-test

不需要 Docker 或 API；录音只在内存中，不落盘。
"""
import argparse
import asyncio
import math
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for extra in (ROOT / 'modules' / 'ai',):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from audio_config import load_audio_config, save_audio_config
from audio_devices import RATE, list_audio_devices, pinned_config
from duplex_audio import DuplexAudio


async def record_three_seconds(audio):
    recording = bytearray()
    needed = RATE * 2 * 3
    async for chunk in audio.chunks():
        recording.extend(chunk[:needed - len(recording)])
        if len(recording) == needed:
            return bytes(recording)


def levels(pcm):
    samples = [x[0] for x in struct.iter_unpack('<h', pcm)]
    if not samples:
        raise ValueError('没有录到音频')
    rms = math.sqrt(sum(x * x for x in samples) / len(samples)) / 32768
    peak = max(abs(x) for x in samples) / 32768
    db = lambda value: 20 * math.log10(max(value, 1e-6))
    return db(rms), db(peak)


async def play_recording(audio, recording):
    audio.speaker.append('headset-test', 0, recording)
    started = time.monotonic()
    drained_at = None
    # Keep consuming mic callbacks during playback, exactly as the full duplex client does.
    async for _ in audio.chunks():
        now = time.monotonic()
        if audio.speaker.size == 0:
            if drained_at is None:
                drained_at = now
            if now - drained_at >= audio.speaker.latency_ms / 1000 + 0.1:
                return
        if now - started > 8:
            raise RuntimeError('回放超时，请检查输出设备')


async def run_test(args):
    import sounddevice as sd
    if args.list:
        list_audio_devices(sd)
        return
    config = load_audio_config(args.audio_config, args.input_device, args.output_device)
    audio = DuplexAudio(config=config)
    try:
        audio.start()  # Prints both exact device names and host APIs; never uses defaults.
        print('离线自检：不需要Docker/API，不保存录音；本地录音不包含云端降噪。')
        noise = None
        if args.noise_test:
            print('现在保持安静，采集3秒环境底噪……', flush=True)
            raw = await asyncio.wait_for(record_three_seconds(audio), timeout=8)
            noise, _ = levels(raw)
            print(f'环境录音 RMS: {noise:.1f} dBFS（越接近0越响，不是声压级）')
        print('现在对着耳机麦克风说话，录音3秒……', flush=True)
        recording = await asyncio.wait_for(record_three_seconds(audio), timeout=8)
        rms, peak = levels(recording)
        print(f'录音 RMS: {rms:.1f} dBFS；峰值: {peak:.1f} dBFS')
        if peak <= -100:
            raise RuntimeError('录音接近全静音，请检查麦克风静音键、权限和设备索引')
        if peak >= -0.5:
            print('提示：峰值接近削波，请降低麦克风增益后复测。')
        if noise is not None:
            print(f'说话片段比底噪高 {rms-noise:.1f} dB（粗略电平比较，不等于VAD阈值或精确信噪比）')
        print('正在通过选定耳机回放，请用耳朵确认声音来自耳机且人声清楚……', flush=True)
        await asyncio.wait_for(play_recording(audio, recording), timeout=10)
        if args.save_config:
            save_audio_config(args.save_config, pinned_config(config, audio.endpoints))
            print('设备索引、名称、音频后端及VAD参数已保存至 ' + args.save_config)
        print('录放流程完成；听感、声道和实际佩戴效果需人工确认，不代表AEC或实时打断验收通过。')
    finally:
        audio.close()


def main():
    # 设备名常含 ® 等字符，GBK 控制台默认会抛 UnicodeEncodeError 直接崩溃
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description='耳机自检：打印设备、录3秒、回放；无需Docker/API')
    parser.add_argument('--list', action='store_true', help='只列出设备索引和名称')
    parser.add_argument('--input-device', type=int)
    parser.add_argument('--output-device', type=int)
    parser.add_argument('--audio-config', help='配置文件；默认读取当前目录或仓库根目录的audio_config.json')
    parser.add_argument('--save-config', metavar='PATH', help='录放成功后保存所选设备身份及参数')
    parser.add_argument('--noise-test', action='store_true', help='先额外录3秒环境底噪，再录3秒人声')
    args = parser.parse_args()
    try:
        asyncio.run(run_test(args))
    except KeyboardInterrupt:
        print('自检已取消')
        raise SystemExit(130)
    except Exception as exc:
        print('自检失败：' + str(exc))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
