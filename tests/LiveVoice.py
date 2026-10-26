"""Opt-in real cloud ASR/tool test using a prerecorded WAV and a fake device.

Never opens a microphone, speaker, camera or drone. Model code is inspected as
AST; by default it is not executed. --real-sandbox executes allowed queries in
real Docker against an explicit fake device. --real opts into paid cloud use.
"""
import argparse
import ast
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'modules' / 'ai'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from DuplexAudio import PlaybackBuffer
from VoiceProtocol import (VolcDuplex, Downsampler, DEFAULT_URL, DEFAULT_MODEL,
                         EV_ASR_STARTED, EV_ASR_DELTA, EV_ASR_COMPLETED,
                         EV_TOOL_CALL, EV_RESPONSE_DONE)


INSTRUCTIONS = '''你正在参与软件接口测试，连接的是明确标记为 simulated 的假设备。
用户请求查询无人机状态时，必须调用 run_program，code 为 print(drone.get_status())。
只能查询状态，不要起飞、移动、旋转、跟随或降落。不许凭空编造状态。
根据工具返回值简短回答，并明确说明这是模拟设备的测试状态。
你没有真实无人机或图片。'''
TOOLS = [{'type': 'function', 'name': 'run_program',
          'description': '仅查询明确标记为simulated的测试假设备状态，不得操作飞机。',
          'parameters': {'type': 'object', 'properties': {'code': {'type': 'string'}},
                         'required': ['code'], 'additionalProperties': False}}]


def status_query_only(code):
    try:
        tree = ast.parse(code)
    except (SyntaxError, TypeError):
        return False
    found = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.With, ast.AsyncWith)):
            return False
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == 'print':
                continue
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id == 'drone' and func.attr == 'get_status'
                    and not node.args and not node.keywords):
                found = True
            else:
                return False
    return found


class QueryOnlyRunner:
    def __init__(self, real_sandbox=False, timeout=15):
        self.calls, self.interrupts, self.cancel_count = [], [], 0
        self.container_rpc = False
        self.sandbox = None
        if real_sandbox:
            from sandbox_runner import SandboxRunner
            from gateway import Gateway
            from Support import MockBackend
            self.sandbox = SandboxRunner(Gateway(MockBackend()), timeout=timeout)

    async def run(self, code):
        allowed = status_query_only(code)
        self.calls.append({'code': code, 'allowed_status_query': allowed})
        if not allowed:
            return {'ok': False, 'status': 'failed', 'simulated': True,
                    'reason': 'real_test_only_allows_status_query_no_code_execution'}
        if self.sandbox is not None:
            result = await self.sandbox.run(code)
            self.container_rpc = any(entry.get('method') == 'get_status'
                                     and 'result' in entry for entry in result.get('journal', []))
            return result
        state = {'battery': 73, 'flight_state': 'landed', 'owner': None,
                 'simulated': True, 'source': 'test_fixture_not_a_real_drone'}
        return {'ok': True, 'status': 'completed', 'simulated': True,
                'journal': [{'method': 'get_status', 'args': [], 'kwargs': {}, 'result': state}],
                'debug': json.dumps(state, ensure_ascii=False)}

    def cancel(self):
        self.cancel_count += 1
        if self.sandbox is not None:
            self.sandbox.cancel()
            self.sandbox.gateway.cancelled.set()

    async def interrupt(self, land=False):
        self.interrupts.append(bool(land))
        if self.sandbox is not None:
            self.cancel()
            return await self.sandbox.gateway.interrupt(bool(land))
        return {'ok': True, 'status': 'interrupted', 'land': bool(land),
                'simulated': True, 'source': 'test_fixture_not_a_real_drone'}


class FileAudio:
    source_name = 'prerecorded_test_wav_no_audio_hardware'
    def __init__(self, wav_path):
        with wave.open(str(wav_path), 'rb') as stream:
            if stream.getnchannels() != 1 or stream.getsampwidth() != 2:
                raise ValueError('WAV必须为单声道16bit PCM')
            rate = stream.getframerate()
            if rate not in (16000, 22050, 24000, 32000, 44100, 48000):
                raise ValueError('不支持该WAV采样率')
            self.seconds = stream.getnframes() / rate
            if not 0 < self.seconds <= 15:
                raise ValueError('测试WAV必须为0..15秒')
            pcm = stream.readframes(stream.getnframes())
        self.pcm = pcm if rate == 24000 else Downsampler(src=rate, dst=24000).process(pcm)
        self.speaker = PlaybackBuffer()
        self.started = self.closed = False
        self.position = 0

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    async def chunks(self):
        # VolcDuplex itself paces 20ms packets. Padding is real silence, not ASR text.
        while not self.closed:
            chunk = self.pcm[self.position:self.position + 960]
            self.position += len(chunk)
            yield chunk.ljust(960, b'\0')


async def scenario(args):
    if not os.getenv('VOLC_API_KEY'):
        raise ValueError('缺少VOLC_API_KEY；不要将密钥放入命令行参数')
    runner = QueryOnlyRunner(real_sandbox=args.real_sandbox,
                             timeout=max(2, args.max_seconds - 10))
    audio = FileAudio(args.wav)
    logs, schemas, replies, transcripts, usages = [], {}, [], [], []
    finished = asyncio.Event()

    def log(event):
        logs.append(event)

    def usage(value):
        usages.append(value)
        if runner.calls and any(x.get('event') == 'tool_result_sent' for x in logs):
            finished.set()

    class DiagnosticDialog(VolcDuplex):
        async def _handle(self, event):
            kind = event.get('type')
            if kind in (EV_ASR_STARTED, EV_ASR_DELTA, EV_ASR_COMPLETED, EV_TOOL_CALL, EV_RESPONSE_DONE):
                schemas.setdefault(kind, sorted(event.keys()))
            if kind == EV_TOOL_CALL:
                schemas.setdefault('tool_item', sorted((event.get('items') or [{}])[0].keys()))
            if kind == EV_RESPONSE_DONE and isinstance(event.get('response'), dict):
                schemas.setdefault('response_object', sorted(event['response'].keys()))
            if kind in ('response.output_audio.started', 'response.output_audio.delta'):
                schemas.setdefault(kind, sorted(event.keys()))
            await super()._handle(event)

    dialog = DiagnosticDialog(runner, audio, instructions=INSTRUCTIONS, tools=TOOLS,
                              on_log=log, on_user_text=transcripts.append,
                              on_reply_text=replies.append, on_usage=usage,
                              max_session_seconds=args.max_seconds)
    started = time.perf_counter()
    client = asyncio.create_task(dialog.run())
    completion = asyncio.create_task(finished.wait())
    error = None
    try:
        done, _ = await asyncio.wait({client, completion}, return_when=asyncio.FIRST_COMPLETED)
        if client in done:
            try:
                await client
            except Exception as exc:
                error = type(exc).__name__ + ': ' + str(exc)
    finally:
        completion.cancel()
        if not client.done():
            client.cancel()
        await asyncio.gather(client, completion, return_exceptions=True)
        await dialog.close()
        audio.close()
    tool_results = [event for event in logs if event.get('event') == 'tool_result']
    ok = bool(transcripts and runner.calls and all(call['allowed_status_query'] for call in runner.calls)
              and any(event.get('result', {}).get('ok') is True for event in tool_results)
              and usages and error is None and (not args.real_sandbox or runner.container_rpc))
    return {'ok': ok, 'scope': 'real_cloud_prerecorded_synthetic_speech_fake_device',
            'model': dialog.model, 'endpoint': dialog.url,
            'elapsed_seconds': round(time.perf_counter() - started, 3),
            'wav_seconds': round(audio.seconds, 3), 'transcripts': transcripts,
            'execution': 'real_docker_gateway_fake_device' if args.real_sandbox else 'query_only_fake_runner_no_execution',
            'model_replies': replies, 'calls': runner.calls, 'tool_results': tool_results,
            'usage': usages, 'event_schemas': schemas,
            'tts_bytes_received_not_played': sum(audio.speaker.generated.values()),
            'audio_closed': audio.closed, 'error': error,
            'verified': {'real_cloud_asr': bool(transcripts),
                         'real_cloud_function_call': bool(runner.calls),
                         'tool_result_returned': any(x.get('event') == 'tool_result_sent' for x in logs),
                         'microphone': False, 'speaker': False, 'drone': False,
                         'vision': False, 'docker': runner.container_rpc},
            'cost_rmb': None, 'cost_note': 'usage retained; exact bill requires provider billing records'}


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real', action='store_true')
    parser.add_argument('--wav', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-seconds', type=int, default=25)
    parser.add_argument('--real-sandbox', action='store_true',
                        help='模型代码实际通过Docker/Gateway执行；底层仍为显式假设备，仅允许查询状态')
    args = parser.parse_args(argv)
    if not args.real or not 1 <= args.max_seconds <= 30:
        parser.error('收费测试必须显式--real，并设置1..30秒时限')
    result = asyncio.run(scenario(args))
    key = os.getenv('VOLC_API_KEY', '')
    text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if key:
        text = text.replace(key, '[redacted]')
    args.output.write_text(text + '\n', encoding='utf-8')
    print(text)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
