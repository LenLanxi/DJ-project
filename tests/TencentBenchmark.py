"""Explicit real-cloud benchmark of Tencent TRTC's public voice cascade.

Uses only prerecorded PCM and the official Windows native SDK. The regular test
suite never imports this as a test or starts paid tasks. No devices or UI open.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'modules'/'ai'))
from TencentConversation import LLMConfig, TencentApiError, TencentConfig, TencentConversation

EVIDENCE = ROOT/'ScenarioAcceptance'/'Evidence'/'RealtimeComparison'
BUILD = ROOT/'modules'/'rtc'/'Build'/'TencentNative'
BENCHMARK_PROMPT = '你正在进行实时语音交互的软件测试。只根据本轮用户实际说的话，用中文简短回答。不主动发起问候，不执行设备动作，不输出推理过程。'


def subtitle(event, user_id, bot_id):
    """Official current type=10000 and documented older subtitle messages."""
    if event.get('event') != 'custom_message':
        return None
    value = event.get('payload')
    if not isinstance(value, dict):
        return None
    if value.get('type') == 10000:
        payload = value.get('payload', {})
        if not isinstance(payload, dict):
            return None
        sender, text = value.get('sender'), payload.get('text')
        if sender not in (user_id, bot_id) or not isinstance(text, str) or not text:
            return None
        return {'role': 'assistant' if sender == bot_id else 'user', 'text': text,
                'final': payload.get('end') is True, 'round_id': payload.get('roundid')}
    if value.get('type') in ('subtitle', 'transcription'):
        sender, text = value.get('userid'), value.get('text')
        if sender not in (user_id, bot_id) or not isinstance(text, str) or not text:
            return None
        return {'role': 'assistant' if sender == bot_id else 'user', 'text': text,
                'final': False, 'round_id': value.get('roundid')}
    return None


def state_event(event):
    value = event.get('payload', {}) if event.get('event') == 'custom_message' else {}
    if not isinstance(value, dict) or value.get('type') != 10001:
        return None, None
    payload = value.get('payload', {})
    if not isinstance(payload, dict):
        return None, None
    state = payload.get('state', payload.get('State'))
    code = state.get('code', state.get('Code')) if isinstance(state, dict) else state
    return code, payload.get('roundid')


def completion_matches(event, target_round, input_completed):
    code, round_id = state_event(event)
    return bool(input_completed and target_round and code == 5 and round_id == target_round)


class RoundAudio:
    """First two continuous nonzero PCM windows associated with a complete ASR round."""
    def __init__(self):
        self.count = 0
        self.candidate = None
        self.result = None

    def reset(self):
        if self.result is None:
            self.count, self.candidate = 0, None

    def feed(self, event, target_round, speaking_round, endpoint):
        if self.result is not None:
            return self.result
        stamp = event.get('monotonic_ms')
        valid = bool(event.get('event') == 'output_audio_frame' and target_round
            and target_round == speaking_round and endpoint is not None
            and isinstance(stamp, (int, float)) and stamp >= endpoint
            and event.get('rms', 0) >= 64)
        if not valid:
            self.reset()
            return None
        if self.count == 0:
            self.candidate = stamp
        self.count += 1
        if self.count >= 2:
            self.result = self.candidate-endpoint
        return self.result


def response_finished(event):
    if event.get('event') != 'custom_message':
        return False
    value = event.get('payload', {})
    payload = value.get('payload', {}) if isinstance(value, dict) else {}
    state = payload.get('state', payload.get('State', {})) if isinstance(payload, dict) else {}
    if isinstance(state, dict):
        return state.get('code', state.get('Code')) == 5
    return state == 5  # Actual public SDK type=10001 payload.state integer.


def combine_text(items):
    # Mode 1 may return sentence increments; other versions repeat a prefix.
    text = ''
    for item in items:
        value = item['text']
        if value.startswith(text): text = value
        elif not text.endswith(value): text += value
    return text


def recompute_latency(result):
    """Reconstruct the shared actual-send anchor from immutable native events."""
    events = result.get('native_events', result.get('events', []))
    fixture = result.get('full_input_fixture', {})
    end_samples, rate = fixture.get('speech_end_samples'), fixture.get('sample_rate')
    if not isinstance(end_samples, int) or rate not in (16000, 48000):
        return False
    endpoint = None
    for event in events:
        if event.get('event') == 'audio_input_started':
            result.setdefault('nominal_voiced_endpoint_ms', event['monotonic_ms']+end_samples*1000/rate)
        if event.get('event') == 'audio_chunk' and event.get('last_sample', 0) >= end_samples:
            endpoint = event['monotonic_ms']-(event['last_sample']-end_samples)*1000/rate
            result['endpoint_chunk_last_sample'] = event['last_sample']
            break
    if endpoint is None:
        result['endpoint_confirmed'] = False
        return False
    result['voiced_endpoint_ms'] = endpoint
    result['endpoint_confirmed'] = True
    result['endpoint_source'] = 'actual_covering_audio_chunk_enqueue_interpolated_to_last_voiced_sample'
    target = result.get('response_round_id')
    bot_id = next((e.get('payload', {}).get('sender') for e in events
        if state_event(e) == (3, target)), None) if target else None
    tracker = RoundAudio()
    speaking = None
    first_text = None
    completed = None
    ended = False
    for event in events:
        if event.get('event') == 'pcm_eof': ended = True
        code, round_id = state_event(event)
        if code == 3: speaking = round_id
        elif code == 4: speaking = None; tracker.reset()
        value = event.get('payload', {})
        payload = value.get('payload', {}) if isinstance(value, dict) else {}
        if first_text is None and target and bot_id and event.get('event') == 'custom_message' \
                and value.get('type') == 10000 and value.get('sender') == bot_id \
                and isinstance(payload, dict) and payload.get('roundid') == target and payload.get('text'):
            first_text = event['monotonic_ms']-endpoint
        if event.get('event') == 'output_audio_frame':
            tracker.feed(event, target, speaking, endpoint)
        if completed is None and completion_matches(event, target, ended):
            completed = event['monotonic_ms']-endpoint
    result['latency'] = {'first_text_ms': first_text, 'first_audio_ms': tracker.result, 'completed_ms': completed}
    if tracker.result is not None:
        result['audio_latency_source'] = 'native_20ms_pcm_same_complete_asr_round_two_nonzero_frames'
    if completed is not None: result['completion_scope'] = 'provider_state_finished'
    return True


def finalize_delivery(result):
    """Delivery timing is independent of exact-word instruction adherence.

    Only saved native events can establish provider completion. In particular,
    an earlier round's state 5 and a state before pcm_eof cannot finish this turn.
    """
    events = result.get('native_events', result.get('events', []))
    target_round = result.get('response_round_id')
    endpoint = result.get('voiced_endpoint_ms')
    input_completed = False
    completion = None
    for event in events:
        if event.get('event') == 'pcm_eof':
            input_completed = True
        if completion_matches(event, target_round, input_completed):
            completion = event.get('monotonic_ms')
            break
    if isinstance(endpoint, (int, float)) and isinstance(completion, (int, float)) and completion >= endpoint:
        result['latency']['completed_ms'] = completion-endpoint
        result['completion_scope'] = 'provider_state_finished'
    result['protocol_errors'] = [e.get('event') for e in events if e.get('event') in
                                 ('sdk_error', 'probe_error', 'malformed_message')]
    first_audio = result.get('latency', {}).get('first_audio_ms')
    round_audio = result.get('audio_latency_source') == 'native_20ms_pcm_same_complete_asr_round_two_nonzero_frames'
    delivered = bool(result.get('asr_verified') and target_round and round_audio
        and isinstance(first_audio, (int, float)) and first_audio >= 0
        and result.get('input_completed') and input_completed
        and result.get('cleanup', {}).get('ok') and result.get('cleanup', {}).get('cloud_stop_confirmed')
        and result.get('room_exit_confirmed') and result.get('native_returncode') == 0
        and not result['protocol_errors'])
    result['delivery_verified'] = delivered
    result['delivery_verification'] = 'complete_asr_round_and_same_round_pcm_with_confirmed_cloud_and_room_cleanup'
    # Preserve strict wording compliance separately from actual media delivery.
    result['media_verified'] = bool(delivered and result.get('answer_verified'))
    result['ok'] = result['media_verified']
    return result


def read_report(path, development):
    path = Path(path)
    if not path.exists():
        return {'schema_version': 1, 'provider': 'TencentTRTC', 'task': 'speech', 'samples': [],
                'ranking_eligible': not development, 'media_path': 'official_windows_native_rtc',
                'vision_tool_comparison_supported': False, 'billing_verified': False}
    result = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(result, dict) or result.get('provider') != 'TencentTRTC' \
            or result.get('task') != 'speech' or not isinstance(result.get('samples'), list) \
            or result.get('ranking_eligible') is not (not development):
        raise ValueError('Existing Tencent report is incompatible; it will not be overwritten')
    return result


def append_sample(path, value, development):
    path = Path(path)
    result = read_report(path, development)
    value['trial'] = len(result['samples'])+1
    result['samples'].append(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.stem+'Pending'+uuid.uuid4().hex[:8]+path.suffix)
    try:
        pending.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)
    return result


def sample(config, llm, *, development=False):
    fixture = json.loads((EVIDENCE/'Fixtures.json').read_text(encoding='utf-8'))['audio']['Speech']
    route = TencentConversation(config, llm, seconds=60)
    run_id = 'Run' + uuid.uuid4().hex[:12]
    config_path, output_pcm = BUILD/(run_id+'.json'), BUILD/(run_id+'.pcm')
    executable = BUILD/'TencentProbe.exe'
    if not executable.is_file():
        raise RuntimeError('Tencent native client missing; follow TencentSetup.json before paid testing')
    params = route.client_parameters()
    params.update({'pcmPath': str(EVIDENCE/'Speech.pcm'), 'imagePath': str(EVIDENCE/'RedTent.i420'),
                   'outputPcmPath': str(output_pcm), 'sampleRate': fixture['sample_rate'],
                   'speechEndSamples': fixture['speech_end_samples'], 'width': 640, 'height': 360,
                   'fps': 2, 'maxSeconds': 45, 'rmsThreshold': 64.0})
    # Generated short-lived UserSig only. Long-term credentials never leave this process.
    config_path.write_text(json.dumps(params), encoding='utf-8')
    result = {'provider': 'TencentTRTC', 'task': 'speech', 'turn_mode': 'automatic',
              'phase': 'development' if development else 'measurement',
              'model': llm.model, 'thinking': {'requested': 'disabled', 'confirmed': False,
                'method': 'LLMConfig.ExtraBody', 'note': 'No LLM request inspection; provider forwarding is unverified'},
              'client': 'official_windows_native', 'media_verified': False, 'delivery_verified': False, 'ok': False,
              'physical_devices': False, 'full_input_fixture': fixture,
              'video_understanding_verified': False, 'client_tools_verified': False,
              'latency': {'first_text_ms': None, 'first_audio_ms': None, 'completed_ms': None},
              'events': [], 'transcripts': [], 'replies': [], 'control_events': route.events,
              'output_pcm_path': str(output_pcm), 'billing_verified': False}
    process = None
    incoming = queue.Queue()
    reader = None
    native_events = result['events']
    host_started = time.monotonic()
    endpoint = None
    pcm_ended = False
    target_round = None
    speaking_round = None
    audio_tracker = RoundAudio()
    last_loud = None
    response_done = None
    junk_lines = 0

    def write(command):
        process.stdin.write((json.dumps(command)+'\n').encode())
        process.stdin.flush()

    def read():
        for raw in process.stdout:
            try:
                value = json.loads(raw.decode('utf-8'))
                if not isinstance(value, dict): raise ValueError()
            except (ValueError, UnicodeError):
                # SDK text could contain tokens. Never retain unstructured stdout.
                incoming.put(None)
                continue
            incoming.put(value)
        incoming.put({'event': 'process_eof'})

    def receive(timeout=.1):
        nonlocal endpoint, pcm_ended, last_loud, response_done, junk_lines
        nonlocal target_round, speaking_round
        try: event = incoming.get(timeout=timeout)
        except queue.Empty: return None
        if event is None:
            junk_lines += 1
            return None
        native_events.append(event)
        if len(native_events) > 8000: raise RuntimeError('native event budget exceeded')
        name = event.get('event')
        stamp = event.get('monotonic_ms')
        if name == 'audio_input_started':
            result['nominal_voiced_endpoint_ms'] = stamp + fixture['speech_end_seconds']*1000
        if endpoint is None and name == 'audio_chunk' and event.get('last_sample', 0) >= fixture['speech_end_samples']:
            endpoint = stamp-(event['last_sample']-fixture['speech_end_samples'])*1000/fixture['sample_rate']
            result['voiced_endpoint_ms'] = endpoint
            result['endpoint_confirmed'] = True
        if name == 'pcm_eof': pcm_ended = True
        if name == 'output_audio_frame' and event.get('rms', 0) >= 64:
            last_loud = stamp
        item = subtitle(event, route.user_id, route.bot_id)
        if item:
            item['monotonic_ms'] = stamp
            result['replies' if item['role'] == 'assistant' else 'transcripts'].append(item)
            if item['role'] == 'user' and item['final'] and item.get('round_id') \
                    and recompact(item['text']) == recompact(fixture['text']):
                target_round = item['round_id']
                result['response_round_id'] = target_round
            if item['role'] == 'assistant' and item.get('round_id') == target_round and endpoint is not None and result['latency']['first_text_ms'] is None:
                result['latency']['first_text_ms'] = stamp-endpoint
        code, round_id = state_event(event)
        if code == 3:
            speaking_round = round_id
        elif code == 4:
            speaking_round = None
            audio_tracker.reset()
        if name == 'output_audio_frame' and result['latency']['first_audio_ms'] is None:
            result['latency']['first_audio_ms'] = audio_tracker.feed(event, target_round, speaking_round, endpoint)
            if result['latency']['first_audio_ms'] is not None:
                result['audio_latency_source'] = 'native_20ms_pcm_same_complete_asr_round_two_nonzero_frames'
        if completion_matches(event, target_round, pcm_ended): response_done = stamp
        if name == 'sdk_error': raise RuntimeError('Tencent native SDK error '+str(event.get('code')))
        if name == 'probe_error': raise RuntimeError('Tencent native probe failed')
        return event

    try:
        process = subprocess.Popen([str(executable), str(config_path)], cwd=BUILD,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        join_deadline = time.monotonic()+10
        joined = False
        while time.monotonic()<join_deadline:
            event = receive()
            if event and event.get('event') == 'room_joined': joined=True;break
            if process.poll() is not None: break
        if not joined: raise RuntimeError('Tencent native room entry timed out')
        result['start'] = route.start(BENCHMARK_PROMPT)
        result['system_prompt'] = BENCHMARK_PROMPT
        bot_deadline = time.monotonic()+12
        ready = False
        while time.monotonic()<bot_deadline:
            event = receive()
            if event and event.get('event') == 'bot_ready': ready=True;break
            if process.poll() is not None: break
        if not ready:
            result['cloud_status'] = route.describe()
            raise RuntimeError('Tencent bot did not enter the native room')
        write({'command': 'play'})
        response_deadline = time.monotonic()+25
        while time.monotonic()<response_deadline:
            event = receive()
            if not event: continue
            stamp = event.get('monotonic_ms')
            answer = combine_text([t for t in result['replies'] if t.get('round_id') == target_round])
            audio = result['latency']['first_audio_ms'] is not None
            if answer.strip() and audio and pcm_ended:
                if response_done is not None:
                    result['latency']['completed_ms'] = response_done-endpoint
                    result['completion_scope'] = 'provider_state_finished'
                    break
                if last_loud is not None and isinstance(stamp, (int,float)) and stamp-last_loud>=1000:
                    result['latency']['completed_ms'] = stamp-endpoint
                    result['completion_scope'] = 'inferred_audio_tail_1000ms'
                    break
            if process.poll() is not None or event.get('event') == 'process_eof': break
        result['reply_text'] = combine_text([t for t in result['replies'] if t.get('round_id') == target_round])
        result['answer_verified'] = recompact(result['reply_text']) == '测试收到'
        result['answer_verification'] = 'exact_normalized_official_bot_subtitle'
        result['asr_text'] = combine_text(result['transcripts'])
        result['asr_verified'] = bool(target_round and '测试收到' in recompact(result['asr_text']))
        result['vad_silence_ms'] = route.vad_silence_ms
        result['vad_note'] = '1000ms endpoint wait avoids observed 430ms internal pause splitting; latency includes this wait'
        result['asr_rounds'] = len({t['round_id'] for t in result['transcripts'] if t.get('round_id')})
        result['automatic_interruption_events'] = sum(1 for e in native_events
            if e.get('event') == 'custom_message' and isinstance(e.get('payload'), dict)
            and e['payload'].get('type') == 10020 and e['payload'].get('payload', {}).get('metric') == 'interruption')
        result['input_completed'] = pcm_ended
        result['media_verified'] = bool(result['answer_verified'] and result['asr_verified'] and pcm_ended and result['latency']['first_audio_ms'] is not None)
    except TencentApiError as exc:
        result['error_code'] = exc.code
        result['request_id'] = exc.request_id
    except Exception as exc:
        # Local errors are fixed messages, not raw JSON/provider error text.
        result['error_type'] = type(exc).__name__
        result['error'] = str(exc)[:200] if not isinstance(exc, json.JSONDecodeError) else 'invalid local JSON'
    finally:
        result['cleanup'] = route.stop()
        if process:
            try:
                if process.poll() is None: write({'command': 'stop'})
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill();process.wait(timeout=3)
                result['native_forced_cleanup'] = True
            if reader: reader.join(timeout=2)
            while not incoming.empty():
                try: receive(timeout=0)
                except Exception: break
            result['native_returncode'] = process.returncode
            result['room_exit_confirmed'] = any(e.get('event')=='room_exit_confirmation' and e.get('confirmed') for e in native_events)
        config_path.unlink(missing_ok=True)
        result['short_token_config_removed'] = not config_path.exists()
        result['unstructured_sdk_lines_discarded'] = junk_lines
        result['host_duration_ms'] = round((time.monotonic()-host_started)*1000, 3)
        recompute_latency(result)
        finalize_delivery(result)
    return result


def recompact(text):
    return ''.join(ch for ch in text if ch.isalnum())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real', action='store_true')
    parser.add_argument('--task', choices=['speech'], default='speech')
    parser.add_argument('--trials', type=int, default=1)
    parser.add_argument('--development', action='store_true')
    parser.add_argument('--credentials', default=r'C:\Users\bente\Desktop\tmp\DomesticAiTrials\TencentCredentials.json')
    parser.add_argument('--ark-credentials', default=r'C:\Users\bente\Desktop\tmp\DoubaoRealtime\Credentials.json')
    parser.add_argument('--output', default=str(EVIDENCE/'TencentMedia.json'))
    args = parser.parse_args(argv)
    if not args.real: parser.error('--real is required for a cloud and native media session')
    if not 1<=args.trials<=3: parser.error('--trials must be 1..3')
    config, llm = TencentConfig.from_file(args.credentials), LLMConfig.from_ark_file(args.ark_credentials)
    output = Path(args.output)
    read_report(output, args.development)  # Refuse incompatible evidence before a paid call.
    new_samples = []
    for trial in range(args.trials):
        value = sample(config, llm, development=args.development)
        append_sample(output, value, args.development)
        new_samples.append(value)
        print(json.dumps({'provider': 'TencentTRTC', 'trial': value['trial'], 'ok': value['ok'],
            'media_verified': value['media_verified'], 'delivery_verified': value['delivery_verified'], 'latency': value['latency'],
            'error_code': value.get('error_code'), 'error': value.get('error'),
            'cleanup': value['cleanup']['status']}, ensure_ascii=False), flush=True)
        if not value['cleanup'].get('ok') or value.get('protocol_errors'): break
    return 0 if len(new_samples)==args.trials and all(s['ok'] for s in new_samples) else 2


if __name__ == '__main__':
    raise SystemExit(main())
