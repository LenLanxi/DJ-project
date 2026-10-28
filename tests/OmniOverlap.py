"""Opt-in real-cloud takeover test: prerecorded speech, real Docker, fake device.

No microphone, speaker, camera, browser, aircraft, network changes or retries.
The first fake status RPC is deliberately held while a second spoken request
must replace it without closing the same cloud connection.
"""
import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import traceback

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'modules'/'ai'),str(ROOT/'tests'),str(ROOT)]
from AlibabaRealtime import AlibabaConfig
from AlibabaBenchmark import fixture, FOLDER
from OmniLive import Audit, QueryOnlyBackend, QueryGateway, observed_connector
from OmniSession import OmniSession
from main import UnifiedRuntime, OMNI_INSTRUCTIONS, OMNI_TOOLS
from sandbox_runner import SandboxRunner
from RealtimeFixtures import synthesize


class DelayedQueryBackend(QueryOnlyBackend):
    def __init__(self):
        super().__init__(FOLDER/'RedTent.jpg')
        self.old_started=threading.Event()
        self.release_old=threading.Event()
        self.query_count=0
        self.query_lock=threading.Lock()
    def get_status(self):
        with self.query_lock:
            self.query_count+=1
            number=self.query_count
        if number==1:
            self.old_started.set()
            if not self.release_old.wait(40): raise RuntimeError('fixture_query_not_released')
        return super().get_status()


async def run(config,output):
    audit=Audit(config.api_key)
    backend=DelayedQueryBackend()
    sandbox=SandboxRunner(QueryGateway(backend,audit),timeout=45)
    runtime=UnifiedRuntime(sandbox,audit)
    session=None
    cloud=feeder=None
    pcm,metadata=fixture('Status')
    chunks=asyncio.Queue()
    feeder_stop=asyncio.Event()
    replies={}
    audio_bytes={}
    completed=set()
    result={'scope':'real_cloud_prerecorded_speech_real_docker_fake_query_device',
            'physical_devices':False,'billing_verified':False,'automatic_retry':False,
            'ok':False,'fixture_sha256':hashlib.sha256(pcm).hexdigest(),
            'fixture_text':metadata['text'],'cleanup':{}}
    replacement_text='取消上一项任务，立即重新查询模拟无人机电量。不要等待旧查询，不要操作飞机。'
    replacement=synthesize(replacement_text)
    result['replacement_text']=replacement_text
    result['replacement_sha256']=hashlib.sha256(replacement).hexdigest()
    def record(event):
        audit(event)
        if event.get('event') in ('user_text','program_start','program_result','tool_result',
                                  'omni_cloud_processing_failed','omni_response_drain_timeout'):
            print(json.dumps({'phase':event['event'],'epoch':event.get('epoch'),
                              'status':event.get('result',{}).get('status')},ensure_ascii=False),flush=True)
        persist()
    # Keep every phase, including failures before the final receipt.
    runtime.log=record
    sandbox.gateway.audit=record
    async def client(event):
        epoch=session.epoch
        if event.get('type')=='reply_text': replies[epoch]=replies.get(epoch,'')+event.get('text','')
        if event.get('type')=='audio':
            raw=base64.b64decode(event['audio'],validate=True)
            if any(raw): audio_bytes[epoch]=audio_bytes.get(epoch,0)+len(raw)
        if event.get('type')=='state' and event.get('text')=='本轮回复结束': completed.add(epoch)
    async def until(predicate,seconds):
        deadline=time.monotonic()+seconds
        while not predicate():
            if cloud is not None and cloud.done():
                await cloud
                raise RuntimeError('cloud_closed_before_test_completion')
            if session is not None and session.fault is not None: raise session.fault
            if time.monotonic()>deadline: raise TimeoutError('overlap_test_phase_timeout')
            await asyncio.sleep(.02)
    def enqueue(data):
        for offset in range(0,len(data),640): chunks.put_nowait(data[offset:offset+640].ljust(640,b'\0'))
    async def audio():
        deadline=time.monotonic()
        while not feeder_stop.is_set():
            packet=chunks.get_nowait() if not chunks.empty() else bytes(640)
            await session.audio(packet)
            deadline=max(deadline+.02,time.monotonic())
            await asyncio.sleep(max(0,deadline-time.monotonic()))
    def persist():
        result['events']=audit.events
        output.write_text(json.dumps(audit.clean(result),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    try:
        await sandbox.check()
        session=OmniSession(runtime,config,OMNI_INSTRUCTIONS,OMNI_TOOLS,client,audit,
                            connect=observed_connector(audit))
        session.log=record
        cloud=asyncio.create_task(session.run())
        await until(session.ready.is_set,15)
        feeder=asyncio.create_task(audio())
        enqueue(pcm)
        await until(backend.old_started.is_set,20)
        result['old_program_running_before_new_speech']=runtime.running
        record({'event':'overlap_second_utterance_queued','old_runtime_running':runtime.running})
        enqueue(replacement)
        await until(lambda:2 in completed,30)
        result['connection_preserved']=not cloud.done() and not session.closed and session.fault is None
        result['runtime_authority_current']=session.authority_current()
        result['replies']=replies
        result['nonzero_audio_bytes']=audio_bytes
        programs=[event for event in audit.events if event.get('event')=='program_result']
        result['program_results']=[event['result'] for event in programs]
        result['fake_priority_stops']=[call for call in backend.calls if call[0]=='priority_stop']
        result['ok']=bool(result['old_program_running_before_new_speech']
            and result['connection_preserved'] and result['runtime_authority_current']
            and len(programs)==2 and programs[0]['result'].get('status')=='cancelled'
            and programs[1]['result'].get('ok') is True
            and result['fake_priority_stops'] and all(not call[1][0] for call in result['fake_priority_stops'])
            and ('80' in replies.get(2,'') or '八十' in replies.get(2,''))
            and audio_bytes.get(2,0)>0 and not backend.rejected_actions)
    except Exception as exc:
        result.update(error_type=type(exc).__name__,error=str(exc),traceback=traceback.format_exc())
    finally:
        backend.release_old.set()
        feeder_stop.set()
        if feeder is not None:
            feeder.cancel()
            try: await asyncio.wait_for(asyncio.gather(feeder,return_exceptions=True),3)
            except TimeoutError: result['cleanup']['feeder_stop_unconfirmed']=True
        if session is not None:
            try:
                await asyncio.wait_for(session.close(),12)
                result['cleanup']['session_closed']=True
            except Exception as exc: result['cleanup']['session_close_error']=type(exc).__name__
        if cloud is not None:
            try: await asyncio.wait_for(cloud,3)
            except Exception as exc: result['cleanup']['cloud_exit_error']=type(exc).__name__
        result['cleanup']['sandbox_not_running']=not sandbox.running
        result['cleanup']['sandbox']=dict(sandbox.last_cleanup)
        result['cleanup']['fake_old_worker_released']=backend.release_old.is_set()
        result['ok']=bool(result['ok'] and result['cleanup'].get('session_closed')
            and result['cleanup']['sandbox_not_running'] and sandbox.last_cleanup.get('confirmed') is True
            and 'cloud_exit_error' not in result['cleanup'])
        persist()
    print(json.dumps({key:result.get(key) for key in
          ('ok','connection_preserved','old_program_running_before_new_speech','fake_priority_stops','error_type','error','cleanup')},ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real',action='store_true',required=True)
    parser.add_argument('--credentials',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--api-budget-rmb',type=float,required=True)
    args=parser.parse_args()
    if not 0<args.api_budget_rmb<=15: parser.error('budget must be in (0,15] RMB')
    with args.output.open('x',encoding='utf-8') as handle: json.dump({'ok':False,'phase':'reserved'},handle)
    config=AlibabaConfig.from_credentials(args.credentials,paid_budget_rmb=args.api_budget_rmb)
    async def bounded():
        async with asyncio.timeout(75): return await run(config,args.output)
    raise SystemExit(asyncio.run(bounded()))
