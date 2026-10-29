"""RTC tool events to the sole runtime; subtitles are display data, never admission."""
import asyncio
import base64
import hmac
import json
import math
import secrets
import time
from urllib.parse import parse_qs, urlsplit
from app.Results import close_result, close_step, tool_result
from app.Tools import validate_tool


class RTCBridge:
    def __init__(self, runtime, device, log, *, max_session_seconds=30000, result_sink=None):
        self.runtime, self.device, self.log = runtime, device, log
        self.result_sink = result_sink
        self.max_session_seconds = max_session_seconds
        self.token = secrets.token_urlsafe(32)
        self.server = self.client = self.session = self.url = None
        self.session_deadline = None
        self.revoked = False
        self.closing = False
        self.close_task = None
        self.land_requested = False
        self.epoch = 0
        self.runtime_generation = runtime.generation
        self.calls = set()
        self.tasks = set()
        self.execution_task = None
        self.blocked_reason = None
        self.finished = asyncio.Event()
        self.failure = None
        self.pending_frame = None

    async def start(self):
        from websockets.asyncio.server import serve
        self.server = await serve(self.handle, '127.0.0.1', 0, max_size=2_000_000,
                                  max_queue=16, ping_interval=None)
        self.url = f'ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/?token={self.token}'
        return self.url

    async def send(self, value):
        if self.client is not None:
            await self.client.send(json.dumps(value, ensure_ascii=False, allow_nan=False))

    def bind(self, message):
        identity = {key: message[key] for key in ('sessionId','roomId','taskId','botId','userId')}
        if self.revoked or self.closing: raise RuntimeError('rtc_identity_revoked')
        if self.session is not None and self.session != identity: raise RuntimeError('rtc_identity_already_bound')
        if self.session is not None: return
        self.session = identity
        self.session_deadline = time.monotonic() + self.max_session_seconds
        self.runtime_generation = self.runtime.generation
        self.log({'event':'rtc_session_bound', **identity})

    def current(self, message):
        if self.session is None or self.revoked or self.closing:
            raise RuntimeError('rtc_session_not_authorized')
        if message.get('sessionId') != self.session['sessionId']:
            raise RuntimeError('obsolete_rtc_identity')
        if self.runtime.generation != self.runtime_generation:
            raise RuntimeError('runtime_authority_changed')
        if time.monotonic() >= self.session_deadline:
            raise RuntimeError('rtc_session_deadline_exceeded')

    async def revoke(self, reason, land=False):
        self.revoked = True
        self.epoch += 1
        self.land_requested |= bool(land)
        receipt = self.runtime.begin_interrupt(self.land_requested)
        self.log({'event': 'stop_request_source', 'source': reason,
                  'requested_land': land, 'effective_land': self.land_requested,
                  'generation': self.runtime.generation})
        self.runtime_generation = self.runtime.generation
        try:
            result = await asyncio.wait_for(self.runtime.finish_interrupt(receipt), 3)
        except Exception as exc:
            result = {'ok':False,'reason':'interrupt_unconfirmed: '+type(exc).__name__}
        if result.get('ok') is not True: self.failure = result.get('reason','rtc_stop_unconfirmed')
        try:
            self.log({'event':'rtc_authority_revoked','reason':reason,'result':result})
            try: await self.send({'type':'session_reset_required','reason':reason,'result':result})
            except Exception: pass
        finally:
            self.finished.set()  # Application owns Node/cloud teardown even if the browser has vanished.
        return result

    async def wait(self):
        await self.finished.wait()
        if self.failure: raise RuntimeError(self.failure)

    async def message(self, message):
        kind = message.get('type')
        if kind == 'bind':
            self.bind(message)
            await self.send({'type':'bound','epoch':self.epoch})
            return
        if kind == 'interrupt':
            if type(message.get('land')) is not bool: raise ValueError('land_requires_boolean')
            await self.revoke('operator_interrupt', message['land'])
            return
        self.current(message)
        if kind == 'frame_published':
            pending = self.pending_frame
            if pending is not None:
                cid, after, future = pending
                stamp = message.get('timestamp')
                if (message.get('request_id') == cid and not future.done()
                        and type(stamp) in (int, float) and math.isfinite(stamp)
                        and after < stamp <= time.time()):
                    future.set_result({'status': 'published', 'frame_id': message.get('frame_id'),
                        'timestamp': stamp, 'camera': 'front', 'cloud_verified': False})
        elif kind == 'tool_calls':
            await self.accept_tools(message.get('calls'))
        elif kind == 'cloud_event':
            event = message['event']
            if event['type'] == 'tool':
                await self.accept_tools(event['value'].get('tool_calls'))
            elif event['type'] == 'subv':
                for item in event['value'].get('data', []):
                    text = item.get('text')
                    if isinstance(text, str) and text:
                        self.log({'event':'user_text' if item.get('userId') == self.session['userId'] else 'reply_text',
                                  'transport':'rtc','text':text,'completed':bool(item.get('definite'))})
            else:
                self.log({'event':'rtc_cloud_event','type':event['type'],'value':event['value']})

    async def accept_tools(self, calls):
        if not isinstance(calls, list) or not calls: raise ValueError('invalid_tool_calls')
        batch = []
        for call in calls:
            cid = call['id']
            if not isinstance(cid,str) or not cid: raise ValueError('invalid_tool_call_id')
            if cid in self.calls: continue
            self.calls.add(cid)  # Reserve before scheduling; never replay motion on duplicate delivery.
            function = call['function']
            raw = function.get('arguments')
            self.log({'event':'rtc_tool_received','id':cid,'name':function.get('name'),
                      'arguments_raw':raw,'epoch':self.epoch})
            try:
                args = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_json')))
                validate_tool(function.get('name'), args)
                batch.append((cid,function['name'],args,None))
            except (ValueError,TypeError) as exc:
                batch.append((cid,function.get('name'),None,{'ok':False,'status':'failed',
                              'reason':type(exc).__name__+': '+str(exc)}))
        if not batch: return
        # Malformed calls report errors without stopping an otherwise valid running task.
        executable = any(args is not None for _,_,args,_ in batch)
        previous = self.execution_task
        receipt = None
        if executable:
            self.epoch += 1
            stops = [args['land'] for _,name,args,_ in batch if name == 'interrupt' and args is not None]
            self.land_requested |= any(stops)
            if stops or previous is not None and not previous.done() or self.runtime.running:
                receipt = self.runtime.begin_interrupt(self.land_requested, expected_generation=self.runtime_generation)
                if receipt is None: raise RuntimeError('runtime_authority_changed')
                self.log({'event': 'stop_request_source', 'source': 'model_interrupt' if stops else 'superseding_program',
                          'tool_call_ids': [cid for cid, _, _, _ in batch],
                          'effective_land': self.land_requested, 'generation': receipt.generation})
                self.runtime_generation = receipt.generation
        task = asyncio.create_task(self.dispatch(batch,self.epoch,receipt,previous))
        if executable: self.execution_task = task
        self.tasks.add(task)
        task.add_done_callback(self.task_done)

    def task_done(self, task):
        self.tasks.discard(task)
        if task.cancelled(): return
        error = task.exception()
        if error is not None:
            self.log({'event':'rtc_tool_transport_failed','reason':str(error)})
            self.failure = 'rtc_tool_transport_failed: '+str(error)
            self.revoked = True
            self.runtime.cancel()
            self.finished.set()

    async def dispatch(self, batch, epoch, receipt, previous):
        prepared = {'ok':True}
        if receipt is not None:
            try:
                prepared = await asyncio.wait_for(self.runtime.finish_interrupt(receipt),3)
                if previous is not None and not previous.done():
                    await asyncio.wait_for(asyncio.shield(previous),5)
            except TimeoutError:
                prepared = {'ok':False,'reason':'previous_cleanup_unconfirmed'}
            if prepared.get('ok') is not True:
                self.blocked_reason = prepared.get('reason','previous_stop_unconfirmed')
            elif any(name == 'interrupt' for _,name,args,_ in batch if args is not None):
                self.blocked_reason = None
        for cid,name,args,error in batch:
            generation = self.runtime_generation
            if self.revoked or self.closing or epoch != self.epoch or self.runtime.generation != generation:
                result = {'ok':False,'status':'cancelled','reason':'superseded_input'}
            elif error is not None: result = error
            elif not prepared.get('ok'): result = prepared
            elif name == 'interrupt': result = prepared
            elif self.blocked_reason:
                result = {'ok':False,'status':'failed','reason':self.blocked_reason}
            else:
                try: result = await self.runtime.run(args['code'],expected_generation=generation)
                except Exception as exc:
                    result = {'ok':False,'status':'failed','reason':type(exc).__name__+': '+str(exc)}
            if epoch != self.epoch or self.runtime.generation != generation:
                result = {'ok':False,'status':'cancelled','reason':'superseded_input'}
            if self.revoked or self.closing: return
            safe = tool_result(result,secrets=(*getattr(self.log,'secrets',()),self.token))
            if (name == 'run_program' and safe.get('status') != 'cancelled'
                    and any(row.get('method') in ('takeoff', 'land', 'rotate', 'update_velocity',
                        'move_forward', 'move_back', 'move_left', 'move_right', 'move_up', 'move_down',
                        'start_follow', 'stop_follow', 'stop_motion') for row in result.get('journal', []))):
                safe['camera_observation'] = await self.post_action_frame(cid)
            if epoch != self.epoch or self.runtime.generation != generation:
                safe = {'ok':False,'status':'cancelled','reason':'superseded_input'}
            if self.revoked or self.closing: return
            self.log({'event':'tool_result','transport':'rtc','id':cid,'epoch':epoch,'result':safe})
            if self.result_sink is None: raise RuntimeError('rtc_result_sink_not_configured')
            await self.result_sink(dict(self.session),cid,safe)
            try: await self.send({'type':'tool_result','id':cid,'epoch':epoch,'result':safe})
            except Exception: pass  # The authoritative cloud delivery already completed.

    async def post_action_frame(self, call_id):
        """Wait for a new frame submitted to RTC, not a guessed cloud receipt."""
        after = time.time()
        future = asyncio.get_running_loop().create_future()
        pending = self.pending_frame = (call_id, after, future)
        try:
            await self.send({'type': 'frame_request', 'request_id': call_id, 'after': after})
            result = await asyncio.wait_for(future, 2)
        except (TimeoutError, OSError):
            result = {'status': 'unavailable', 'camera': 'front', 'cloud_verified': False,
                      'reason': 'post_action_frame_not_confirmed; wait for a new image before visual decisions'}
        finally:
            if self.pending_frame is pending: self.pending_frame = None
        self.log({'event': 'post_action_frame', 'call_id': call_id, 'after': after, **result})
        return result

    async def frames(self):
        previous = None
        pending = None
        unavailable = False
        try:
            while not self.closing and not self.revoked:
                if self.session is not None:
                    if self.runtime.generation != self.runtime_generation or time.monotonic() >= self.session_deadline:
                        await self.revoke('runtime_authority_changed_or_expired')
                        return
                try:
                    if pending is None: pending = asyncio.create_task(asyncio.to_thread(self.device.get_frame))
                    packet = await asyncio.wait_for(asyncio.shield(pending),.75)
                    pending = None
                    if (packet.stale or packet.frame is None or not math.isfinite(packet.timestamp)
                            or not 0 <= time.time()-packet.timestamp <= 1):
                        raise ValueError('sdk_frame_unavailable')
                    if packet.frame_id == previous:
                        await asyncio.sleep(.1)
                        continue
                    import cv2
                    ok, encoded = cv2.imencode('.jpg',packet.frame,[cv2.IMWRITE_JPEG_QUALITY,75])
                    if not ok: raise RuntimeError('frame_encoding_failed')
                    await self.send({'type':'frame','id':packet.frame_id,'timestamp':packet.timestamp,
                                     'jpeg':base64.b64encode(encoded).decode()})
                    previous = packet.frame_id
                    unavailable = False
                except Exception:
                    if pending is not None and pending.done(): pending = None
                    if not unavailable: await self.send({'type':'invalid_frame','reason':'sdk_frame_unavailable'})
                    unavailable = True
                await asyncio.sleep(.1)
        finally:
            if pending is not None: pending.cancel()

    async def handle(self, socket):
        supplied = parse_qs(urlsplit(socket.request.path).query).get('token',[''])[0]
        if not hmac.compare_digest(supplied,self.token) or self.client is not None or self.revoked:
            await socket.close(1008,'unauthorized_or_already_connected')
            return
        self.client = socket
        pump = asyncio.create_task(self.frames())
        receiver = None
        try:
            async def receive():
                async for raw in socket:
                    try: await self.message(json.loads(raw))
                    except Exception as exc:
                        self.log({'event':'rtc_message_rejected','reason':str(exc)})
                        await self.send({'type':'error','reason':str(exc)})
            receiver = asyncio.create_task(receive())
            done,_ = await asyncio.wait((pump,receiver),return_when=asyncio.FIRST_COMPLETED)
            for task in done: await task
        finally:
            await self.revoke('rtc_client_disconnected',self.land_requested)
            for task in (pump,receiver,*tuple(self.tasks)):
                if task is not None: task.cancel()
            await asyncio.gather(*(task for task in (pump,receiver,*tuple(self.tasks)) if task is not None),
                                 return_exceptions=True)
            self.client = None

    async def close(self):
        async def finish():
            self.closing = True
            self.finished.set()
            if self.server is not None:
                self.server.close()
                await self.server.wait_closed()
            return close_result(close_step('rtc_bridge',True))
        if self.close_task is None: self.close_task = asyncio.create_task(finish())
        return await asyncio.shield(self.close_task)
