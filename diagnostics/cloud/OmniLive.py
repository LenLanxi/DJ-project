"""Opt-in production loopback/Omni test with prerecorded media and a fake device.

--real explicitly permits the configured cloud. --offline uses only a local
protocol fixture. Both paths execute allowed status queries in real Docker.
No browser, microphone, speaker, camera, aircraft, retry or provider fallback.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
import sys
import time
import unicodedata
from urllib.request import urlopen
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from providers.AlibabaRealtime import AlibabaConfig, strict_json
from AlibabaProbe import PcmActivity
from RealtimeFixtures import fixture, FOLDER
from execution.sandbox_runner import SandboxRunner
from app.Runtime import UnifiedRuntime
from app.SessionLog import SessionLog
from app.Tools import OMNI_INSTRUCTIONS, OMNI_TOOLS
from providers.OmniRuntime import OmniService
from drone_sdk.contract import FrameResult
from QueryProgram import QueryDevice, QUERY_METHODS


class Audit:
    clean = SessionLog.clean
    def __init__(self, key):
        self.secrets = (key,)
        self.origin = time.perf_counter()
        self.events = []
        self.turn = None
        self.effective = None
    def stamp(self): return round((time.perf_counter()-self.origin)*1000, 3)
    def __call__(self, event):
        value = self.clean(dict(event, monotonic_ms=self.stamp()))
        self.events.append(value)
        turn = self.turn
        if turn is None: return
        if event.get('event') == 'omni_cloud_event' and event.get('type') == 'input_audio_buffer.speech_started':
            turn['speech_started_ms'] = self.stamp()
            turn['speech_started_count'] += 1
            turn['_speech'].set()
        if event.get('event') == 'program_result': turn['program_results'].append(value['result'])
        if event.get('event') == 'cloud_tool_output_sent':
            turn['tool_result_sent_ms'] = self.stamp()
            turn['_post_activity'] = PcmActivity()
        if event.get('event') == 'cloud_image_sent': turn['images_to_model'] += 1
    def function(self, item, response_id, source):
        arguments = item.get('arguments')
        evidence = {'event':'cloud_function_call_received','source':source,
                    'response_id':response_id,'call_id':item.get('call_id'),'name':item.get('name')}
        if isinstance(arguments,str):
            raw = arguments.encode('utf-8')
            retained = raw[:64000].decode('utf-8',errors='ignore')
            evidence.update(arguments_total_utf8=len(raw),arguments_retained_utf8=len(retained.encode()),
                            arguments_truncated=len(raw)>64000)
            if len(retained) <= 16000: evidence['arguments'] = retained
            else:
                # SessionLog bounds each string at 16k characters. Preserve a
                # reconstructable bounded argument rather than silently losing
                # a valid 60k program; every part still passes key redaction.
                evidence['arguments_parts'] = [retained[index:index+16000] for index in range(0,len(retained),16000)]
        else: evidence['arguments_format_invalid'] = True
        self(evidence)


class ObservedCloud:
    def __init__(self, wire, audit): self.wire, self.audit = wire, audit
    async def send(self, raw):
        value = strict_json(raw)
        await self.wire.send(raw)
        if value.get('type') == 'conversation.item.create' and value.get('item',{}).get('type') == 'function_call_output':
            self.audit({'event':'cloud_tool_output_sent','call_id':value['item'].get('call_id')})
        elif value.get('type') == 'input_image_buffer.append':
            self.audit({'event':'cloud_image_sent'})
    async def recv(self):
        raw = await self.wire.recv()
        value = strict_json(raw)
        if value.get('type') == 'response.function_call_arguments.done':
            self.audit.function(value,value.get('response_id'),'response.function_call_arguments.done')
        elif value.get('type') == 'response.done':
            response = value.get('response',{})
            for item in response.get('output',[]):
                if isinstance(item,dict) and item.get('type') == 'function_call':
                    self.audit.function(item,response.get('id'),'response.done')
        turn = self.audit.turn
        if turn is not None:
            if value.get('type') == 'input_audio_buffer.speech_started': turn['speech_item_ids'].append(value.get('item_id'))
            elif value.get('type') == 'input_audio_buffer.committed': turn['committed_item_ids'].append(value.get('item_id'))
            elif value.get('type') == 'conversation.item.input_audio_transcription.completed':
                turn['completed_asr_items'].append({'item_id':value.get('item_id'),'transcript':value.get('transcript')})
        if value.get('type') == 'session.updated':
            session = value.get('session',{})
            turn = session.get('turn_detection')
            self.audit.effective = {key:session.get(key) for key in
                ('model','turn_detection','max_tokens','input_audio_transcription')}
            self.audit.effective['input_format'] = (session.get('audio',{}).get('input') or {}).get('format')
            self.audit.effective['output_format'] = (session.get('audio',{}).get('output') or {}).get('format')
            self.audit({'event':'effective_cloud_session','config':self.audit.effective})
            if not isinstance(turn,dict) or turn.get('type') != 'server_vad' or turn.get('silence_duration_ms') != 1200:
                raise RuntimeError('effective_server_vad_1200_not_confirmed')
        return raw
    def __aiter__(self): return self
    async def __anext__(self):
        try: return await self.recv()
        except Exception as exc:
            from websockets.exceptions import ConnectionClosedOK
            if isinstance(exc,ConnectionClosedOK): raise StopAsyncIteration
            raise
    async def close(self): await self.wire.close()


def observed_connector(audit, offline_url=None):
    @asynccontextmanager
    async def connect(url, **kwargs):
        from websockets.asyncio.client import connect as websocket
        if offline_url is not None:
            url = offline_url
            kwargs.pop('additional_headers',None)  # Even the fake key stays local.
        async with websocket(url,**kwargs) as wire:
            yield ObservedCloud(wire,audit)
    return connect


class OfflineCloud:
    """Protocol fixture only: its text and tone are not cloud ASR/TTS evidence."""
    def __init__(self): self.server = None
    async def start(self):
        from websockets.asyncio.server import serve
        self.server = await serve(self.handle,'127.0.0.1',0,max_size=2_000_000)
        return 'ws://127.0.0.1:'+str(self.server.sockets[0].getsockname()[1])
    async def close(self):
        if self.server is not None:
            self.server.close(); await self.server.wait_closed()
    async def handle(self, socket):
        import base64
        async def send(value): await socket.send(json.dumps(value,ensure_ascii=False))
        async def response(rid, text):
            await send({'type':'response.created','response':{'id':rid}})
            await send({'type':'response.audio_transcript.delta','response_id':rid,'delta':text})
            tone = struct.pack('<h',1200)*2400
            await send({'type':'response.audio.delta','response_id':rid,'delta':base64.b64encode(tone).decode()})
            await send({'type':'response.done','response':{'id':rid,'status':'completed','output':[],
                                                         'usage':{'input_tokens':1,'output_tokens':1}}})
        await send({'type':'session.created','session':{'id':'OfflineSession'}})
        active = False; round_id = 0; silence = 0; tool_requested = False
        async for raw in socket:
            value = strict_json(raw)
            if value.get('type') == 'session.update':
                session = dict(value['session'],model='qwen3.8-omni-flash-realtime')
                session['turn_detection'] = dict(session['turn_detection'])
                await send({'type':'session.updated','session':session})
            elif value.get('type') == 'input_audio_buffer.append':
                pcm = base64.b64decode(value['audio'],validate=True)
                voiced = any(abs(item[0]) > 300 for item in struct.iter_unpack('<h',pcm))
                if voiced and not active:
                    round_id += 1; active = True; silence = 0
                    await send({'type':'input_audio_buffer.speech_started','item_id':'Input'+str(round_id)})
                if active:
                    silence = 0 if voiced else silence+1
                    if silence >= 60:
                        active = False
                        await send({'type':'input_audio_buffer.committed','item_id':'Input'+str(round_id)})
                        await send({'type':'conversation.item.input_audio_transcription.completed','item_id':'Input'+str(round_id),
                                    'transcript':'请查询模拟无人机的电量。不要操作飞机。' if round_id == 1 else '画面里的帐篷是什么颜色？只回答颜色。'})
            elif value.get('type') == 'response.create':
                if round_id == 1 and not tool_requested:
                    tool_requested = True
                    item = {'type':'function_call','call_id':'OfflineCall','name':'run_program',
                            'arguments':json.dumps({'code':"import json\ns=drone.get_status()\nprint(json.dumps({'battery':s.get('battery'),'simulated':s.get('simulated')}))"})}
                    await send({'type':'response.created','response':{'id':'OfflineToolPlan'}})
                    await send({'type':'response.done','response':{'id':'OfflineToolPlan','status':'completed','output':[item]}})
                elif round_id == 1:
                    await response('OfflineStatus','这是模拟设备，电量为80%。')
                else: await response('OfflineVision','红色。')


def read_http(url):
    with urlopen(url,timeout=3) as response:
        return response.read(),dict(response.headers)


async def phase(event, receiver, seconds, *, workers=()):
    waiter = asyncio.create_task(event.wait())
    try:
        watched = (receiver, *workers)
        done,_ = await asyncio.wait({waiter,*watched},timeout=seconds,return_when=asyncio.FIRST_COMPLETED)
        for task in watched:
            if task in done:
                await task
                raise RuntimeError('production_phase_worker_closed')
        if waiter not in done: raise TimeoutError('production_phase_deadline')
        await waiter
    finally:
        waiter.cancel(); await asyncio.gather(waiter,return_exceptions=True)


async def play_turn(socket, turn, pcm, metadata, audit):
    import base64
    origin = time.perf_counter(); position = 0; deadline = origin
    while not turn['_stop'].is_set():
        chunk = pcm[position:position+640]
        payload = chunk.ljust(640,b'\0')
        await socket.send(json.dumps({'type':'audio','audio':base64.b64encode(payload).decode()}))
        stamp = audit.stamp()
        if turn.get('audio_started_ms') is None: turn['audio_started_ms'] = stamp
        previous = position; position = min(len(pcm),position+len(chunk))
        turn['packets_sent'] += 1
        if turn.get('speech_end_anchor_ms') is None and previous < metadata['speech_end_samples']*2 <= position:
            turn['speech_end_anchor_ms'] = round(stamp-(position-metadata['speech_end_samples']*2)/32,3)
        if position == len(pcm) and not turn['input_complete']:
            turn['input_complete'] = True; turn['input_complete_ms'] = stamp
        deadline = max(deadline+.02,time.perf_counter())
        await asyncio.sleep(max(0,deadline-time.perf_counter()))


async def receive(socket, audit, ready):
    import base64
    async for raw in socket:
        event = strict_json(raw); kind = event.get('type'); turn = audit.turn
        if kind == 'ready': ready.set(); continue
        if kind == 'error': raise RuntimeError('production_loopback_reported_error')
        if turn is None: continue
        stamp = audit.stamp()
        if kind == 'user_text': turn['transcripts'].append(event.get('text',''))
        elif kind == 'reply_text':
            turn['replies'].append(event.get('text',''))
            if turn.get('first_reply_ms') is None: turn['first_reply_ms'] = stamp
        elif kind == 'audio':
            pcm = base64.b64decode(event.get('audio',''),validate=True)
            if not pcm or len(pcm)%2: raise ValueError('invalid_production_pcm24k')
            audit({'event':'client_audio_chunk','task':turn['task'],'samples':len(pcm)//2,
                   'sample_rate':24000})
            turn['output_audio_bytes'] += len(pcm)
            activity = turn['_activity'].push(pcm,stamp)
            if activity is not None:
                turn['audio_activity'] = activity
                if turn.get('first_nonzero_audio_ms') is None: turn['first_nonzero_audio_ms'] = activity['first_window_received_ms']
            if turn.get('_post_activity') is not None:
                activity = turn['_post_activity'].push(pcm,stamp)
                if activity is not None and turn.get('post_tool_nonzero_audio_ms') is None:
                    turn['post_tool_nonzero_audio_ms'] = activity['first_window_received_ms']
        elif kind == 'state' and event.get('text') == '本轮回复结束':
            turn['completed_ms'] = stamp; turn['_done'].set()
        elif kind == 'frame': turn['browser_frame_count'] += 1


def verify_turn(turn):
    asr = ''.join(turn['transcripts']); reply = ''.join(turn['replies'])
    def normalized(text):
        return ''.join(character for character in unicodedata.normalize('NFKC',text)
                       if unicodedata.category(character).startswith(('L','N')))
    actual = normalized(asr)
    phrases = (['查询','模拟无人机','电量','不要操作飞机'] if turn['task'] == 'status'
               else ['画面','帐篷','什么颜色','只回答颜色'])
    position = 0; complete = True
    for phrase in phrases:
        found = actual.find(phrase,position)
        if found < 0: complete = False; break
        position = found+len(phrase)
    identities = turn['speech_item_ids']
    asr_items = turn['completed_asr_items']
    identity_verified = bool(turn['speech_started_count'] == 1 and len(identities) == 1
        and isinstance(identities[0],str) and identities[0]
        and turn['committed_item_ids'] == identities and len(asr_items) == 1
        and asr_items[0]['item_id'] == identities[0]
        and isinstance(asr_items[0]['transcript'],str)
        and normalized(asr_items[0]['transcript']) == actual)
    actual_rpc = any(result.get('ok') is True and any(entry.get('method') == 'get_status'
        and entry.get('result',{}).get('simulated') is True and entry.get('result',{}).get('battery') == 80
        for entry in result.get('journal',[])) for result in turn['program_results'])
    delivery = bool(turn['input_complete'] and complete and identity_verified and turn.get('speech_started_ms') is not None
                    and turn.get('completed_ms') is not None and turn.get('first_nonzero_audio_ms') is not None)
    if turn['task'] == 'status':
        answer = '80' in reply or '八十' in reply
        required = actual_rpc and turn.get('post_tool_nonzero_audio_ms') is not None
    else:
        answer = '红' in reply and '蓝' not in reply
        required = turn['images_to_model'] > 0 and not turn['program_results']
    turn['verification'] = {'completed_asr':bool(complete and identity_verified),
        'single_speech_started':turn['speech_started_count'] == 1,'asr_identity_chain':identity_verified,
        'asr_complete_keyphrases':complete,'asr_exact_ignoring_punctuation':actual == normalized(turn['fixture']['expected_text']),
        'required_asr_phrases':phrases,'input_fully_sent':turn['input_complete'],
        'real_docker_get_status':actual_rpc,'nonzero_pcm_received':turn.get('first_nonzero_audio_ms') is not None,
        'post_tool_nonzero_audio':turn.get('post_tool_nonzero_audio_ms') is not None,
        'simulation_disclosure_observed':('模拟' in reply or 'simulat' in reply.lower()) if turn['task'] == 'status' else None,
        'core_answer':bool(answer and required),
        'semantic_reply':answer,'delivery_verified':delivery,'required_task_path':bool(required),
        'audio_played':False,'assistant_audio_independently_transcribed':False}
    turn['ok'] = bool(delivery and answer and required and not turn.get('error_type'))
    anchor = turn.get('speech_end_anchor_ms')
    turn['latency_ms'] = {key:round(turn[key]-anchor,3) if anchor is not None and turn.get(key) is not None else None
                         for key in ('first_reply_ms','first_nonzero_audio_ms','tool_result_sent_ms','post_tool_nonzero_audio_ms','completed_ms')}


def persist(path, result, audit):
    result['events'] = audit.events
    result['effective_session'] = audit.effective
    cleaned = audit.clean(result)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(cleaned,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    temporary.replace(path)


async def attempt(config, output, *, real, seconds=60):
    from websockets.asyncio.client import connect
    result = {'schema_version':1,'attempt_id':uuid.uuid4().hex,'started_at':datetime.now(timezone.utc).isoformat(),
        'ok':False,'real_cloud':real,'scope':'production_omni_http_ws_docker_prerecorded_media_fake_device',
        'physical_devices':False,'turns':[],'cleanup':{},'billing_verified':False,'cost_rmb':None,
        'limits':{'session_seconds':seconds,'speech_started_deadline_seconds':8,
                  'no_automatic_retry':True,'no_audio_compression':True},
        'limitations':['No browser permissions, Bluetooth, microphone, playback, camera or aircraft tested.',
            'Assistant text and nonzero PCM are checked; returned audio is not independently transcribed.',
            'Offline mode uses scripted protocol text and a tone; it is not ASR, model or TTS evidence.']}
    audit = Audit(config.api_key)
    offline = OfflineCloud() if not real else None
    device = QueryDevice(FOLDER/'RedTent.jpg')
    sandbox = SandboxRunner(timeout=10)
    runtime = UnifiedRuntime(device,device,log=audit,runner=sandbox,methods=QUERY_METHODS)
    service = socket = receiver = heartbeat = None
    try:
        await sandbox.check()  # Fail locally before any API request.
        offline_url = await offline.start() if offline else None
        service = OmniService(runtime,device,audit,config,OMNI_INSTRUCTIONS,OMNI_TOOLS,
                              seconds=seconds,connect=observed_connector(audit,offline_url))
        url = await service.start()
        persist(output,result,audit)
        static = {}
        for route in ('/','/OmniClient.js','/OmniAudio.js'):
            content,headers = await asyncio.to_thread(read_http,url+route)
            static[route] = {'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest(),
                            'camera_policy':headers.get('Permissions-Policy')}
        result['static_assets'] = static
        raw,_ = await asyncio.to_thread(read_http,url+'/api/session')
        session = strict_json(raw.decode())
        if session.get('ok') is not True or session.get('provider') != 'alibaba': raise ValueError('local_session_contract')
        async with asyncio.timeout(seconds):
            socket = await connect(session['url'],origin=url,max_size=256*1024,proxy=None,close_timeout=1)
            ready = asyncio.Event()
            receiver = asyncio.create_task(receive(socket,audit,ready))
            async def beat():
                while True:
                    await socket.send(json.dumps({'type':'heartbeat'})); await asyncio.sleep(2)
            heartbeat = asyncio.create_task(beat())
            await socket.send(json.dumps({'type':'start'}))
            await phase(ready,receiver,15)
            if audit.effective is None: raise ValueError('missing_effective_session')
            for task,name in (('status','Status'),('vision','Vision')):
                pcm,metadata = fixture(name)
                turn = {'task':task,'fixture':{'name':name,'sha256':hashlib.sha256(pcm).hexdigest(),
                    'bytes':len(pcm),'expected_text':metadata['text'],'synthetic':True},
                    'transcripts':[],'replies':[],'program_results':[],'packets_sent':0,'output_audio_bytes':0,
                    'speech_started_count':0,'speech_item_ids':[],'committed_item_ids':[],'completed_asr_items':[],
                    'images_to_model':0,'browser_frame_count':0,'input_complete':False,
                    '_speech':asyncio.Event(),'_done':asyncio.Event(),'_stop':asyncio.Event(),'_activity':PcmActivity()}
                audit.turn = turn; result['turns'].append(turn)
                sender = asyncio.create_task(play_turn(socket,turn,pcm,metadata,audit))
                completion = None
                try:
                    await phase(turn['_speech'],receiver,8,workers=(sender,))
                    completion = asyncio.create_task(turn['_done'].wait())
                    done,_ = await asyncio.wait({completion,receiver,sender},timeout=30,return_when=asyncio.FIRST_COMPLETED)
                    if receiver in done: await receiver; raise RuntimeError('loopback_receiver_closed')
                    if sender in done: await sender; raise RuntimeError('audio_sender_closed')
                    if completion not in done: raise TimeoutError('omni_turn_completion_timeout')
                    await completion
                    # Let the production dispatch task finish its final await
                    # before feeding the next user utterance (no barge-in).
                    while service.cloud.tasks or service.cloud.turn_open: await asyncio.sleep(.01)
                except BaseException as exc:
                    turn['error_type'] = type(exc).__name__
                    raise
                finally:
                    turn['_stop'].set(); sender.cancel(); await asyncio.gather(sender,return_exceptions=True)
                    if completion is not None:
                        completion.cancel(); await asyncio.gather(completion,return_exceptions=True)
                    for key in ('_speech','_done','_stop','_activity','_post_activity'): turn.pop(key,None)
                    verify_turn(turn); audit.turn = None
                    persist(output,result,audit)
            await socket.send(json.dumps({'type':'stop'}))
            await asyncio.wait_for(service.wait(),8)
            result['ok'] = all(turn['ok'] for turn in result['turns'])
    except BaseException as exc:
        result['error_type'] = type(exc).__name__
    finally:
        audit.turn = None
        for task in (receiver,heartbeat):
            if task is not None: task.cancel()
        await asyncio.gather(*(task for task in (receiver,heartbeat) if task is not None),return_exceptions=True)
        if socket is not None:
            try: await socket.close(); result['cleanup']['local_websocket_closed'] = True
            except Exception: result['cleanup']['local_websocket_closed'] = False
        if service is not None:
            try: await asyncio.wait_for(service.close(),12); result['cleanup']['production_service_closed'] = True
            except Exception: result['cleanup']['production_service_closed'] = False
            result['cleanup']['cloud_closed'] = bool(service.cloud and service.cloud.closed)
            result['cleanup']['stop_confirmed'] = bool(service.stop_result and service.stop_result.get('ok') is True)
            result['cleanup']['http_thread_stopped'] = bool(service.thread and not service.thread.is_alive())
            result['cleanup']['pending'] = service.cleanup_pending
            result['production_failure'] = service.failure
        if offline: await offline.close()
        result['cleanup']['sandbox_not_running'] = not runtime.running
        result['cleanup']['sandbox_last_cleanup'] = await runtime.retry_cleanup()
        result['cleanup']['synthetic_interrupts'] = device.interrupts
        result['cleanup']['physical_stop_verified'] = False
        result['usage'] = [event for event in audit.events if event.get('event') == 'model_usage']
        result['ok'] = bool(result['ok'] and not result.get('error_type')
            and all(result['cleanup'].get(key) is True for key in ('local_websocket_closed','production_service_closed',
                'cloud_closed','stop_confirmed','http_thread_stopped','sandbox_not_running')))
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        result['cloud_media_verified'] = bool(real and result['ok'])
        persist(output,result,audit)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--real',action='store_true')
    mode.add_argument('--offline',action='store_true')
    parser.add_argument('--credentials',type=Path)
    parser.add_argument('--output',type=Path,required=True,help='New JSON file for this one attempt; never overwritten by another invocation.')
    parser.add_argument('--max-seconds',type=int,default=60)
    args = parser.parse_args(argv)
    if not 20 <= args.max_seconds <= 90: parser.error('max-seconds must be 20..90')
    if args.real and args.credentials is None: parser.error('--real requires --credentials')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    # Reserve first: a repeated command must not erase earlier failure evidence.
    with args.output.open('x',encoding='utf-8') as handle:
        json.dump({'ok':False,'phase':'reserved','real_cloud':args.real},handle)
    attempt_started = False
    try:
        config = (AlibabaConfig.from_credentials(args.credentials) if args.real else
                  AlibabaConfig('OfflinePrivateFixtureKey','offline','wss://offline.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime'))
        attempt_started = True
        result = asyncio.run(attempt(config,args.output,real=args.real,seconds=args.max_seconds))
    except Exception as exc:
        result = {'ok':False,'error_type':type(exc).__name__,'real_cloud':args.real,
                  'api_call_may_have_started':bool(args.real and attempt_started)}
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:result.get(key) for key in ('ok','real_cloud','cloud_media_verified','error_type','cleanup')},ensure_ascii=False),flush=True)
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
