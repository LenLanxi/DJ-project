"""Opt-in causal route probes. New evidence only; historical receipts stay frozen.

No device capture or aircraft. All outcomes retained, one cloud session at a
time. This is a screening experiment, not a reliability or flight certificate.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'modules' / 'ai'))
sys.path.insert(0, str(ROOT / 'tests'))
OUT = ROOT / 'ScenarioAcceptance' / 'Evidence' / 'RouteSelection20261027'
OLD = ROOT / 'ScenarioAcceptance' / 'Evidence' / 'RealtimeComparison'


def plan(stage):
    if stage == 'screen':
        return [dict(id=f'AliVision{vad}{schedule}', provider='Alibaba', task='vision',
                     image='RedTent', vad=vad, interval=schedule)
                for vad in ('semantic_vad', 'server_vad') for schedule in (None, 1)] + [
            dict(id='AliStatusSemantic', provider='Alibaba', task='status', vad='semantic_vad'),
            dict(id='AliStatusAcoustic', provider='Alibaba', task='status', vad='server_vad')]
    if stage == 'confirm':
        return [dict(id=f'{provider}{task}{image}{repeat}', provider=provider, task=task,
                     image=image, vad='server_vad', interval=1, silence=1200)
                for repeat in range(1, 4) for task, image in
                    (('vision', 'RedTent'), ('vision', 'BlueTent'), ('status', None))
                for provider in ('Alibaba', 'Doubao')]
    if stage == 'speech':
        return [dict(id=f'{provider}Speech{repeat}', provider=provider, task='speech', vad='server_vad', silence=1200 if provider == 'Alibaba' else 1000)
                for repeat in range(1, 4) for provider in ('Alibaba', 'Tencent', 'Doubao')]
    if stage == 'vad':
        return [dict(id=f'TencentVad{vad}', provider='Tencent', task='speech', silence=vad)
                for vad in (500, 800)]
    if stage == 'endpoint':
        return [dict(id=f'AliEndpoint{task}{interval}', provider='Alibaba', task=task,
                     image='RedTent' if task == 'vision' else None, vad='server_vad',
                     interval=interval, silence=1200)
                for task, interval in (('vision', None), ('vision', 1), ('status', None))]
    if stage == 'doubao':
        return [dict(id=f'DoubaoEndpoint{task}{image}{repeat}', provider='Doubao', task=task,
                     image=image, endpoint_ms=1200)
                for repeat in (1, 2) for task, image in
                    (('vision', 'RedTent'), ('vision', 'BlueTent'), ('status', None))]
    if stage == 'postfix':
        return [dict(id=f'AlibabaPostfixStatus{repeat}', provider='Alibaba', task='status',
                     vad='server_vad', silence=1200) for repeat in (1, 2)]
    raise ValueError('unknown_stage')


def cleanup_ok(sample):
    c = sample.get('cleanup', {})
    if sample['provider'] == 'AlibabaRealtime':
        return all(c.get(k) is True for k in ('connection_closed', 'sender_finished', 'receiver_finished')) \
            and c.get('query_runtime_stopped', True) is True
    if sample['provider'] == 'DoubaoRTC':
        return c.get('cloud_stop', {}).get('ok') is True and c.get('native_exit_code') == 0 \
            and c.get('node_exit_code') == 0 and c.get('short_token_file_deleted') is True \
            and c.get('query_runtime_stopped', True) is True
    return c.get('ok') is True and c.get('cloud_stop_confirmed') is True \
        and sample.get('native_returncode') == 0 and sample.get('room_exit_confirmed') is True \
        and sample.get('short_token_config_removed') is True


def frozen_files():
    paths = list(OLD.glob('*')) + [ROOT / 'ProjectOverview.html',
        Path(r'C:\Users\bente\Desktop\DroneArchitectureBefore.html')]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}


def save(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    tmp.replace(path)


async def run(spec):
    if spec['provider'] == 'Alibaba':
        from AlibabaRealtime import AlibabaConfig
        from AlibabaBenchmark import sample
        config = AlibabaConfig.from_credentials(r'C:\Users\bente\Desktop\tmp\DomesticAiTrials\AlibabaCredentials.json')
        return await sample(config, spec['task'], mode='automatic', image_name=spec.get('image'),
            phase='causal_probe', vad_type=spec.get('vad', 'server_vad'),
            silence_ms=spec.get('silence', 800), image_interval_seconds=spec.get('interval'))
    if spec['provider'] == 'Doubao':
        from DoubaoBenchmark import run_trial, redact
        creds = Path(r'C:\Users\bente\Desktop\tmp\DoubaoRealtime\Credentials.json')
        private = json.loads(creds.read_text(encoding='utf-8-sig'))
        secrets = []
        def collect(v):
            if isinstance(v, dict):
                for k, item in v.items():
                    if any(s in k.lower() for s in ('key', 'token', 'secret')) and isinstance(item, str):
                        secrets.append(item)
                    else: collect(item)
        collect(private)
        meta = json.loads((OLD/'Fixtures.json').read_text(encoding='utf-8-sig'))
        item = await run_trial(spec['task'], spec.get('image') or 'RedTent', creds, meta, 1, secrets,
                               endpoint_ms=spec.get('endpoint_ms'))
        return json.loads(redact(item, secrets))
    from TencentBenchmark import sample
    from TencentConversation import TencentConfig, LLMConfig
    config = TencentConfig.from_file(r'C:\Users\bente\Desktop\tmp\DomesticAiTrials\TencentCredentials.json')
    llm = LLMConfig.from_ark_file(r'C:\Users\bente\Desktop\tmp\DoubaoRealtime\Credentials.json')
    return await asyncio.to_thread(sample, config, llm, vad_silence_ms=spec.get('silence', 1000))


async def main(args):
    if not args.real:
        raise ValueError('explicit_real_required')
    path = OUT/(args.stage.title()+'.json')
    report = json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(
        schema_version=1, stage=args.stage, created_at=datetime.now(timezone.utc).isoformat(),
        design=plan(args.stage), historical_hashes=frozen_files(), attempts=[],
        physical_devices=False, billing_verified=False, planned_session_upper_bound=46)
    if any(a.get('state') != 'finished' or not a.get('cleanup_confirmed') for a in report['attempts']):
        raise ValueError('previous_attempt_needs_review_no_automatic_retry')
    for spec in report['design']:
        if any(a['spec']['id'] == spec['id'] for a in report['attempts']):
            continue
        attempt = dict(spec=spec, state='started', started_at=datetime.now(timezone.utc).isoformat())
        attempt['source_hashes'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [ROOT/'tests'/'RouteSelection.py', ROOT/'modules'/'ai'/'AlibabaRealtime.py',
                      ROOT/'tests'/'AlibabaBenchmark.py', ROOT/'tests'/'TencentBenchmark.py',
                      ROOT/'tests'/'DoubaoBenchmark.py']}
        report['attempts'].append(attempt)
        save(path, report)  # Preserve invocation even if process or SDK crashes.
        print(json.dumps(dict(starting=spec)), flush=True)
        try:
            result = await run(spec)
        except Exception as e:
            attempt.update(state='exception', error=type(e).__name__, cleanup_confirmed=False)
            save(path, report)
            raise
        attempt.update(state='finished', sample=result, cleanup_confirmed=cleanup_ok(result),
            finished_at=datetime.now(timezone.utc).isoformat())
        if frozen_files() != report['historical_hashes']:
            raise ValueError('historical_evidence_changed')
        save(path, report)
        print(json.dumps(dict(id=spec['id'], ok=result.get('ok'), cleanup=attempt['cleanup_confirmed'],
            latency=result.get('latency'), asr=result.get('transcripts'), reply=result.get('replies'),
            error=result.get('error_code', result.get('failure'))), ensure_ascii=False), flush=True)
        if not attempt['cleanup_confirmed']:
            raise ValueError('cleanup_unconfirmed_no_more_cloud_calls')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real', action='store_true')
    parser.add_argument('--stage', choices=['screen', 'confirm', 'speech', 'vad', 'endpoint', 'doubao', 'postfix'], required=True)
    arguments = parser.parse_args()
    asyncio.run(main(arguments))
