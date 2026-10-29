"""Loopback bridge from the RTC media client to the sole aircraft runtime.

RTC publishes SDK frames, never a PC webcam. Long-lived cloud credentials do
not pass through this bridge. A disconnected/interrupted RTC identity is revoked
and cannot regain aircraft authority by reusing the same bot name.
"""
import asyncio
import base64
import hashlib
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
        self.subtitle_round = None
        self.subtitle_text = ""
        self.turn_open = False
        self.token = secrets.token_urlsafe(32)
        if not 0 < max_session_seconds <= 30000:
            raise ValueError('invalid_rtc_session_deadline')
        self.max_session_seconds = max_session_seconds
        self.session_deadline = None
        self.server = self.client = None
        self.url = None
        self.session = None
        self.revoked = set()
        self.epoch = 0
        self.turn_id = None
        self.calls = {}
        self.seen_turns = set()
        self.active_call = None
        self.runtime_generation = getattr(runtime, "generation", 0)
        self.reset_pending = False
        self.tasks = set()
        self.frame_fault = False
        self.closing = False
        self.close_task = None

    async def start(self):
        from websockets.asyncio.server import serve
        self.server = await serve(self.handle, '127.0.0.1', 0,
                                  max_size=2_000_000, max_queue=16, ping_interval=None)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f'ws://127.0.0.1:{port}/?token={self.token}'
        return self.url

    async def send(self, value):
        if self.client is not None:
            await self.client.send(json.dumps(value, ensure_ascii=False, allow_nan=False))

    async def revoke(self, reason, land=False):
        # Invalidate synchronously before any network await. A failing stop
        # result must not leave the client authorized or cleanup half-finished.
        self.epoch += 1
        revoked_epoch = self.epoch
        self.reset_pending = False
        self.turn_id = None
        if self.session:
            self.revoked.add(self.session['sessionId'])
        self.runtime.cancel()
        try:
            result = await asyncio.wait_for(self.runtime.interrupt(land), 3)
        except Exception as exc:
            result = {'ok': False, 'status': 'failed', 'reason': 'interrupt_unconfirmed: ' + type(exc).__name__}
        safe = tool_result(result, secrets=(*getattr(self.log,'secrets',()),self.token))
        try:
            self.log({'event': 'rtc_authority_revoked', 'reason': reason, 'result': safe})
        except Exception:
            pass
        if revoked_epoch == self.epoch:
            try:
                await self.send({'type': 'session_reset_required', 'reason': reason, 'result': safe})
            except Exception:
                pass
        return result

    def bind(self, message):
        keys = ('sessionId', 'roomId', 'taskId', 'botId', 'userId')
        if any(not isinstance(message.get(k), str) or not 0 < len(message[k]) <= 128 for k in keys):
            raise ValueError('invalid_rtc_identity')
        identity = {k: message[k] for k in keys}
        if identity['sessionId'] in self.revoked:
            raise RuntimeError('rtc_identity_revoked')
        if self.session == identity:
            return  # retries cannot reset replay protection or authorize a turn
        if self.session and self.session['sessionId'] not in self.revoked:
            raise RuntimeError('close_previous_rtc_identity_first')
        self.session = identity
        self.session_deadline = time.monotonic() + self.max_session_seconds
        self.runtime_generation = getattr(self.runtime, 'generation', 0)
        self.reset_pending = False
        self.epoch += 1
        self.turn_id = None
        self.calls.clear()
        self.seen_turns.clear()
        self.subtitle_round, self.subtitle_text, self.turn_open = None, "", False
        self.log({'event': 'rtc_session_bound', 'schema_version': 1,
                  'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  **identity, 'epoch': self.epoch, 'hardware_verified': False})

    def current(self, message):
        if self.session is None or self.session['sessionId'] in self.revoked:
            raise RuntimeError('rtc_session_not_authorized')
        if message.get('sessionId') != self.session['sessionId']:
            raise RuntimeError('obsolete_rtc_identity')
        if self.session_deadline is not None and time.monotonic() >= self.session_deadline:
            self.revoked.add(self.session['sessionId'])
            self.runtime.cancel()
            self.epoch += 1
            self.turn_id = None
            self.reset_pending = True
            raise RuntimeError('rtc_session_deadline_exceeded')
        for name in ('roomId', 'taskId', 'botId', 'userId'):
            if name in message and message[name] != self.session[name]:
                raise RuntimeError('rtc_identity_mismatch: ' + name)
        if getattr(self.runtime, 'generation', 0) != self.runtime_generation:
            self.revoked.add(self.session['sessionId'])
            self.epoch += 1
            self.turn_id = None
            self.reset_pending = True
            raise RuntimeError('runtime_authority_changed')

    def task_done(self, task):
        self.tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()  # consume transport/log exceptions explicitly
        if error is not None:
            self.runtime.cancel()
            try:
                self.log({'event': 'rtc_tool_transport_failed', 'reason': type(error).__name__})
            except Exception:
                pass
            recovery = asyncio.create_task(self.revoke('rtc_tool_transport_failed'))
            self.tasks.add(recovery)
            recovery.add_done_callback(self.task_done)

    async def message(self, message):
        if not isinstance(message, dict):
            raise ValueError('invalid_rtc_message')
        kind = message.get('type')
        if kind == 'bind':
            self.bind(message)
            await self.send({'type': 'bound', 'epoch': self.epoch})
            return
        if kind == 'interrupt':
            if type(message.get('land')) is not bool:
                raise ValueError('land_requires_boolean')
            source = message.get('source')
            source = source if source in ('operator', 'client_stop', 'cloud_barge_in',
                'invalid_subtitle_during_turn', 'user_speech_during_turn') else 'unspecified_client'
            detail = message.get('reason')
            try:
                self.log({'event': 'rtc_client_interrupt', 'source': source,
                          'reported_reason': detail[:1000] if isinstance(detail, str) else '',
                          'land': message['land'], 'epoch': self.epoch,
                          'client_reported': True})
            finally:
                reason = 'operator_interrupt' if source in ('operator', 'unspecified_client') else source
                await self.revoke(reason, message['land'])
            return
        try:
            self.current(message)
        except RuntimeError as exc:
            if str(exc) == 'rtc_session_deadline_exceeded':
                # Do not wait for the image pump before issuing the physical
                # zero/priority path when an expired client sends a command.
                await self.revoke('rtc_session_deadline_exceeded')
            raise
        if kind == 'cloud_event':
            await self.cloud_event(message)
            return
        raise ValueError('unknown_rtc_message')

    async def accept_turn(self, message):
        identity = message.get('id')
        text = message.get('text')
        if (message.get('completed') is not True or not isinstance(identity, str)
                or not 0 < len(identity) <= 128 or not isinstance(text, str) or not 0 < len(text) <= 4000):
            raise ValueError('invalid_completed_user_turn')
        if message.get('userId') != self.session['userId']:
            raise ValueError('subtitle_not_from_target_user')
        if identity in self.seen_turns:
            return
        self.seen_turns.add(identity)
        if self.active_call is not None or self.runtime.running:
            await self.revoke('speech_during_program')
            return
        self.epoch += 1
        for previous in self.calls.values(): previous['result'] = None
        self.turn_id = identity
        self.log({'event': 'user_text', 'text': text, 'epoch': self.epoch, 'transport': 'rtc'})
        self.turn_open = True

    async def accept_tools(self, message):
        if message.get('botId') != self.session['botId']:
            raise ValueError('tool_not_from_current_bot')
        if type(message.get('epoch')) is not int or message['epoch'] != self.epoch or self.turn_id is None:
            raise RuntimeError('stale_or_missing_user_turn')
        calls = message.get('calls')
        if not isinstance(calls, list) or not 0 < len(calls) <= 8:
            raise ValueError('invalid_tool_calls')
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get('id'), str) or not 0 < len(call['id']) <= 128:
                raise ValueError('invalid_tool_call_id')
            fingerprint = hashlib.sha256(json.dumps(call.get('function'),sort_keys=True,
                                        ensure_ascii=False).encode('utf-8')).digest()
            previous = self.calls.get(call['id'])
            if previous is not None:
                if previous['fingerprint'] != fingerprint:
                    raise RuntimeError('tool_id_reused_with_different_payload')
                if previous['epoch'] != self.epoch:
                    await self.publish({'type':'tool_result','id':call['id'],'epoch':self.epoch,
                                     'result':{'ok':False,'status':'cancelled','reason':'obsolete_tool_call'}})
                elif (completed := previous['result']) is not None:
                    await self.publish(completed)
                continue
            self.calls[call['id']] = {'fingerprint': fingerprint, 'epoch': self.epoch, 'result': None}
            function = call.get('function')
            if not isinstance(function, dict):
                raise ValueError('invalid_tool_function')
            busy = self.active_call is not None or self.runtime.running
            if function.get('name') == 'run_program' and not busy:
                self.active_call = (self.epoch, call['id'])  # admission before scheduling
            task = asyncio.create_task(self.call(call, self.epoch, busy, getattr(self.runtime, 'generation', 0)))
            self.tasks.add(task)
            task.add_done_callback(self.task_done)

    async def cloud_event(self, message):
        event = message.get('event')
        if not isinstance(event, dict) or not isinstance(event.get('value'), dict):
            raise ValueError('invalid_cloud_event')
        kind, value = event.get('type'), event['value']
        if kind == 'conv':
            code = value.get('Stage', {}).get('Code')
            try: code = int(code)
            except (TypeError, ValueError): code = None
            if code == 4:
                await self.revoke('cloud_barge_in')
            elif code == 5 and self.active_call is None:
                self.turn_open = False
        elif kind == 'subv':
            for item in value.get('data', []):
                if not isinstance(item, dict) or item.get('userId') != self.session['userId']:
                    continue
                text = item.get('text', '')
                text = text.strip() if isinstance(text, str) else ''
                rid = item.get('roundId')
                valid = (type(rid) is int and 0 <= rid <= 9007199254740991
                         and type(item.get('paragraph')) is bool and type(item.get('definite')) is bool)
                if not valid:
                    if text and (self.turn_open or self.active_call is not None):
                        await self.revoke('invalid_subtitle_during_turn')
                    continue
                identity = str(rid)
                if identity in self.seen_turns or self.subtitle_round is not None and rid < self.subtitle_round:
                    continue
                if text and (self.turn_open or self.active_call is not None):
                    await self.revoke('user_speech_during_turn')
                    return
                if rid != self.subtitle_round:
                    self.subtitle_round, self.subtitle_text = rid, ''
                if text:
                    self.subtitle_text = text
                if item['definite'] and item['paragraph'] and self.subtitle_text:
                    await self.accept_turn(dict(self.session, id=identity, text=self.subtitle_text, completed=True))
        elif kind == 'tool':
            await self.accept_tools(dict(self.session, epoch=self.epoch, calls=value.get('tool_calls')))

    async def publish(self, reply):
        if self.result_sink is None:
            raise RuntimeError('rtc_result_sink_not_configured')
        await self.result_sink(dict(self.session), reply['id'], reply['result'])
        try:
            await self.send(reply)
        except Exception as exc:
            # The cloud already has the result; display delivery is optional.
            self.log({'event': 'rtc_display_delivery_failed', 'reason': type(exc).__name__})

    async def call(self, call, epoch, busy, runtime_generation):
        name = call['function'].get('name')
        admitted = name == 'run_program' and not busy
        try:
            raw = call['function'].get('arguments')
            self.log({'event': 'rtc_tool_received', 'id': call['id'], 'epoch': epoch,
                      'name': str(name)[:100],
                      'arguments_preview': raw[:2000] if isinstance(raw, str) else '[non-string]',
                      'arguments_bytes': len(raw.encode('utf-8')) if isinstance(raw, str) else None,
                      'preview_truncated': isinstance(raw, str) and len(raw) > 2000})
            if not isinstance(raw, str):
                raise ValueError('invalid_tool_arguments')
            args = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_json')))
            validate_tool(name, args)
            if (epoch != self.epoch or self.closing or self.session is None
                    or self.session['sessionId'] in self.revoked
                    or runtime_generation != getattr(self.runtime, 'generation', 0)):
                result = {'ok': False, 'status': 'cancelled', 'reason': 'stale_rtc_turn'}
            elif name == 'run_program':
                if busy:
                    result = {'ok': False, 'status': 'busy', 'reason': 'another_program_running'}
                else:
                    result = await self.runtime.run(args['code'], expected_generation=runtime_generation)
            elif name == 'interrupt':
                result = await self.revoke('model_interrupt', args['land'])
        except Exception as exc:
            result = {'ok': False, 'status': 'failed', 'reason': type(exc).__name__ + ': ' + str(exc)[:250]}
        finally:
            if admitted and self.active_call == (epoch, call['id']):
                self.active_call = None
        safe = tool_result(result, secrets=(*getattr(self.log,'secrets',()),self.token))
        self.log({'event': 'tool_result', 'transport': 'rtc', 'id': call['id'], 'epoch': epoch, 'result': safe})
        # After an interrupt the client must recreate the RTC task, so no old
        # callback can inject a tool reply into the new conversation.
        if epoch == self.epoch and not self.closing:
            reply = {'type': 'tool_result', 'id': call['id'], 'epoch': epoch, 'result': safe}
            self.calls[call['id']]['result'] = reply
            await self.publish(reply)

    async def frames(self):
        previous = None
        previous_stamp = None
        pending = None
        try:
            while True:
                if self.session is not None and self.session_deadline is not None and time.monotonic() >= self.session_deadline:
                    await self.revoke('rtc_session_deadline_exceeded')
                    raise TimeoutError('rtc_session_deadline_exceeded')
                if self.reset_pending:
                    await self.revoke('runtime_authority_changed')
                elif (self.session and self.session['sessionId'] not in self.revoked
                        and getattr(self.runtime, 'generation', 0) != self.runtime_generation):
                    await self.revoke('runtime_authority_changed')
                try:
                    if pending is None:
                        pending = asyncio.create_task(asyncio.to_thread(self.device.get_frame))
                    packet = await asyncio.wait_for(asyncio.shield(pending), .75)
                    pending = None
                    stamp, identity = packet.timestamp, packet.frame_id
                    if (packet.stale or packet.frame is None
                            or type(stamp) not in (int,float) or not math.isfinite(stamp)
                            or not 0 <= time.time()-stamp <= 1.0 or type(identity) is not int or identity < 0
                            or previous is not None and (identity < previous or stamp < previous_stamp
                                or identity == previous and stamp != previous_stamp)):
                        raise ValueError('sdk_frame_unavailable')
                    if identity == previous:
                        await asyncio.sleep(.1)
                        continue
                    import cv2
                    ok, encoded = cv2.imencode('.jpg', packet.frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                    if not ok or time.time()-stamp > 1.0:
                        raise RuntimeError('frame_encoding_failed')
                    jpeg = base64.b64encode(encoded).decode()
                except Exception:
                    if pending is not None and pending.done(): pending = None
                    if not self.frame_fault:
                        await self.send({'type':'invalid_frame','reason':'sdk_frame_unavailable'})
                    self.frame_fault = True
                else:
                    previous, previous_stamp = identity, stamp
                    self.frame_fault = False
                    await self.send({'type':'frame','id':identity,'timestamp':stamp,'jpeg':jpeg})
                await asyncio.sleep(.1)
        finally:
            if pending is not None: pending.cancel()

    async def handle(self, socket):
        supplied = parse_qs(urlsplit(socket.request.path).query).get('token', [''])[0]
        if not hmac.compare_digest(supplied.encode('utf-8'), self.token.encode('utf-8')):
            await socket.close(1008, 'unauthorized')
            return
        if self.client is not None or self.closing:
            await socket.close(1008, 'client_already_connected')
            return
        self.client = socket
        self.frame_fault = False
        pump = asyncio.create_task(self.frames())
        receiver = None
        try:
            async def receive():
                async for raw in socket:
                    try:
                        await self.message(json.loads(raw))
                    except Exception as exc:
                        self.log({'event':'rtc_message_rejected','reason':type(exc).__name__+': '+str(exc)[:250]})
                        await self.send({'type':'error','reason':type(exc).__name__+': '+str(exc)[:250]})
            receiver = asyncio.create_task(receive())
            done,_ = await asyncio.wait((pump,receiver),return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except Exception as exc:
            try:
                self.log({'event':'rtc_client_fault','reason':type(exc).__name__ + ': ' + str(exc)[:200]})
            except Exception:
                pass
        finally:
            for task in (pump,receiver):
                if task and not task.done(): task.cancel()
            await asyncio.gather(*(t for t in (pump,receiver) if t),return_exceptions=True)
            try:
                await self.revoke('rtc_client_disconnected')
            finally:
                pending = list(self.tasks)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending,return_exceptions=True)
                self.client = None
                self.session = None
                self.session_deadline = None
                self.active_call = None
                try:
                    await asyncio.wait_for(socket.close(), 2)
                except Exception:
                    pass

    async def close(self):
        async def finish():
            self.closing = True
            if self.server:
                self.server.close()
                await self.server.wait_closed()
            return close_result(close_step('rtc_bridge', True))
        if (self.close_task is None or self.close_task.cancelled()
                or self.close_task.done() and self.close_task.exception() is not None):
            self.close_task = asyncio.create_task(finish())
        return await asyncio.shield(self.close_task)
