"""Offline route evidence, including archived providers; no live driver required."""
import argparse
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT/'ScenarioAcceptance'/'Evidence'/'RouteSelection20261027'
FOLDER = ROOT/'ScenarioAcceptance'/'Evidence'/'Current'/'RouteSelection'


def input_complete(sample):
    task = sample['task']
    required = {'vision': ('帐篷', '什么颜色', '只回答'),
                'status': ('查询', '电量', '不要', '飞机'),
                'speech': ('只回答', '测试收到')}[task]
    if sample['provider'] == 'AlibabaRealtime':
        texts = [i['text'] for i in sample.get('transcription_items', [])]
        # One user item must contain the complete utterance, not a concatenation
        # of distinct, prematurely committed rounds.
        return len(texts) == 1 and all(x in texts[0] for x in required)
    if sample['provider'] == 'TencentTRTC':
        return sample.get('delivery_verified') is True and sample.get('asr_rounds') == 1
    # Doubao emits cumulative/fragment subtitles. Require both clauses in one
    # completed user paragraph, with assistant data carrying the same round.
    user_ids = {e.get('payload', {}).get('UserID') for e in sample.get('events', [])
                if e.get('type') == 'conv'} - {None}
    by_round, final, bots = {}, set(), set()
    for e in sample.get('events', []):
        if e.get('type') != 'subv':
            continue
        for item in e.get('payload', {}).get('data', []):
            rid = item.get('roundId')
            if item.get('definite') is not True or type(rid) is not int:
                continue
            if item.get('userId') in user_ids:
                by_round.setdefault(rid, []).append(item.get('text', ''))
                if item.get('paragraph') is True:
                    final.add(rid)
            else:
                bots.add(rid)
    return len(by_round) == 1 and any(r in final & bots and all(x in ''.join(t) for x in required)
                                    for r, t in by_round.items())


def video_tokens(sample):
    return sum(u.get('input_tokens_details', {}).get('video_tokens', 0) for u in sample.get('usage', []))


def decompose(sample):
    latency = sample.get('latency', {})
    first_audio = latency.get('first_audio_ms')
    result_sent = latency.get('tool_result_sent_ms')
    call = latency.get('tool_call_ms')
    row = dict(first_audio_ms=first_audio, first_text_ms=latency.get('first_text_ms'),
        tool_request_ms=call,
        local_tool_and_return_ms=result_sent-call if result_sent is not None and call is not None else None,
        post_tool_first_audio_ms=first_audio-result_sent if first_audio is not None and result_sent is not None else None)
    anchor = sample.get('timing', {}).get('speech_end_anchor_ms', sample.get('speech_endpoint', {}).get('native_ms'))
    events = sample.get('events', [])
    endpoints = []
    metrics = []
    for e in events:
        if e.get('event') == 'server_event' and e.get('type') == 'input_audio_buffer.speech_stopped':
            endpoints.append(e['t_ms'])
        if e.get('type') == 'conv' and e.get('payload', {}).get('Stage', {}).get('Code') == 2:
            endpoints.append(e['monotonic_ms'])
        p = e.get('payload', {})
        if isinstance(p, dict) and p.get('type') == 10020:
            metrics.append(p.get('payload', {}))
    if anchor is not None and endpoints:
        wait = max(endpoints)-anchor
        row['voiced_end_to_endpoint_notice_ms'] = wait
        row['endpoint_notice_to_first_audio_ms'] = first_audio-wait if first_audio is not None else None
    if sample.get('provider') == 'TencentTRTC' or metrics:
        row['tencent_reported_metrics_not_additive'] = metrics
    return row


def analyze(folder=FOLDER):
    folder = Path(folder)
    records = []
    for stage in ('Screen', 'Endpoint', 'Confirm', 'Speech', 'Vad', 'Doubao', 'Postfix'):
        path = folder/(stage+'.json')
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        for a in data['attempts']:
            s = a.get('sample')
            if not s:
                records.append(dict(stage=stage, id=a['spec']['id'], qualified=False, unfinished=True))
                continue
            complete = input_complete(s)
            delivered = s.get('delivery_verified') is True if s['task'] == 'speech' else s.get('ok') is True
            vision_proof = s['task'] != 'vision' or s['provider'] != 'AlibabaRealtime' or video_tokens(s) > 0
            records.append(dict(stage=stage, id=a['spec']['id'], provider=s['provider'], task=s['task'],
                image=a['spec'].get('image'), input_complete=complete, answer_ok=s.get('ok'),
                qualified=bool(complete and delivered and vision_proof and a.get('cleanup_confirmed')),
                cleanup_confirmed=a.get('cleanup_confirmed'), alibaba_video_tokens=video_tokens(s),
                transcription_items=s.get('transcription_items'),
                transcripts=s.get('transcripts'), replies=s.get('replies'),
                error_code=s.get('error_code', s.get('failure')), components=decompose(s)))
    groups = []
    for stage in ('Confirm', 'Speech', 'Doubao', 'Postfix'):
        for provider in sorted({r['provider'] for r in records if 'provider' in r}):
            for task in ('speech', 'vision', 'status'):
                rows = [r for r in records if r.get('stage') == stage and r.get('provider') == provider and r.get('task') == task]
                if not rows:
                    continue
                values = [r['components']['first_audio_ms'] for r in rows if r['qualified']
                          and r['components']['first_audio_ms'] is not None]
                groups.append(dict(stage=stage, provider=provider, task=task, attempts=len(rows),
                    qualified=sum(r['qualified'] for r in rows),
                    content_check_passed=sum(r.get('answer_ok') is True for r in rows),
                    latency_scope='media_delivery_not_exact_wording' if task == 'speech' else 'complete_input_correct_content',
                    host_load_isolated=stage != 'Confirm',
                    first_audio_median_ms=statistics.median(values) if values else None,
                    first_audio_range_ms=[min(values), max(values)] if values else None))
    return dict(schema_version=1, rows=records, groups=groups,
        scope='same_original_fixtures_corrected_endpoint_and_frame_stream_no_devices',
        limits=['screening_sample_size_not_reliability_proof', 'fresh_session_per_fixture',
            'confirm_discovery_trials_overlapped_local_pytest_do_not_use_as_isolated_speed_ranking',
            'latency_includes_provider_endpointing_transport_decode_and_local_tools',
            'not_an_isolated_model_compute_benchmark', 'no_billing_receipt',
            'not_continuous_barge_in_or_flight_acceptance'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=FOLDER)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = analyze(args.folder)
    path = args.output or args.folder/'Analysis.json'
    if path.resolve().is_relative_to(ARCHIVE.resolve()):
        raise ValueError('archived_evidence_is_read_only_specify_output_outside_archive')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result['groups'], ensure_ascii=False, indent=2))
