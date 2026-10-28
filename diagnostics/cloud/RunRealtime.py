"""Opt-in, serial, balanced real-cloud comparison. Never retries failed samples.

Existing formal attempts count toward --rounds, including failures, so resuming
does not cherry-pick successful trials. Use a fresh --output-folder for a new
experiment. Long-lived credentials are file paths only, never CLI key values.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
ARCHIVE=ROOT/'ScenarioAcceptance'/'Evidence'/'RealtimeComparison'
DEFAULT=ROOT/'ScenarioAcceptance'/'Evidence'/'Current'/'RealtimeComparison'
FILES={'alibaba':'AlibabaMedia.json','doubao':'DoubaoMedia.json'}
NAMES={'alibaba':'AlibabaRealtime','doubao':'DoubaoRTC'}


def read_samples(path):
    return json.loads(path.read_text(encoding='utf-8-sig')).get('samples',[]) if path.is_file() else []


def cleanup_confirmed(provider, cleanup):
    if provider=='alibaba':
        return all(cleanup.get(k) is True for k in
                   ('connection_closed','sender_finished','receiver_finished'))
    if provider!='doubao':
        return False
    return (cleanup.get('cloud_stop',{}).get('ok') is True
            and cleanup.get('native_exit_code')==0 and cleanup.get('node_exit_code')==0
            and cleanup.get('short_token_file_deleted') is True)


def main(args):
    if not args.real:
        raise ValueError('explicit_--real_required')
    folder=args.output_folder.resolve()
    if folder==ARCHIVE.resolve():
        raise ValueError('archived_evidence_is_read_only_use_fresh_output_folder')
    credentials={'alibaba':args.alibaba_credentials,'doubao':args.doubao_credentials}
    for path in credentials.values():
        if not path.is_file(): raise ValueError('missing_credentials_file_'+str(path))
    subprocess.run(['docker','image','inspect','companion-rtc-probe:1'],capture_output=True,check=True)
    for script in ['AlibabaBenchmark.py','DoubaoBenchmark.py']:
        if not (ROOT/'diagnostics'/'cloud'/script).is_file(): raise ValueError('missing_driver_'+script)
    folder.mkdir(parents=True,exist_ok=True)
    # Drivers use the fixed shared evidence fixture directory. A custom output
    # folder changes receipts only; it cannot silently replace shared inputs.
    schedule=folder/'Schedule.json'
    order=json.loads(schedule.read_text(encoding='utf-8')).get('order',[]) if schedule.is_file() else []
    def attempt(provider,task='speech',image=None):
        path=folder/FILES[provider]
        before=read_samples(path)
        command=[sys.executable,'-X','utf8','-B',str(ROOT/'diagnostics'/'cloud'/(provider.title()+'Benchmark.py')),
                 '--real','--task',task,'--trials','1','--credentials',str(credentials[provider]),'--output',str(path)]
        if provider=='alibaba': command += ['--phase','formal']
        if image: command += ['--image',image if provider=='doubao' else ('red' if image=='RedTent' else 'blue')]
        print(json.dumps({'event':'serial_trial','provider':NAMES[provider],'task':task,'image':image}),flush=True)
        started=datetime.now(timezone.utc).isoformat()
        invocation=dict(provider=NAMES[provider],task=task,image=image,started_at=started,
                        receipt_index=len(before),status='invocation_started')
        order.append(invocation)
        schedule.write_text(json.dumps({'serial':True,'order':order},indent=2)+'\n',encoding='utf-8')
        # Provider drivers own bounded-session finally cleanup. Do not kill the
        # driver on an arbitrary parent timeout and strand its billable task.
        completed=subprocess.run(command,cwd=ROOT,env=dict(os.environ,PYTHONUTF8='1'))
        after=read_samples(path)
        invocation.update(exit_code=completed.returncode,receipts_added=len(after)-len(before),
                          status='finished' if len(after)==len(before)+1 else 'receipt_missing_or_invalid')
        schedule.write_text(json.dumps({'serial':True,'order':order},indent=2)+'\n',encoding='utf-8')
        if len(after)!=len(before)+1:
            raise RuntimeError('provider_did_not_record_exactly_one_attempt_'+provider)
        sample=after[-1]
        cleanup=sample.get('cleanup',{})
        invocation['ok']=sample.get('ok')
        (folder/'Schedule.json').write_text(json.dumps({'serial':True,'order':order},indent=2)+'\n',encoding='utf-8')
        if not cleanup_confirmed(provider,cleanup):
            raise RuntimeError('cloud_cleanup_unconfirmed_stop_comparison_'+provider)
    rotations=[['alibaba','doubao'],['doubao','alibaba']]
    for round_index in range(args.rounds):
        for provider in rotations[round_index % len(rotations)]:
            previous=[s for s in read_samples(folder/FILES[provider])
                      if s.get('phase') not in ('development','diagnostic') and s.get('task')=='speech'
                      and s.get('turn_mode')=='automatic']
            if len(previous)<=round_index:
                attempt(provider)
    if args.capabilities:
        for task,image in [('status',None),('vision','RedTent'),('vision','BlueTent')]:
            for provider in ['alibaba','doubao']:
                previous=[s for s in read_samples(folder/FILES[provider])
                          if s.get('phase')!='development' and s.get('task')==task
                          and (task!='vision' or s.get('fixture',{}).get('image')==image or s.get('image')==image)]
                if not previous: attempt(provider,task,image)
    subprocess.run([sys.executable,str(ROOT/'diagnostics'/'cloud'/'CompareRealtime.py'),'--folder',str(folder)],check=True,cwd=ROOT)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--real',action='store_true')
    p.add_argument('--rounds',type=int,choices=[1,2,3],default=3)
    p.add_argument('--capabilities',action='store_true')
    p.add_argument('--output-folder',type=Path,default=DEFAULT)
    private=Path.home()/'Desktop'/'tmp'
    p.add_argument('--alibaba-credentials',type=Path,default=private/'DomesticAiTrials'/'AlibabaCredentials.json')
    p.add_argument('--doubao-credentials',type=Path,default=private/'DoubaoRealtime'/'Credentials.json')
    main(p.parse_args())
