"""Qwen Realtime text/JPEG visual requests, isolated from the voice session.

Structured output is validated locally, not claimed as server JSON Schema mode.
No aircraft access, tool execution, provider fallback, or automatic retry.
"""
import asyncio
import base64
import hashlib
import json
import math
import threading
import time
import uuid

from AlibabaRealtime import session_update, strict_json
from model_client import accounting_usage


def validate_schema(value, schema):
    """The bounded JSON Schema subset used by navigation observations."""
    if schema is False:
        raise ValueError('unexpected_schema_item')
    if not isinstance(schema, dict):
        raise ValueError('unsupported_visual_schema')
    allowed={'type','properties','required','additionalProperties','anyOf','prefixItems','items',
             'enum','description','minimum','maximum','minItems','maxItems','maxLength'}
    if set(schema)-allowed: raise ValueError('unsupported_visual_schema_keyword')
    if 'anyOf' in schema:
        for option in schema['anyOf']:
            try: validate_schema(value,option); return
            except ValueError: pass
        raise ValueError('visual_schema_anyof')
    kind=schema.get('type')
    valid={'object':isinstance(value,dict),'array':isinstance(value,list),
           'boolean':type(value) is bool,'integer':type(value) is int,
           'number':type(value) in (int,float) and math.isfinite(value),
           'string':isinstance(value,str),'null':value is None}
    if kind not in valid or not valid[kind]: raise ValueError('visual_schema_type')
    if 'enum' in schema and not any(type(value) is type(x) and value==x for x in schema['enum']):
        raise ValueError('visual_schema_enum')
    if isinstance(value,dict):
        properties=schema.get('properties',{})
        if any(key not in value for key in schema.get('required',[])): raise ValueError('visual_schema_required')
        if schema.get('additionalProperties') is False and set(value)-set(properties): raise ValueError('visual_schema_extra')
        for key,item in value.items():
            if key in properties: validate_schema(item,properties[key])
    if isinstance(value,list):
        if not schema.get('minItems',0)<=len(value)<=schema.get('maxItems',10000): raise ValueError('visual_schema_length')
        prefix=schema.get('prefixItems',[])
        for index,item in enumerate(value):
            rule=prefix[index] if index<len(prefix) else schema.get('items')
            if rule is not None: validate_schema(item,rule)
    if isinstance(value,str) and len(value)>schema.get('maxLength',16000): raise ValueError('visual_schema_length')
    if type(value) in (int,float) and not schema.get('minimum',-math.inf)<=value<=schema.get('maximum',math.inf):
        raise ValueError('visual_schema_range')


class AlibabaVisionClient:
    def __init__(self, config, on_usage=None, *, connect=None, timeout_seconds=30, max_output_tokens=512):
        config.validate()
        if not 0<timeout_seconds<=60 or type(max_output_tokens) is not int or not 1<=max_output_tokens<=2048:
            raise ValueError('invalid_alibaba_vision_limits')
        self.config,self.on_usage,self.connect=config,on_usage,connect
        self.timeout_seconds,self.max_output_tokens=timeout_seconds,max_output_tokens
        self.model,self.url=config.model,config.validate()
        self.last_usage={};self._lock=threading.Lock()

    def _prepare(self,messages):
        if not isinstance(messages,list) or not 1<=len(messages)<=8: raise ValueError('invalid_visual_messages')
        instructions=[];texts=[];image=None
        for message in messages:
            if not isinstance(message,dict) or message.get('role') not in ('system','user'):
                raise ValueError('invalid_visual_role')
            content=message.get('content')
            if isinstance(content,str):
                (instructions if message['role']=='system' else texts).append(content)
                continue
            if message['role']!='user' or not isinstance(content,list): raise ValueError('invalid_visual_content')
            for item in content:
                if item.get('type')=='text' and isinstance(item.get('text'),str): texts.append(item['text'])
                elif item.get('type')=='image_url':
                    uri=(item.get('image_url') or {}).get('url')
                    if image is not None or not isinstance(uri,str) or not uri.startswith('data:image/jpeg;base64,') or len(uri)>8_000_000:
                        raise ValueError('invalid_visual_jpeg_input')
                    data=base64.b64decode(uri.split(',',1)[1],validate=True)
                    import cv2
                    import numpy as np
                    frame=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
                    if frame is None: raise ValueError('invalid_visual_jpeg_input')
                    height,width=frame.shape[:2]
                    scale=min(1.,640/width,480/height)
                    if scale<1: frame=cv2.resize(frame,(max(1,int(width*scale)),max(1,int(height*scale))))
                    ok,encoded=cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,80])
                    if not ok: raise ValueError('visual_jpeg_encode_failed')
                    image=encoded.tobytes()
                    if len(base64.b64encode(image))>256*1024: raise ValueError('visual_jpeg_budget')
                else: raise ValueError('unsupported_visual_content')
        system='\n'.join(instructions);text='\n'.join(texts)
        if not text.strip() or len((system+text).encode())>32000: raise ValueError('visual_prompt_budget')
        return system,text,image

    def complete(self,messages): return self._complete(messages,None)

    def complete_structured(self,messages,schema):
        if not isinstance(schema,dict) or schema.get('type')!='object': raise ValueError('structured_schema_must_be_object')
        schema=strict_json(json.dumps(schema,ensure_ascii=False,allow_nan=False))
        if len(json.dumps(schema).encode())>16000: raise ValueError('visual_schema_budget')
        return self._complete(messages,schema)

    def _complete(self,messages,schema):
        try: asyncio.get_running_loop()
        except RuntimeError: pass
        else: raise RuntimeError('visual_sync_client_requires_worker_thread')
        system,text,image=self._prepare(messages)
        if schema is not None:
            system+='\n只输出一个JSON对象，不要Markdown或额外文字，必须满足以下结构：'+json.dumps(schema,ensure_ascii=False)
        request_id=uuid.uuid4().hex;started=time.monotonic()
        base={'request_id':request_id,'provider':'alibaba','model':self.model,'attempt':1,'retried':False,
              'output_validation':'local_json_schema' if schema else 'text','billing_verified':False,
              'image_sha256':hashlib.sha256(image).hexdigest() if image else None}
        usage={}
        if self.on_usage:self.on_usage(dict(base,event='model_request_start',status='started'))
        try:
            result,usage=asyncio.run(self._request(system,text,image))
            if schema is not None:
                if self.on_usage:self.on_usage(dict(base,event='visual_response_received',text=result))
                value=strict_json(result);validate_schema(value,schema)
                # Enforce geometric/business constraints beyond schema typing.
                from vision import Vision, OBSERVATION_SCHEMA
                if schema==OBSERVATION_SCHEMA: Vision.parse_observation(result,require_format=True)
            with self._lock:self.last_usage=usage
        except Exception as exc:
            if self.on_usage:self.on_usage(dict(base,event='model_request_end',status='failed',
                error_type=type(exc).__name__,usage=usage,usage_unknown=not bool(usage),
                duration_ms=round((time.monotonic()-started)*1000,3)))
            raise
        if self.on_usage:self.on_usage(dict(base,event='model_request_end',status='completed',usage=usage,
            usage_unknown=not bool(usage),duration_ms=round((time.monotonic()-started)*1000,3)))
        return result

    async def _request(self,instructions,text,image):
        from websockets.asyncio.client import connect
        connector=self.connect or connect
        async with asyncio.timeout(self.timeout_seconds):
            async with connector(self.config.validate(),additional_headers={'Authorization':'Bearer '+self.config.api_key},
                                 open_timeout=8,close_timeout=1,max_size=2_000_000,max_queue=16,proxy=None) as ws:
                count=0
                async def send(event):
                    await ws.send(json.dumps(dict(event,event_id='Vision'+uuid.uuid4().hex),ensure_ascii=False,allow_nan=False))
                async def receive():
                    nonlocal count
                    count+=1
                    if count>512:raise ValueError('visual_event_budget')
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
                if image is not None:
                    # A silent PCM carrier associates this JPEG with one committed
                    # media item. There is no microphone, ASR or spoken output.
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
                        if image is not None and usage.get('input_tokens_details',{}).get('video_tokens',0)<=0:
                            raise ValueError('visual_image_input_not_confirmed')
                        result=done_text if done_text is not None else ''.join(parts)
                        if not result.strip():raise ValueError('empty_visual_response')
                        return result,usage
