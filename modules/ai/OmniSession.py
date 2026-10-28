"""Persistent server-side Qwen session; aircraft authority stays in UnifiedRuntime.

Cloud speech, ASR and response identities never replace SDK control epochs.
No microphone, camera, host exec, provider switch or automatic action retry.
"""
import asyncio
import base64
import json
import time
import traceback
import uuid

from AlibabaRealtime import AlibabaError, normalize_tools, session_update, strict_json, validate_arguments

MAX_RESPONSE_TOKENS = 2048
INVALID_RESPONSE_GRACE_SECONDS = 8

class OmniSession:
    def __init__(self, runtime, config, instructions, tools, send_client, log,
                 *, connect=None, silence_ms=1200):
        self.runtime, self.config = runtime, config
        self.instructions, self.tools = instructions, normalize_tools(tools)
        self.send_client, self.log, self.connect = send_client, log, connect
        self.silence_ms = silence_ms
        self.ws = None
        self.lock = asyncio.Lock()
        self.epoch = 0
        self.input_id = None
        self.committed = False
        self.asr_ready = asyncio.Event()
        self.ready = asyncio.Event()
        self.responses, self.calls = {}, {}
        self.completed_responses = set()
        self.tasks = set()
        self.tool_outputs = {}
        self.closed = False
        self.output_active = False
        self.interrupt_task = None
        self.last_runtime_generation = getattr(runtime, 'generation', 0)
        self.fault = None
        self.audio_started = False
        self.audio_buffer_generation = 0
        self.speech_active = False
        self.input_count = 0
        self.turn_open = False
        self.expect_response = False
        self.stopping = False
        self.land_requested = False
        self.stop_result = None
        self.inputs = {}
        self.input_sequence = 0
        self.accepted_sequence = 0
        self.pending_response = None
        self.active_response = None
        self.cloud_changed = asyncio.Event()
        self.request_lock = asyncio.Lock()
        self.cancel_requests = {}
        self.execution_tasks = set()
        self.task_epochs = {}
        self.blocked_reason = None
        self.invalid_responses = set()

    async def send(self, event, *, guard=None):
        if self.closed or self.ws is None:
            raise RuntimeError('omni_session_closed')
        payload = dict(event, event_id=event.get('event_id') or 'Companion'+uuid.uuid4().hex)
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(raw.encode()) > 2_000_000:
            raise ValueError('omni_event_too_large')
        async with self.lock:
            if self.closed:
                raise RuntimeError('omni_session_closed')
            if guard is not None and not guard():
                return False
            await asyncio.wait_for(self.ws.send(raw), 2)
            return True

    async def audio(self, pcm):
        if not self.ready.is_set() or not isinstance(pcm, bytes) or len(pcm) != 640:
            raise ValueError('omni_requires_20ms_pcm16_16k')
        generation = self.audio_buffer_generation
        await self.send({'type':'input_audio_buffer.append', 'audio':base64.b64encode(pcm).decode()})
        if generation == self.audio_buffer_generation:
            self.audio_started = True

    async def image(self, jpeg):
        def eligible():
            return (self.ready.is_set() and self.audio_started and self.speech_active
                    and not self.committed)
        if not eligible():
            return False
        if not isinstance(jpeg, bytes) or not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9'):
            raise ValueError('omni_image_not_jpeg')
        image = base64.b64encode(jpeg).decode()
        if len(image) > 256*1024:
            raise ValueError('omni_image_budget')
        generation = self.audio_buffer_generation
        return await self.send({'type':'input_image_buffer.append','image':image},
                               guard=lambda:eligible() and generation == self.audio_buffer_generation)

    def _new_input(self, item_id):
        if not self.authority_current():
            raise RuntimeError('omni_runtime_authority_changed')
        if not isinstance(item_id, str) or not 0 < len(item_id) <= 256:
            raise ValueError('omni_missing_input_identity')
        if item_id in self.inputs:
            return  # Repeated VAD notification must not reset a captured input.
        self.input_sequence += 1
        self.inputs[item_id] = dict(sequence=self.input_sequence,committed=False,text=None,accepted=False)
        while len(self.inputs) > 32:
            del self.inputs[next(iter(self.inputs))]
        self.input_id = item_id
        self.committed = False
        self.audio_buffer_generation += 1
        self.audio_started = False
        self.speech_active = True
        # Sound detection is capture metadata only. It grants no action and
        # neither cancels a program nor interrupts the operator's connection.
    def authority_current(self):
        return getattr(self.runtime,'generation',0) == self.last_runtime_generation

    def spawn(self, operation, epoch, *, execution=False):
        task = asyncio.create_task(operation)
        self.tasks.add(task)
        self.task_epochs[task] = epoch
        if execution: self.execution_tasks.add(task)
        task.add_done_callback(self.done)
        return task

    async def accept_input(self, item_id):
        item = self.inputs.get(item_id)
        if (not item or item['accepted'] or not item['committed'] or not item['text']
                or item['sequence'] <= self.accepted_sequence):
            return
        if not self.authority_current(): raise RuntimeError('omni_runtime_authority_changed')
        item['accepted'] = True
        self.accepted_sequence = item['sequence']
        self.input_count += 1
        self.epoch += 1
        self.cloud_changed.set()
        self.turn_open = True
        self.asr_ready = asyncio.Event()
        self.asr_ready.set()
        self.log({'event':'user_text','transport':'alibaba','text':item['text'],'epoch':self.epoch,
                  'input_item_id':item_id})
        await self.send_client({'type':'user_text','text':item['text']})
        await self.send_client({'type':'interrupted'})
        self.spawn(self.request_response(self.epoch),self.epoch)

    async def request_response(self, epoch):
        # No automatic cloud responses: one request remains outstanding until
        # its response.done is received. That barrier binds late created events
        # to their original input instead of borrowing a newer authorization.
        async with self.request_lock:
            deadline = time.monotonic()+8
            while self.pending_response is not None or self.active_response is not None:
                if self.closed or epoch != self.epoch: return
                self.cloud_changed.clear()
                rid = self.active_response
                if rid is not None and rid not in self.cancel_requests.values():
                    event_id = 'Companion'+uuid.uuid4().hex
                    self.cancel_requests[event_id] = rid
                    await self.send({'type':'response.cancel','event_id':event_id})
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    await self.send_client({'type':'state','text':'上一段云端回复尚未结束，本次未执行；语音连接保留。'})
                    self.log({'event':'omni_response_drain_timeout','epoch':epoch})
                    return
                try: await asyncio.wait_for(self.cloud_changed.wait(),remaining)
                except TimeoutError:
                    await self.send_client({'type':'state','text':'上一段云端回复尚未结束，本次未执行；语音连接保留。'})
                    self.log({'event':'omni_response_drain_timeout','epoch':epoch})
                    return
            if self.closed or epoch != self.epoch: return
            if not self.authority_current(): raise RuntimeError('omni_runtime_authority_changed')
            self.pending_response = epoch
            self.expect_response = True
            await self.send({'type':'response.create'})

    async def handle(self, event):
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            raise ValueError('omni_invalid_cloud_event')
        kind = event['type']
        self.log({'event':'omni_cloud_event', 'type':kind, 'epoch':self.epoch})
        if kind in ('conversation.item.created','conversation.item.deleted'):
            item=event.get('item',{})
            item=item if isinstance(item,dict) else {}
            self.log({'event':'omni_conversation_identity','type':kind,'epoch':self.epoch,
                      'item_id':str(item.get('id',event.get('item_id','')))[:256],
                      'item_type':str(item.get('type',''))[:64],
                      'call_id':str(item.get('call_id',''))[:256]})
        if kind == 'conversation.item.input_audio_transcription.failed':
            self.log({'event':'omni_input_discarded','item_id':str(event.get('item_id',''))[:256],
                      'reason':'transcription_failed'})
            return
        if kind == 'error':
            # Keep bounded diagnostics through the shared secret-redacting log.
            # Never retain raw requests, headers or unbounded provider payloads.
            error=event.get('error',{})
            if isinstance(error,dict):
                details={key:str(error[key])[:2000] for key in ('type','code','message','param','event_id')
                         if key in error and isinstance(error[key],(str,int,float,bool))}
                self.log({'event':'omni_cloud_error_details','type':kind,'epoch':self.epoch,'error':details})
                cancelled_id = self.cancel_requests.get(error.get('event_id'))
                if cancelled_id in self.completed_responses:
                    # A cancel can race the final done event. Only the error
                    # causally tied to that already-finished cancellation is benign.
                    self.log({'event':'omni_cancel_already_finished','response_id':cancelled_id})
                    return
            raise RuntimeError('omni_cloud_error')
        if kind == 'session.updated':
            session = event.get('session', {})
            turn = session.get('turn_detection') or {}
            audio = session.get('audio') or {}
            input_format = (audio.get('input') or {}).get('format') or {}
            output_format = (audio.get('output') or {}).get('format') or {}
            if (session.get('model') != self.config.model
                    or turn.get('type') != 'server_vad'
                    or turn.get('silence_duration_ms') != self.silence_ms
                    or turn.get('create_response') is not False
                    or turn.get('interrupt_response') is not False
                    or input_format.get('type') != 'pcm' or input_format.get('sample_rate') != 16000
                    or output_format.get('type') != 'pcm' or output_format.get('sample_rate') != 24000
                    or session.get('max_tokens') != MAX_RESPONSE_TOKENS
                    or (session.get('input_audio_transcription') or {}).get('model') != 'qwen3-asr-flash-realtime'):
                raise RuntimeError('omni_effective_config_unconfirmed')
            self.log({'event':'omni_effective_config','model':self.config.model,
                      'turn_detection':{key:turn[key] for key in
                          ('type','silence_duration_ms','create_response','interrupt_response')},
                      'input_format':{key:input_format[key] for key in ('type','sample_rate')},
                      'output_format':{key:output_format[key] for key in ('type','sample_rate')},
                      'max_tokens':session['max_tokens']})
            self.ready.set()
            await self.send_client({'type':'ready'})
        elif kind == 'input_audio_buffer.speech_started':
            self._new_input(event.get('item_id'))
        elif kind in ('input_audio_buffer.speech_stopped','input_audio_buffer.cleared'):
            if kind.endswith('cleared') or event.get('item_id') in (None,self.input_id):
                self.speech_active = False
                self.audio_started = False
                self.audio_buffer_generation += 1
        elif kind == 'input_audio_buffer.committed':
            item_id = event.get('item_id')
            item = self.inputs.get(item_id)
            if item is None: return
            item['committed'] = True
            if item_id == self.input_id:
                self.committed = True
                self.speech_active = False
                self.audio_started = False
                self.audio_buffer_generation += 1
            await self.accept_input(item_id)
        elif kind == 'conversation.item.input_audio_transcription.completed':
            item_id = event.get('item_id')
            item = self.inputs.get(item_id)
            if item is None or item['accepted']: return
            text = event.get('transcript')
            if not isinstance(text,str) or not text.strip():
                self.log({'event':'omni_input_discarded','item_id':item_id,'reason':'empty_transcription'})
                return
            if len(text) > 4000: raise ValueError('omni_transcription_too_large')
            item['text'] = text.strip()
            await self.accept_input(item_id)
        elif kind == 'response.created':
            response = event.get('response', {})
            rid = response.get('id')
            if not isinstance(rid,str) or not 0 < len(rid) <= 256:
                raise ValueError('omni_unbound_response')
            if rid in self.responses:
                self.log({'event':'omni_duplicate_response_ignored','response_id':rid})
                return
            if len(self.responses) >= 4096: raise ValueError('omni_response_budget')
            if self.pending_response is None or self.active_response is not None:
                self.log({'event':'omni_unbound_response_ignored','response_id':rid})
                self.responses[rid] = None
                return
            self.responses[rid] = self.pending_response
            self.pending_response = None
            self.active_response = rid
            self.cloud_changed.set()
            self.expect_response = False
            self.output_active = self.responses[rid] == self.epoch
        elif kind in ('response.audio.delta','response.audio_transcript.delta','response.text.delta'):
            if not self.current_response(event.get('response_id')):
                return
            if kind == 'response.audio.delta':
                pcm = base64.b64decode(event.get('delta',''), validate=True)
                if len(pcm) % 2 or len(pcm) > 24000*2*2:
                    raise ValueError('omni_invalid_audio_output')
                if pcm: await self.send_client({'type':'audio','audio':base64.b64encode(pcm).decode()})
            else:
                text = event.get('delta')
                if isinstance(text, str) and text:
                    self.log({'event':'reply_text','transport':'alibaba','epoch':self.epoch,
                              'response_id':event.get('response_id'),'text':text[:8000]})
                    await self.send_client({'type':'reply_text','text':text[:8000]})
        elif kind == 'response.function_call_arguments.done':
            self.remember(event, event.get('response_id'))
        elif kind == 'response.done':
            response = event.get('response', {})
            rid = response.get('id')
            self.log_response_end(response)
            current = self.current_response(rid)
            if not current:
                if rid in self.completed_responses:
                    return
                if rid in self.responses:
                    self.completed_responses.add(rid)
                    # Retired tool plans are acknowledged as cancelled, never executed.
                    for item in response.get('output',[]):
                        if isinstance(item,dict) and item.get('type') == 'function_call':
                            cid = item.get('call_id')
                            if isinstance(cid,str) and 0 < len(cid) <= 256:
                                await self.publish_tool(cid,{'ok':False,'status':'cancelled','reason':'superseded_input'})
                self.finish_response(rid)
                return
            status = response.get('status')
            if status in ('cancelled','canceled'):
                self.completed_responses.add(rid)
                self.output_active = False
                self.turn_open = False
                self.finish_response(rid)
                await self.send_client({'type':'state','text':'本轮回复已取消，可以继续提出要求。'})
                return
            if status not in ('completed','incomplete','failed'):
                raise RuntimeError('omni_response_not_completed')
            for item in response.get('output', []):
                if isinstance(item, dict) and item.get('type') == 'function_call':
                    self.remember(item, rid)
            self.completed_responses.add(rid)
            self.finish_response(rid)
            batch = [(cid,c) for cid,c in self.calls.items() if c['response_id'] == rid and not c['scheduled']]
            self.output_active = False
            if status != 'completed' or rid in self.invalid_responses:
                for _,c in batch: c['scheduled'] = True
                reason = 'invalid_tool_arguments' if rid in self.invalid_responses else 'cloud_response_'+status
                self.spawn(self.reject_response(batch,self.epoch,reason),self.epoch)
            elif batch:
                for _,c in batch: c['scheduled'] = True
                self.spawn(self.dispatch(batch,self.epoch),self.epoch,execution=True)
            else:
                self.turn_open = False
                await self.send_client({'type':'state','text':'本轮回复结束'})
            self.log({'event':'model_usage','provider':'alibaba','usage':response.get('usage',{}),
                      'epoch':self.epoch})

    def finish_response(self, rid):
        if rid == self.active_response:
            self.active_response = None
            self.cloud_changed.set()

    def log_response_end(self, response):
        # Capture completion metadata before parsing tools. Do not log full
        # audio/text output or assume the provider always supplies a reason.
        details = response.get('status_details')
        details = details if isinstance(details,dict) else {}
        error = details.get('error',response.get('error'))
        error = error if isinstance(error,dict) else {}
        usage = response.get('usage')
        usage = usage if isinstance(usage,dict) else {}
        self.log({'event':'omni_response_end','response_id':str(response.get('id',''))[:256],
                  'epoch':self.epoch,'status':str(response.get('status',''))[:128],
                  'status_details':{key:str(details[key])[:1000] for key in ('type','reason') if key in details},
                  'finish_reason':str(response.get('finish_reason',''))[:1000],
                  'error':{key:str(error[key])[:1000] for key in ('type','code','message') if key in error},
                  'usage':{key:usage[key] for key in ('input_tokens','output_tokens','total_tokens')
                           if type(usage.get(key)) is int}})

    async def await_invalid_response_end(self, rid, epoch):
        # Keep receiving response.done so the cause of truncation can be saved.
        # A provider that never finishes must still trigger existing fail-safe
        # cleanup, rather than leave the aircraft waiting indefinitely.
        await asyncio.sleep(INVALID_RESPONSE_GRACE_SECONDS)
        if not self.closed and epoch == self.epoch and self.current_response(rid):
            self.log({'event':'omni_response_end_missing','response_id':rid,'epoch':epoch})
            raise TimeoutError('omni_invalid_response_end_timeout')

    async def reject_response(self, batch, epoch, reason):
        if self.closed or epoch != self.epoch:
            return
        # Invalid arguments never reached execution: return the error to the
        # model without changing flight state.
        # A corrected tool request still passes normal authority/safety checks.
        self.log({'event':'omni_response_rejected','epoch':epoch,'reason':reason})
        for cid,call in batch:
            if self.closed or epoch != self.epoch:
                return
            result = {'ok':False,'status':'failed','reason':reason,
                      'debug':call.get('argument_error') or 'Response incomplete; no tool in this response was executed.'}
            self.log({'event':'tool_result','transport':'alibaba','epoch':epoch,'id':cid,'result':result})
            await self.publish_tool(cid,result)
        if self.closed or epoch != self.epoch or not self.authority_current():
            return
        if batch:
            await self.send_client({'type':'state','text':'工具参数无效或响应不完整，本次工具未执行；错误已返回模型处理。'})
            await self.request_response(epoch)
        else:
            self.turn_open = False
            await self.send_client({'type':'state','text':'云端响应未完成，未收到可执行的工具调用，请重新提出要求。'})

    def current_response(self, response_id):
        return (not self.closed and isinstance(response_id,str)
                and self.responses.get(response_id) == self.epoch
                and response_id not in self.completed_responses)

    def remember(self, event, rid):
        if not self.current_response(rid): return
        cid, name, raw = event.get('call_id'), event.get('name'), event.get('arguments')
        schemas = {t['function']['name']:t['function']['parameters'] for t in self.tools}
        if not isinstance(cid,str) or not 0 < len(cid) <= 256 or name not in schemas or not isinstance(raw,str) or len(raw.encode()) > 64000:
            raise ValueError('omni_invalid_tool_request')
        argument_error = None
        try:
            args = strict_json(raw)
            validate_arguments(args, schemas[name])
        except (json.JSONDecodeError, AlibabaError) as exc:
            if str(exc) == 'unsupported_argument_schema':
                raise  # Configuration defects are not recoverable model output.
            args = None
            argument_error = type(exc).__name__+': '+str(exc)[:1000]
        signature = json.dumps([name,raw if argument_error else args],sort_keys=True,ensure_ascii=False,allow_nan=False)
        if cid in self.calls:
            if self.calls[cid]['signature'] != signature or self.calls[cid]['response_id'] != rid:
                raise ValueError('omni_tool_identity_reused')
            return
        if len(self.calls) >= 1000: raise ValueError('omni_tool_budget')
        self.calls[cid] = dict(name=name,args=args,response_id=rid,signature=signature,scheduled=False,
                               argument_error=argument_error)
        if argument_error:
            self.log({'event':'omni_tool_arguments_rejected','response_id':rid,'call_id':cid,
                      'name':name,'error':argument_error,'argument_bytes':len(raw.encode()),
                      'arguments_head':raw[:1000],'arguments_tail':raw[-1000:]})
            if rid not in self.invalid_responses:
                self.invalid_responses.add(rid)
                self.spawn(self.await_invalid_response_end(rid,self.epoch),self.epoch)

    def done(self, task):
        self.tasks.discard(task)
        was_execution = task in self.execution_tasks
        self.execution_tasks.discard(task)
        epoch = self.task_epochs.pop(task,self.epoch)
        if not task.cancelled() and task.exception() is not None:
            if epoch != self.epoch:
                if was_execution:
                    self.blocked_reason = 'retired_program_cleanup_unconfirmed'
                self.log({'event':'omni_retired_task_failed','epoch':epoch,
                          'error_type':type(task.exception()).__name__})
                return
            # Surface errors to run(), never silently discard a failed host tool.
            self.fault = task.exception()
            self.runtime.cancel()

    async def own_interrupt(self, land):
        if not self.authority_current():
            return {'ok':False,'reason':'external_authority_changed'}
        # UnifiedRuntime.interrupt increments generation synchronously before
        # its first await. Predict exactly OUR increment; never adopt a later
        # external F11/F12 generation after an await.
        expected = self.last_runtime_generation+1
        self.last_runtime_generation = expected
        result = await asyncio.wait_for(self.runtime.interrupt(land),3)
        if getattr(self.runtime,'generation',0) != expected:
            return {'ok':False,'reason':'external_authority_changed'}
        return result

    async def prepare_execution(self, epoch, *, land=None):
        previous = [task for task in self.execution_tasks
                    if task is not asyncio.current_task() and not task.done()]
        if land is not None or previous or self.runtime.running:
            try:
                result = await self.own_interrupt(bool(land))
            except TimeoutError:
                self.blocked_reason = 'previous_stop_unconfirmed'
                return {'ok':False,'reason':self.blocked_reason}
            if result.get('ok') is not True:
                self.blocked_reason = 'previous_stop_unconfirmed'
                return result
            # Stop/land is sent before waiting for Docker and the old lease's
            # cleanup. Delayed host workers retain their old SDK epochs.
            if previous:
                try:
                    await asyncio.wait_for(asyncio.shield(asyncio.gather(*previous,return_exceptions=True)),5)
                except TimeoutError:
                    self.blocked_reason = 'previous_cleanup_unconfirmed'
                    return {'ok':False,'reason':self.blocked_reason}
            if self.runtime.running:
                self.blocked_reason = 'previous_program_still_running'
                return {'ok':False,'reason':self.blocked_reason}
            cleanup = getattr(getattr(self.runtime,'runner',None),'last_cleanup',{})
            if cleanup and (cleanup.get('confirmed') is not True or cleanup.get('follow_cleanup_error')):
                self.blocked_reason = 'previous_cleanup_unconfirmed'
                return {'ok':False,'reason':self.blocked_reason}
            if land is not None: self.blocked_reason = None
        if epoch != self.epoch or self.closed:
            return {'ok':False,'status':'cancelled','reason':'superseded_input'}
        if self.blocked_reason and land is None:
            return {'ok':False,'reason':self.blocked_reason}
        if not self.authority_current():
            return {'ok':False,'reason':'external_authority_changed'}
        return result if land is not None else {'ok':True}

    async def publish_tool(self, cid, result):
        if self.closed or cid in self.tool_outputs: return
        from RTCBridge import RTCBridge
        safe = RTCBridge.compact(result,secrets=(self.config.api_key,))
        self.tool_outputs[cid] = safe
        await self.send({'type':'conversation.item.create','item':{
            'type':'function_call_output','call_id':cid,
            'output':json.dumps(safe,ensure_ascii=False,allow_nan=False)}})

    async def dispatch(self, batch, epoch):
        for cid, call in batch:
            execution_generation = None
            if self.closed: return
            if epoch != self.epoch:
                await self.publish_tool(cid,{'ok':False,'status':'cancelled','reason':'superseded_input'})
                continue
            name, args = call['name'], call['args']
            if name == 'interrupt':
                self.land_requested = self.land_requested or args['land']
                await self.send_client({'type':'interrupted'})
                result = await self.prepare_execution(epoch,land=self.land_requested)
            elif name == 'run_program':
                code = args.get('code')
                if not isinstance(code,str) or not 0 < len(code.encode('utf-8')) <= 60000:
                    raise ValueError('omni_invalid_program')
                result = await self.prepare_execution(epoch)
                if result.get('ok') is True:
                    generation = self.last_runtime_generation
                    execution_generation = generation
                    result = await self.runtime.run(code,expected_generation=generation)
            else:
                raise ValueError('omni_tool_not_exposed')
            from RTCBridge import RTCBridge
            safe = RTCBridge.compact(result, secrets=(self.config.api_key,))
            self.log({'event':'tool_result','transport':'alibaba','epoch':epoch,'id':cid,'result':safe})
            if (execution_generation is not None
                    and execution_generation != getattr(self.runtime,'generation',0)):
                safe = {'ok':False,'status':'cancelled','reason':'superseded_input'}
            await self.publish_tool(cid,safe)
        if not self.closed and epoch == self.epoch and self.authority_current():
            await self.request_response(epoch)

    async def run(self):
        from websockets.asyncio.client import connect
        self.fault = None
        connector = self.connect or connect
        async with connector(self.config.validate(), additional_headers={'Authorization':'Bearer '+self.config.api_key},
                             max_size=2_000_000,max_queue=16,open_timeout=8,close_timeout=1,proxy=None) as ws:
            self.ws = ws
            while True:
                event = strict_json(await asyncio.wait_for(ws.recv(),8))
                if event.get('type') == 'session.created': break
                if event.get('type') == 'error': raise RuntimeError('omni_session_create_failed')
            update = session_update(self.config,self.instructions,self.tools,
                                    mode='server_vad',silence_ms=self.silence_ms)
            # A multi-step Python tool call needs more than a short spoken
            # reply's 512-token budget; truncated programs must never run.
            update['session']['max_tokens'] = MAX_RESPONSE_TOKENS
            update['session']['turn_detection'].update(create_response=False,interrupt_response=False)
            await self.send(update)
            event = None
            try:
                async for raw in ws:
                    if self.fault is not None: raise self.fault
                    event = strict_json(raw)
                    await self.handle(event)
                if not self.closed: raise RuntimeError('omni_cloud_disconnected')
            except Exception as exc:
                # Preserve the original processing fault BEFORE __aexit__ closes
                # the socket. Otherwise a concurrent audio send can report only
                # ConnectionClosedOK and hide the cause of our own close.
                self.fault = exc
                self.runtime.cancel()
                details = {'event':'omni_cloud_processing_failed',
                           'error_type':type(exc).__name__, 'error':str(exc)[:2000],
                           'traceback':traceback.format_exc(),
                           'cloud_event_type':event.get('type') if isinstance(event,dict) else None}
                if isinstance(event,dict) and event.get('type') == 'response.function_call_arguments.done':
                    details['tool'] = {key:str(event[key])[:2000] for key in
                        ('response_id','item_id','call_id','name','arguments') if key in event}
                self.log(details)
                raise

    async def close(self):
        if self.closed: return
        self.closed = True
        self.epoch += 1
        self.runtime.cancel()
        for task in tuple(self.tasks): task.cancel()
        await asyncio.gather(*tuple(self.tasks),return_exceptions=True)
        if self.ws is not None:
            await self.ws.close()
