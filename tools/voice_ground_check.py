"""AI 语音地面测试的前置自检（不连飞机、不下发任何指令）。

检查"语音链路"到底能不能用：麦克风/播放设备、本地 whisper、SAPI 播报、
全局热键依赖、图传 HUD 依赖，以及实飞入口是否还带着 2组 补的那些参数。

用法：
    python tools/voice_ground_check.py
    python tools/voice_ground_check.py --devices     # 顺带把音频设备全列出来

退出码：0 = 可以开测；1 = 有必须解决的项。
"""
import argparse
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AI_DIR = ROOT / 'modules' / 'ai'
ENTRY = AI_DIR / 'main.py'

# 蓝牙耳机在 Windows 上常见的设备名片段（只用于提示，不做唯一判据）
BLUETOOTH_HINTS = ('蓝牙', 'bluetooth', 'hands-free', 'handsfree', 'headset', 'headphone',
                   'airpods', 'buds', 'wh-', 'wf-', 'freebuds', 'soundcore', 'jbl')

REQUIRED_FLAGS = ('--real-drone', '--allow-real-takeoff', '--offline-voice', '--always-on',
                  '--min-battery', '--log-file')

results = []


def report(ok, name, detail=''):
    tag = 'PASS' if ok else 'FAIL'
    results.append((tag, name, detail))
    print('[%s] %-22s %s' % (tag, name, detail), flush=True)
    return ok


def warn(name, detail=''):
    results.append(('WARN', name, detail))
    print('[WARN] %-22s %s' % (name, detail), flush=True)


def check_python():
    version = '%d.%d.%d' % sys.version_info[:3]
    report(sys.version_info >= (3, 10), 'Python 版本', version + '（需 3.10+）')


def check_flags():
    if not ENTRY.exists():
        report(False, '实飞入口', '找不到 %s' % ENTRY)
        return
    text = ENTRY.read_text(encoding='utf-8', errors='replace')
    missing = [flag for flag in REQUIRED_FLAGS if flag not in text]
    report(not missing, '入口参数', '齐全' if not missing else '缺少 ' + ', '.join(missing))
    for optional in ('--show-video', '--input-device', '--climb-target-cm'):
        if optional not in text:
            warn('入口可选参数', '缺少 %s（对应功能本次不可用）' % optional)


def check_modules():
    for name, label, required in (('sounddevice', 'sounddevice 采集', True),
                                  ('numpy', 'numpy', True),
                                  ('pynput', 'pynput 全局热键', True),
                                  ('cv2', 'OpenCV 图传窗口', False),
                                  ('faster_whisper', 'faster-whisper 本地识别', True)):
        found = importlib.util.find_spec(name) is not None
        if found:
            report(True, label, '已安装')
        elif required:
            report(False, label, '未安装（pip install %s）' % name.replace('_', '-'))
        else:
            warn(label, '未安装：图传窗口会降级成只有终端输出')


def check_stt_tts():
    if str(AI_DIR) not in sys.path:
        sys.path.insert(0, str(AI_DIR))
    try:
        import local_stt
        installed = local_stt.installed()
        detail = '模型目录 %s' % getattr(local_stt, 'DEFAULT_DIR', '?')
        report(installed, '本地 STT', detail if installed else detail + ' 下没有可用模型')
    except Exception as exc:                       # noqa: BLE001 - 自检要吞掉一切
        report(False, '本地 STT', '%s: %s' % (type(exc).__name__, exc))
    try:
        import offline_tts
        report(offline_tts.available(), '离线 TTS（SAPI）',
               '可用' if offline_tts.available() else '当前系统不支持 Windows SAPI')
    except Exception as exc:                       # noqa: BLE001
        report(False, '离线 TTS（SAPI）', '%s: %s' % (type(exc).__name__, exc))


def audio_devices():
    import sounddevice as sd
    return sd.query_devices()


def check_devices(show_all=False):
    try:
        devices = audio_devices()
        default_in, default_out = sd_defaults()
    except Exception as exc:                       # noqa: BLE001
        report(False, '音频设备', '%s: %s' % (type(exc).__name__, exc))
        return
    if default_in is None:
        report(False, '默认麦克风', '系统没有默认输入设备')
    else:
        info = devices[default_in]
        report(True, '默认麦克风', '#%d %s' % (default_in, info['name']))
        if not _looks_bluetooth(info['name']):
            warn('蓝牙耳机', '当前默认麦克风不像蓝牙耳机；若已连接，用 -InputDevice N 指定')
    if default_out is None:
        report(False, '默认播放设备', '系统没有默认输出设备')
    else:
        info = devices[default_out]
        report(True, '默认播放设备', '#%d %s' % (default_out, info['name']))
    if show_all:
        print('\n--- 全部音频设备（> 默认输入   < 默认输出）---')
        for index, info in enumerate(devices):
            mark = '>' if index == default_in else ('<' if index == default_out else ' ')
            print('%s %2d %s (%d in, %d out)' % (mark, index, info['name'],
                                                  info['max_input_channels'],
                                                  info['max_output_channels']))
        print('\n蓝牙耳机一般会同时出现在 MME / DirectSound / WASAPI 里；'
              '优先选 MME 或 WASAPI 的那一条。\n')


def sd_defaults():
    import sounddevice as sd
    return sd.default.device[0], sd.default.device[1]


def _looks_bluetooth(name):
    lowered = str(name).lower()
    return any(hint in lowered for hint in BLUETOOTH_HINTS)


def check_log_dir():
    logs = ROOT / 'logs'
    try:
        logs.mkdir(parents=True, exist_ok=True)
        probe = logs / '.write_probe'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink()
        report(True, '日志目录', str(logs))
    except OSError as exc:
        report(False, '日志目录', '%s: %s' % (type(exc).__name__, exc))


def mic_test(seconds=3.0, device=None):
    """真录一段：确认蓝牙耳机真的能收到声音（只看峰值电平，不做识别）。"""
    import numpy as np
    import sounddevice as sd
    print('\n--- 麦克风实测：请对着麦克风清楚说一句话（%.0f 秒）---' % seconds, flush=True)
    try:
        frames = int(seconds * 16000)
        audio = sd.rec(frames, samplerate=16000, channels=1, dtype='int16', device=device)
        sd.wait()
    except Exception as exc:                       # noqa: BLE001
        report(False, '麦克风实测', '%s: %s' % (type(exc).__name__, exc))
        return False
    samples = audio.reshape(-1).astype(np.float32) / 32768.0
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
    peak_db = 20 * np.log10(peak) if peak > 0 else -120.0
    rms_db = 20 * np.log10(rms) if rms > 0 else -120.0
    detail = '峰值 %.1f dBFS  RMS %.1f dBFS' % (peak_db, rms_db)
    if peak_db > -40.0:
        return report(True, '麦克风实测', detail)
    if peak_db > -55.0:
        # 蓝牙耳机在 Windows 的 Hands-Free 模式普遍偏小声，本地识别有 AGC 会拉到 -20dBFS
        warn('麦克风实测', detail + '  → 偏小但可用：说话时电平应该更高，识别不准就靠近点说')
        return True
    return report(False, '麦克风实测',
                  detail + '  → 基本没声音：确认耳机麦克风没静音、没被别的程序占用，'
                           '或改用 -InputDevice 指定别的设备')


def loopback_test():
    """TTS→WAV→whisper→口令映射 全链路自环：不用人说话也能验证识别与意图。"""
    import tempfile
    try:
        import local_stt
        import offline_tts
        from demo_ai import scripted_action
    except Exception as exc:                       # noqa: BLE001
        report(False, '语音自环', '%s: %s' % (type(exc).__name__, exc))
        return False
    phrases = (('现在什么状态', 'get_status()'), ('还有多少电', 'get_battery()'),
               ('起飞', 'takeoff()'), ('向前走30厘米', 'move_forward(30)'))
    print('\n--- 语音自环：合成 → 识别 → 口令映射（不用说话）---', flush=True)
    try:
        stt = local_stt.LocalWhisper()
    except Exception as exc:                       # noqa: BLE001
        report(False, '语音自环', 'whisper 加载失败：%s: %s' % (type(exc).__name__, exc))
        return False
    path = tempfile.gettempdir() + '/voice_loopback.wav'
    hits = 0
    for text, expected in phrases:
        try:
            offline_tts.synthesize_to_wav(text, path)
            with open(path, 'rb') as handle:
                heard = stt.transcribe(handle.read()).strip()
            got = scripted_action(heard)
        except Exception as exc:                   # noqa: BLE001
            print('  [FAIL] %-8s %s: %s' % (text, type(exc).__name__, exc), flush=True)
            continue
        if got == expected:
            hits += 1
            print('  [OK  ] 说「%s」→ 识别「%s」→ %s' % (text, heard, got), flush=True)
        else:
            print('  [MISS] 说「%s」→ 识别「%s」→ %s（期望 %s）'
                  % (text, heard, got, expected), flush=True)
    return report(hits == len(phrases), '语音自环', '%d/%d 命中' % (hits, len(phrases)))


def main():
    parser = argparse.ArgumentParser(description='AI 语音地面测试前置自检')
    parser.add_argument('--devices', action='store_true', help='列出全部音频设备')
    parser.add_argument('--record-test', action='store_true', help='真录 3 秒，确认麦克风能收到声音')
    parser.add_argument('--loopback', action='store_true',
                        help='语音自环：合成语音→本地识别→口令映射，验证理解链路（不用说话）')
    parser.add_argument('--input-device', type=int, default=None, help='配合 --record-test 指定设备号')
    args = parser.parse_args()

    print('=== AI 语音地面测试 · 前置自检（不连飞机）===\n')
    check_python()
    check_flags()
    check_modules()
    check_stt_tts()
    check_devices(args.devices)
    if args.record_test:
        mic_test(device=args.input_device)
    if args.loopback:
        loopback_test()
    check_log_dir()

    failed = [row for row in results if row[0] == 'FAIL']
    warned = [row for row in results if row[0] == 'WARN']
    print('\n--- 汇总：PASS %d / WARN %d / FAIL %d ---'
          % (len(results) - len(failed) - len(warned), len(warned), len(failed)))
    if failed:
        print('必须解决的项：' + '；'.join(row[1] for row in failed))
        return 1
    print('可以开测：先连蓝牙耳机，再连 Tello（桨叶拔掉）。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
