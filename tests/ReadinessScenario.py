"""Opt-in natural-language planning -> Docker -> Gateway -> Vision -> tracker.

All flight/camera state belongs to a strict synthetic backend. No drone SDK,
microphone, speaker, browser or physical camera is opened. --offline uses an
explicit fake planner and a delayed pixel-based seed client, with real Docker.
--real replaces those two model boundaries with Qwen text planning and Ark.
It is a planning/navigation integration exercise, not an end-to-end voice or
physical arrival demonstration. No retries or output-file replacement.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid
import sys

import cv2
import numpy as np
from websockets.asyncio.client import connect

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'modules'/'ai'))
sys.path.insert(0,str(ROOT/'tests'))
from Support import MockBackend
from gateway import Gateway
from sandbox_runner import SandboxRunner
from vision import Vision
from main import PROGRAM_INSTRUCTIONS
from AlibabaRealtime import AlibabaConfig, session_update

USER_PREPARE='请先检查当前模拟设备的连接、飞行状态与电量；如果已连接、电量至少30且尚未起飞，就起飞，然后开始跟随向前行走的用户。只依据实际返回，不重复起飞。'
USER_TARGET=('当前模拟设备已经起飞且正在跟随。请飞向前面的红色帐篷，在本次保守演示停止点原地旋转一周，然后恢复原跟随。'
    '这不是实际到达证明。每次先observe确认唯一可见且居中的红帐篷，最多前进累计200厘米，每步20厘米；'
    '当width_fraction至少0.29时停止靠近。在最多10次观察内未达到条件就抛错停止。'
    '不作横移、上下移动或其他探索，不调整原跟随参考；程序成功后由现有票据恢复原跟随，不另行start_follow。'
    '每步检查工具结果，失败即抛错；打印关键观察、动作与未完成分支。')
OFFLINE_PREPARE='''state=drone.get_status()
print("prepare_status",state)
if not state.get("connected") or state.get("battery",0)<30:
    raise RuntimeError("preflight_failed")
if state.get("flight_state")=="landed":
    print("takeoff",drone.takeoff())
elif state.get("flight_state")!="flying":
    raise RuntimeError("unknown_flight_state")
print("follow",drone.start_follow())
'''
OFFLINE_TARGET='''total=0
ready=False
for step in range(10):
    observed=drone.observe("前面的红色帐篷")
    print("observation",observed)
    if not observed.get("visible") or abs(observed.get("center_x",0)-0.5)>0.12:
        raise RuntimeError("target_not_usable")
    if observed["width_fraction"]>=0.29:
        ready=True
        break
    if total+20>200:
        raise RuntimeError("distance_budget")
    result=drone.move_forward(20)
    print("move_result",result)
    if not result.get("ok"):raise RuntimeError("move_failed")
    total+=20
if not ready:raise RuntimeError("demonstration_stop_not_reached")
print("rotation",drone.rotate(360))
print("stage_status","program_finished_wait_for_follow_cleanup",total)
'''


class Audit:
    def __init__(self):self.events=[];self.secrets=();self.lock=threading.RLock()
    def clean(self,value):
        if isinstance(value,dict):
            return {str(k):('[omitted]' if str(k).lower() in ['image_url','audio','jpeg','authorization','api_key','apikey']
                           else self.clean(v)) for k,v in value.items()}
        if isinstance(value,(tuple,list)):return [self.clean(v) for v in value]
        if isinstance(value,str):
            for secret in self.secrets:
                if secret:value=value.replace(secret,'[REDACTED]')
            return value[:90000]
        if isinstance(value,(str,int,float,bool)) or value is None:return value
        return type(value).__name__
    def __call__(self,event):
        with self.lock:self.events.append(self.clean(dict(event,timestamp=time.time())))


class ScenarioBackend(MockBackend):
    """Only fake motion; producer stamps newly rendered frames at creation."""
    def __init__(self,audit,photo=None):
        super().__init__();self.audit=audit;self.gateway=None
        self.forward_cm=0.;self.total_requested_cm=0;self.camera_id=0
        self.camera_packet=None;self.camera_stop=threading.Event()
        self.photo=None
        if photo is not None:
            self.photo=cv2.imread(str(photo),cv2.IMREAD_COLOR)
            if self.photo is None:raise ValueError('photo_unreadable')
            self.photo=cv2.resize(self.photo,(640,360),interpolation=cv2.INTER_AREA)
        self.frame_gaps=[];self.render_times=[]
        rng=np.random.default_rng(83092)
        grain=cv2.GaussianBlur(rng.normal(0,22,(140,170,3)).astype(np.float32),(3,3),.5)
        self.fabric=np.clip(grain+np.array([38,47,186],dtype=np.float32),0,255).astype(np.uint8)
        for _ in range(140):
            x,y=rng.integers([4,4],[166,136]);radius=int(rng.integers(1,3))
            cv2.circle(self.fabric,(int(x),int(y)),radius,(23,33,int(rng.integers(100,230))),-1)
        self.camera_thread=threading.Thread(target=self._camera,daemon=True,name='readiness-synthetic-camera')
        self.camera_thread.start()
        until=time.monotonic()+2
        while self.camera_packet is None and time.monotonic()<until:time.sleep(.01)

    def render(self,fid,distance):
        if self.photo is not None:
            scale=1.+distance*.002
            matrix=cv2.getRotationMatrix2D((320,180),0.,scale)
            image=cv2.warpAffine(self.photo,matrix,(640,360),borderMode=cv2.BORDER_REFLECT)
            rng=np.random.default_rng(fid+11923)
            return np.clip(image.astype(np.int16)+rng.integers(-1,2,image.shape,dtype=np.int16),0,255).astype(np.uint8)
        image=np.full((360,640,3),(170,190,200),dtype=np.uint8)
        image[260:]=np.array([100,125,100],dtype=np.uint8)
        width=120.+distance*.6;height=width*.82
        left=round(320-width/2);top=round(258-height);w=round(width);h=round(height)
        texture=cv2.resize(self.fabric,(w,h),interpolation=cv2.INTER_LINEAR)
        mask=np.zeros((h,w),dtype=np.uint8)
        cv2.fillConvexPoly(mask,np.array([[w//2,0],[w-1,h-1],[0,h-1]],dtype=np.int32),255)
        roi=image[top:top+h,left:left+w];roi[mask>0]=texture[mask>0]
        cv2.line(image,(320,top+3),(320,top+h-2),(20,28,96),1)
        # A declared synthetic sensor variation prevents identical cached pixels
        # being mislabeled as newly captured frames. It is not a target marker.
        rng=np.random.default_rng(fid+11923)
        noise=rng.integers(-1,2,image.shape,dtype=np.int16)
        return np.clip(image.astype(np.int16)+noise,0,255).astype(np.uint8)

    def _camera(self):
        while not self.camera_stop.is_set():
            with self.lock:distance=self.forward_cm;self.camera_id+=1;fid=self.camera_id
            rendering=time.monotonic()
            frame=self.render(fid,distance)
            made=time.time()
            with self.lock:
                if self.camera_packet is not None:self.frame_gaps.append(made-self.camera_packet['timestamp'])
                self.render_times.append(time.monotonic()-rendering)
                self.camera_packet=dict(frame=frame,frame_id=fid,timestamp=made,stale=False,source='sdk_camera',simulated=True)
            self.camera_stop.wait(.05)

    def get_frame(self):
        with self.lock:
            packet=dict(self.camera_packet);packet['frame']=packet['frame'].copy()
            return packet

    def _live(self,after_send=False):
        from backends import OPERATION
        operation=OPERATION.get()
        # Mirrors actual SDK: image deadline fences FINAL SEND, while a
        # command already sent waits under external cancellation + epochs.
        # Do not make frame age invalidate a legitimate in-flight reply.
        if after_send:operation=getattr(operation,'event',operation)
        grant=self._context()
        return (operation is None or not operation.is_set()) and self.owner=='model' and grant.get('control_epoch')==self.control_epoch and grant.get('command_epoch')==self.command_epoch

    def action(self,name,*args):
        with self.lock:
            if not self._live():return {'ok':False,'status':'failed','reason':'strict_mock_obsolete_operation'}
            if name=='takeoff' and self.flying:return {'ok':False,'status':'failed','reason':'strict_mock_already_flying'}
            if name.startswith('move_'):
                observation=self.gateway.observation if self.gateway else None
                if (name!='move_forward' or len(args)!=1 or type(args[0]) is not int or not 20<=args[0]<=40
                    or self.total_requested_cm+args[0]>200 or not observation or not observation.get('visible')
                    or not observation.get('target_tracking') or time.time()-observation['timestamp']>.5):
                    return {'ok':False,'status':'failed','reason':'strict_mock_requires_tracked_observation_and_200cm_budget'}
            if name=='update_velocity':return {'ok':False,'status':'failed','reason':'strict_mock_no_continuous_motion'}
            result=super().action(name,*args)
            if not result.get('ok'):return result
            initial=self.forward_cm
            if name=='move_forward':self.total_requested_cm+=args[0]
        if name=='move_forward':
            for index in range(1,9):
                time.sleep(.05)
                with self.lock:
                    if not self._live(after_send=True):return {'ok':False,'status':'failed','reason':'strict_mock_cancelled_mid_motion'}
                    self.forward_cm=initial+args[0]*index/8
        elif name=='rotate':time.sleep(.15)  # No fake claim of a physical yaw response.
        self.audit({'event':'mock_action_completed','name':name,'args':args,'simulated':True,'forward_cm':self.forward_cm})
        return result

    def stop_cancelled_follow(self,cancelled):
        return self.stop_follow() if cancelled.is_set() else {'ok':True}

    def close(self):
        self.camera_stop.set();self.camera_thread.join(2)


class DelayedSeedClient:
    def __init__(self,audit,delay=1.):self.audit=audit;self.delay=delay;self.calls=0
    def complete(self,messages):
        self.calls+=1
        image_url=messages[-1]['content'][-1]['image_url']['url']
        encoded=base64.b64decode(image_url.split(',',1)[1])
        image=cv2.imdecode(np.frombuffer(encoded,np.uint8),cv2.IMREAD_COLOR)
        b,g,r=cv2.split(image)
        mask=((r.astype(float)>1.5*g)&(r.astype(float)>1.5*b)&(r>110)).astype(np.uint8)*255
        contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        found=[c for c in contours if cv2.contourArea(c)>1000]
        time.sleep(self.delay)
        result={'visible':len(found)==1,'ambiguous':len(found)>1,'candidate_count':len(found),'bbox_format':'xyxy','bbox':None,
                'description':'offline pixel segmentation of declared synthetic red fabric, not a cloud result'}
        if len(found)==1:
            x,y,w,h=cv2.boundingRect(found[0]);height,width=image.shape[:2]
            result['bbox']=[x/width,y/height,(x+w)/width,(y+h)/height]
        self.audit({'event':'offline_cloud_seed','delay_seconds':self.delay,'image_sha256':hashlib.sha256(encoded).hexdigest(),'result':result})
        return json.dumps(result)


class BoundedArk:
    def __init__(self,path,audit):
        from model_client import ModelClient
        data=json.loads(path.read_text(encoding='utf-8-sig'))['Ark']
        audit.secrets+=tuple([data['APIKey']])
        self.calls=0;self.active=0;self.completed=0;self.closing=False
        self.audit=audit;self.lock=threading.Lock();self.context=threading.local()
        self.settled=threading.Event();self.settled.set()
        self.client=ModelClient(on_usage=self._usage,key=data['APIKey'],model=data['ModelName'],
            url='https://ark.cn-beijing.volces.com/api/v3/chat/completions',max_output_tokens=512)
    def _usage(self,event):
        # ModelClient emits start/end synchronously in the calling worker. Bind
        # request IDs to that worker's call, never its shared last_usage field.
        call_id=getattr(self.context,'call_id',None)
        request_id=event.get('request_id')
        if call_id and request_id:self.context.request_id=request_id
        self.audit(dict(event,readiness_call_id=call_id))
    def complete(self,messages):
        return self._call(messages)
    def complete_structured(self,messages,schema):
        # A failed strict request is evidence, never a reason to silently retry
        # through ordinary complete() or pretend an unstructured reply passed.
        return self._call(messages,schema=schema)
    def _call(self,messages,schema=None):
        with self.lock:
            if self.closing:raise RuntimeError('readiness_Ark_cleanup_started')
            if self.calls>=3:raise RuntimeError('readiness_Ark_request_budget_exhausted')
            self.calls+=1;self.active+=1;self.settled.clear()
        call_id=uuid.uuid4().hex
        self.context.call_id=call_id;self.context.request_id=None
        metadata={'readiness_call_id':call_id,'model':self.client.model,'schema':copy.deepcopy(schema),
                  'structured':schema is not None,'retried':False}
        self.audit(dict(metadata,event='readiness_Ark_call_start'))
        try:
            raw=(self.client.complete_structured(messages,schema) if schema is not None
                 else self.client.complete(messages))
            # Keep malformed JSON too, before Vision can reject it. Model text
            # is bounded and data URLs are removed; input images are never logged.
            safe_raw=re.sub(r'data:image/[^;,\s]+;base64,[A-Za-z0-9+/=\r\n]+','[image omitted]',raw)
            record=dict(metadata,event='readiness_Ark_raw',request_id=self.context.request_id,
                        raw_response=safe_raw[:60000],raw_response_truncated=len(safe_raw)>60000,
                        raw_response_sha256=hashlib.sha256(raw.encode('utf-8')).hexdigest())
            if schema is not None:
                try:Vision.parse_observation(raw,require_format=True)
                except Exception as exc:
                    record.update(validation_ok=False,validation_error_type=type(exc).__name__,
                                  validation_error=str(exc)[:300])
                else:record['validation_ok']=True
            self.audit(record)
            return raw
        except Exception as exc:
            self.audit(dict(metadata,event='readiness_Ark_call_failed',request_id=self.context.request_id,
                            error_type=type(exc).__name__))
            raise
        finally:
            self.context.call_id=None;self.context.request_id=None
            # An asyncio timeout cancels its waiter, not the HTTP worker. This
            # event is set only after that worker has emitted usage and raw/error.
            with self.lock:
                self.active-=1;self.completed+=1
                if not self.active:self.settled.set()
    async def wait_settled(self,timeout=35):
        """Freeze new calls and retain late HTTP evidence without retrying."""
        started=time.monotonic()
        with self.lock:
            self.closing=True
            active_before=self.active
        deadline=started+timeout
        while not self.settled.is_set() and time.monotonic()<deadline:
            await asyncio.sleep(min(.05,max(0,deadline-time.monotonic())))
        with self.lock:
            result={'status':'settled' if not self.active else 'unsettled',
                'http_workers_settled':not bool(self.active),'active_at_cleanup':active_before,
                'active_remaining':self.active,'started_calls':self.calls,'completed_calls':self.completed,
                'max_wait_seconds':timeout,'wait_seconds':round(time.monotonic()-started,3),
                'late_evidence_may_be_missing':bool(self.active),'retried':False}
        self.audit(dict(result,event='readiness_Ark_settlement'))
        return result


async def settle_cloud(cloud):
    if isinstance(cloud,BoundedArk):return await cloud.wait_settled(timeout=35)
    return {'status':'not_applicable','http_workers_settled':True,'real_http_requests':False}


async def plan(config,user,audit):
    instructions=PROGRAM_INSTRUCTIONS+'\n只返回JSON对象：{"code":"完整Python","explanation":"简短说明"}。程序失败应抛异常停止，不捕获后继续动作。'
    result={'input_text':user,'input_confirmed':False,'events':[],'code':'','usage':[]}
    async with asyncio.timeout(35):
        async with connect(config.validate(),additional_headers={'Authorization':'Bearer '+config.api_key},
            max_size=1000000,open_timeout=8,close_timeout=1,proxy=None) as ws:
            async def send(event):await ws.send(json.dumps(dict(event,event_id='Ready'+uuid.uuid4().hex),ensure_ascii=False))
            async def receive():
                event=json.loads(await ws.recv());kind=event.get('type')
                result['events'].append({'type':kind,'event_id':event.get('event_id')})
                audit({'event':'readiness_planner_event','type':kind,'event_id':event.get('event_id')})
                if kind=='error':
                    detail=event.get('error',{})
                    audit({'event':'readiness_planner_error','details':{key:str(detail[key])[:500]
                           for key in ['type','code','message','param'] if isinstance(detail,dict) and key in detail}})
                    raise RuntimeError('readiness_planner_provider_error')
                return event
            if (await receive()).get('type')!='session.created':raise RuntimeError('planner_session_missing')
            update=session_update(config,instructions,[],mode='manual')
            update['session'].update(modalities=['text'],max_tokens=2048,input_audio_transcription=None)
            await send(update)
            while (await receive()).get('type')!='session.updated':pass
            await send({'type':'conversation.item.create','item':{'type':'message','role':'user',
                'content':[{'type':'input_text','text':user}]}})
            while not result['input_confirmed']:
                event=await receive();item=event.get('item',{})
                result['input_confirmed']=(event.get('type')=='conversation.item.created' and item.get('role')=='user'
                    and any(c.get('type')=='input_text' and c.get('text')==user for c in item.get('content',[])))
            await send({'type':'response.create'})
            chunks=[];text=''
            while True:
                event=await receive();kind=event.get('type')
                if kind=='response.text.delta':chunks.append(event.get('delta',''))
                if kind=='response.text.done':text=event.get('text','')
                if kind=='response.done':
                    response=event.get('response',{})
                    result['usage'].append(response.get('usage',{}))
                    audit({'event':'readiness_Qwen_usage','usage':response.get('usage',{})})
                    if response.get('status')!='completed':raise RuntimeError('readiness_planner_response_incomplete')
                    break
            text=text or ''.join(chunks)
            result['raw_reply']=text
            audit({'event':'readiness_planner_reply','input_text':user,'input_confirmed':result['input_confirmed'],'reply':text})
            parsed=json.loads(text)
            code=parsed.get('code')
            if not isinstance(code,str) or not code.strip() or len(code.encode())>60000:
                raise RuntimeError('readiness_planner_empty_or_oversized_code')
            result['code']=code;result['explanation']=parsed.get('explanation','')
    return result


async def photo_probe(args):
    """One observational Ark/Vision/Tracker request; no planner or motion."""
    if args.photo is None:raise ValueError('photo_probe_requires_photo')
    if args.output.exists():raise ValueError('output_already_exists_no_overwrite')
    if not math.isfinite(args.api_budget_rmb) or not 0<args.api_budget_rmb<=15:
        raise ValueError('explicit_budget_must_be_positive_at_most_15')
    audit=Audit();backend=ScenarioBackend(audit,args.photo);vision=None;cloud=None
    receipt={'schema_version':1,'scope':'licensed_transformed_photo_Vision_TargetTracker_only',
        'image_source':'transformed_photo_not_drone_footage','physical_devices_opened':False,
        'voice_or_language_planner_tested':False,'real_cloud':bool(args.real),'ok':False,
        'photo_sha256':hashlib.sha256(args.photo.read_bytes()).hexdigest(),
        'photo':str(args.photo.resolve()),'generated_3D_or_physical_arrival_verified':False,
        'retries':0,'api_budget_rmb_authorized':args.api_budget_rmb,'actual_bill_verified':False}
    try:
        cloud=BoundedArk(args.doubao_credentials,audit) if args.real else DelayedSeedClient(audit,args.offline_seed_delay)
        vision=Vision(cloud,backend);gateway=Gateway(backend,vision);backend.gateway=gateway
        runner=SandboxRunner(gateway,image=args.sandbox_image,timeout=30,max_rpc=10)
        code='observed=drone.observe("红色帐篷")\nprint(observed)\nif not observed.get("visible") or not observed.get("target_tracking"):raise RuntimeError("photo_target_not_confirmed")'
        receipt['result']=await runner.run(code)
        receipt['sandbox_cleanup']=dict(runner.last_cleanup)
        receipt['cloud_requests']=cloud.calls
        observed=next((e.get('result') for e in receipt['result'].get('journal',[]) if e.get('method')=='observe'),None)
        receipt['observation']=observed
        receipt['flight_actions']=[name for name,_ in backend.calls if name not in ['stop_motion']]
        receipt['ok']=bool(receipt['result'].get('ok') and observed and observed.get('target_tracking')
                           and not receipt['flight_actions'] and runner.last_cleanup.get('confirmed') is True)
    except Exception as exc:receipt['failure']={'type':type(exc).__name__,'reason':audit.clean(str(exc))[:300]}
    finally:
        if vision is not None:vision.close()
        backend.close()
        settlement=await settle_cloud(cloud)
        receipt['events']=audit.events
        receipt['cleanup']={'synthetic_camera_finished':not backend.camera_thread.is_alive(),
                            'cloud_http':settlement}
        if not settlement['http_workers_settled']:receipt['ok']=False
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf-8') as output:
            output.write(json.dumps(audit.clean(receipt),ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'ok':receipt['ok'],'output':str(args.output.resolve()),'cloud_requests':receipt.get('cloud_requests'),
        'failure':receipt.get('failure'),'reason':receipt.get('result',{}).get('reason')},ensure_ascii=True))
    return 0 if receipt['ok'] else 1


async def run(args):
    if args.photo_probe:return await photo_probe(args)
    if args.output.exists():raise ValueError('output_already_exists_no_overwrite')
    if not 0<args.api_budget_rmb<=15:raise ValueError('explicit_budget_must_be_positive_at_most_15')
    audit=Audit();backend=ScenarioBackend(audit,args.photo);vision=None;cloud=None
    report={'schema_version':1,'scope':'natural_language_planning_real_Docker_Gateway_Vision_TargetTracker_synthetic_device',
        'real_cloud':bool(args.real),'voice_input_verified':False,'physical_devices_opened':False,
        'physical_arrival_or_flight_verified':False,'follow_dynamics_verified':False,
        'tello_driver_or_ArUco_recognition_exercised':False,
        'started_at':datetime.now(timezone.utc).isoformat(),
        'plans':[],'stages':[],'ok':False,'api_budget_rmb_authorized':args.api_budget_rmb,
        'budget_is_not_provider_bill_hard_cap':True,'retries':0,
        'image_source':'transformed_photo_not_drone_footage' if args.photo else 'synthetic_textured_tent',
        'photo_sha256':hashlib.sha256(args.photo.read_bytes()).hexdigest() if args.photo else None,
        'photo_3D_or_field_navigation_verified':False,
        'scene_mapping':{'starting_target_width_pixels':120,'width_pixels_per_fake_cm':.6,
                         'demonstration_stop_width_fraction':.29,'physical_distance_claimed':False}}
    try:
        if args.real:
            config=AlibabaConfig.from_credentials(args.alibaba_credentials,paid_budget_rmb=args.api_budget_rmb)
            audit.secrets+=(config.api_key,)
            cloud=BoundedArk(args.doubao_credentials,audit)
        else:config=None;cloud=DelayedSeedClient(audit,args.offline_seed_delay)
        vision=Vision(cloud,backend);gateway=Gateway(backend,vision);backend.gateway=gateway
        runner=SandboxRunner(gateway,image=args.sandbox_image,timeout=70,max_rpc=200)
        await runner.check()
        for index,user in enumerate([USER_PREPARE,USER_TARGET]):
            planned=await plan(config,user,audit) if args.real else {
                'input_text':user,'input_confirmed':False,'offline_fake_planner':True,
                'code':OFFLINE_PREPARE if index==0 else OFFLINE_TARGET}
            report['plans'].append(planned)
            result=await runner.run(planned['code'])
            report['stages'].append({'index':index+1,'result':result,'following_after':backend.is_following(),
                                     'status_after':backend.get_status(),'sandbox_cleanup':dict(runner.last_cleanup)})
            if not result.get('ok'):raise RuntimeError('readiness_stage_'+str(index+1)+'_failed')
        methods=[e.get('method') for stage in report['stages'] for e in stage['result'].get('journal',[])]
        rotations=[e for stage in report['stages'] for e in stage['result'].get('journal',[])
                   if e.get('method')=='rotate' and e.get('args') in ([360],[-360])]
        report['checks']={'takeoff_called':'takeoff' in methods,'start_follow_called':'start_follow' in methods,
            'observe_called':'observe' in methods,'move_forward_called':'move_forward' in methods,
            'rotate_one_turn_called':len(rotations)==1,'final_following':backend.is_following(),
            'movement_cm':backend.total_requested_cm,'movement_within_200cm':backend.total_requested_cm<=200,
            'fresh_tracker_observed':any(e.get('method')=='observe' and e.get('result',{}).get('target_tracking')
                for stage in report['stages'] for e in stage['result'].get('journal',[])),
            'follow_ticket_restored':report['stages'][-1]['result'].get('follow_cleanup',{}).get('status')=='follow_resumed'}
        report['checks']['all_stage_containers_cleanup_confirmed']=all(s['sandbox_cleanup'].get('confirmed') is True for s in report['stages'])
        # Independent deterministic failure fixture; no model/API calls. Keep
        # the successful scenario's final follow state untouched.
        failed_backend=ScenarioBackend(audit)
        try:
            failed_gateway=Gateway(failed_backend);failed_backend.gateway=failed_gateway
            failed_runner=SandboxRunner(failed_gateway,image=args.sandbox_image,timeout=15)
            failed=await failed_runner.run(OFFLINE_PREPARE+'\nprint(drone.stop_motion())\nraise RuntimeError("controlled_failure_after_handoff")')
            report['failure_cleanup_probe']={'result':failed,'following_after_failure':failed_backend.is_following(),
                'sandbox_cleanup':dict(failed_runner.last_cleanup),
                'passed':not failed.get('ok') and not failed_backend.is_following() and failed_runner.last_cleanup.get('confirmed') is True}
        finally:failed_backend.close()
        report['ok']=all(value is True for key,value in report['checks'].items() if key!='movement_cm') and report['failure_cleanup_probe']['passed']
    except Exception as error:
        report['failure']={'type':type(error).__name__,'reason':audit.clean(str(error))[:300]}
    finally:
        if vision is not None:vision.close()
        backend.close()
        settlement=await settle_cloud(cloud)
        report['events']=audit.events;report['finished_at']=datetime.now(timezone.utc).isoformat()
        report['synthetic_camera_timing']={'frames':backend.camera_id,
            'maximum_frame_gap_seconds':max(backend.frame_gaps,default=0),
            'maximum_render_seconds':max(backend.render_times,default=0)}
        report['cleanup']={'synthetic_camera_thread_finished':not backend.camera_thread.is_alive(),
                           'no_cloud_stream_left_open':settlement['http_workers_settled'],
                           'cloud_http':settlement,'gateway_result_cleanup_recorded':bool(report['stages'])}
        if not settlement['http_workers_settled']:report['ok']=False
        text=json.dumps(audit.clean(report),ensure_ascii=False,indent=2,allow_nan=False)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        # Exclusive creation: every requested invocation has its own receipt.
        with args.output.open('x',encoding='utf-8') as output:output.write(text+'\n')
    print(json.dumps({'ok':report['ok'],'output':str(args.output.resolve()),'checks':report.get('checks'),
                      'failure':report.get('failure')},ensure_ascii=True))
    return 0 if report['ok'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--real',action='store_true');mode.add_argument('--offline',action='store_true')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--api-budget-rmb',type=float,default=15)
    parser.add_argument('--offline-seed-delay',type=float,default=1.)
    parser.add_argument('--sandbox-image',default='companion-sandbox:0.4')
    parser.add_argument('--photo',type=Path,help='Optional licensed image; transformed photo, never drone footage')
    parser.add_argument('--photo-probe',action='store_true',help='Only observe supplied photo through Vision/Tracker; no planner/flight actions')
    parser.add_argument('--alibaba-credentials',type=Path,default=Path.home()/'Desktop'/'tmp'/'DomesticAiTrials'/'AlibabaCredentials.json')
    parser.add_argument('--doubao-credentials',type=Path,default=Path.home()/'Desktop'/'tmp'/'DoubaoRealtime'/'Credentials.json')
    parsed=parser.parse_args()
    if not math.isfinite(parsed.offline_seed_delay) or not .6<=parsed.offline_seed_delay<=3:
        parser.error('--offline-seed-delay must be .6..3 seconds')
    raise SystemExit(asyncio.run(run(parsed)))
