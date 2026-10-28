"""Loopback bridge from the RTC media client to the sole aircraft runtime.

RTC publishes SDK frames, never a PC webcam. Long-lived cloud credentials do
not pass through this bridge. A disconnected/interrupted RTC identity is revoked
and cannot regain aircraft authority by reusing the same bot name.
"""
import asyncio
import base64
import hmac
import json
import math
import os
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

RTC_PROGRAM_MAX_BYTES = 600  # RTC custom-message envelope is limited to 1 KiB.

class RTCBridge:
    def __init__(self, runtime, backend, log, *, heartbeat_seconds=8, max_session_seconds=30000):
        self.runtime, self.backend, self.log = runtime, backend, log
        self.token = secrets.token_urlsafe(32)
        self.heartbeat_seconds = heartbeat_seconds
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
        self.seen = set()
        self.fingerprints = {}
        self.seen_turns = set()
        self.active_call = None
        self.runtime_generation = getattr(runtime, "generation", 0)
        self.reset_pending = False
        self.tasks = set()
        self.program_active = False
        self.last_heartbeat = 0
        self.frame_fault = False
        self.closing = False

    async def start(self):
        from websockets.asyncio.server import serve
        self.server = await serve(self.handle, '127.0.0.1', 0,
                                  max_size=128000, max_queue=8, ping_interval=3, ping_timeout=5)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f'ws://127.0.0.1:{port}/?token={self.token}'
        return self.url

    async def send(self, value):
        if self.client is not None:
            await asyncio.wait_for(self.client.send(json.dumps(value, ensure_ascii=False, allow_nan=False)), 2)

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
        safe = self.compact(result, secrets=(self.token,))
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
        if len(self.revoked) >= 2000:
            raise RuntimeError('rtc_session_budget_exceeded_restart_bridge')
        self.session = identity
        self.session_deadline = time.monotonic() + self.max_session_seconds
        self.runtime_generation = getattr(self.runtime, 'generation', 0)
        self.reset_pending = False
        self.epoch += 1
        self.turn_id = None
        self.seen.clear()
        self.fingerprints.clear()
        self.seen_turns.clear()
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
        if self.frame_fault:
            raise RuntimeError('rtc_camera_unavailable')

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
        if kind == 'heartbeat':
            self.last_heartbeat = time.monotonic()
            return
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
        if kind == 'user_turn':
            identity = message.get('id')
            text = message.get('text')
            if (message.get('completed') is not True or not isinstance(identity, str)
                    or not 0 < len(identity) <= 128 or not isinstance(text, str) or not 0 < len(text) <= 4000):
                raise ValueError('invalid_completed_user_turn')
            if message.get('userId') != self.session['userId']:
                raise ValueError('subtitle_not_from_target_user')
            if identity in self.seen_turns:
                return
            if len(self.seen_turns) >= 2000:
                raise RuntimeError('user_turn_budget_exceeded')
            self.seen_turns.add(identity)
            if self.program_active or self.runtime.running:
                await self.revoke('speech_during_program')
                return
            self.epoch += 1
            self.turn_id = identity
            self.log({'event': 'user_text', 'text': text, 'epoch': self.epoch, 'transport': 'rtc'})
            await self.send({'type': 'turn', 'epoch': self.epoch, 'id': identity})
            return
        if kind == 'tool':
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
                fingerprint = json.dumps(call.get('function'), sort_keys=True, ensure_ascii=False)
                if call['id'] in self.seen:
                    if self.fingerprints[call['id']] != fingerprint:
                        raise RuntimeError('tool_id_reused_with_different_payload')
                    continue
                if len(self.seen) >= 2000:
                    raise RuntimeError('tool_call_budget_exceeded')
                self.seen.add(call['id'])
                self.fingerprints[call['id']] = fingerprint
                function = call.get('function')
                if not isinstance(function, dict):
                    raise ValueError('invalid_tool_function')
                busy = self.program_active or self.runtime.running
                if function.get('name') == 'run_program' and not busy:
                    self.program_active = True  # admission before scheduling
                    self.active_call = (self.epoch, call['id'])
                task = asyncio.create_task(self.call(call, self.epoch, busy, getattr(self.runtime, 'generation', 0)))
                self.tasks.add(task)
                task.add_done_callback(self.task_done)
            return
        if kind == 'interrupted':
            await self.revoke('cloud_barge_in')
            return
        raise ValueError('unknown_rtc_message')

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
            if not isinstance(raw, str) or len(raw.encode()) > 64000:
                raise ValueError('invalid_tool_arguments')
            args = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_json')))
            if not isinstance(args, dict):
                raise ValueError('invalid_tool_arguments')
            if (epoch != self.epoch or self.closing or self.session is None
                    or self.session['sessionId'] in self.revoked or self.frame_fault
                    or runtime_generation != getattr(self.runtime, 'generation', 0)):
                result = {'ok': False, 'status': 'cancelled', 'reason': 'stale_rtc_turn'}
            elif name == 'run_program':
                if (set(args) != {'code'} or not isinstance(args['code'], str)
                        or not 0 < len(args['code'].encode('utf-8')) <= RTC_PROGRAM_MAX_BYTES):
                    size = len(args['code'].encode('utf-8')) if isinstance(args.get('code'), str) else None
                    raise ValueError(f'invalid_program_arguments: code_bytes={size}; max=600; '
                                     'return a shorter complete Python program')
                if busy:
                    result = {'ok': False, 'status': 'busy', 'reason': 'another_program_running'}
                else:
                    result = await self.runtime.run(args['code'], expected_generation=runtime_generation)
            elif name == 'interrupt':
                if set(args) != {'land'} or type(args['land']) is not bool:
                    raise ValueError('invalid_interrupt_arguments')
                result = await self.revoke('model_interrupt', args['land'])
            else:
                raise ValueError('host_tool_not_exposed')
        except Exception as exc:
            result = {'ok': False, 'status': 'failed', 'reason': type(exc).__name__ + ': ' + str(exc)[:250]}
        finally:
            if admitted and self.active_call == (epoch, call['id']):
                self.program_active = False
                self.active_call = None
        safe = self.compact(result, secrets=(self.token,))
        self.log({'event': 'tool_result', 'transport': 'rtc', 'id': call['id'], 'epoch': epoch, 'result': safe})
        # After an interrupt the client must recreate the RTC task, so no old
        # callback can inject a tool reply into the new conversation.
        if epoch == self.epoch and not self.closing:
            await self.send({'type': 'tool_result', 'id': call['id'], 'epoch': epoch, 'result': safe})

    @staticmethod
    def compact(value, *, secrets=()):
        hidden = tuple(secrets) + tuple(v for k, v in os.environ.items() if v and len(v) >= 8
                        and any(word in k.upper() for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')))
        def clean(item, key=''):
            if any(word in key.lower() for word in ('api_key','access_key','authorization','password','secret','token')):
                return '[redacted]'
            if key.lower() in ('image_url','frame','audio','pcm','jpeg'):
                return '[payload omitted]'
            if isinstance(item, dict):
                return {str(k):clean(v,str(k)) for k,v in list(item.items())[:100]}
            if isinstance(item, (list, tuple)):
                return [clean(v) for v in item[:100]]
            if isinstance(item, str):
                if item.startswith('data:'):
                    return '[payload omitted]'
                for secret in hidden:
                    item = item.replace(secret, '[redacted]')
                item = re.sub(r'''data:(?:image|audio)/[^\s"']+''', '[payload omitted]', item)
                return item[:8000]
            if isinstance(item, float) and not math.isfinite(item):
                return None
            if item is None or isinstance(item, (bool,int,float)):
                return item
            return '[object omitted]'
        value = clean(value)
        result = {}
        for key in ('ok', 'simulated'):
            if key in value:
                result[key] = value[key] is True
        for key, limit in (('status',128), ('reason',1000)):
            if key in value:
                result[key] = str(value[key])[:limit]
        result['debug'] = str(value.get('debug',''))[:2000]
        journal = []
        for item in value.get('journal', [])[-12:]:
            if not isinstance(item,dict):
                continue
            entry = {k:v for k,v in item.items() if k in ('method','args','error')}
            detail = item.get('result')
            if isinstance(detail,dict):
                entry['result'] = detail
            journal.append(entry)
        result['journal'] = journal
        if len(json.dumps(result,ensure_ascii=False,allow_nan=False).encode()) > 12000:
            result['journal'] = []
            result['detail_omitted'] = True
        return result

    async def frames(self):
        previous = None
        while True:
            if self.session is not None and self.session_deadline is not None and time.monotonic() >= self.session_deadline:
                await self.revoke('rtc_session_deadline_exceeded')
                raise TimeoutError('rtc_session_deadline_exceeded')
            if time.monotonic() - self.last_heartbeat > self.heartbeat_seconds:
                raise TimeoutError('rtc_client_heartbeat_expired')
            if self.reset_pending:
                await self.revoke('runtime_authority_changed')
            elif (self.session and self.session['sessionId'] not in self.revoked
                    and getattr(self.runtime, 'generation', 0) != self.runtime_generation):
                await self.revoke('runtime_authority_changed')
            packet = await asyncio.wait_for(asyncio.to_thread(self.backend.get_frame), .75)
            stamp = packet.get('timestamp')
            fresh = (not packet.get('stale') and packet.get('frame') is not None
                     and type(stamp) in (int,float) and math.isfinite(stamp) and 0 <= time.time()-stamp <= 1.0)
            if not fresh:
                await self.send({'type': 'invalid_frame', 'reason': 'sdk_frame_expired'})
                if not self.frame_fault:
                    self.frame_fault = True
                    await self.revoke('sdk_frame_expired')
            elif packet.get('frame_id') != previous:
                import cv2
                ok, encoded = cv2.imencode('.jpg', packet['frame'], [cv2.IMWRITE_JPEG_QUALITY, 75])
                if not ok:
                    raise RuntimeError('frame_encoding_failed')
                previous = packet['frame_id']
                self.frame_fault = False
                await self.send({'type':'frame','id':previous,'timestamp':stamp,
                                 'jpeg':base64.b64encode(encoded).decode()})
            await asyncio.sleep(.1)

    async def handle(self, socket):
        supplied = parse_qs(urlsplit(socket.request.path).query).get('token', [''])[0]
        if not hmac.compare_digest(supplied.encode('utf-8'), self.token.encode('utf-8')):
            await socket.close(1008, 'unauthorized')
            return
        if self.client is not None or self.closing:
            await socket.close(1008, 'client_already_connected')
            return
        self.client = socket
        self.last_heartbeat = time.monotonic()
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
                self.program_active = False
                self.active_call = None
                try:
                    await asyncio.wait_for(socket.close(), 2)
                except Exception:
                    pass

    async def close(self):
        if self.closing: return
        self.closing = True
        if self.server:
            self.server.close()
            await self.server.wait_closed()
