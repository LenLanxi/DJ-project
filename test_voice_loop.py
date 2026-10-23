"""有线耳机 + 真实语音 API 的端到端链路测试（不连飞机）。

流程与 4 组 main.py 的 /voice 完全一致：
    录音 → STT(转文字) → 大模型(DeepSeek) → TTS(合成语音) → 耳机播放

每个环节缺失时自动降级，并把降级原因打在括号里，方便现场判断：
    STT 未配置  → 键盘输入（Mock STT）
    LLM 未配置  → 回声（Mock LLM）
    TTS 未配置  → Windows SAPI 离线合成（无需任何 Key）

用法
    python test_voice_loop.py                     # 说话 3 秒，跑完整链路
    python test_voice_loop.py --seconds 4 --turns 2
    python test_voice_loop.py --text 向前飞        # 跳过录音与 STT，直接测 LLM/TTS
    python test_voice_loop.py --no-play           # 不播放（只看文字链路）
    python test_voice_loop.py --self-check        # 离线自检，不碰声卡、不联网

设备索引来自 audio_config.json（由 test_audio_device.py --save-config 生成），
可用 --input-device / --output-device 临时覆盖。
"""
import argparse
import contextlib
import io
import math
import os
import subprocess
import sys
import tempfile
import time
import wave

ROOT = os.path.dirname(os.path.abspath(__file__))
AI_DIR = os.path.join(ROOT, 'modules', 'ai')
if AI_DIR not in sys.path:
    sys.path.insert(0, AI_DIR)

import audio_config
import audio_io
import local_stt
from model_client import ModelClient
from voice_service import VoiceService

STAGE_TAGS = ('录音', 'STT', 'LLM', 'TTS')
SYSTEM_PROMPT = '你是无人机语音助手，回答简短口语化，中文，不超过 30 字。'


def emit(stage, text):
    print('[%s] %s' % (stage, text), flush=True)


def env_ready(*names):
    return all(os.getenv(name) for name in names)


def stt_ready():
    return env_ready('AI_STT_URL', 'AI_STT_MODEL')


def tts_ready():
    return env_ready('AI_TTS_URL', 'AI_TTS_MODEL', 'AI_TTS_VOICE')


def llm_ready():
    return env_ready('AI_CHAT_URL', 'AI_API_KEY', 'AI_MODEL')


def audio_deps():
    try:
        return audio_io.dependencies()
    except RuntimeError as exc:
        emit('错误', str(exc))
        emit('提示', '安装音频依赖：pip install -r modules/ai/requirements-audio.txt')
        return None, None


def resolve_devices(args):
    config = audio_config.load_audio_config(args.audio_config, args.input_device, args.output_device)
    return config.input_device, config.output_device


def device_label(sd, index, kind):
    try:
        info = sd.query_devices(index) if index is not None else sd.query_devices(kind=kind)
    except Exception as exc:
        return '默认（查询失败：%s）' % exc
    if isinstance(info, dict) and 'name' in info:
        return '[%s] %s' % (index if index is not None else '默认', info['name'])
    return str(info)


def record(args, device):
    """按设备原生采样率录音，避免 24kHz 耳机在 16kHz 下报 Invalid sample rate。"""
    sd, np = audio_deps()
    if sd is None:
        return None, 0.0
    try:
        info = sd.query_devices(device) if device is not None else sd.query_devices(kind='input')
        native = int(float(info.get('default_samplerate') or 16000))
    except Exception:
        native = 16000
    rate = max(16000, min(48000, native))
    emit('设备', '输入=%s  输出=%s  采样率=%d' % (
        device_label(sd, device, 'input'), '见播放阶段', rate))
    print('现在对着耳机麦克风说一句话（%d 秒）……' % args.seconds, flush=True)
    wav = audio_io.record(args.seconds, device=device, rate=rate)
    pcm, actual_rate, _channels = audio_io.decode_wav(wav)
    samples = np.frombuffer(pcm, dtype='<i2').astype('float64')
    rms = 20 * math.log10(max(1.0, float((samples ** 2).mean() ** .5)) / 32768.0)
    peak = 20 * math.log10(max(1.0, float(abs(samples).max())) / 32768.0) if samples.size else -120.0
    emit('录音', '捕获到 %.1f 秒音频（%dHz，RMS %.1f dBFS，峰值 %.1f dBFS）'
         % (len(pcm) / 2.0 / actual_rate, actual_rate, rms, peak))
    if peak <= -60:
        emit('录音', '警告：几乎全静音，检查麦克风静音键 / Windows 麦克风权限 / 设备索引')
    return wav, actual_rate


def mock_transcript(args):
    if args.mock_text:
        return args.mock_text
    try:
        typed = input('（Mock STT）请输入刚才说的内容，直接回车用“向前飞”：').strip()
    except EOFError:
        typed = ''
    return typed or '向前飞'


def transcribe(args, wav):
    if args.text:
        emit('STT', '识别文本：%s（来源：命令行 --text，跳过录音）' % args.text)
        return args.text, 'arg'
    if stt_ready():
        try:
            started = time.monotonic()
            text = VoiceService().transcribe(wav)
            emit('STT', '识别文本：%s（来源：API，耗时 %.0fms）' % (text, (time.monotonic() - started) * 1000))
            return text, 'api'
        except Exception as exc:
            emit('STT', 'API 调用失败：%s' % exc)
            emit('STT', '尝试本地 faster-whisper')
    if not args.no_local_stt and local_stt.installed():
        try:
            started = time.monotonic()
            engine = local_stt.LocalWhisper(args.whisper_model).warmup()
            text = engine.transcribe(wav)
            elapsed = (time.monotonic() - started) * 1000
            if not text:
                raise RuntimeError('本地模型没有识别到文字（可能只有底噪，VAD 已过滤）')
            info = engine.last_info
            level = info.get('level') or {}
            emit('STT', '识别文本：%s（来源：本地 faster-whisper %s，耗时 %.0fms，音频 %.2fs，'
                        'AGC %+.1fdB，输入 RMS %s dBFS，VAD=%s）'
                 % (text, os.path.basename(local_stt.resolve_model(args.whisper_model)),
                    elapsed, info.get('duration', 0.0), info.get('gain_db', 0.0),
                    level.get('rms_db', '?'), 'on' if info.get('vad') else 'off'))
            return text, 'local'
        except Exception as exc:
            emit('STT', '本地转写失败：%s' % exc)
            emit('STT', '降级为键盘输入（Mock）')
    else:
        missing = [name for name in ('AI_STT_URL', 'AI_STT_MODEL') if not os.getenv(name)]
        emit('STT', '未配置 %s 且本地 faster-whisper 不可用，降级为键盘输入（Mock）'
             % '/'.join(missing))
    text = mock_transcript(args)
    emit('STT', '识别文本：%s（来源：Mock 键盘）' % text)
    return text, 'mock'


def ask_llm(args, text):
    if not llm_ready():
        missing = [name for name in ('AI_CHAT_URL', 'AI_API_KEY', 'AI_MODEL') if not os.getenv(name)]
        reply = '（Mock 回复）收到：%s' % text
        emit('LLM', '%s（未配置 %s）' % (reply, '/'.join(missing)))
        return reply, 'mock'
    try:
        started = time.monotonic()
        client = ModelClient()
        reply = client.complete([{'role': 'system', 'content': SYSTEM_PROMPT},
                                 {'role': 'user', 'content': text}])
        usage = getattr(client, 'last_usage', {}) or {}
        vendor = 'DeepSeek' if 'deepseek' in (os.getenv('AI_CHAT_URL') or '').lower() else '模型'
        emit('LLM', '%s 回复：%s（%s，耗时 %.0fms，tokens=%s）' % (
            vendor, reply.strip(), os.getenv('AI_MODEL'),
            (time.monotonic() - started) * 1000,
            usage.get('total_tokens', '?')))
        return reply, 'api'
    except Exception as exc:
        reply = '（Mock 回复，模型调用失败）收到：%s' % text
        emit('LLM', '调用失败：%s' % exc)
        emit('LLM', reply)
        return reply, 'mock'


def speak_sapi(text):
    """Windows 自带语音合成：无需任何 Key，离线可用。"""
    import offline_tts
    offline_tts.speak(text)


def speak(args, text, device):
    if args.no_play:
        emit('TTS', '已跳过播放（--no-play）；待播文本：%s' % text)
        return 'skip'
    if tts_ready():
        try:
            service = VoiceService()
            started = time.monotonic()
            wav = service.synthesize(text[:2000])
            audio_io.play(wav, device=device)
            emit('TTS', '成功合成音频并播放（来源：API，%d 字节，耗时 %.0fms）'
                 % (len(wav), (time.monotonic() - started) * 1000))
            return 'api'
        except Exception as exc:
            emit('TTS', 'API 合成失败：%s' % exc)
            emit('TTS', '降级为 Windows SAPI 离线合成')
    else:
        missing = [name for name in ('AI_TTS_URL', 'AI_TTS_MODEL', 'AI_TTS_VOICE')
                   if not os.getenv(name)]
        emit('TTS', '未配置 %s，降级为 Windows SAPI 离线合成' % '/'.join(missing))
    try:
        speak_sapi(text)
        emit('TTS', '成功合成音频并播放（来源：Windows SAPI 降级；中文音色取决于系统语音包）')
        return 'sapi'
    except Exception as exc:
        emit('TTS', 'SAPI 也失败：%s' % exc)
        emit('TTS', '仅打印文本，请在 Windows 设置里安装中文语音包再试')
        return 'none'


def one_turn(args, input_device, output_device):
    if args.text:
        wav, text = None, args.text
        emit('录音', '已跳过（--text 模式）')
    elif args.wav:
        with open(args.wav, 'rb') as handle:
            wav = handle.read()
        pcm, rate, _channels = audio_io.decode_wav(wav)
        emit('录音', '使用已有录音 %s（%.1f 秒，%dHz）' % (args.wav, len(pcm) / 2.0 / rate, rate))
    else:
        wav, _rate = record(args, input_device)
        if wav is None:
            return False
    text, _stt_source = transcribe(args, wav)
    if not text:
        emit('STT', '没有识别到文字，本轮结束（不会调用模型）')
        return False
    reply, _llm_source = ask_llm(args, text)
    speak(args, reply.strip(), output_device)
    return True


def always_on_checks(np):
    """免按键监听（audio_io.listen_once）的离线验证：用假声卡喂合成波形，不碰真麦克风。"""
    import threading

    class FakeStream:
        """模拟 PortAudio 输入流：start() 后按块把数据推给回调（真实设备是回调式）。"""

        def __init__(self, blocks, callback):
            self._blocks, self._callback = list(blocks), callback
            self._stopped = threading.Event()

        def start(self):
            def feed():
                for index, block in enumerate(self._blocks):
                    if self._stopped.is_set():
                        return
                    self._callback(block.reshape(-1, 1), len(block), None, None)
                    time.sleep(0.001)
            threading.Thread(target=feed, daemon=True).start()

        def stop(self):
            self._stopped.set()

        def close(self):
            self._stopped.set()

    original = audio_io._open_input

    def make(kind):
        rate, block = 16000, 800                      # 50ms 一块
        t = np.arange(block, dtype=np.float64)
        quiet = (np.sin(2 * np.pi * 120 * t / rate) * 4).astype('<i2')     # 约 -70dBFS 底噪
        speech = (np.sin(2 * np.pi * 220 * t / rate) * 4000).astype('<i2')  # 约 -18dBFS 人声
        if kind == 'utterance':
            blocks = [quiet] * 20 + [speech] * 20 + [quiet] * 40            # 1s静音+1s说话+2s静音
        elif kind == 'silence':
            blocks = []                                                     # 一点声音都没有
        else:                                                               # 一直有人在说
            blocks = [speech] * 400
        def opener(sd, device, rate_arg, block_ms, callback):
            stream = FakeStream(blocks, callback)
            stream.start()
            return stream, rate, block

        audio_io._open_input = opener
        return rate, block

    def restore():
        audio_io._open_input = original

    checks = []
    try:
        rate, _ = make('utterance')
        wav, info = audio_io.listen_once(None, rate=rate, silence_s=1.5)
        speech = info['speech_s']
        checks.append(('VAD 检测到人声才开始录（前 1 秒静音被丢弃）', info['reason'] == 'ok'
                       and wav is not None, str(info)))
        checks.append(('说完 1.5 秒静音后自动收尾（录音 1.0~3.0 秒）', 1.0 <= speech <= 3.0, str(speech)))
        pcm, back_rate, channels = audio_io.decode_wav(wav)
        checks.append(('返回的是可解码的 16k 单声道 WAV',
                       back_rate == rate and channels == 1 and len(pcm) == int(speech * rate * 2),
                       '%d/%d/%d' % (back_rate, channels, len(pcm))))

        make('silence')
        wav, info = audio_io.listen_once(None, rate=16000, silence_s=1.5, wait_s=0.5)
        checks.append(('一直没人说话时按时返回 None（不吞住线程）',
                       wav is None and info['reason'] == 'timeout', str(info)))

        make('speech')
        pause = threading.Event()
        pause.set()
        wav, info = audio_io.listen_once(None, rate=16000, silence_s=1.5, pause=pause)
        checks.append(('暂停标志置位时立刻放弃采集（TTS/动作期间闭嘴）',
                       wav is None and info['reason'] == 'paused', str(info)))

        make('speech')
        wav, info = audio_io.listen_once(None, rate=16000, silence_s=1.5, max_s=1.0)
        checks.append(('一直说不完时被 max_s 截断（有上限，不会无限录）',
                       info['reason'] == 'ok' and 0.9 <= info['speech_s'] <= 1.3, str(info['speech_s'])))
    finally:
        restore()
    return checks


def self_check():
    """离线自检：不碰声卡、不联网，只验证 WAV 编解码与四个阶段的打印。"""
    checks = []

    def check(name, ok, detail=''):
        checks.append((name, bool(ok), detail))

    import numpy as np
    pcm = (np.zeros(16000, dtype='<i2')).tobytes()
    wav = audio_io.wav_bytes(pcm, 16000)
    back, rate, channels = audio_io.decode_wav(wav)
    check('wav_bytes / decode_wav 往返一致', back == pcm and rate == 16000 and channels == 1)
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as handle:
        handle.write(wav)
        temp_path = handle.name
    try:
        with wave.open(temp_path, 'rb') as handle:
            check('生成的是合法 WAV 文件', handle.getframerate() == 16000
                  and handle.getnframes() == 16000)
    finally:
        os.unlink(temp_path)

    args = argparse.Namespace(text='向前飞', no_play=False, mock_text=None, seconds=1)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        text, stt_source = transcribe(args, None)
        reply, llm_source = ask_llm(argparse.Namespace(text='向前飞'), text)
        tts_source = speak(namespace_no_play(text), reply, None)
    output = buffer.getvalue()
    check('STT 阶段打印了识别文本', '[STT] 识别文本：向前飞' in output, output[-120:])
    check('LLM 阶段在无 Key 时走 Mock 并打印', '收到：向前飞' in output, '')
    check('TTS 阶段在有文本时给出结论', '[TTS]' in output, '')
    check('三个阶段都有来源标注', all(tag in output for tag in ('来源：', 'Mock')), '')
    check('STT 在无配置时确实是 Mock', stt_source in ('mock', 'arg'), stt_source)
    check('LLM 在无配置时确实是 Mock', llm_source == 'mock', llm_source)
    check('TTS 有降级路径', tts_source in ('sapi', 'skip', 'none', 'api'), tts_source)

    for name, ok, detail in always_on_checks(np):
        check(name, ok, detail)

    failures = [item for item in checks if not item[1]]
    for name, ok, detail in checks:
        print('[%s] %s%s' % ('PASS' if ok else 'FAIL', name, ('  <- ' + detail) if detail and not ok else ''))
    print('\n%d/%d 通过；STT-API=%s 本地STT=%s LLM=%s TTS=%s' % (
        len(checks) - len(failures), len(checks), stt_ready(),
        local_stt.installed() and local_stt.model_cached(), llm_ready(), tts_ready()))
    return 0 if not failures else 1


def namespace_no_play(text):
    return argparse.Namespace(no_play=True, text=text)


def stt_smoke(args):
    """离线语音链路冒烟：SAPI 合成“向前飞” → AGC/VAD → 本地 whisper → 断言识别结果。"""
    if not local_stt.installed():
        print('[FAIL] 未安装 faster-whisper（pip install -r requirements-stt.txt）')
        return 1
    import offline_tts
    path = os.path.join(tempfile.gettempdir(), 'voice_loop_smoke.wav')
    try:
        offline_tts.synthesize_to_wav('向前飞', path)
    except Exception as exc:
        print('[FAIL] SAPI 合成失败：%s' % exc)
        return 1
    wav = open(path, 'rb').read()
    started = time.monotonic()
    engine = local_stt.LocalWhisper(args.whisper_model).warmup()
    text = engine.transcribe(wav)
    info = engine.last_info
    level = info.get('level') or {}
    print('[STT] 识别文本：%s（耗时 %.0fms，AGC %+.1fdB，输入 RMS %s dBFS，VAD=%s）'
          % (text, (time.monotonic() - started) * 1000, info.get('gain_db', 0.0),
             level.get('rms_db', '?'), 'on' if info.get('vad') else 'off'))
    ok = '向前飞' in text
    print('[%s] 合成语音「向前飞」被正确识别' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='有线耳机 + 真实语音 API 端到端测试（不连飞机）')
    parser.add_argument('--self-check', action='store_true', help='离线自检，不碰声卡、不联网')
    parser.add_argument('--stt-smoke', action='store_true',
                        help='离线语音冒烟：SAPI 合成“向前飞”再本地识别（不发声、不用麦克风）')
    parser.add_argument('--seconds', type=float, default=3.0, help='录音时长（1~20 秒）')
    parser.add_argument('--turns', type=int, default=1, help='连续跑几轮')
    parser.add_argument('--text', default='', help='跳过录音+STT，直接用这句文本')
    parser.add_argument('--wav', default='', help='用已有 WAV 文件代替现场录音（便于复现）')
    parser.add_argument('--no-local-stt', action='store_true', help='禁用本地 faster-whisper')
    parser.add_argument('--whisper-model', default=None,
                        help='本地模型名或目录（默认取 AI_LOCAL_STT_MODEL / small）')
    parser.add_argument('--mock-text', default='', help='Mock STT 时直接使用这句，不提示输入')
    parser.add_argument('--no-play', action='store_true', help='不播放合成语音')
    parser.add_argument('--audio-config', default=None, help='默认读取仓库根目录 audio_config.json')
    parser.add_argument('--input-device', type=int, default=None, help='临时覆盖输入设备索引')
    parser.add_argument('--output-device', type=int, default=None, help='临时覆盖输出设备索引')
    return parser.parse_args(argv)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, OSError):
            pass
    args = parse_args(argv)
    if args.self_check:
        return self_check()
    if args.stt_smoke:
        return stt_smoke(args)
    if not 1 <= args.seconds <= 20:
        emit('错误', '--seconds 必须在 1~20 之间')
        return 2
    try:
        input_device, output_device = resolve_devices(args)
    except Exception as exc:
        emit('错误', '音频配置读取失败：%s' % exc)
        emit('提示', '先跑：python test_audio_device.py --list，然后 '
                     'python test_audio_device.py --input-device N --output-device M --save-config audio_config.json')
        return 2
    stt_desc = 'API' if stt_ready() else (
        '本地 faster-whisper' if (not args.no_local_stt and local_stt.installed()) else 'Mock')
    emit('配置', 'STT=%s  LLM=%s(%s)  TTS=%s' % (
        stt_desc,
        '已配置' if llm_ready() else 'Mock',
        os.getenv('AI_MODEL') or '-',
        '已配置' if tts_ready() else 'Windows SAPI'))
    completed = 0
    try:
        for index in range(max(1, args.turns)):
            if args.turns > 1:
                print('\n===== 第 %d/%d 轮 =====' % (index + 1, args.turns), flush=True)
            if one_turn(args, input_device, output_device):
                completed += 1
    except KeyboardInterrupt:
        emit('状态', '已中断')
    finally:
        audio_io.close()
    emit('汇总', '%d/%d 轮完成；语音链路%s' % (
        completed, max(1, args.turns), '可用' if completed else '未完成'))
    return 0 if completed else 1


if __name__ == '__main__':
    sys.exit(main())
