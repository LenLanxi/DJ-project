"""Opt-in serial balanced comparison; failed attempts count and are never retried."""
import argparse
import asyncio
import json
from pathlib import Path
from Trials import cleanup_ok, execute, run_trial, credential_arguments, save
ROOT=Path(__file__).resolve().parents[2]
ARCHIVE=ROOT/'ScenarioAcceptance'/'Evidence'/'RealtimeComparison'
DEFAULT=ROOT/'ScenarioAcceptance'/'Evidence'/'Current'/'RealtimeComparison'
FILES={'alibaba':'AlibabaMedia.json','doubao':'DoubaoMedia.json'}

def read_samples(path):
    return json.loads(path.read_text(encoding='utf-8-sig')).get('samples',[]) if path.is_file() else []

def cleanup_confirmed(provider, cleanup):
    return cleanup_ok(dict(provider={'alibaba':'AlibabaRealtime','doubao':'DoubaoRTC'}.get(provider,provider),cleanup=cleanup))

async def main(args):
    if not args.real: raise ValueError('explicit_--real_required')
    folder=args.output_folder.resolve()
    if folder==ARCHIVE.resolve(): raise ValueError('archived_evidence_is_read_only_use_fresh_output_folder')
    path=folder/'Schedule.json'
    design=[]
    for index in range(args.rounds):
        for provider in (('Alibaba','Doubao') if index%2==0 else ('Doubao','Alibaba')):
            design.append(dict(id=f'{provider}Speech{index}',provider=provider,task='speech',phase='formal',interval=1,silence=1200))
    if args.capabilities:
        for task,image in [('status',None),('vision','RedTent'),('vision','BlueTent')]:
            for provider in ('Alibaba','Doubao'):
                design.append(dict(id=f'{provider}{task}{image}',provider=provider,task=task,image=image,phase='formal',interval=1,silence=1200))
    report=json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(design=design,attempts=[],serial=True)
    if 'attempts' not in report: raise ValueError('old_schedule_requires_new_output_folder')
    report['design']=design
    credentials={'alibaba':args.alibaba_credentials,'doubao':args.doubao_credentials}
    async def collect(spec):
        item=await run_trial(spec,credentials)
        provider=spec['provider'].lower()
        receipt=folder/FILES[provider]
        samples=read_samples(receipt)
        samples.append(item)
        save(receipt,dict(provider=item['provider'],samples=samples))
        return item
    await execute(report,path,collect)
    from CompareRealtime import compare
    save(folder/'Summary.json',compare(folder))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--real',action='store_true')
    p.add_argument('--rounds',type=int,choices=[1,2,3],default=3)
    p.add_argument('--capabilities',action='store_true')
    p.add_argument('--output-folder',type=Path,default=DEFAULT)
    credential_arguments(p)
    asyncio.run(main(p.parse_args()))
