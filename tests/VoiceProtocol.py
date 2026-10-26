"""Doubao JSON full duplex, connected to the unified async execution runtime.

Official protocol (checked 2026-10-26):
https://docs.volcengine.com/docs/DoubaoVoice/endtoend-realtime-voice-full-duplex-version?lang=en
The older /api/v3/realtime/dialogue uses a different binary protocol. There is
no automatic protocol/device fallback. Imports never open an audio device.
"""
import array
import asyncio
import base64
import binascii
import inspect
import json
import os
import sys
import time
import uuid
from urllib.parse import urlsplit

DEFAULT_URL = 'wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue'
DEFAULT_MODEL = '1.2.6.1'
DEFAULT_VOICE = 'zh_female_vv_jupiter_bigtts'
INPUT_RATE, OUTPUT_RATE, INPUT_PACKET_MS = 16000, 24000, 20
INPUT_PACKET_BYTES = 640
MAX_PROGRAM_BYTES, MAX_TOOL_CALLS, MAX_EVENT_BYTES = 60000, 2000, 1_000_000
EV_SESSION_CREATE = 'session.create'
EV_SESSION_UPDATE = 'session.update'
EV_SESSION_CLOSE = 'session.close'
EV_AUDIO_APPEND = 'input_audio_buffer.append'
EV_AUDIO_COMMIT = 'input_audio_buffer.commit'
EV_AUDIO_MUTE = 'input_audio_mute.commit'
EV_AUDIO_UNMUTE = 'input_audio_unmute.commit'
EV_ITEM_CREATE = 'conversation.item.create'
EV_RESPONSE_CANCEL = 'response.cancel'
EV_SPEECH_TEXT_COMMIT = 'speech_text_buffer.commit'
EV_SESSION_CREATED = 'session.created'
EV_SESSION_CLOSED = 'session.closed'
EV_AUDIO_COMMITTED = 'input_audio_buffer.committed'
EV_ASR_STARTED = 'conversation.item.input_audio_transcription.started'
EV_ASR_DELTA = 'conversation.item.input_audio_transcription.delta'
EV_ASR_COMPLETED = 'conversation.item.input_audio_transcription.completed'
EV_ASR_FAILED = 'conversation.item.input_audio_transcription.failed'
EV_TEXT_DELTA = 'response.output_text.delta'
EV_TEXT_DONE = 'response.output_text.done'
EV_AUDIO_STARTED = 'response.output_audio.started'
EV_AUDIO_DELTA = 'response.output_audio.delta'
EV_AUDIO_DONE = 'response.output_audio.done'
EV_TOOL_CALL = 'response.function_call_arguments.done'
EV_RESPONSE_DONE = 'response.done'
EV_RESPONSE_CANCELED = 'response.canceled'
EV_ERROR = 'error'
VOICES = ('zh_female_vv_jupiter_bigtts', 'zh_female_xiaohe_jupiter_bigtts',
          'zh_male_yunzhou_jupiter_bigtts', 'zh_male_xiaotian_jupiter_bigtts')


class VolcDialogError(RuntimeError):
    pass


def strict_json(text):
    def invalid(_):
        raise ValueError('nonfinite JSON')
    return json.loads(text, parse_constant=invalid)


def validate_url(url):
    parts = urlsplit(url)
    if parts.path.rstrip('/') == '/api/v3/realtime/dialogue':
        raise VolcDialogError('这是旧版二进制协议端点；请明确配置全双工 '
                              '/api/v3/duplex/realtime/dialogue，禁止自动回退')
    if parts.username or parts.password or parts.query or parts.fragment:
        raise VolcDialogError('端点不得包含凭据、查询参数或片段')
    if parts.hostname in ('127.0.0.1', 'localhost', '::1') and parts.scheme == 'ws':
        return  # Explicit loopback protocol tests only.
    if (parts.scheme != 'wss' or parts.hostname != 'openspeech.bytedance.com'
            or parts.path.rstrip('/') != '/api/v3/duplex/realtime/dialogue'):
        raise VolcDialogError('仅支持官方 wss 全双工端点或显式本地测试服务')


class Downsampler:
    """Streaming linear interpolation, preserving phase across audio chunks."""
    def __init__(self, src=OUTPUT_RATE, dst=INPUT_RATE):
        if src <= 0 or dst <= 0:
            raise ValueError('sample rates must be positive')
        self.step, self.pos = float(src) / float(dst), 0.0
        self.tail = array.array('h')

    def process(self, data):
        if len(data) % 2:
            raise ValueError('PCM 字节数必须为偶数')
        block = array.array('h')
        block.frombytes(data)
        if sys.byteorder != 'little':
            block.byteswap()
        block = self.tail + block
        out = array.array('h')
        pos, total = self.pos, len(block)
        while pos + 1 < total:
            index, frac = int(pos), pos - int(pos)
            out.append(int(block[index] * (1 - frac) + block[index + 1] * frac))
            pos += self.step
        consumed = int(pos)
        self.tail, self.pos = block[consumed:], pos - consumed
        if sys.byteorder != 'little':
            out.byteswap()
        return out.tobytes()


def build_session(*, model=DEFAULT_MODEL, voice=DEFAULT_VOICE, instructions, tools=None):
    if not isinstance(model, str) or not model.strip():
        raise ValueError('model must be configured')
    if voice not in VOICES:
        raise ValueError('未知音色；不会静默切换')
    if not isinstance(instructions, str):
        raise ValueError('instructions must be a string')
    session = {'model': model, 'instructions': instructions,
               'audio': {'input': {'format': {'type': 'pcm', 'rate': INPUT_RATE}},
                         'output': {'format': {'type': 'pcm_s16le', 'rate': OUTPUT_RATE},
                                    'speed': 0, 'loudness': 0, 'voice': voice}}}
    if tools:
        session['tools'] = tools
    return {'type': EV_SESSION_CREATE, 'session': session,
            'extension': {'asr': {}, 'tts': {}, 'dialog': {}}}


class VolcDuplex:
    """runner: async run(code), async interrupt(land=False), sync cancel().

    Untagged output needs completed ASR. After interruption it additionally
    needs a cancellation ack. Old known response IDs stay blocked. Missing
    cancellation acknowledgements therefore fail closed rather than guessing.
    """
    def __init__(self, runner, audio, *, instructions, tools=None, api_key=None,
                 url=None, model=None, voice=None, on_log=None, on_user_text=None,
                 on_reply_text=None, on_speech_started=None, on_usage=None,
                 max_session_seconds=180):
        self.runner, self.audio = runner, audio
        self.instructions, self.tools = instructions, tools or []
        self.api_key = os.getenv('VOLC_API_KEY', '') if api_key is None else api_key
        self.url = url or os.getenv('VOLC_DIALOG_URL') or DEFAULT_URL
        self.model = model or os.getenv('VOLC_MODEL') or DEFAULT_MODEL
        self.voice = voice or os.getenv('VOLC_VOICE') or DEFAULT_VOICE
        self.log = on_log or (lambda event: None)
        self.on_user_text = on_user_text or (lambda text: None)
        self.on_reply_text = on_reply_text or (lambda text: None)
        self.on_speech_started = on_speech_started or (lambda: None)
        self.on_usage = on_usage or (lambda usage: None)
        if not 0 < max_session_seconds <= 1800:
            raise ValueError('session deadline must be within 0..1800 seconds')
        self.max_session_seconds = max_session_seconds
        self.ws, self.session_id, self.response_id = None, None, None
        self.ready, self.send_lock = asyncio.Event(), asyncio.Lock()
        self.downsampler = Downsampler()
        self.epoch = 0
        self.asr_text = self.reply_text = ''
        self.speech_started_at = self.speech_completed_at = None
        self.first_audio_logged = False
        self.tool_tasks, self.seen_calls = set(), set()
        self.background_error = asyncio.Queue()
        self.active_run = None
        self.response_epochs, self.cancelled_responses = {}, set()
        self.input_id, self.round_id = None, None
        self.input_epochs, self.round_epochs = {}, {}
        self.turn_complete = self.await_cancel_ack = self.output_active = False
        self.closing = self.muted = False
        self.stopped_tool_epoch = None

    def check_credentials(self):
        validate_url(self.url)
        if not self.api_key:
            raise VolcDialogError('缺少 VOLC_API_KEY；禁止模拟回复或固定口令降级')
        for method in ('run', 'interrupt', 'cancel'):
            if not callable(getattr(self.runner, method, None)):
                raise VolcDialogError('统一执行器缺少 ' + method)
        build_session(model=self.model, voice=self.voice,
                      instructions=self.instructions, tools=self.tools)

    def headers(self):
        return {'X-Api-Key': self.api_key, 'X-Api-Connect-Id': str(uuid.uuid4())}

    async def send(self, event):
        if self.ws is None:
            raise VolcDialogError('WebSocket 尚未连接')
        payload = dict(event)
        payload.setdefault('event_id', str(uuid.uuid4()))
        async with self.send_lock:
            await self.ws.send(json.dumps(payload, ensure_ascii=False, allow_nan=False))

    def track(self, task):
        self.tool_tasks.add(task)
        def done(item):
            self.tool_tasks.discard(item)
            if not item.cancelled() and item.exception() is not None:
                self.background_error.put_nowait(item.exception())
        task.add_done_callback(done)
        return task

    async def run(self):
        self.check_credentials()
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus
        try:
            async with asyncio.timeout(self.max_session_seconds):
                await self._session(connect)
        except InvalidStatus as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', '?')
            # A server error body could echo credentials, so do not print it.
            raise VolcDialogError(f'全双工握手被拒绝 HTTP {status}；请核对服务权限及端点') from None
        except TimeoutError:
            raise VolcDialogError('语音会话达到时限，已取消任务并关闭音频') from None
        finally:
            await self.close()

    async def _session(self, connect):
        try:
            async with connect(self.url, additional_headers=self.headers(),
                               max_size=MAX_EVENT_BYTES, open_timeout=20,
                               ping_interval=20, close_timeout=3) as ws:
                self.ws = ws
                response = getattr(ws, 'response', None)
                self.log({'event': 'websocket_open', 'url': self.url,
                          'logid': response.headers.get('X-Tt-Logid') if response else None,
                          'model': self.model, 'voice': self.voice,
                          'audio_source': getattr(self.audio, 'source_name', type(self.audio).__name__),
                          'hardware_verified': False})
                await self.send(build_session(model=self.model, voice=self.voice,
                                              instructions=self.instructions, tools=self.tools))
                receiver = asyncio.create_task(self._receive_loop())
                waiter = asyncio.create_task(self.ready.wait())
                tasks = {receiver, waiter}
                try:
                    done, _ = await asyncio.wait(tasks, timeout=20,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if receiver in done:
                        await receiver
                        raise VolcDialogError('服务在建立会话前关闭')
                    if waiter not in done:
                        raise VolcDialogError('20 秒内未收到 session.created')
                    tasks.remove(waiter)
                    sender = asyncio.create_task(self._send_audio())
                    errors = asyncio.create_task(self.background_error.get())
                    tasks.update((sender, errors))
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    if errors in done:
                        raise errors.result()
                    for task in done:
                        await task
                finally:
                    for task in tasks:
                        task.cancel()
                    waiter.cancel()
                    await asyncio.gather(*tasks, waiter, return_exceptions=True)
                    await self.close()
        finally:
            self.audio.close()

    async def close(self):
        if self.closing:
            return
        self.closing = True
        self.epoch += 1
        self.turn_complete = False
        self.audio.speaker.interrupt()
        self.runner.cancel()
        error = None
        try:
            stopped = await asyncio.wait_for(self.runner.interrupt(land=False), 5)
            if not isinstance(stopped, dict) or stopped.get('ok') is not True:
                raise VolcDialogError('执行器未确认停止')
        except Exception as exc:
            error = exc
        pending = list(self.tool_tasks)
        if pending:
            _, running = await asyncio.wait(pending, timeout=5)
            for task in running:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if self.ws is not None:
            try:
                await asyncio.wait_for(self.send({'type': EV_SESSION_CLOSE}), 2)
            except Exception:
                pass
        self.audio.close()
        if error is not None:
            raise VolcDialogError('关闭时停止请求失败：' + type(error).__name__) from error

    async def _send_audio(self):
        self.audio.start()
        pending = bytearray()
        next_packet_at = time.perf_counter()
        async for chunk in self.audio.chunks():
            if self.closing:
                return
            if self.muted:
                pending.clear()
                next_packet_at = time.perf_counter()
                continue
            pending.extend(self.downsampler.process(chunk))
            if len(pending) > INPUT_PACKET_BYTES * 10:
                raise VolcDialogError('上行音频积压超过200ms；停止会话')
            while len(pending) >= INPUT_PACKET_BYTES:
                now = time.perf_counter()
                if now - next_packet_at > .2:
                    raise VolcDialogError('音频发送落后超过200ms；停止会话')
                await asyncio.sleep(max(0, next_packet_at - now))
                pcm = bytes(pending[:INPUT_PACKET_BYTES])
                del pending[:INPUT_PACKET_BYTES]
                await self.send({'type': EV_AUDIO_APPEND,
                                 'audio': base64.b64encode(pcm).decode('ascii')})
                next_packet_at = max(next_packet_at + .02, time.perf_counter())

    async def mute(self, muted=True):
        if type(muted) is not bool:
            raise ValueError('muted must be bool')
        if self.muted != muted:
            self.muted = muted
            await self.send({'type': EV_AUDIO_MUTE if muted else EV_AUDIO_UNMUTE})

    async def cancel_response(self):
        await self.send({'type': EV_RESPONSE_CANCEL})

    async def say_text(self, text):
        """Greeting speech, not a text user query or evidence of voice recognition."""
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError('speech text must be 1..4000 characters')
        await self.send({'type': EV_SPEECH_TEXT_COMMIT, 'text': text})

    def _invalidate(self):
        had_output = self.turn_complete or self.output_active or self.response_id is not None
        active = self.active_run is not None and not self.active_run.done()
        self.epoch += 1
        self.turn_complete = False
        self.audio.speaker.interrupt()
        self.runner.cancel()  # Before any network await.
        if self.response_id:
            self.cancelled_responses.add(self.response_id)
        self.await_cancel_ack = self.await_cancel_ack or had_output or active
        self.response_id, self.output_active = None, False
        return active

    async def emergency(self, land=False):
        if type(land) is not bool:
            raise ValueError('land 必须是布尔值')
        self._invalidate()
        result = await self.runner.interrupt(land=land)
        if self.ws is not None:
            self.track(asyncio.create_task(self.cancel_response()))
        return result

    def _fresh(self, event):
        round_id = event.get('round_id')
        if round_id and self.round_epochs.get(round_id, self.epoch) != self.epoch:
            return False
        response_id = event.get('response_id')
        if response_id is not None:
            if not isinstance(response_id, str) or response_id in self.cancelled_responses:
                return False
            if self.response_epochs.get(response_id, self.epoch) != self.epoch:
                return False
        if not self.turn_complete or self.await_cancel_ack or self.closing:
            return False
        if response_id:
            self.response_epochs[response_id] = self.epoch
            self.response_id = response_id
        return True

    async def _receive_loop(self):
        async for message in self.ws:
            if isinstance(message, bytes):
                raise VolcDialogError('收到二进制事件：不是预期的全双工 JSON 协议')
            try:
                event = strict_json(message)
            except (ValueError, TypeError):
                raise VolcDialogError('服务端返回无效 JSON') from None
            if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                raise VolcDialogError('服务端事件缺少 type')
            await self._handle(event)
            if event['type'] == EV_SESSION_CLOSED:
                return

    async def _handle(self, event):
        kind = event.get('type')
        if kind == EV_SESSION_CREATED:
            self.session_id = (event.get('session') or {}).get('id')
            if not isinstance(self.session_id, str) or not self.session_id:
                raise VolcDialogError('session.created 缺少 session.id')
            self.log({'event': 'session_started', 'session_id': self.session_id})
            self.ready.set()
        elif kind == EV_ASR_STARTED:
            active = self._invalidate()
            self.input_id, self.round_id = event.get('item_id'), event.get('round_id')
            if self.input_id:
                self.input_epochs[self.input_id] = self.epoch
            if self.round_id:
                self.round_epochs[self.round_id] = self.epoch
            self.asr_text = self.reply_text = ''
            self.speech_started_at, self.speech_completed_at = time.monotonic(), None
            self.first_audio_logged = False
            self.on_speech_started()
            self.log({'event': 'speech_started', 'epoch': self.epoch})
            if active:
                self.track(asyncio.create_task(self._interrupt_then_cancel()))
            elif self.await_cancel_ack:
                self.track(asyncio.create_task(self.cancel_response()))
        elif kind == EV_ASR_DELTA:
            if event.get('item_id') and self.input_epochs.get(event['item_id'], self.epoch) != self.epoch:
                return
            delta = event.get('delta', '')
            if not isinstance(delta, str):
                raise VolcDialogError('ASR delta 不是文字')
            # 1.2.6.1 real-service replay: delta contains the current hypothesis,
            # including previous words and later corrections. Do not concatenate.
            self.asr_text = delta
            if len(self.asr_text) > 20000:
                raise VolcDialogError('ASR 文本超限')
        elif kind == EV_ASR_COMPLETED:
            if event.get('item_id') and self.input_epochs.get(event['item_id'], self.epoch) != self.epoch:
                return
            # Current official endpoint sends authoritative final `text`.
            text = event.get('text') or event.get('transcript') or self.asr_text
            self.asr_text = ''
            if not isinstance(text, str):
                raise VolcDialogError('ASR transcript 不是文字')
            self.turn_complete = bool(text.strip())
            self.speech_completed_at = time.monotonic()
            self.log({'event': 'asr_final', 'text': text, 'epoch': self.epoch})
            if text:
                self.on_user_text(text)
        elif kind == EV_ASR_FAILED:
            self.turn_complete = False
            self.log({'event': 'asr_failed', 'error': event.get('error')})
        elif kind == EV_RESPONSE_CANCELED:
            self.await_cancel_ack = False
            self.log({'event': 'response_cancel_ack', 'epoch': self.epoch})
        elif kind in (EV_TEXT_DELTA, EV_TEXT_DONE, EV_AUDIO_STARTED,
                      EV_AUDIO_DELTA, EV_AUDIO_DONE, EV_TOOL_CALL):
            if not self._fresh(event):
                self.log({'event': 'stale_output_dropped', 'type': kind})
                return
            if kind == EV_TEXT_DELTA:
                delta = event.get('delta', '')
                if not isinstance(delta, str):
                    raise VolcDialogError('回复 delta 不是文字')
                self.reply_text += delta
            elif kind == EV_TEXT_DONE:
                text = event.get('text') or self.reply_text
                self.reply_text = ''
                if isinstance(text, str) and text:
                    self.on_reply_text(text)
            elif kind == EV_AUDIO_STARTED:
                self.output_active = True
                self.log({'event': 'tts_started', 'tts_type': event.get('tts_type')})
            elif kind == EV_AUDIO_DELTA:
                try:
                    pcm = base64.b64decode(event.get('delta', ''), validate=True)
                except (ValueError, TypeError, binascii.Error):
                    raise VolcDialogError('无效 Base64 音频') from None
                if len(pcm) % 2:
                    raise VolcDialogError('服务端 PCM16 音频字节数为奇数')
                if pcm:
                    self.output_active = True
                    self.audio.speaker.append(self.response_id or f'turn{self.epoch}', 0, pcm)
                    if not self.first_audio_logged and self.speech_completed_at is not None:
                        self.log({'event': 'asr_completed_to_first_audio_received_ms',
                                  'value': round((time.monotonic() - self.speech_completed_at) * 1000),
                                  'scope': 'server_ASR_completion_to_received_audio_not_audible_latency'})
                        self.first_audio_logged = True
            elif kind == EV_AUDIO_DONE:
                self.output_active = False
                self.log({'event': 'tts_done', 'status_code': event.get('status_code')})
            else:
                self._queue_tools(event)
        elif kind == EV_RESPONSE_DONE:
            usage = event.get('usage') or (event.get('response') or {}).get('usage') or {}
            if not isinstance(usage, dict):
                raise VolcDialogError('usage 必须为对象')
            self.log({'event': 'response_done', 'usage': usage})
            value = self.on_usage(usage)
            if inspect.isawaitable(value):
                await value
        elif kind == EV_ERROR:
            error = event.get('error') or {}
            code = error.get('code') if isinstance(error, dict) else None
            raise VolcDialogError('豆包服务端错误，code=' + str(code or event.get('status_code', 'unknown')))

    def _queue_tools(self, event):
        items = event.get('items')
        if not isinstance(items, list) or len(items) > 20:
            raise VolcDialogError('工具调用 items 必须为至多20项的数组')
        for item in items:
            if not isinstance(item, dict):
                raise VolcDialogError('工具调用项必须为对象')
            call_id = item.get('call_id')
            if not isinstance(call_id, str) or not call_id or len(call_id) > 256:
                raise VolcDialogError('缺少有效工具 call_id')
            if call_id in self.seen_calls:
                self.log({'event': 'duplicate_tool_dropped', 'call_id': call_id})
                continue
            if len(self.seen_calls) >= MAX_TOOL_CALLS:
                raise VolcDialogError('本会话工具调用预算已耗尽')
            self.seen_calls.add(call_id)
            busy = self.active_run is not None and not self.active_run.done()
            task = self.track(asyncio.create_task(self._run_tool(item, self.epoch, busy)))
            if item.get('name') == 'run_program' and not busy:
                self.active_run = task

    async def _interrupt_then_cancel(self):
        await self.runner.interrupt(land=False)
        await self.cancel_response()

    async def _run_tool(self, item, epoch, busy=False):
        call_id, name = item['call_id'], item.get('name')
        try:
            raw = item.get('arguments')
            if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_PROGRAM_BYTES + 2000:
                raise ValueError('arguments 必须是有限长度的 JSON 字符串')
            arguments = strict_json(raw)
            if not isinstance(arguments, dict):
                raise ValueError('参数必须是对象')
            if epoch != self.epoch or self.closing or (name == 'run_program' and self.stopped_tool_epoch == epoch):
                result = {'ok': False, 'status': 'cancelled', 'reason': 'stale_turn'}
            elif name == 'run_program' and busy:
                result = {'ok': False, 'status': 'busy', 'reason': 'another_program_running'}
            else:
                result = await self._dispatch(name, arguments)
                if epoch != self.epoch and name != 'interrupt':
                    result = {'ok': False, 'status': 'cancelled', 'reason': 'stale_turn',
                              'execution_result': result}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = str(exc).replace(self.api_key, '[redacted]') if self.api_key else str(exc)
            result = {'ok': False, 'status': 'failed',
                      'reason': type(exc).__name__ + ': ' + detail[:500]}
        self.log({'event': 'tool_result', 'name': name, 'call_id': call_id,
                  'epoch': epoch, 'result': result})
        if self.closing or epoch != self.epoch:
            return
        await self.send({'type': EV_ITEM_CREATE, 'items': [{
            'call_id': call_id, 'role': 'tool',
            'content': [{'type': 'input_text',
                         'text': json.dumps(result, ensure_ascii=False, allow_nan=False)}]}]})
        self.log({'event': 'tool_result_sent', 'call_id': call_id, 'epoch': epoch})

    async def _dispatch(self, name, arguments):
        if name == 'run_program':
            if set(arguments) != {'code'}:
                raise ValueError('run_program 只接受 code')
            code = arguments['code']
            if not isinstance(code, str) or not code.strip() or len(code.encode('utf-8')) > MAX_PROGRAM_BYTES:
                raise ValueError('code 必须为1..60000字节的程序')
            result = await self.runner.run(code)
        elif name == 'interrupt':
            if set(arguments) != {'land'} or type(arguments['land']) is not bool:
                raise ValueError('interrupt 只接受布尔值 land')
            self.runner.cancel()
            self.stopped_tool_epoch = self.epoch
            result = await self.runner.interrupt(land=arguments['land'])
        else:
            raise ValueError('未知工具：' + str(name))
        if not isinstance(result, dict) or type(result.get('ok')) is not bool:
            raise VolcDialogError('执行器结果必须含布尔 ok，不得推测执行成功')
        return result
