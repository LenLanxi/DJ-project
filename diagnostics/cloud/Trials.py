"""One serial cloud-attempt workflow; plans and raw provider evidence remain explicit."""
from datetime import datetime, timezone
import json
import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
OLD = ROOT/'ScenarioAcceptance'/'Evidence'/'RealtimeComparison'


def credential_arguments(parser):
    parser.add_argument('--alibaba-credentials', type=Path, default=os.getenv('ALIBABA_CREDENTIALS_FILE'))
    parser.add_argument('--doubao-credentials', type=Path, default=os.getenv('DOUBAO_CREDENTIALS_FILE'))

def cleanup_ok(sample):
    c = sample.get('cleanup', {})
    if sample['provider'] == 'AlibabaRealtime':
        return all(c.get(k) is True for k in ('connection_closed', 'sender_finished', 'receiver_finished')) \
            and c.get('query_runtime_stopped', True) is True
    if sample['provider'] == 'DoubaoRTC':
        return c.get('cloud_stop', {}).get('ok') is True and c.get('native_exit_code') == 0 \
            and c.get('node_exit_code') == 0 and c.get('short_token_file_deleted') is True \
            and c.get('query_runtime_stopped', True) is True
    return False


def save(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    tmp.replace(path)


async def run_trial(spec, credentials):
    path=credentials[spec['provider'].lower()]
    if path is None or not Path(path).is_file():
        raise ValueError('missing_credentials_file_'+spec['provider'])
    if spec['provider'] == 'Alibaba':
        from providers.AlibabaRealtime import AlibabaConfig
        from AlibabaBenchmark import sample
        config = AlibabaConfig.from_credentials(credentials['alibaba'])
        return await sample(config, spec['task'], mode='automatic', image_name=spec.get('image'),
            phase=spec.get('phase','causal_probe'), vad_type=spec.get('vad', 'server_vad'),
            silence_ms=spec.get('silence', 800), image_interval_seconds=spec.get('interval'))
    from DoubaoBenchmark import run_trial, redact
    creds = credentials['doubao']
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



async def execute(report, path, run):
    if any(a.get('state') != 'finished' or not a.get('cleanup_confirmed') for a in report['attempts']):
        raise ValueError('previous_attempt_needs_review_no_automatic_retry')
    for spec in report['design']:
        if any(a['spec']['id'] == spec['id'] for a in report['attempts']): continue
        attempt = dict(spec=spec, state='started', started_at=datetime.now(timezone.utc).isoformat())
        report['attempts'].append(attempt)
        save(path, report)
        try:
            result = await run(spec)
        except Exception as error:
            attempt.update(state='exception', error=type(error).__name__, cleanup_confirmed=False)
            save(path, report)
            raise
        attempt.update(state='finished', sample=result, cleanup_confirmed=cleanup_ok(result),
                       finished_at=datetime.now(timezone.utc).isoformat())
        save(path, report)
        print(json.dumps(dict(id=spec['id'], ok=result.get('ok'), cleanup=attempt['cleanup_confirmed']),ensure_ascii=False),flush=True)
        if not attempt['cleanup_confirmed']: raise ValueError('cleanup_unconfirmed_no_more_cloud_calls')
