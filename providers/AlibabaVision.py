"""Qwen Realtime text/JPEG visual requests, isolated from the voice session.

Structured output is validated locally, not claimed as server JSON Schema mode.
No aircraft access, tool execution, provider fallback, or automatic retry.
"""
import asyncio
import base64
import json
import threading
import time
import uuid

from providers.AlibabaRealtime import session_update, strict_json
from providers.model_client import accounting_usage
from navigation.Observation import DESCRIBE_INSTRUCTIONS, OBSERVE_INSTRUCTIONS, OBSERVATION_SCHEMA


class AlibabaVisionClient:
    image_bounds = (640,480)

    def __init__(self, config, on_usage=None, *, connect=None, timeout_seconds=30, max_output_tokens=512):
        config.validate()
        if not 0<timeout_seconds<=60 or type(max_output_tokens) is not int or not 1<=max_output_tokens<=2048:
            raise ValueError('invalid_alibaba_vision_limits')
        self.config,self.on_usage,self.connect=config,on_usage,connect
        self.timeout_seconds,self.max_output_tokens=timeout_seconds,max_output_tokens
        self.model,self.url=config.model,config.validate()
        self.last_usage={};self._lock=threading.Lock()

    def describe_image(self, jpeg, question):
        return self._complete(jpeg, DESCRIBE_INSTRUCTIONS, question, self.timeout_seconds)

    def observe_image(self, jpeg, target, *, timeout):
        instructions = OBSERVE_INSTRUCTIONS + '\n只输出JSON对象，必须满足以下结构：' + json.dumps(OBSERVATION_SCHEMA,ensure_ascii=False)
        return self._complete(jpeg, instructions, '查找：' + target, min(timeout,self.timeout_seconds))

    def _complete(self,image,system,text,timeout):
        try: asyncio.get_running_loop()
        except RuntimeError: pass
        else: raise RuntimeError('visual_sync_client_requires_worker_thread')
        if len(base64.b64encode(image)) > 256*1024: raise ValueError('visual_jpeg_budget')
        request_id=uuid.uuid4().hex;started=time.monotonic()
        base={'request_id':request_id,'provider':'alibaba','model':self.model,'attempt':1,'retried':False,
              'billing_verified':False}
        usage={}
        if self.on_usage:self.on_usage(dict(base,event='model_request_start',status='started'))
        try:
            result,usage=asyncio.run(self._request(system,text,image,timeout))
            with self._lock:self.last_usage=usage
        except Exception as exc:
            if self.on_usage:self.on_usage(dict(base,event='model_request_end',status='failed',
                error_type=type(exc).__name__,usage=usage,usage_unknown=not bool(usage),
                duration_ms=round((time.monotonic()-started)*1000,3)))
            raise
        if self.on_usage:self.on_usage(dict(base,event='model_request_end',status='completed',usage=usage,
            usage_unknown=not bool(usage),duration_ms=round((time.monotonic()-started)*1000,3)))
        return result

    async def _request(self,instructions,text,image,timeout):
        from websockets.asyncio.client import connect
        connector=self.connect or connect
        async with asyncio.timeout(timeout):
            async with connector(self.config.validate(),additional_headers={'Authorization':'Bearer '+self.config.api_key},
                                 open_timeout=8,close_timeout=1,max_size=2_000_000,max_queue=16,proxy=None) as ws:
                async def send(event):
                    await ws.send(json.dumps(dict(event,event_id='Vision'+uuid.uuid4().hex),ensure_ascii=False,allow_nan=False))
                async def receive():
                    raw=await ws.recv()
                    if not isinstance(raw,str) or len(raw.encode())>2_000_000:raise ValueError('invalid_visual_event')
                    event=strict_json(raw)
                    if not isinstance(event,dict):raise ValueError('invalid_visual_event')
                    if event.get('type')=='error':raise RuntimeError('alibaba_visual_provider_error')
                    return event
                async def until(kind):
                    while True:
                        event=await receive()
                        if event.get('type')==kind:return event
                await until('session.created')
                update=session_update(self.config,instructions,[],mode='manual')
                update['session'].update(modalities=['text'],input_audio_transcription=None,max_tokens=self.max_output_tokens)
                await send(update)
                effective=(await until('session.updated')).get('session',{})
                if (effective.get('modalities')!=['text'] or effective.get('turn_detection') is not None
                        or effective.get('max_tokens')!=self.max_output_tokens):
                    raise RuntimeError('alibaba_visual_effective_config_unconfirmed')
                # This fixed Realtime model associates JPEGs with an audio item.
                await send({'type':'input_audio_buffer.append','audio':base64.b64encode(b'\0\0'*3200).decode()})
                await send({'type':'input_image_buffer.append','image':base64.b64encode(image).decode()})
                await send({'type':'input_audio_buffer.commit'})
                await until('input_audio_buffer.committed')
                await send({'type':'conversation.item.create','item':{'type':'message','role':'user',
                            'content':[{'type':'input_text','text':text}]}})
                while True:
                    event=await until('conversation.item.created');item=event.get('item',{})
                    if (item.get('type')=='message' and item.get('role')=='user'
                            and any(part.get('type')=='input_text' and part.get('text')==text for part in item.get('content',[]))):break
                await send({'type':'response.create'})
                response_id=None;parts=[];done_text=None
                while True:
                    event=await receive();kind=event.get('type')
                    if kind=='response.created':
                        if response_id is not None:raise ValueError('unexpected_second_visual_response')
                        response_id=event.get('response',{}).get('id')
                        if not isinstance(response_id,str) or not response_id:raise ValueError('missing_visual_response_id')
                    elif kind in ('response.text.delta','response.text.done'):
                        if response_id is None or event.get('response_id')!=response_id:raise ValueError('visual_response_identity_mismatch')
                        chunk=event.get('delta' if kind.endswith('delta') else 'text','')
                        if not isinstance(chunk,str):raise ValueError('invalid_visual_text')
                        if kind.endswith('delta'):parts.append(chunk)
                        else:done_text=chunk
                        if sum(map(len,parts))>16000 or done_text is not None and len(done_text)>16000:
                            raise ValueError('visual_output_budget')
                    elif kind=='response.function_call_arguments.done':raise ValueError('visual_tools_forbidden')
                    elif kind=='response.done':
                        response=event.get('response',{})
                        if response_id is None or response.get('id')!=response_id or response.get('status')!='completed':
                            raise ValueError('visual_response_not_completed')
                        if any(item.get('type')=='function_call' for item in response.get('output',[])):
                            raise ValueError('visual_tools_forbidden')
                        usage=accounting_usage(response.get('usage',{}))
                        result=done_text if done_text is not None else ''.join(parts)
                        if not result.strip():raise ValueError('empty_visual_response')
                        return result,usage
