"""Opt-in causal route probes. New evidence only; historical receipts stay frozen.

No device capture or aircraft. All outcomes retained, one cloud session at a
time. This is a screening experiment, not a reliability or flight certificate.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
ARCHIVE = ROOT / 'ScenarioAcceptance' / 'Evidence' / 'RouteSelection20261027'
OUT = ROOT / 'ScenarioAcceptance' / 'Evidence' / 'Current' / 'RouteSelection'
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
                for repeat in range(1, 4) for provider in ('Alibaba', 'Doubao')]
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


from Trials import cleanup_ok, save, run_trial, execute, credential_arguments


async def main(args):
    if not args.real:
        raise ValueError('explicit_real_required')
    folder = args.output_folder.resolve()
    if folder == ARCHIVE.resolve():
        raise ValueError('archived_evidence_is_read_only_use_fresh_output_folder')
    path = folder/(args.stage.title()+'.json')
    report = json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(
        schema_version=1, stage=args.stage, created_at=datetime.now(timezone.utc).isoformat(),
        design=plan(args.stage), attempts=[],
        physical_devices=False, billing_verified=False,
        planned_session_upper_bound=sum(len(plan(stage)) for stage in
            ('screen', 'confirm', 'speech', 'endpoint', 'doubao', 'postfix')))
    if any(spec.get('provider') not in ('Alibaba', 'Doubao') for spec in report['design']):
        raise ValueError('archived_provider_report_is_read_only_use_fresh_output_folder')
    credentials = {'alibaba': args.alibaba_credentials, 'doubao': args.doubao_credentials}
    await execute(report, path, lambda spec: run_trial(spec, credentials))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real', action='store_true')
    parser.add_argument('--output-folder', type=Path, default=OUT)
    parser.add_argument('--stage', choices=['screen', 'confirm', 'speech', 'endpoint', 'doubao', 'postfix'], required=True)
    credential_arguments(parser)
    arguments = parser.parse_args()
    asyncio.run(main(arguments))
