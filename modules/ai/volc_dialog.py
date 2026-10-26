"""豆包全双工实时语音（Seeduplex 3.0）WebSocket 客户端。

与旧版 `/api/v3/realtime/dialogue`（二进制帧、无工具调用）不同，本模块对接的是
**全双工版本**：

- 端点：wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue
  （可用 VOLC_DIALOG_URL 覆盖）
- 鉴权：握手头 `X-Api-Key: <VOLC_API_KEY>`（不是 App ID + Access Key 那一套）
- 协议：纯 JSON 文本帧，事件风格与 OpenAI Realtime 一致
- 音频：上行 PCM16 单声道 16kHz（本模块把 24kHz 采集重采样成 16kHz）；
        下行 pcm_s16le 单声道 24kHz（直接进 PlaybackBuffer）
- **原生 function calling**：`response.function_call_arguments.done` 下发调用，
  结果用 `conversation.item.create` 以 `role=tool` 回传

安全约定：本模块只负责"听得见、说得出、把工具调用转成真实执行"，
不替模型编造任何设备状态。模型只能通过工具返回值了解真实结果。
"""
import asyncio
import base64
import inspect
import json
import os
import time
import uuid

DEFAULT_URL = 'wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue'
DEFAULT_MODEL = '1.2.6.0'
DEFAULT_VOICE = 'zh_female_vv_jupiter_bigtts'
INPUT_RATE = 16000
OUTPUT_RATE = 24000
INPUT_PACKET_MS = 20

# 只读状态工具：一条轻量调用就能回答"现在什么状态/多少电/多高"，
# 不必为了一句问话去起 Docker 沙箱（本机没装 docker 时 run_program 根本不可用）。
STATUS_TOOL = {
    'type': 'function', 'name': 'get_status',
    'description': '读取无人机真实状态：电量、高度、是否在飞、链路、跟随状态',
    'parameters': {'type': 'object', 'properties': {}, 'required': [],
                   'additionalProperties': False},
}

# 客户端事件
EV_SESSION_CREATE = 'session.create'
EV_SESSION_UPDATE = 'session.update'
EV_SESSION_CLOSE = 'session.close'
EV_AUDIO_APPEND = 'input_audio_buffer.append'
EV_AUDIO_COMMIT = 'input_audio_buffer.commit'
EV_ITEM_CREATE = 'conversation.item.create'
EV_RESPONSE_CANCEL = 'response.cancel'
EV_SPEECH_TEXT_COMMIT = 'speech_text_buffer.commit'

# 服务端事件
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


class Downsampler:
    """24kHz → 16kHz 线性插值重采样；逐块调用保持相位连续。"""

    def __init__(self, src=OUTPUT_RATE, dst=INPUT_RATE):
        import array
        self.step = float(src) / float(dst)
        self.pos = 0.0
        self.tail = array.array('h')

    def process(self, data):
        import array
        if len(data) % 2:
            raise ValueError('PCM 字节数必须为偶数')
        block = array.array('h')
        block.frombytes(data)
        if self.tail:
            block = self.tail + block
        out = array.array('h')
        pos, total = self.pos, len(block)
        while pos + 1 < total:
            index = int(pos)
            frac = pos - index
            out.append(int(block[index] * (1.0 - frac) + block[index + 1] * frac))
            pos += self.step
        consumed = int(pos)
        self.tail = block[consumed:]
        self.pos = pos - consumed
        return out.tobytes()


def build_session(*, model=DEFAULT_MODEL, voice=DEFAULT_VOICE, instructions,
                  tools=None):
    """组装 session.create。全双工由服务端原生 VAD 判停，不传 turn_detection。"""
    if voice not in VOICES:
        voice = DEFAULT_VOICE
    session = {
        'type': 'realtime',
        'model': model,
        'instructions': instructions or '',
        'audio': {
            'input': {'format': {'type': 'pcm', 'rate': INPUT_RATE}},
            'output': {'format': {'type': 'pcm_s16le', 'rate': OUTPUT_RATE},
                       'speed': 0, 'loudness': 0, 'voice': voice},
        },
    }
    if tools:
        session['tools'] = tools
    return {'type': EV_SESSION_CREATE, 'session': session,
            'extension': {'asr': {}, 'tts': {}, 'dialog': {}}}


class VolcDuplex:
    """一条豆包全双工实时语音连接。

    runner 需提供 run(code) 与可选 cancel()/demo；audio 需提供
    chunks()/speaker/start()/close()（与 modules/ai/duplex_audio.DuplexAudio 同形状）。
    """

    def __init__(self, runner, audio, *, instructions, tools=None, api_key=None,
                 url=None, model=None, voice=None, on_log=None, on_user_text=None,
                 on_reply_text=None, on_speech_started=None):
        self.runner = runner
        self.audio = audio
        self.instructions = instructions
        self.tools = tools or []
        self.api_key = api_key or os.getenv('VOLC_API_KEY', '')
        self.url = url or os.getenv('VOLC_DIALOG_URL') or DEFAULT_URL
        self.model = model or os.getenv('VOLC_MODEL') or DEFAULT_MODEL
        self.voice = voice or os.getenv('VOLC_VOICE') or DEFAULT_VOICE
        self.log = on_log or (lambda event: None)
        self.on_user_text = on_user_text or (lambda text: None)
        self.on_reply_text = on_reply_text or (lambda text: None)
        self.on_speech_started = on_speech_started or (lambda: None)
        self.ws = None
        self.ready = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.downsampler = Downsampler()
        self.session_id = None
        self.epoch = 0
        self.asr_text = ''
        self.reply_text = ''
        self.response_id = None
        self.speech_started_at = None
        self.first_audio_logged = False
        self.tool_tasks = set()
        self.background_error = asyncio.Queue()
        # 回音抑制：默认播报期间不上行麦克风；要支持"打断我"就设 VOLC_BARGE_IN=1
        self.barge_in = os.getenv('VOLC_BARGE_IN', '0') not in ('0', '', 'false', 'False')
        self.speaking = False
        self.speaking_since = None

    # ---------- 生命周期 ----------
    def check_credentials(self):
        if not self.api_key:
            raise VolcDialogError(
                '缺少 VOLC_API_KEY：到火山控制台「API Key 管理」复制真正的密钥值，然后\n'
                '  $env:VOLC_API_KEY="xxxx"')

    def headers(self):
        return {'X-Api-Key': self.api_key}

    async def send(self, event):
        async with self.send_lock:
            await self.ws.send(json.dumps(event, ensure_ascii=False))

    def track(self, task):
        self.tool_tasks.add(task)

        def done(item):
            self.tool_tasks.discard(item)
            if not item.cancelled() and item.exception():
                self.background_error.put_nowait(item.exception())
        task.add_done_callback(done)

    async def run(self):
        self.check_credentials()
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidStatus

        last = None
        for attempt in (1, 2):                 # 连 Tello 的 Wi-Fi 时 DNS 可能很慢，重试一次
            try:
                await self._session(connect, ConnectionClosed)
                return
            except InvalidStatus as exc:
                response = getattr(exc, 'response', None)
                status = getattr(response, 'status_code', '?')
                body = ''
                try:
                    body = (response.body or b'').decode('utf-8', 'replace')[:300]
                except Exception:
                    pass
                hint = ''
                if str(status).startswith('401'):
                    hint = ('\n→ 鉴权失败：确认用的是「全双工」端点的 API Key（X-Api-Key），'
                            '不是 App ID / Access Token 那一套。')
                raise VolcDialogError(f'握手被拒绝 HTTP {status}: {body or exc}{hint}') from None
            except (asyncio.TimeoutError, TimeoutError, OSError) as exc:
                last = exc
                self.log({'event': 'handshake_retry', 'attempt': attempt, 'error': str(exc)[:200]})
                if attempt == 1:
                    print('[豆包] 握手超时，重试一次……', flush=True)
                    continue
        raise VolcDialogError(
            '连接豆包超时：%s\n→ 最常见原因：电脑连着 Tello 的 Wi-Fi，而那个热点没有外网/'
            'DNS 很慢。请同时接上外网（有线或手机热点），或改用离线模式'
            '（不加 -Volc，本地 whisper + SAPI）。' % (last,)) from None

    async def _session(self, connect, ConnectionClosed):
        async with connect(self.url, additional_headers=self.headers(),
                           max_size=16_000_000, open_timeout=45, ping_interval=20) as ws:
            self.ws = ws
            log_id = ws.response.headers.get('X-Tt-Logid') if ws.response else None
            self.log({'event': 'websocket_open', 'url': self.url, 'logid': log_id,
                      'model': self.model, 'voice': self.voice})
            await self.send(build_session(model=self.model, voice=self.voice,
                                          instructions=self.instructions, tools=self.tools))
            receiver = asyncio.create_task(self._receive_loop())
            try:
                await asyncio.wait_for(self.ready.wait(), timeout=20)
            except asyncio.TimeoutError:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
                raise VolcDialogError('会话建立超时（20 秒内没收到 session.created）')
            sender = asyncio.create_task(self._send_audio())
            errors = asyncio.create_task(self.background_error.get())
            try:
                done, _ = await asyncio.wait({receiver, sender, errors},
                                             return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    exc = task.exception()
                    if exc is None:
                        continue
                    if isinstance(exc, ConnectionClosed):
                        break
                    raise exc
            finally:
                for task in (sender, receiver, errors):
                    task.cancel()
                await asyncio.gather(sender, receiver, errors, return_exceptions=True)
                try:
                    await self.send({'type': EV_SESSION_CLOSE})
                except Exception:
                    pass
                self.audio.close()

    # ---------- 上下行 ----------
    async def _send_audio(self):
        await asyncio.wait_for(self.ready.wait(), timeout=20)
        self.audio.start()
        async for chunk in self.audio.chunks():
            pcm = self.downsampler.process(chunk)
            if not pcm:
                continue
            if self._input_gated():
                # 只发等长静音，不能停发：服务端会以 AudioServerNoAudioInputTooLongError
                # 直接掐断会话（实测长回复时必现）。静音既防回音又不让流中断。
                pcm = b'\x00' * len(pcm)
            await self.send({'type': EV_AUDIO_APPEND,
                             'audio': base64.b64encode(pcm).decode()})

    def _input_gated(self):
        """播报期间不上行麦克风：用外放时扬声器的声音会被自己当指令（实测严重回音）。"""
        if self.barge_in or not self.speaking:
            return False
        if self.speaking_since and time.monotonic() - self.speaking_since > 30:
            self.speaking = False       # 兜底：没收到 audio_done 也不会永久闭嘴
            return False
        return True

    async def _receive_loop(self):
        async for message in self.ws:
            if isinstance(message, bytes):
                continue
            try:
                event = json.loads(message)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                await self._handle(event)

    # ---------- 对外动作 ----------
    async def cancel_response(self):
        """打断服务端当前回答（本地已停播报后再调用）。"""
        try:
            await self.send({'type': EV_RESPONSE_CANCEL})
        except Exception as exc:
            self.log({'event': 'cancel_failed', 'error': str(exc)})

    async def say_text(self, text):
        """让服务端直接合成指定文本（用于播报本地真实结果）。"""
        if not text:
            return
        await self.send({'type': EV_SPEECH_TEXT_COMMIT, 'event_id': str(uuid.uuid4()),
                         'text': text})

    async def emergency(self, land):
        """热键急停：直接走工具通道，不经过模型判断。"""
        return await self._dispatch('interrupt', {'land': bool(land)})

    # ---------- 事件处理 ----------
    async def _handle(self, event):
        kind = event.get('type')
        if kind == EV_SESSION_CREATED:
            self.session_id = (event.get('session') or {}).get('id')
            self.log({'event': 'session_started', 'session_id': self.session_id})
            self.ready.set()

        elif kind == EV_ASR_STARTED:
            # 用户开口：立刻停本地播报，避免把自己的声音当新指令。
            self.epoch += 1
            self.asr_text = ''
            self.reply_text = ''
            self.speech_started_at = time.monotonic()
            self.first_audio_logged = False
            self.audio.speaker.interrupt()
            self.on_speech_started()
            self.log({'event': 'speech_started'})

        elif kind == EV_ASR_DELTA:
            delta = event.get('delta') or ''
            if delta:
                self.asr_text += delta

        elif kind == EV_ASR_COMPLETED:
            text = event.get('transcript') or self.asr_text
            self.asr_text = ''
            self.log({'event': 'asr_final', 'text': text})
            if text:
                self.on_user_text(text)

        elif kind == EV_ASR_FAILED:
            self.log({'event': 'asr_failed', 'error': event.get('error')})

        elif kind == EV_TEXT_DELTA:
            self.reply_text += event.get('delta') or ''

        elif kind == EV_TEXT_DONE:
            text = event.get('text') or self.reply_text
            self.reply_text = ''
            self.response_id = event.get('response_id') or self.response_id
            if text:
                self.on_reply_text(text)

        elif kind == EV_AUDIO_STARTED:
            self.response_id = event.get('response_id') or self.response_id
            self.speaking = True
            self.speaking_since = time.monotonic()
            self.log({'event': 'tts_started', 'tts_type': event.get('tts_type')})

        elif kind == EV_AUDIO_DELTA:
            raw = event.get('delta') or ''
            if not raw:
                return
            try:
                pcm = base64.b64decode(raw)
            except Exception:
                return
            if len(pcm) % 2:
                pcm = pcm[:-1]
            item = event.get('response_id') or self.response_id or self.session_id
            try:
                self.audio.speaker.append(item, 0, pcm)
            except RuntimeError as exc:
                # 服务端把长回答的音频一口气推下来，本地还在按实时播；队列满了绝不能
                # 让异常掀掉整条会话（实测表现为"执行失败：音频输出队列超限"）
                self.log({'event': 'audio_queue_full', 'error': str(exc)[:120],
                          'dropped_bytes': len(pcm)})
            if not self.first_audio_logged and self.speech_started_at is not None:
                self.log({'event': 'speech_end_to_first_audio_ms',
                          'value': round((time.monotonic() - self.speech_started_at) * 1000)})
                self.first_audio_logged = True

        elif kind == EV_AUDIO_DONE:
            # 服务端音频发完；本地缓冲还有几十毫秒，留一点尾巴再恢复上行
            self.speaking = False
            self.speaking_since = None
            stats = self._playback_stats()
            if stats:
                self.log({'event': 'playback_stats', **stats})
            self.log({'event': 'tts_done', 'status_code': event.get('status_code')})

        elif kind == EV_TOOL_CALL:
            epoch = self.epoch
            for item in event.get('items') or []:
                if isinstance(item, dict):
                    self.track(asyncio.create_task(self._run_tool(item, epoch)))

        elif kind == EV_RESPONSE_DONE:
            self.log({'event': 'response_done',
                      'usage': (event.get('response') or {}).get('usage')})

        elif kind == EV_ERROR:
            error = event.get('error') or {}
            message = error.get('message') if isinstance(error, dict) else str(error)
            raise VolcDialogError('服务端错误：' + str(message or event)[:300])

    async def _run_tool(self, item, epoch):
        call_id = item.get('call_id')
        name = item.get('name')
        try:
            arguments = json.loads(item.get('arguments') or '{}')
            if not isinstance(arguments, dict):
                raise ValueError('参数必须是对象')
            if epoch != self.epoch:
                result = {'ok': False, 'status': 'cancelled', 'reason': 'stale_turn'}
            else:
                result = await self._dispatch(name, arguments)
        except Exception as exc:
            result = {'ok': False, 'status': 'failed', 'reason': str(exc)[:500]}
        self.log({'event': 'tool_result', 'name': name, 'call_id': call_id, 'result': result})
        try:
            await self.send({'type': EV_ITEM_CREATE, 'items': [{
                'call_id': call_id,
                'role': 'tool',
                'content': [{'type': 'input_text',
                             'text': json.dumps(result, ensure_ascii=False)}],
            }]})
        except Exception as exc:
            self.log({'event': 'tool_result_send_failed', 'error': str(exc)})

    def _backend(self):
        return getattr(getattr(self.runner, 'gateway', None), 'backend', None)

    def _playback_stats(self):
        """本段播报的缓冲水位与播空次数：卡不卡就看这两个数。"""
        adapter = getattr(self.audio, 'output_adapter', None) if self.audio is not None else None
        if adapter is None:
            return None
        low = getattr(adapter, 'min_queued_ms', None)
        stats = {'min_queued_ms': None if low is None else round(low, 1),
                 'starves': getattr(adapter, 'starves', 0)}
        adapter.min_queued_ms = None
        adapter.starves = 0
        return stats

    async def _dispatch(self, name, arguments):
        if name == 'run_program':
            code = arguments.get('code')
            if not isinstance(code, str) or not code.strip():
                raise ValueError('code 不能为空')
            result = self.runner.run(code)
            # SandboxRunner.run 是 async：直接 await to_thread 只会拿到未 await 的协程，
            # 工具结果永远是空的（实测报 RuntimeWarning: coroutine was never awaited）
            if inspect.isawaitable(result):
                result = await result
            return result
        if name == 'get_status':
            backend = self._backend()
            if backend is not None:
                state = await asyncio.to_thread(backend.get_status)
            else:
                # 本地白名单执行器（无 docker 时的退路）没有 gateway，状态从 demo.drone 取
                drone = getattr(getattr(self.runner, 'demo', None), 'drone', None)
                if drone is None:
                    raise RuntimeError('当前执行器不支持读取状态')
                state = await asyncio.to_thread(drone.get_status)
            return {'ok': True, 'status': 'ok', 'state': state}
        if name == 'interrupt':
            land = arguments.get('land')
            if type(land) is not bool:
                raise ValueError('land 必须是布尔值')
            if hasattr(self.runner, 'cancel'):
                self.runner.cancel()
            # 实飞入口的 runner 是 SandboxRunner（没有 .demo），急停必须走 backend 的
            # priority 通道，否则"语音急停"在豆包模式下会直接报不支持打断
            backend = getattr(getattr(self.runner, 'gateway', None), 'backend', None)
            if backend is not None and hasattr(backend, 'emergency'):
                result = await asyncio.to_thread(backend.emergency, land)
                return {'ok': bool(result.get('ok')), 'land': land,
                        'message': result.get('message', '')}
            drone = getattr(getattr(self.runner, 'demo', None), 'drone', None)
            if drone is None:
                raise RuntimeError('当前执行器不支持打断')
            result = (drone.land() if land else drone.stop_motion()) or {}
            return {'ok': bool(result.get('ok')), 'land': land,
                    'message': result.get('message', '')}
        raise ValueError('未知工具：' + str(name))
