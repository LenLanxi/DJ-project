"""有线耳麦现场自检：列设备 → 录音 → 回放 → 报电平。

用法（项目根目录，使用 .venv）：
    .venv\\Scripts\\python.exe tools\\check_audio.py
    .venv\\Scripts\\python.exe tools\\check_audio.py --input-device 1 --output-device 3
    .venv\\Scripts\\python.exe tools\\check_audio.py --list-only
    .venv\\Scripts\\python.exe tools\\check_audio.py --seconds 5 --no-play

退出码：0 正常；2 缺少依赖/设备；3 录到的声音过小（可能麦克风未选中或被静音）。
"""

import argparse
import math
import os
import sys

RATE = 24000
CHANNELS = 1
INPUT_ENV = "AI_INPUT_DEVICE"
OUTPUT_ENV = "AI_OUTPUT_DEVICE"
SILENCE_RMS = 120          # 16bit；低于此值基本可判为没有声音
CLIP_RMS = 30000           # 接近满量程，说明增益过高


def load_sounddevice():
    try:
        import sounddevice as sd
        import numpy as np
    except (ImportError, OSError) as exc:
        print("音频库不可用：%s" % exc)
        print("请在项目根目录执行：.venv\\Scripts\\python.exe -m pip install sounddevice numpy")
        return None, None
    return sd, np


def resolve_device(cli_value, env_name):
    if cli_value is not None:
        return cli_value, "命令行"
    raw = (os.getenv(env_name) or "").strip()
    if raw:
        try:
            return int(raw), "环境变量 %s" % env_name
        except ValueError:
            print("警告：%s=%r 不是整数，已忽略" % (env_name, raw))
    return None, "系统默认"


def show_devices(sd):
    print("=" * 62)
    print("可用音频设备")
    print("=" * 62)
    try:
        devices = sd.query_devices()
        default_in, default_out = sd.default.device
    except Exception as exc:
        print("查询设备失败：%s" % exc)
        return False
    if len(devices) == 0:
        print("没有检测到任何音频设备。请确认耳机已插入并安装驱动。")
        return False
    for index, device in enumerate(devices):
        tags = []
        if index == default_in:
            tags.append("默认输入")
        if index == default_out:
            tags.append("默认输出")
        if device["max_input_channels"]:
            tags.append("输入%d" % device["max_input_channels"])
        if device["max_output_channels"]:
            tags.append("输出%d" % device["max_output_channels"])
        print("%3d | %-42s | %s" % (index, device["name"][:42], ", ".join(tags) or "-"))
    print("-" * 62)
    print("默认输入=%s  默认输出=%s" % (default_in, default_out))
    return True


def record(sd, np, seconds, device):
    print("开始录音 %.1f 秒，请正常说一句话……" % seconds)
    try:
        audio = sd.rec(int(seconds * RATE), samplerate=RATE, channels=CHANNELS,
                       dtype="int16", device=device, blocking=True)
    except Exception as exc:
        print("录音失败：%s" % exc)
        print("→ 检查设备编号是否正确（见上表），或耳机是否被系统占用。")
        return None
    return audio


def report_level(audio, np):
    data = audio.astype("float64")
    rms = float(np.sqrt((data ** 2).mean()))
    peak = float(np.abs(data).max())
    dbfs = 20 * math.log10(max(rms, 1e-9) / 32768.0)
    print("-" * 62)
    print("电平：RMS=%.0f  Peak=%.0f  (%.1f dBFS)" % (rms, peak, dbfs))
    if rms < SILENCE_RMS:
        print("⚠ 几乎没有声音。请检查：麦克风是否被静音、是否选错设备、"
              "Windows 设置 → 隐私 → 麦克风 是否允许应用访问。")
        return 3
    if rms > CLIP_RMS:
        print("⚠ 电平过高，可能削波。请在系统里调低麦克风增益。")
    else:
        print("✓ 电平正常。")
    return 0


def playback(sd, audio, device):
    print("回放录音……")
    try:
        sd.play(audio, samplerate=RATE, device=device, blocking=True)
        sd.wait()
        print("✓ 回放结束。听到自己的声音说明耳机输出正常。")
        return 0
    except Exception as exc:
        print("回放失败：%s" % exc)
        print("→ 检查输出设备编号，或耳机插孔是否被系统识别为扬声器。")
        return 2
    finally:
        try:
            sd.stop()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="有线耳麦自检（不联网、不接触无人机）")
    parser.add_argument("--input-device", type=int, help="麦克风设备编号")
    parser.add_argument("--output-device", type=int, help="耳机设备编号")
    parser.add_argument("--seconds", type=float, default=3.0, help="录音时长，默认 3 秒")
    parser.add_argument("--list-only", action="store_true", help="只列设备，不录音")
    parser.add_argument("--no-play", action="store_true", help="录音后不回放")
    args = parser.parse_args()

    sd, np = load_sounddevice()
    if sd is None:
        return 2
    if not show_devices(sd):
        return 2

    input_device, input_source = resolve_device(args.input_device, INPUT_ENV)
    output_device, output_source = resolve_device(args.output_device, OUTPUT_ENV)
    print("本次使用：输入=%s (%s)  输出=%s (%s)"
          % (input_device if input_device is not None else "默认", input_source,
             output_device if output_device is not None else "默认", output_source))
    print("提示：设备编号可写入环境变量 %s / %s，或用 --input-device/--output-device 指定。"
          % (INPUT_ENV, OUTPUT_ENV))

    if args.list_only:
        return 0
    if not 0.5 <= args.seconds <= 20:
        print("--seconds 需在 0.5 到 20 之间")
        return 2

    audio = record(sd, np, args.seconds, input_device)
    if audio is None:
        return 2
    code = report_level(audio, np)
    if not args.no_play:
        code = playback(sd, audio, output_device) or code
    print("=" * 62)
    print("自检完成")
    return code


if __name__ == "__main__":
    sys.exit(main())
