"""Realtime voice event loop. No intermediate STT/LLM/TTS request chain."""
import asyncio
import base64
import json
import os
import time
from urllib.parse import quote

INSTRUCTIONS='''你是AI随行小伙伴，通过耳机用中文简短交流。声音由AI生成。
默认设备为模拟或待验证SDK，始终如实说明执行环境和结果。
用户无需看屏幕或按键确认；语义不清时用语音询问。
你可以调用run_program编写完整Python程序。变量、循环、条件和多步动作均可使用。
容器提供drone代理：get_status(), get_frame(), observe(target), is_following(),
takeoff(), land(), stop_motion(), move_forward/back/left/right/up/down(cm), rotate(degrees),
update_velocity(left_right,forward_back,up_down,yaw), start_follow(), stop_follow()。
程序中无需传owner，宿主网关统一管理控制权。移动cm为20至500整数；速度为-100至100整数百分比。
observe返回visible,center_x,width_fraction,description,frame_id,source,simulated。
视觉结果能直接用于分支和循环；需要新观察时再次调用observe。不要用一次画面推断全程。
未知场景不能根据目标框宽度推断实际距离、避障或已经真正到达。
程序应读取每步结果，可import time适当等待，遇目标丢失应停止。任务有执行时限。
如观察失败、没有新画面、接口失败，不编造成功。只根据工具结果和journal报告动作。
用户说停、别动、降落时优先调用interrupt工具，不等待普通任务结束。
语音打断会中断当前程序，不要自动续飞；先理解用户的新需求。
run_program的completed仅指Python程序结束，仍要检查journal与视觉来源，不能自动宣布导航到达。
这是具备视觉反馈的程序执行框架，未验证真实自主导航；不承诺无法测量的结果。'''
TOOLS=[{'type':'function','name':'run_program','description':'在独立容器中执行完整Python程序，可循环观察并连续调用设备接口',
        'parameters':{'type':'object','properties':{'code':{'type':'string'}},'required':['code'],'additionalProperties':False}},
       {'type':'function','name':'interrupt','description':'立即取消当前程序并停止运动或降落，不等待普通动作结束',
        'parameters':{'type':'object','properties':{'land':{'type':'boolean'}},'required':['land'],'additionalProperties':False}}]

def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def session_config(model,voice):
    return {'type':'session.update','session':{'type':'realtime','model':model,
        'output_modalities':['audio'],'instructions':INSTRUCTIONS,'tools':TOOLS,'tool_choice':'auto',
        'audio':{'input':{'format':{'type':'audio/pcm','rate':24000},
            # 服务端 VAD 只负责切句；是否真的打断播报由客户端 barge_in() 判断，
            # 因此 interrupt_response 固定为 False：否则飞机噪音会被服务端直接掐断播报。
            'turn_detection':{'type':'server_vad',
                              'threshold':_env_float('REALTIME_VAD_THRESHOLD', .6),
                              'prefix_padding_ms':300,
                              'silence_duration_ms':int(_env_float('REALTIME_SILENCE_MS', 500)),
                              'create_response':True,'interrupt_response':False}},
            'output':{'format':{'type':'audio/pcm','rate':24000},'voice':voice}}}}

class RealtimeSession:
    def __init__(self,runner,audio,logger=None):
        self.runner,self.audio=runner,audio
        self.log=logger or (lambda event:None)
        self.send_lock=asyncio.Lock(); self.ws=None
        self.epoch=0; self.response_epoch={}; self.blocked=set(); self.seen=set()
        self.tool_tasks=set(); self.response_active=False; self.pending_response=False
        self.speech_end=None; self.first_audio=True; self.current_response=None
        # 打断防抖：冷却时间 + 麦克风电平门限，防止飞机噪音/耳机灵敏度过高误打断
        self.barge_cooldown=_env_float('BARGE_IN_COOLDOWN_MS', 700)/1000
        self.barge_rms_min=_env_float('BARGE_IN_RMS_MIN', 250)
        self.last_barge_at=0.0
        self.ready=asyncio.Event()
        self.background_error=asyncio.Queue()
    def track(self,task):
        self.tool_tasks.add(task)
        def done(t):
            self.tool_tasks.discard(t)
            if not t.cancelled() and t.exception():
                self.background_error.put_nowait(t.exception())
        task.add_done_callback(done)

    async def send(self,event):
        async with self.send_lock: await self.ws.send(json.dumps(event,ensure_ascii=False))
    async def request_response(self):
        if self.response_active:
            self.pending_response=True
        else:
            self.response_active=True
            await self.send({'type':'response.create'})
    async def call_tool(self,item,epoch):
        name=item.get('name'); call_id=item['call_id']
        try:
            args=json.loads(item.get('arguments','{}'))
            if not isinstance(args,dict): raise ValueError('tool arguments must be object')
            if epoch!=self.epoch:
                result={'ok':False,'status':'cancelled','reason':'stale_model_turn'}
            elif name=='interrupt':
                if set(args)!={'land'} or type(args['land']) is not bool: raise ValueError('invalid land flag')
                self.runner.cancel()
                result=await self.runner.gateway.interrupt(args['land'])
                if not self.runner.running: await self.runner.gateway.finish(False)
            elif name=='run_program':
                if epoch!=self.epoch: result={'ok':False,'status':'cancelled','reason':'stale_model_turn'}
                elif set(args)!={'code'} or not isinstance(args['code'],str): raise ValueError('invalid code argument')
                else: result=await self.runner.run(args['code'])
            else: raise ValueError('unknown tool')
        except Exception as exc:
            result={'ok':False,'status':'failed','reason':str(exc)[:1000]}
        self.log({'event':'tool_result','name':name,'call_id':call_id,'result':result})
        # Complete the call even if cancelled; prevent stale assistant speech after barge-in.
        await self.send({'type':'conversation.item.create','item':{'type':'function_call_output',
            'call_id':call_id,'output':json.dumps(result,ensure_ascii=False)}})
        if epoch==self.epoch: await self.request_response()
    async def barge_in(self):
        now=time.monotonic()
        if now-self.last_barge_at < self.barge_cooldown:
            self.log({'event':'barge_in_ignored','reason':'cooldown',
                      'cooldown_ms':round(self.barge_cooldown*1000)})
            return
        level_getter=getattr(self.audio,'level',None)
        level=level_getter() if callable(level_getter) else None
        if level is not None and level < self.barge_rms_min:
            self.log({'event':'barge_in_ignored','reason':'low_level',
                      'rms':round(level,1),'threshold':self.barge_rms_min})
            return
        self.last_barge_at=now
        # 服务端不再自动打断，这里显式取消当前回答，避免旧音频继续播放。
        if self.response_active:
            try: await self.send({'type':'response.cancel'})
            except Exception: pass
        self.epoch+=1
        self.blocked.update(self.response_epoch)
        self.runner.cancel()
        played=self.audio.speaker.interrupt()
        # Dispatch device interruption before a potentially blocked network send.
        if self.runner.running:
            task=asyncio.create_task(self.runner.gateway.interrupt(False))
            self.track(task)
        if played: await self.send({'type':'conversation.item.truncate',**played})
        self.pending_response=False
        self.log({'event':'barge_in','epoch':self.epoch,'played':played})
    async def handle(self,event):
        typ=event.get('type')
        if typ=='session.updated': self.ready.set()
        elif typ=='input_audio_buffer.speech_started': await self.barge_in()
        elif typ=='input_audio_buffer.speech_stopped':
            self.speech_end=time.monotonic(); self.first_audio=True
        elif typ=='response.created':
            self.response_active=True
            rid=event['response']['id']; self.current_response=rid
            self.response_epoch[rid]=self.epoch
        elif typ=='response.output_audio.delta':
            rid=event.get('response_id')
            if rid in self.blocked or self.response_epoch.get(rid)!=self.epoch: return
            raw=base64.b64decode(event['delta'],validate=True)
            self.audio.speaker.append(event['item_id'],event.get('content_index',0),raw)
            if self.first_audio and self.speech_end is not None:
                self.log({'event':'speech_end_to_first_audio_ms',
                          'value':round((time.monotonic()-self.speech_end)*1000)})
                self.first_audio=False
        elif typ=='response.output_item.done':
            item=event.get('item',{})
            if item.get('type')=='function_call' and item.get('call_id') not in self.seen:
                self.seen.add(item['call_id'])
                epoch=self.response_epoch.get(event.get('response_id'),-1)
                # A stale tool, including interrupt, must not affect a newer request.
                if epoch!=self.epoch: return
                task=asyncio.create_task(self.call_tool(item,epoch))
                self.track(task)
        elif typ=='response.done':
            response=event.get('response',{})
            if response.get('id') != self.current_response: return
            self.response_active=False
            self.log({'event':'response_done','status':response.get('status'),'usage':response.get('usage')})
            if self.pending_response:
                self.pending_response=False
                await self.request_response()
        elif typ=='error':
            # Avoid logging raw request payloads/credentials.
            error=event.get('error',{})
            raise RuntimeError('Realtime API: '+str(error.get('code','unknown'))+': '+str(error.get('message',''))[:300])
    async def stream_input(self):
        await self.ready.wait()
        async for chunk in self.audio.chunks():
            await self.send({'type':'input_audio_buffer.append','audio':base64.b64encode(chunk).decode()})
    async def stream_events(self):
        async for message in self.ws:
            await self.handle(json.loads(message))
    async def run(self):
        from websockets.asyncio.client import connect
        key=os.getenv('OPENAI_API_KEY') or os.getenv('AI_API_KEY','')
        model=os.getenv('REALTIME_MODEL','')
        if not key or not model: raise ValueError('缺少OPENAI_API_KEY和REALTIME_MODEL')
        url='wss://api.openai.com/v1/realtime?model='+quote(model,safe='')
        voice=os.getenv('REALTIME_VOICE','marin')
        await self.runner.check()
        try:
            async with connect(url,additional_headers={'Authorization':'Bearer '+key},max_size=2_000_000,
                               open_timeout=20,ping_interval=20) as ws:
                self.ws=ws
                self.audio.start()
                await self.send(session_config(model,voice))
                receive=asyncio.create_task(self.stream_events())
                sender=asyncio.create_task(self.stream_input())
                errors=asyncio.create_task(self.background_error.get())
                try:
                    done,_=await asyncio.wait([receive,sender,errors],return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        value=task.result()
                        if isinstance(value,BaseException): raise value
                finally:
                    sender.cancel(); receive.cancel(); errors.cancel()
                    await asyncio.gather(sender,receive,errors,return_exceptions=True)
        finally:
            self.audio.close(); self.runner.cancel()
            try:
                await asyncio.wait_for(self.runner.gateway.interrupt(False),5)
            except Exception: pass
            for task in list(self.tool_tasks): task.cancel()
            await asyncio.gather(*list(self.tool_tasks),return_exceptions=True)
            await self.runner.gateway.finish(False)
