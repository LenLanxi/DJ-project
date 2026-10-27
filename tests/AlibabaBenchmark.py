"""Opt-in real Alibaba media benchmark; shared synthetic fixtures, no devices.

Every invocation appends all outcomes. Development and formal samples are kept
separate, and each trial uses a fresh native WebSocket session. No retry loop.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'modules' / 'ai'))
sys.path.insert(0, str(ROOT / 'tests'))
from AlibabaRealtime import AlibabaConfig, AlibabaRealtime
from RealtimeMetrics import exact_acknowledgement

FOLDER = ROOT / 'ScenarioAcceptance' / 'Evidence' / 'RealtimeComparison'
SPEECH_INSTRUCTIONS = '你正在进行实时语音交互的软件测试。只根据本轮用户实际说的话，用中文简短回答。不主动发起问候，不执行设备动作，不输出推理过程。'


def fixture(name, folder=FOLDER):
    metadata = json.loads((folder / 'Fixtures.json').read_text(encoding='utf-8-sig'))['audio'][name]
    pcm = (folder / (name + '.pcm')).read_bytes()
    wav_path = folder / (name + '.wav')
    with wave.open(str(wav_path), 'rb') as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (16000, 1, 2):
            raise ValueError('shared_fixture_format_mismatch')
        if stream.readframes(stream.getnframes()) != pcm:
            raise ValueError('shared_pcm_wav_mismatch')
    if (len(pcm) != metadata['samples'] * 2
            or metadata['sha256'] not in (hashlib.sha256(pcm).hexdigest(), hashlib.sha256(wav_path.read_bytes()).hexdigest())):
        raise ValueError('shared_fixture_hash_mismatch')
    return pcm, metadata


def latency(metrics):
    anchor = metrics.get('speech_end_anchor_ms')
    mapping = {'first_text_ms': 'first_text_ms', 'first_audio_ms': 'first_audio_ms',
               'first_audio_chunk_ms': 'first_audio_chunk_ms',
               'tool_call_ms': 'tool_call_ms', 'tool_result_sent_ms': 'result_sent_ms',
               'completed_ms': 'response_done_ms',
               'post_result_first_text_ms': 'post_result_first_text_ms',
               'post_result_first_audio_ms': 'post_result_first_audio_ms'}
    return {label: round(metrics[field]-anchor, 3) if anchor is not None and field in metrics else None
            for label, field in mapping.items()}


def semantic_check(task, raw, expected_color=None, container_rpc=False):
    reply = raw.get('reply_text', '')
    audible_bytes = raw.get('output_audio_bytes', 0)
    nonzero = raw.get('metrics', {}).get('first_nonzero_audio_ms') is not None
    transport = raw.get('ok') is True
    if task == 'speech':
        answer = exact_acknowledgement(reply)
        understood = '测试收到' in re.sub(r'\s+', '', raw.get('asr_text', ''))
        return transport and answer and understood and audible_bytes > 0 and nonzero, {
            'exact_acknowledgement': answer, 'actual_asr_phrase_present': understood,
            'nonzero_audio_detected': nonzero}
    if task == 'status':
        battery_results = []
        for output in raw.get('tool_results', []):
            for item in output.get('result', {}).get('journal', []):
                if item.get('method') == 'get_status':
                    battery_results.append(item.get('result', {}).get('battery'))
        answer = bool(re.search(r'80|八十', reply))
        actual = bool(raw.get('tool_calls')) and 80 in battery_results and container_rpc
        return transport and actual and answer and audible_bytes > 0 and nonzero, {
            'actual_tool_battery_80': actual, 'reply_contains_80': answer,
            'container_rpc': container_rpc, 'battery_results': battery_results}
    color = expected_color in reply if expected_color else False
    opposite = ('蓝' if expected_color == '红' else '红') in reply
    return transport and raw.get('image_sent') is True and color and not opposite and audible_bytes > 0 and nonzero, {
        'expected_color': expected_color, 'correct_color_present': color,
        'opposite_color_present': opposite, 'image_sent': raw.get('image_sent', False)}


def delivery_check(raw, cleanup, *, task='speech'):
    """Verify complete media delivery separately from exact-answer correctness."""
    return (raw.get('ok') is True and bool(raw.get('asr_text', '').strip())
            and (task != 'speech' or '测试收到' in raw.get('asr_text', ''))
            and raw.get('metrics', {}).get('asr_final_ms') is not None
            and raw.get('metrics', {}).get('first_nonzero_audio_ms') is not None
            and raw.get('output_audio_bytes', 0) > 0
            and all(cleanup.get(key) is True for key in
                    ('connection_closed', 'sender_finished', 'receiver_finished')))


def recompute_delivery_evidence(sample):
    """Revalidate an existing receipt without changing its original observations.

    This function does not write a receipt or call an API. The caller can append
    its returned evidence to an earlier schema's sample after exclusive access
    to the evidence file has been obtained. Missing acoustic proof fails closed.
    """
    metrics = sample.get('timing', {}).get('raw_metrics', {})
    activity = sample.get('audio_activity') or {}
    acoustic_proof = (activity.get('rms_threshold') == 64
        and activity.get('window_ms') == 10
        and activity.get('consecutive_windows') == 2
        and activity.get('first_window_received_ms') == metrics.get('first_nonzero_audio_ms')
        and sample.get('first_audio_definition') ==
            'first_received_window_RMS64_two_consecutive_10ms_not_playback')
    raw = {'ok': sample.get('transport_ok'), 'asr_text': ''.join(sample.get('transcripts', [])),
           'metrics': metrics, 'output_audio_bytes': sample.get('output_audio_bytes', 0)}
    verified = bool(sample.get('task') == 'speech' and acoustic_proof
        and delivery_check(raw, sample.get('cleanup', {})))
    return {'delivery_verified': verified,
            'delivery_evidence': {'recomputed_from_existing_evidence': True,
                'method': 'existing_complete_asr_nonzero_pcm_transport_and_cleanup_v1',
                'original_metrics_preserved': True, 'acoustic_proof': bool(acoustic_proof)}}


async def sample(config, task, *, mode, image_name=None, phase='development', timeout=30):
    name = {'speech': 'Speech', 'status': 'Status', 'vision': 'Vision'}[task]
    pcm, meta = fixture(name)
    picture = (FOLDER / (image_name + '.jpg')).read_bytes() if image_name else None
    runner = None
    tools = []
    if task == 'speech':
        instructions = SPEECH_INSTRUCTIONS
    elif task == 'vision':
        instructions = '观察本轮实际收到的图片，回答用户的问题。只回答图片中帐篷的颜色，不要猜测或补充解释。'
    else:
        from LiveVoice import QueryOnlyRunner
        runner = QueryOnlyRunner(real_sandbox=True, timeout=8)
        await runner.sandbox.check()  # Fail locally before spending API quota.
        tools = [{'type': 'function', 'name': 'run_program',
                  'description': '查询明确标记simulated的测试设备状态。禁止起飞、移动、旋转、跟随或降落。',
                  'parameters': {'type': 'object', 'properties': {'code': {'type': 'string', 'maxLength': 600}},
                                 'required': ['code'], 'additionalProperties': False}}]
        instructions = ('你参加实时接口测试，连接的是simulated测试设备。用户查询电量时必须调用run_program，'
                        'code为print(drone.get_status())。根据实际工具结果简短报告模拟电量，不得猜测，不得执行动作。')
    async def dispatch(name, arguments, call_id):
        if name != 'run_program' or runner is None:
            raise ValueError('query_only_dispatcher')
        return await runner.run(arguments['code'])
    adapter = AlibabaRealtime(config, instructions=instructions, tools=tools,
                              tool_handler=dispatch if runner else None)
    cleanup = {}
    started = datetime.now(timezone.utc).isoformat()
    try:
        raw = await adapter.run_turn(pcm, picture, mode='semantic_vad' if mode == 'automatic' else 'manual',
                                     speech_end_seconds=meta['speech_end_seconds'],
                                     timeout_seconds=timeout, max_audio_seconds=15)
    finally:
        await adapter.close()
        if runner:
            stopped = await runner.interrupt(False)
            cleanup['query_runtime_stopped'] = stopped.get('ok') is True
    expected = '红' if image_name == 'RedTent' else '蓝' if image_name == 'BlueTent' else None
    checked, evidence = semantic_check(task, raw, expected, bool(runner and runner.container_rpc))
    cleanup.update(raw.get('cleanup', {}))
    return {'provider': 'AlibabaRealtime', 'task': task, 'turn_mode': mode, 'phase': phase,
            'started_at': started, 'finished_at': datetime.now(timezone.utc).isoformat(),
            'model': config.model, 'ok': checked,
            'answer_verified': checked, 'delivery_verified': delivery_check(raw, cleanup, task=task),
            'system_instructions': instructions,
            'media_verified': checked and raw.get('output_audio_bytes', 0) > 0,
            'latency': latency(raw['metrics']), 'events': raw['events'],
            'transcripts': [raw['asr_text']] if raw['asr_text'] else [],
            'replies': [raw['reply_text']] if raw['reply_text'] else [],
            'cleanup': cleanup, 'semantic_evidence': evidence,
            'vad_config': {'type': 'semantic_vad', 'silence_duration_ms': 800,
                           'extra_silence_seconds': raw['vad_padding_seconds']} if mode == 'automatic' else None,
            'timing': {'origin': 'actual_last_voiced_packet_send_minus_packet_overshoot',
                       'input_end_send_ms': raw['metrics'].get('input_end_send_ms'),
                       'packet_overshoot_ms': raw['metrics'].get('input_end_packet_overshoot_ms'),
                       'speech_end_anchor_ms': raw['metrics'].get('speech_end_anchor_ms'),
                       'speech_end_seconds': meta['speech_end_seconds'],
                       'speech_end_threshold': meta['speech_end_threshold'], 'raw_metrics': raw['metrics']},
            'fixture': {'audio': name, 'image': image_name, 'sha256': meta['sha256'],
                        'image_sha256': hashlib.sha256(picture).hexdigest() if picture else None,
                        'synthetic': True, 'physical_devices': False},
            'tool_calls': raw['tool_calls'], 'tool_results': raw['tool_results'],
            'usage': raw['usage'], 'output_audio_bytes': raw['output_audio_bytes'],
            'audio_activity': raw.get('audio_activity'),
            'first_audio_definition': 'first_received_window_RMS64_two_consecutive_10ms_not_playback',
            'transport_ok': raw['ok'], 'error_code': raw.get('error_code'),
            'error_details': raw.get('error_details'), 'limitations': raw.get('limitations', []),
            'free_only_assurance': raw['free_only_assurance'],
            'audio_input_seconds': raw['audio_input_seconds'],
            'uploaded_audio_seconds': raw['uploaded_audio_seconds'], 'audio_played': False}


def append(output, item):
    report = json.loads(output.read_text(encoding='utf-8')) if output.exists() else {
        'schema_version': 1, 'provider': 'AlibabaRealtime', 'samples': [],
        'scope': 'synthetic_prerecorded_media_no_hardware_no_playback',
        'pricing_assurance': 'declared_free_quota_and_stop_when_exhausted_not_a_billing_receipt'}
    if report.get('provider') != 'AlibabaRealtime' or not isinstance(report.get('samples'), list):
        raise ValueError('unexpected_existing_report')
    report['samples'].append(item)
    report['real_api_attempts'] = len(report['samples'])
    report['successful_sessions'] = sum(entry.get('ok') is True for entry in report['samples'])
    report['updated_at'] = datetime.now(timezone.utc).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(output)


async def main(args):
    if not args.real:
        raise ValueError('explicit_real_flag_required_no_automatic_cloud_call')
    config = AlibabaConfig.from_credentials(args.credentials)
    image_names = ['RedTent', 'BlueTent'] if args.task == 'vision' and args.image == 'both' else (
        ['RedTent' if args.image == 'red' else 'BlueTent'] if args.task == 'vision' else [None])
    for _ in range(args.trials):
        for image in image_names:
            item = await sample(config, args.task, mode=args.mode, image_name=image,
                                phase=args.phase, timeout=args.timeout)
            append(args.output, item)
            print(json.dumps({key: item[key] for key in ('provider', 'task', 'turn_mode', 'phase', 'ok',
                'media_verified', 'latency', 'replies', 'error_code', 'cleanup')}, ensure_ascii=False), flush=True)
            if not item['transport_ok']:
                return 1  # Do not silently spend more quota after a protocol failure.
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real', action='store_true')
    parser.add_argument('--credentials', type=Path,
                        default=Path(r'C:\Users\bente\Desktop\tmp\DomesticAiTrials\AlibabaCredentials.json'))
    parser.add_argument('--task', choices=['speech', 'status', 'vision'], required=True)
    parser.add_argument('--mode', choices=['automatic', 'manual'], default='automatic')
    parser.add_argument('--phase', choices=['development', 'formal', 'diagnostic'], default='development')
    parser.add_argument('--image', choices=['red', 'blue', 'both'], default='both')
    parser.add_argument('--trials', type=int, choices=[1, 2, 3], default=1)
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--output', type=Path, default=FOLDER / 'AlibabaMedia.json')
    arguments = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(main(arguments)))
    except (ValueError, OSError) as error:
        print(json.dumps({'ok': False, 'error_type': type(error).__name__}), flush=True)
        raise SystemExit(2)
