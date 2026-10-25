"""纯本地蓝牙 AI 全链路自检（不连飞机、不下发任何飞行指令）。

流程：
  1) 用默认麦克风（蓝牙耳机/内置麦）录 4 秒
  2) 本地 faster-whisper 识别成文字（离线，不依赖网络）
  3) 豆包云端（VOLC_API_KEY）：握手 + 建会话 + 让它把回复合成成语音回传
  4) 播报：优先播放云端返回的音频；失败回落 Windows SAPI
  5) 丢帧防线：用真正的 realtime.RealtimeSession，注入一次输入丢帧，
     验证「取消当前程序 + 禁用运动工具（只保留停止）」

用法（在本目录）：
    $env:VOLC_API_KEY = "<密钥值，不是名称>"
    .\\.venv\\Scripts\\python.exe -X utf8 -B test_bluetooth_ai.py
    .\\.venv\\Scripts\\python.exe -X utf8 -B test_bluetooth_ai.py --seconds 4 --keep-wav
选项：
    --seconds N    录音时长（默认 4）
    --device N     指定输入设备索引（默认用系统默认输入）
    --no-cloud     跳过豆包（只测本地 STT/TTS + 丢帧防线）
    --keep-wav     保留录音文件 logs/bluetooth_probe.wav
退出码：0 全部通过 / 1 有失败 / 2 关键步骤失败（录音或防线）
"""
import argparse
import asyncio
import base64
import json
import math
import os
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AI = ROOT / 'modules' / 'ai'
for path in (str(AI), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

RESULTS = []


def record(stage, ok, detail='', skipped=False):
    RESULTS.append({'stage': stage, 'ok': ok, 'detail': detail, 'skipped': skipped})
    tag = 'SKIP' if skipped else ('PASS' if ok else 'FAIL')
    print('[%s] %-10s %s' % (tag, stage, detail), flush=True)


def load_dotenv(path=ROOT / '.env'):
    """.env 不会被自动加载；这里读一次，避免每次手敲密钥。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, _, value = line.partition('=')
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name and value and name not in os.environ:
            os.environ[name] = value


# ---------------------------------------------------------------- 1) 录音
def stage_record(seconds, device):
    try:
        import numpy as np
        import sounddevice as sd
    except Exception as exc:
        record('录音', False, '缺少 sounddevice/numpy：%s' % exc)
        return None

    try:
        info = sd.query_devices(device, 'input')
    except Exception as exc:
        record('录音', False, '找不到输入设备：%s' % exc)
        return None
    rate = int(info.get('default_samplerate') or 16000)
    print('[..] 输入设备：%s  %d Hz' % (info.get('name'), rate), flush=True)
    for left in (3, 2, 1):
        print('     %d… 请准备说话' % left, flush=True)
        time.sleep(1.0)
    print('     >>> 开始录音 %.1f 秒（请清楚地说一句话，例如"起飞"）' % seconds, flush=True)
    try:
        audio = sd.rec(int(seconds * rate), samplerate=rate, channels=1,
                       dtype='float32', device=device)
        sd.wait()
    except Exception as exc:
        record('录音', False, '%s: %s' % (type(exc).__name__, exc))
        return None
    data = audio[:, 0]
    peak = float(np.abs(data).max())
    rms = float(np.sqrt((data ** 2).mean()))
    peak_db = 20 * math.log10(max(peak, 1e-9))
    detail = '设备=%s 采样率=%d 峰值=%.1fdBFS RMS=%.1fdBFS' % (
        info.get('name'), rate, peak_db, 20 * math.log10(max(rms, 1e-9)))
    if peak < 0.005:
        record('录音', False, detail + '（几乎无声：麦克风静音/未选中/离得太远）')
        return None
    pcm16 = (np.clip(data, -1, 1) * 32767).astype('<i2').tobytes()
    record('录音', True, detail)
    return {'pcm16': pcm16, 'rate': rate}


# ---------------------------------------------------------------- 2) 本地 STT
def stage_stt(audio):
    if audio is None:
        record('STT', False, '没有录音可用', skipped=True)
        return ''
    try:
        from local_stt import LocalWhisper
    except Exception as exc:
        record('STT', False, '本地 whisper 不可用：%s' % exc)
        return ''
    wav = ROOT / 'logs' / 'bluetooth_probe.wav'
    wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav), 'wb') as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(audio['rate'])
        fh.writeframes(audio['pcm16'])
    try:
        text = LocalWhisper().warmup().transcribe(wav.read_bytes())
    except Exception as exc:
        record('STT', False, '%s: %s' % (type(exc).__name__, exc))
        return ''
    wav_name = wav.name
    if not text.strip():
        record('STT', False, '识别为空（说了吗？离麦克风近一点再说一遍）  录音=%s' % wav_name)
        return ''
    record('STT', True, '识别文本=「%s」  录音=%s' % (text, wav_name))
    return text


# ---------------------------------------------------------------- 3) 豆包云端
async def stage_cloud(text):
    key = os.getenv('VOLC_API_KEY', '').strip()
    if not key:
        record('豆包', False, '缺少 VOLC_API_KEY（控制台里那串 UUID 值，不是 api-key-xxx 名称）',
               skipped=True)
        return None
    try:
        from websockets.asyncio.client import connect
        from volc_dialog import (DEFAULT_MODEL, DEFAULT_URL, DEFAULT_VOICE,
                                 EV_AUDIO_DELTA, EV_ERROR, EV_SESSION_CREATED,
                                 EV_TEXT_DELTA, EV_SPEECH_TEXT_COMMIT, build_session)
    except Exception as exc:
        record('豆包', False, '客户端不可用：%s' % exc)
        return None

    url = os.getenv('VOLC_DIALOG_URL') or DEFAULT_URL
    model = os.getenv('VOLC_MODEL') or DEFAULT_MODEL
    voice = os.getenv('VOLC_VOICE') or DEFAULT_VOICE
    say = '我听到你说：%s' % (text or '你好')
    pcm = bytearray()
    reply = []
    try:
        async with connect(url, additional_headers={'X-Api-Key': key},
                           open_timeout=20, ping_interval=20, max_size=16_000_000) as ws:
            log_id = ws.response.headers.get('X-Tt-Logid') if ws.response else None
            await ws.send(json.dumps(build_session(model=model, voice=voice,
                                                   instructions='你是无人机语音助手，回答要短。'),
                                     ensure_ascii=False))
            created = False
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not created:
                try:
                    event = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                except asyncio.TimeoutError:
                    continue
                kind = event.get('type')
                if kind == EV_SESSION_CREATED:
                    created = True
                elif kind == EV_ERROR:
                    record('豆包', False, '服务端错误：%s' % json.dumps(event, ensure_ascii=False)[:200])
                    return None
            if not created:
                record('豆包', False, '20 秒内没收到 session.created')
                return None
            await ws.send(json.dumps({'type': EV_SPEECH_TEXT_COMMIT,
                                      'event_id': 'probe-1', 'text': say}, ensure_ascii=False))
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                try:
                    event = json.loads(await asyncio.wait_for(ws.recv(), timeout=6))
                except asyncio.TimeoutError:
                    break
                kind = event.get('type')
                if kind == EV_AUDIO_DELTA and event.get('delta'):
                    pcm.extend(base64.b64decode(event['delta']))
                elif kind == EV_TEXT_DELTA and event.get('delta'):
                    reply.append(str(event['delta']))
                elif kind in ('response.output_audio.done', 'response.done'):
                    break
                elif kind == EV_ERROR:
                    record('豆包', False, '合成阶段错误：%s' % json.dumps(event, ensure_ascii=False)[:200])
                    return None
    except Exception as exc:
        record('豆包', False, '%s: %s' % (type(exc).__name__, exc))
        return None

    if not pcm:
        record('豆包', False, '会话建立成功但没收到音频（logid=%s）' % log_id)
        return None
    record('豆包', True, '握手+会话 OK  logid=%s  model=%s  voice=%s  回传音频=%d 字节'
           % (log_id, model, voice, len(pcm)))
    return {'pcm24k': bytes(pcm), 'spoken': say}


# ---------------------------------------------------------------- 4) 播报
def stage_speak(cloud):
    if cloud:
        try:
            import numpy as np
            import sounddevice as sd
            data = np.frombuffer(cloud['pcm24k'], dtype='<i2').astype('float32') / 32768.0
            sd.play(data, 24000)
            sd.wait()
            record('TTS', True, '云端音频已播放（24kHz PCM %d 字节）' % len(cloud['pcm24k']))
            return
        except Exception as exc:
            print('[..] 云端音频播放失败（%s），回落 Windows SAPI' % exc, flush=True)
    try:
        import offline_tts
        if not offline_tts.available():
            record('TTS', False, 'Windows SAPI 不可用（无可用语音）')
            return
        offline_tts.speak('这是离线播报测试，链路正常。')
        record('TTS', True, 'Windows SAPI 离线播报完成')
    except Exception as exc:
        record('TTS', False, '%s: %s' % (type(exc).__name__, exc))


# ---------------------------------------------------------------- 5) 丢帧防线
class _FakeSpeaker:
    def __init__(self):
        self.interrupted = 0

    def interrupt(self):
        self.interrupted += 1


class _FakeAudio:
    def __init__(self):
        self.loss_generation = 0
        self.speaker = _FakeSpeaker()

    def on_input_gap(self):
        return None


class _FakeGateway:
    def __init__(self):
        self.interrupts = []

    async def interrupt(self, land=False):
        self.interrupts.append(bool(land))
        return {'ok': True, 'status': 'accepted', 'land': bool(land)}

    async def finish(self, ok):
        return {'ok': True}


class _FakeRunner:
    def __init__(self):
        self.cancelled = 0
        self.running = True
        self.programs = []
        self.gateway = _FakeGateway()

    def cancel(self):
        self.cancelled += 1

    async def run(self, code):
        self.programs.append(code)
        return {'ok': True, 'status': 'completed', 'result': 'ran'}


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


async def stage_discontinuity():
    try:
        from realtime import RealtimeSession
    except Exception as exc:
        record('丢帧防线', False, '无法导入 realtime：%s' % exc)
        return
    events = []
    runner, audio = _FakeRunner(), _FakeAudio()
    session = RealtimeSession(runner, audio, logger=events.append)
    session.ws = _FakeWS()          # call_tool 要把结果回传给服务端，这里用假 socket 收下
    epoch = session.epoch
    # 模拟蓝牙突发丢包：采集层把 loss_generation 加一，会话立刻处理
    audio.loss_generation = 1
    session.note_input_loss()
    await asyncio.sleep(0.05)

    kinds = [e.get('event') for e in events]
    disabled = [e for e in events if e.get('event') == 'audio_input_discontinuity']
    ok_log = bool(disabled) and disabled[0].get('action') == 'motion_tools_disabled_until_session_restart'
    ok_cancel = runner.cancelled >= 1
    ok_speaker = audio.speaker.interrupted >= 1
    ok_degraded = session.input_degraded is True
    if not (ok_log and ok_cancel and ok_speaker and ok_degraded):
        record('丢帧防线', False,
               '事件=%s 取消=%d 播报打断=%d degraded=%s'
               % (kinds, runner.cancelled, audio.speaker.interrupted, session.input_degraded))
        return

    # 丢帧后：生成运动的程序必须被拒
    await session.call_tool({'name': 'run_program', 'call_id': 'c1',
                             'arguments': json.dumps({'code': 'drone.takeoff()'})},
                            session.epoch)
    # 停止工具仍然可用（interrupt land=False）
    await session.call_tool({'name': 'interrupt', 'call_id': 'c2',
                             'arguments': json.dumps({'land': False})},
                            session.epoch)
    # call_tool 不返回值，结果在 logger 的 tool_result 事件里
    tool_results = [e for e in events if e.get('event') == 'tool_result']
    blocked = next((e.get('result') or {} for e in tool_results if e.get('name') == 'run_program'), {})
    stop = next((e.get('result') or {} for e in tool_results if e.get('name') == 'interrupt'), {})
    ok_block = (blocked.get('ok') is False
                and blocked.get('reason') == 'audio_input_discontinuity_restart_required')
    # 丢帧时 note_input_loss 自己也会调一次 interrupt(False)，所以这里只要求"最后一次是停止"
    ok_stop = (stop.get('ok') is True and runner.gateway.interrupts
               and runner.gateway.interrupts[-1] is False)
    if ok_block and ok_stop:
        record('丢帧防线', True,
               'audio_input_discontinuity→禁用运动工具；run_program 被拒(%s)；停止工具仍可用'
               % blocked.get('reason'))
    else:
        record('丢帧防线', False, '拦截=%s 停止=%s 工具结果=%s' % (blocked, stop, len(tool_results)))


# ---------------------------------------------------------------- main
async def run(args):
    print('=== 纯本地蓝牙 AI 自检（不连飞机）===', flush=True)
    print('python=%s' % sys.executable, flush=True)
    load_dotenv()
    print('VOLC_API_KEY=%s' % ('已设置' if os.getenv('VOLC_API_KEY') else '未设置'), flush=True)

    audio = stage_record(args.seconds, args.device)
    text = stage_stt(audio)
    cloud = None
    if args.no_cloud:
        record('豆包', False, '按 --no-cloud 跳过', skipped=True)
    else:
        cloud = await stage_cloud(text)
    stage_speak(cloud)
    await stage_discontinuity()

    print('\n=== 结果汇总 ===', flush=True)
    for item in RESULTS:
        tag = 'SKIP' if item['skipped'] else ('PASS' if item['ok'] else 'FAIL')
        print('  %-4s %-8s %s' % (tag, item['stage'], item['detail'][:110]), flush=True)
    failed = [r for r in RESULTS if not r['ok'] and not r['skipped']]
    skipped = [r for r in RESULTS if r['skipped']]
    print('\n通过 %d 项，失败 %d 项，跳过 %d 项' % (len(RESULTS) - len(failed) - len(skipped),
                                              len(failed), len(skipped)), flush=True)
    if any(not r['ok'] and r['stage'] in ('录音', '丢帧防线') for r in RESULTS):
        return 2
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=4.0)
    parser.add_argument('--device', type=int, default=None)
    parser.add_argument('--no-cloud', action='store_true')
    parser.add_argument('--keep-wav', action='store_true')
    args = parser.parse_args()
    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        print('\n[中断] 已取消', flush=True)
        return 130
    if not args.keep_wav:
        wav = ROOT / 'logs' / 'bluetooth_probe.wav'
        if wav.is_file():
            wav.unlink(missing_ok=True)
    return code


if __name__ == '__main__':
    sys.exit(main())
