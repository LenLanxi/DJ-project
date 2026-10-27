"""Opt-in real RTC media measurement using files and an explicit fake device.

No browser, microphone, speaker, camera or aircraft is opened. Linux native RTC
SDK runs in its own Docker image. Control-plane timing is never a voice metric.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'ScenarioAcceptance' / 'Evidence' / 'RealtimeComparison'
BUILD = ROOT / 'modules' / 'rtc' / 'Build' / 'RTCNative'
sys.path.insert(0, str(Path(__file__).resolve().parent))
from LiveVoice import QueryOnlyRunner, status_query_only
from RealtimeMetrics import exact_acknowledgement


class Control:
    def __init__(self, credentials):
        self.credentials, self.process, self.sequence = credentials, None, 0
        self.errors = []

    async def open(self):
        env = dict(os.environ, COMPANION_DOU_CREDS=str(self.credentials))
        self.process = await asyncio.create_subprocess_exec(
            'node', str(ROOT / 'modules' / 'rtc' / 'DoubaoProbe.js'),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=ROOT)
        self.error_task = asyncio.create_task(self.drain_errors())

    async def drain_errors(self):
        while chunk := await self.process.stderr.read(4096):
            self.errors.append(len(chunk))

    async def call(self, command, **fields):
        self.sequence += 1
        request = dict(id=self.sequence, command=command, **fields)
        self.process.stdin.write((json.dumps(request, ensure_ascii=False)+'\n').encode())
        await self.process.stdin.drain()
        async with asyncio.timeout(15):
            while line := await self.process.stdout.readline():
                value = json.loads(line)
                if value.get('id') == self.sequence:
                    return value['result']
        raise RuntimeError('control process closed without response')

    async def close(self):
        if self.process is None:
            return
        self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 28)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
        await self.error_task


def task_instructions(task):
    # The production hybrid route and its NOVISION selector remain intact.
    # A speech turn therefore exercises the real S2S fallback; visual/tool turns
    # exercise the external Ark model, exactly as the application config does.
    base = ('这是文件输入的离线设备测试，只有云端语音和视觉真实，绝无真实无人机。'
            '不要主动问候，不要调用任何飞行动作。对用户问题准确简短作答。')
    if task == 'speech':
        return '你正在进行实时语音交互的软件测试。只根据本轮用户实际说的话，用中文简短回答。不主动发起问候，不执行设备动作，不输出推理过程。'
    if task == 'status':
        return base + ('查询电量必须先调用run_program，code精确为print(drone.get_status())。'
                       '只允许这项只读查询，工具结果是明确标记simulated的假设备。'
                       '据实简短回答模拟电量，不准编造状态。')
    return base + '根据当前视频画面的帐篷颜色，只回答颜色，不调用工具。'


TOOLS = [{'type': 'function', 'function': {
    'name': 'run_program', 'description': '仅查询模拟无人机的测试电量，不能操作飞机。',
    'parameters': {'type': 'object', 'properties': {'code': {'type': 'string'}},
                   'required': ['code'], 'additionalProperties': False}}}]


def delivery_verified(sample):
    """Media delivery and exact answer compliance answer different questions."""
    if sample.get('task') != 'speech':
        return sample.get('ok') is True
    cleanup = sample.get('cleanup', {})
    events = sample.get('events', [])
    user_ids = {event.get('payload', {}).get('UserID') for event in events
                if event.get('type') == 'conv' and event.get('payload', {}).get('UserID')}
    user_rounds, assistant_rounds, completed_user_rounds = set(), set(), set()
    for event in events:
        if event.get('type') != 'subv':
            continue
        for item in event.get('payload', {}).get('data', []):
            if item.get('definite') is True and item.get('roundId') is not None:
                destination = user_rounds if item.get('userId') in user_ids else assistant_rounds
                destination.add(item['roundId'])
                if item.get('userId') in user_ids and item.get('paragraph') is True:
                    completed_user_rounds.add(item['roundId'])
    same_round = bool(completed_user_rounds & assistant_rounds) and len(user_rounds) == len(assistant_rounds) == 1
    return bool('测试收到' in ''.join(sample.get('transcripts', []))
        and sample.get('media_verified') and sample.get('latency', {}).get('first_audio_ms') is not None
        and same_round
        and any(event.get('event') == 'pcm_eof' for event in events)
        and not any(event.get('event') in ('malformed_tlv', 'probe_error', 'sdk_error') for event in events)
        and cleanup.get('cloud_stop', {}).get('ok') is True
        and cleanup.get('native_exit_code') == 0 and cleanup.get('node_exit_code') == 0
        and cleanup.get('short_token_file_deleted') is True)


async def run_trial(task, image, credentials, fixture_meta, index, secrets):
    name = {'speech': 'Speech', 'status': 'Status', 'vision': 'Vision'}[task]
    audio = fixture_meta['audio'][name]
    sample = dict(provider='DoubaoRTC', task=task, turn_mode='automatic', ok=False,
                  media_verified=False, image=image if task == 'vision' else None,
                  attempt=index, client='Linux native Server SDK 3.60.107.700 in Docker',
                  production_client='Web SDK 4.69.3', hardware_verified=False,
                  latency={key: None for key in ['first_text_ms', 'first_audio_ms',
                                                'tool_call_ms', 'tool_result_sent_ms', 'completed_ms']},
                  events=[], transcripts=[], replies=[], tools=[], cleanup={})
    control = Control(credentials)
    native = None
    config_path = BUILD / f'Trial{index}{uuid.uuid4().hex}.json'
    container = 'DoubaoBenchmark' + uuid.uuid4().hex
    first_text = first_audio = first_tool = result_sent = completed = endpoint = None
    remote_frames = pulled_frames = nonzero_frames = video_frames = audio_chunks = 0
    max_rms = 0.0
    native_errors = []
    tools_seen = set()
    tlv_seen = set()
    epoch = time.perf_counter()
    play_sent = False
    done_at = None
    stderr_task = None
    runner = None
    initial_speech = audio['speech_end_samples']
    try:
        if task == 'status':
            runner = QueryOnlyRunner(real_sandbox=True, timeout=8)
            await runner.sandbox.check()  # Local failure must not spend API quota.
        await control.open()
        identity = await control.call('identity')
        if not identity.get('token'):
            raise RuntimeError('RTC identity unavailable')
        secrets.append(identity['token'])
        config = dict(identity, pcmPath=f'/fixtures/{name}.pcm', imagePath=f'/fixtures/{image}.i420',
                      sampleRate=16000, width=640, height=360, fps=2, maxSeconds=45,
                      speechEndSamples=initial_speech, rmsThreshold=64)
        BUILD.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config), encoding='utf-8')
        native = await asyncio.create_subprocess_exec(
            'docker', 'run', '--rm', '-i', '--name', container,
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--mount', f'type=bind,source={BUILD},target=/work',
            '--mount', f'type=bind,source={FIXTURES},target=/fixtures,readonly',
            'companion-rtc-probe:1', f'/work/{config_path.name}',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=131072)

        async def drain_stderr():
            while data := await native.stderr.read(4096):
                native_errors.append(len(data))

        stderr_task = asyncio.create_task(drain_stderr())
        async with asyncio.timeout(48):
            while True:
                if done_at is not None and time.perf_counter()-done_at > (0.6 if first_audio is not None else 3):
                    break
                try:
                    line = await asyncio.wait_for(native.stdout.readline(), 0.2)
                except TimeoutError:
                    if done_at is not None and time.perf_counter()-done_at > 0.6 and first_audio is not None:
                        break
                    if done_at is not None and time.perf_counter()-done_at > 3:
                        break
                    continue
                if not line:
                    break
                if not line.startswith(b'{'):
                    continue  # Native dependencies emit two startup version lines.
                event = json.loads(line)
                kind, stamp = event.get('event'), event.get('monotonic_ms')
                if kind == 'room_joined':
                    start = await control.call('start', instructions=task_instructions(task), tools=TOOLS)
                    sample['control_start'] = start
                    if not start.get('ok'):
                        raise RuntimeError('StartVoiceChat rejected')
                if kind in ('bot_ready', 'bot_listening') and not play_sent:
                    # Participant-ready admission mirrors the production route;
                    # readiness setup is kept outside the speech-end latency.
                    await asyncio.sleep(0.4)
                    native.stdin.write(b'{"command":"play"}\n')
                    await native.stdin.drain()
                    play_sent = True
                if kind == 'audio_chunk':
                    audio_chunks += 1
                    if endpoint is None and event['last_sample'] >= initial_speech:
                        endpoint = event['enqueued_ms']-(event['last_sample']-initial_speech)/16
                        sample['speech_endpoint'] = dict(native_ms=endpoint,
                            method='actual_covering_chunk_enqueue_minus_trailing_samples',
                            speech_end_samples=initial_speech, covering_chunk=event)
                        sample['events'].append(dict(event='speech_endpoint', monotonic_ms=endpoint))
                    continue
                if kind == 'video_frame_submitted':
                    video_frames += 1
                    if video_frames > 1:
                        continue
                if kind in ('output_audio_frame', 'remote_audio_observer_frame'):
                    remote_frames += kind == 'remote_audio_observer_frame'
                    pulled_frames += kind == 'output_audio_frame'
                    max_rms = max(max_rms, event['rms'])
                    nonzero_frames += event['rms'] >= 64
                    continue
                if kind == 'remote_audio_nonzero':
                    first_audio = min(first_audio, event['first_window_ms']) if first_audio is not None else event['first_window_ms']
                    sample['first_audio_boundary'] = event.get('source', 'external_pull')
                if kind == 'tool_result_submitted' and result_sent is None:
                    result_sent = stamp
                if kind == 'tlv':
                    value = event.get('payload', {})
                    msg_type = event.get('type')
                    signature = json.dumps([msg_type, value], sort_keys=True, ensure_ascii=False)
                    if signature in tlv_seen:
                        continue  # SDK dispatches both old and new callback overloads.
                    tlv_seen.add(signature)
                    if msg_type == 'subv':
                        for item in value.get('data', []):
                            text = item.get('text', '')
                            if not text:
                                continue
                            user = item.get('userId')
                            if user == identity['botId']:
                                if first_text is None:
                                    first_text = stamp
                                if item.get('definite') is True:
                                    sample['replies'].append(text)
                            elif user == identity['userId'] and item.get('definite') is True:
                                sample['transcripts'].append(text)
                    elif msg_type == 'conv' and value.get('Stage', {}).get('Code') == 5:
                        if play_sent:
                            completed, done_at = stamp, time.perf_counter()
                    elif msg_type == 'tool':
                        for call in value.get('tool_calls', []):
                            if call.get('id') in tools_seen:
                                continue
                            tools_seen.add(call.get('id'))
                            if first_tool is None:
                                first_tool = stamp
                            function = call.get('function', {})
                            arguments = function.get('arguments', {})
                            if isinstance(arguments, str):
                                arguments = json.loads(arguments)
                            allowed = task == 'status' and function.get('name') == 'run_program' and status_query_only(arguments.get('code'))
                            if allowed:
                                result = await runner.run(arguments['code'])
                            else:
                                result = dict(ok=False, simulated=True, status='failed',
                                    source='benchmark_fake_device_no_aircraft', reason='only_status_query_allowed')
                            sample['tools'].append(dict(id=call.get('id'), name=function.get('name'),
                                                       arguments=arguments, result=result))
                            state = next((entry.get('result', {}) for entry in result.get('journal', [])
                                          if entry.get('method') == 'get_status'), {})
                            compact = dict(ok=result.get('ok') is True, simulated=True,
                                status=result.get('status', 'failed'), battery=state.get('battery'),
                                source='real_Docker_Gateway_MockBackend_no_aircraft')
                            reply = dict(command='tool_result', id=call.get('id'),
                                         content=json.dumps(compact, ensure_ascii=False))
                            native.stdin.write((json.dumps(reply, ensure_ascii=False)+'\n').encode())
                            await native.stdin.drain()
                    # Keep subtitles/stages/tools, without media bytes or keys.
                sample['events'].append(event)
                if kind in ('probe_error', 'sdk_error'):
                    raise RuntimeError('native RTC error')
                if done_at is not None and time.perf_counter()-done_at > 0.6 and first_audio is not None:
                    break
    except (Exception, asyncio.CancelledError) as error:
        sample['failure'] = type(error).__name__ + ': ' + str(error)[:200]
    finally:
        try:
            sample['cleanup']['cloud_stop'] = await control.call('stop') if control.process else {'ok': False}
        except Exception as error:
            sample['cleanup']['cloud_stop'] = dict(ok=False, error=type(error).__name__)
        if native is not None:
            try:
                native.stdin.write(b'{"command":"stop"}\n')
                await native.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            try:
                await asyncio.wait_for(native.wait(), 5)
            except TimeoutError:
                killer = await asyncio.create_subprocess_exec('docker', 'rm', '-f', container,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await killer.wait()
                await asyncio.wait_for(native.wait(), 5)
                sample['cleanup']['native_forced'] = True
            sample['cleanup']['native_exit_code'] = native.returncode
            if stderr_task:
                await stderr_task
        await control.close()
        if runner is not None:
            try:
                interrupted = await runner.interrupt(False)
                sample['cleanup']['query_runtime_stopped'] = interrupted.get('ok') is True
            except Exception as error:
                sample['cleanup']['query_runtime_stopped'] = False
                sample['cleanup']['query_runtime_error'] = type(error).__name__
        sample['cleanup']['node_exit_code'] = control.process.returncode if control.process else None
        config_path.unlink(missing_ok=True)
        sample['cleanup']['short_token_file_deleted'] = not config_path.exists()
    sample['elapsed_seconds'] = time.perf_counter()-epoch
    sample['media'] = dict(audio_chunks=audio_chunks, video_frames=video_frames,
                           decoded_audio_frames=remote_frames, pulled_audio_frames=pulled_frames,
                           nonzero_audio_frames=nonzero_frames,
                           maximum_output_rms=max_rms, native_stderr_bytes=sum(native_errors),
                           node_stderr_bytes=sum(control.errors))
    sample['media_verified'] = bool(audio_chunks and video_frames and sample['transcripts'] and first_audio is not None)
    if endpoint is not None:
        for key, value in [('first_text_ms', first_text), ('first_audio_ms', first_audio),
                           ('tool_call_ms', first_tool), ('tool_result_sent_ms', result_sent), ('completed_ms', completed)]:
            sample['latency'][key] = value-endpoint if value is not None else None
    answer = ''.join(sample['replies'])
    expected = '测试收到' if task == 'speech' else '80' if task == 'status' else fixture_meta['images'][image]['expected_color']
    answer_ok = exact_acknowledgement(answer) if task == 'speech' else (expected in answer or task == 'status' and '八十' in answer)
    if task == 'vision':
        other = '蓝' if expected == '红' else '红'
        answer_ok = answer_ok and other not in answer
    sample['container_rpc'] = bool(runner and runner.container_rpc)
    sample['execution'] = 'real_Docker_Gateway_MockBackend_no_aircraft' if task == 'status' else 'no_device_tool'
    actual_batteries = [entry.get('result', {}).get('battery') for tool in sample['tools']
                       for entry in tool['result'].get('journal', []) if entry.get('method') == 'get_status']
    tools_ok = task != 'status' or bool(sample['container_rpc'] and 80 in actual_batteries and
        sample['tools'] and all(t['result']['ok'] for t in sample['tools']) and result_sent is not None)
    sample['expected_answer'] = expected
    sample['answer_correct'] = answer_ok
    sample['asr_correct'] = task != 'speech' or '测试收到' in ''.join(sample['transcripts'])
    sample['assessment'] = dict(asr_received=bool(sample['transcripts']),
                                nonzero_audio_received=first_audio is not None,
                                tool_verified=tools_ok, speech_endpoint_verified=endpoint is not None)
    sample['ok'] = bool(sample['media_verified'] and answer_ok and sample['asr_correct'] and tools_ok and endpoint is not None
                        and not sample.get('failure') and sample['cleanup'].get('cloud_stop', {}).get('ok'))
    sample['delivery_verified'] = delivery_verified(sample)
    return sample


def redact(value, secrets):
    serialized = json.dumps(value, ensure_ascii=False, indent=2)
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 8:
            serialized = serialized.replace(secret, '[REDACTED]')
    return serialized


async def main(args):
    credentials = Path(args.credentials)
    if not args.real:
        raise ValueError('--real is required for paid cloud media sessions')
    meta = json.loads((FIXTURES/'Fixtures.json').read_text(encoding='utf-8-sig'))
    private = json.loads(credentials.read_text(encoding='utf-8-sig'))
    secrets = []

    def collect(value):
        if isinstance(value, dict):
            for key, entry in value.items():
                if any(tag in key.lower() for tag in ['key', 'token', 'secret']) and isinstance(entry, str):
                    secrets.append(entry)
                else:
                    collect(entry)
    collect(private)
    output = Path(args.output)
    report = json.loads(output.read_text(encoding='utf-8')) if output.exists() else dict(
        scope='real_cloud_native_rtc_prerecorded_audio_synthetic_images_fake_device',
        no_physical_devices=True, samples=[])
    for existing in report['samples']:
        existing['delivery_verified'] = delivery_verified(existing)
    for _ in range(args.trials):
        task, image = args.task, args.image
        sample = await run_trial(task, image, credentials, meta, len(report['samples'])+1, secrets)
        sample['phase'] = 'development' if args.development else 'measurement'
        report['samples'].append(sample)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(redact(report, secrets), encoding='utf-8')
        print(json.dumps(dict(attempt=sample['attempt'], task=task, image=image if task == 'vision' else None,
                              ok=sample['ok'], media_verified=sample['media_verified'],
                              latency=sample['latency'], replies=sample['replies'],
                              failure=sample.get('failure')), ensure_ascii=False), flush=True)
        if not sample['ok']:
            break  # Preserve the failure and diagnose; no automatic retry loop.


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real', action='store_true')
    parser.add_argument('--credentials', default=r'C:\Users\bente\Desktop\tmp\DoubaoRealtime\Credentials.json')
    parser.add_argument('--output', default=str(FIXTURES/'DoubaoMedia.json'))
    parser.add_argument('--task', choices=['speech', 'status', 'vision'], default='speech')
    parser.add_argument('--image', choices=['RedTent', 'BlueTent'], default='RedTent')
    parser.add_argument('--trials', type=int, choices=range(1, 4), default=1)
    parser.add_argument('--development', action='store_true', help='exclude setup/debug samples from rankings')
    asyncio.run(main(parser.parse_args()))
