"""豆包端到端实时语音大模型（S2S / Realtime Dialogue）WebSocket 客户端。

协议依据：火山引擎《端到端实时语音大模型 API 接入文档》。

- 端点：wss://openspeech.bytedance.com/api/v3/realtime/dialogue
  （可用 VOLC_DIALOG_URL 覆盖，例如私有化部署或区域变体）
- 鉴权头：X-Api-App-ID / X-Api-Access-Key / X-Api-Resource-Id / X-Api-App-Key
- 帧格式：4 字节头 + [code|sequence|event] + [connect_id] + [session_id] + payload size + payload
- 上行音频：PCM16 单声道 16kHz（本模块把 24kHz 采集重采样成 16kHz）
- 下行音频：PCM16 单声道 24kHz（format=pcm_s16le，直接进 PlaybackBuffer）

**该模型不提供 function calling**：用户语音由服务端 ASR 成文本，
无人机动作意图在本地解析执行；需要播报的文本通过 ChatTTSText(500) 交回服务端合成，
因此本模块只负责「听得清、说得出」，不理解业务动作。
"""
import array
import asyncio
import gzip
import json
import os
import struct
import time
import uuid

PROTOCOL_VERSION = 0b0001
HEADER_SIZE = 0b0001          # 4 字节

MSG_CLIENT_EVENT = 0b0001
MSG_CLIENT_AUDIO = 0b0010
MSG_SERVER_EVENT = 0b1001
MSG_SERVER_AUDIO = 0b1011
MSG_ERROR = 0b1111

FLAG_NO_SEQUENCE = 0b0000
FLAG_EVENT = 0b0100
FLAG_SEQUENCE = 0b0001
FLAG_LAST_NO_SEQ = 0b0010
FLAG_LAST_NEG_SEQ = 0b0011

SER_RAW = 0b0000
SER_JSON = 0b0001
COMP_NONE = 0b0000
COMP_GZIP = 0b0001

# 客户端事件
EV_START_CONNECTION = 1
EV_FINISH_CONNECTION = 2
EV_START_SESSION = 100
EV_FINISH_SESSION = 102
EV_TASK_REQUEST = 200
EV_UPDATE_CONFIG = 201
EV_SAY_HELLO = 300
EV_END_ASR = 400
EV_CHAT_TTS_TEXT = 500
EV_CHAT_TEXT_QUERY = 501
EV_CHAT_RAG_TEXT = 502
EV_CLIENT_INTERRUPT = 515

# 服务端事件
EV_CONNECTION_STARTED = 50
EV_CONNECTION_FAILED = 51
EV_CONNECTION_FINISHED = 52
EV_SESSION_STARTED = 150
EV_SESSION_FINISHED = 152
EV_SESSION_FAILED = 153
EV_USAGE = 154
EV_CONFIG_UPDATED = 251
EV_TTS_SENTENCE_START = 350
EV_TTS_SENTENCE_END = 351
EV_TTS_RESPONSE = 352
EV_TTS_ENDED = 359
EV_ASR_INFO = 450
EV_ASR_RESPONSE = 451
EV_ASR_ENDED = 459
EV_CHAT_RESPONSE = 550
EV_CHAT_ENDED = 559

# Connect 类事件不带 session id；其余带 event 的事件都是 Session 类。
CONNECT_EVENTS = frozenset({EV_START_CONNECTION, EV_FINISH_CONNECTION,
                            EV_CONNECTION_STARTED, EV_CONNECTION_FAILED,
                            EV_CONNECTION_FINISHED})

DEFAULT_URL = 'wss://openspeech.bytedance.com/api/v3/realtime/dialogue'
DEFAULT_APP_KEY = 'PlgvMymc7f3tQnJ6'      # 官方文档固定的公共值，不是密钥
DEFAULT_RESOURCE_ID = 'volc.speech.dialog'
DEFAULT_MODEL = '1.2.1.1'
DEFAULT_SPEAKER = 'zh_female_vv_jupiter_bigtts'

INPUT_RATE = 16000       # 上行音频采样率（服务端要求）
OUTPUT_RATE = 24000      # 下行 TTS 采样率
INPUT_PACKET_MS = 20     # 文档：16k/int16 的 20ms 包为 640 字节


class VolcDialogError(RuntimeError):
    pass


def encode_frame(msg_type, *, event, session_id=None, connect_id=None, payload=b'',
                 serialization=SER_JSON, compression=COMP_NONE):
    """按官方二进制协议组装一帧。payload 可为 dict/list/str/bytes。"""
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    elif isinstance(payload, str):
        payload = payload.encode('utf-8')
    if compression == COMP_GZIP and payload:
        payload = gzip.compress(payload)
    flags = FLAG_EVENT
    frame = bytearray([(PROTOCOL_VERSION << 4) | HEADER_SIZE,
                       (msg_type << 4) | flags,
                       (serialization << 4) | compression,
                       0x00])
    frame += struct.pack('>i', event)
    if connect_id is not None:
        raw = connect_id.encode('utf-8')
        frame += struct.pack('>I', len(raw)) + raw
    if session_id is not None:
        raw = session_id.encode('utf-8')
        frame += struct.pack('>I', len(raw)) + raw
    frame += struct.pack('>I', len(payload))
    return bytes(frame) + payload


def decode_frame(data):
    """解析服务端一帧；返回 dict，payload 为原始 bytes，json 为解析结果或 None。"""
    if len(data) < 4:
        raise ValueError('帧长度不足 4 字节')
    version = data[0] >> 4
    header_size = data[0] & 0x0F
    msg_type = data[1] >> 4
    flags = data[1] & 0x0F
    serialization = data[2] >> 4
    compression = data[2] & 0x0F
    if version != PROTOCOL_VERSION:
        raise ValueError(f'不支持的协议版本 {version}')
    offset = header_size * 4
    code = sequence = event = session_id = None
    if msg_type == MSG_ERROR:
        # 错误帧：code 紧跟头部，payload 直接跟在后面。
        code = struct.unpack('>i', data[offset:offset + 4])[0]
        offset += 4
    elif flags in (FLAG_SEQUENCE, FLAG_LAST_NO_SEQ, FLAG_LAST_NEG_SEQ):
        sequence = struct.unpack('>i', data[offset:offset + 4])[0]
        offset += 4
    if msg_type != MSG_ERROR and flags & FLAG_EVENT:
        event = struct.unpack('>i', data[offset:offset + 4])[0]
        offset += 4
    if event is not None and event not in CONNECT_EVENTS:
        size = struct.unpack('>I', data[offset:offset + 4])[0]
        offset += 4
        session_id = data[offset:offset + size].decode('utf-8', 'replace')
        offset += size
    size = struct.unpack('>I', data[offset:offset + 4])[0]
    offset += 4
    payload = data[offset:offset + size]
    if compression == COMP_GZIP and payload:
        payload = gzip.decompress(payload)
    parsed = None
    if serialization == SER_JSON and payload:
        try:
            parsed = json.loads(payload.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
    return {'version': version, 'msg_type': msg_type, 'flags': flags,
            'serialization': serialization, 'compression': compression,
            'code': code, 'sequence': sequence, 'event': event,
            'session_id': session_id, 'payload': payload, 'json': parsed}


class Downsampler:
    """24kHz → 16kHz 线性插值重采样；逐块调用保持相位连续。"""

    def __init__(self, src=OUTPUT_RATE, dst=INPUT_RATE):
        self.step = float(src) / float(dst)
        self.pos = 0.0
        self.tail = array.array('h')

    def process(self, data):
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


def build_start_session(*, model=DEFAULT_MODEL, speaker=DEFAULT_SPEAKER, system_role=None,
                        bot_name=None, end_smooth_ms=800, strict_audit=False,
                        audio_config=None):
    """组装 StartSession(100) 的 payload。"""
    silence = 1500 if audio_config is None else int(audio_config.silence_duration_ms)
    dialog_extra = {'model': model, 'strict_audit': bool(strict_audit)}
    dialog = {'extra': dialog_extra}
    if system_role:
        dialog['system_role'] = system_role
    if bot_name:
        dialog['bot_name'] = bot_name
    return {
        'asr': {'extra': {'end_smooth_window_ms': int(end_smooth_ms or silence)}},
        'tts': {'speaker': speaker,
                'audio_config': {'channel': 1, 'format': 'pcm_s16le',
                                 'sample_rate': OUTPUT_RATE}},
        'dialog': dialog,
    }


class VolcDialog:
    """一条豆包实时对话连接。

    audio 必须是 DuplexAudio 形状：chunks()/speaker/level()/start()/close()。
    on_user_text(text)      —— 服务端 ASR 出的最终用户文本（可能为空）
    on_chat_text(text)      —— 模型自己生成的回复文本（用于日志/判断闲聊）
    on_speech_started()     —— 用户开口（本地打断播报）
    """

    def __init__(self, audio, *, on_user_text=None, on_chat_text=None,
                 on_speech_started=None, on_log=None, audio_config=None,
                 url=None, app_id=None, access_key=None, model=None, speaker=None,
                 system_role=None, bot_name=None, end_smooth_ms=None):
        self.audio = audio
        self.on_user_text = on_user_text or (lambda text: None)
        self.on_chat_text = on_chat_text or (lambda text: None)
        self.on_speech_started = on_speech_started or (lambda: None)
        self.log = on_log or (lambda event: None)
        self.audio_config = audio_config
        self.url = url or os.getenv('VOLC_DIALOG_URL') or DEFAULT_URL
        self.app_id = app_id or os.getenv('VOLC_APP_ID', '')
        self.access_key = access_key or os.getenv('VOLC_ACCESS_KEY', '')
        self.model = model or os.getenv('VOLC_MODEL') or DEFAULT_MODEL
        self.speaker = speaker or os.getenv('VOLC_SPEAKER') or DEFAULT_SPEAKER
        self.system_role = system_role
        self.bot_name = bot_name
        self.end_smooth_ms = end_smooth_ms
        self.session_id = str(uuid.uuid4())
        self.connect_id = str(uuid.uuid4())
        self.ws = None
        self.ready = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.downsampler = Downsampler()
        self.sentence_index = 0
        self.reply_id = None
        self.partial_text = ''
        self.speech_started_at = None
        self.first_audio_logged = False

    def check_credentials(self):
        if not self.app_id:
            raise VolcDialogError('缺少 VOLC_APP_ID（火山控制台的数字 APP ID）')
        if not self.access_key:
            raise VolcDialogError('缺少 VOLC_ACCESS_KEY（火山控制台的 Access Token）')

    def headers(self):
        return {
            'X-Api-App-ID': self.app_id,
            'X-Api-Access-Key': self.access_key,
            'X-Api-Resource-Id': os.getenv('VOLC_RESOURCE_ID') or DEFAULT_RESOURCE_ID,
            'X-Api-App-Key': os.getenv('VOLC_APP_KEY') or DEFAULT_APP_KEY,
            'X-Api-Connect-Id': self.connect_id,
        }

    async def send(self, frame):
        async with self.send_lock:
            await self.ws.send(frame)

    async def send_event(self, event, payload=b'', *, session_id=None,
                         msg_type=MSG_CLIENT_EVENT, serialization=SER_JSON):
        await self.send(encode_frame(msg_type, event=event, payload=payload,
                                     session_id=session_id,
                                     serialization=serialization))

    async def say_text(self, text):
        """让服务端把这段文本合成语音播报（用于播报真实执行结果）。"""
        if not text:
            return
        await self.send_event(EV_CHAT_TTS_TEXT,
                              {'start': True, 'content': text, 'end': True},
                              session_id=self.session_id)

    async def interrupt(self):
        """打断服务端当前回答（客户端主动）。"""
        self.audio.speaker.interrupt()
        await self.send_event(EV_CLIENT_INTERRUPT, {}, session_id=self.session_id)

    async def _handle_frame(self, frame):
        event = frame['event']
        data = frame['json']
        if event == EV_CONNECTION_STARTED:
            self.log({'event': 'connection_started'})
            payload = build_start_session(model=self.model, speaker=self.speaker,
                                          system_role=self.system_role, bot_name=self.bot_name,
                                          end_smooth_ms=self.end_smooth_ms,
                                          audio_config=self.audio_config)
            await self.send_event(EV_START_SESSION, payload, session_id=self.session_id)
        elif event in (EV_CONNECTION_FAILED, EV_SESSION_FAILED):
            detail = (data or {}).get('error', 'unknown')
            raise VolcDialogError(f'服务端拒绝会话：{detail}')
        elif event == EV_SESSION_STARTED:
            dialog_id = (data or {}).get('dialog_id')
            self.log({'event': 'session_started', 'dialog_id': dialog_id, 'model': self.model})
            self.ready.set()
        elif event == EV_ASR_INFO:
            # 用户开口：立刻停本地播报，避免自己打断自己。
            self.sentence_index = 0
            self.reply_id = None
            self.partial_text = ''
            self.speech_started_at = time.monotonic()
            self.first_audio_logged = False
            self.audio.speaker.interrupt()
            self.on_speech_started()
            self.log({'event': 'speech_started'})
        elif event == EV_ASR_RESPONSE:
            results = (data or {}).get('results') or []
            text = ''.join(str(item.get('text', '')) for item in results if isinstance(item, dict))
            interim = all(bool(item.get('is_interim')) for item in results) if results else True
            if interim:
                self.partial_text = text
            else:
                final = text or self.partial_text
                self.partial_text = ''
                self.log({'event': 'asr_final', 'text': final})
                outcome = self.on_user_text(final)
                if asyncio.iscoroutine(outcome):
                    await outcome
        elif event == EV_CHAT_RESPONSE:
            text = (data or {}).get('content', '')
            if text:
                self.on_chat_text(text)
                self.log({'event': 'chat_response', 'text': text[:200]})
        elif event == EV_TTS_SENTENCE_START:
            self.reply_id = (data or {}).get('reply_id') or self.reply_id or self.session_id
            self.sentence_index += 1
            self.log({'event': 'tts_sentence', 'text': (data or {}).get('text', '')[:120],
                      'tts_type': (data or {}).get('tts_type')})
        elif event == EV_TTS_RESPONSE:
            audio = frame['payload']
            if not audio:
                return
            if len(audio) % 2:
                audio = audio[:-1]
            item = self.reply_id or self.session_id
            self.audio.speaker.append(item, self.sentence_index, audio)
            if not self.first_audio_logged and self.speech_started_at is not None:
                self.log({'event': 'speech_end_to_first_audio_ms',
                          'value': round((time.monotonic() - self.speech_started_at) * 1000)})
                self.first_audio_logged = True
        elif event == EV_TTS_ENDED:
            self.log({'event': 'tts_ended'})
            self.reply_id = None
        elif event == EV_USAGE:
            self.log({'event': 'usage', 'usage': (data or {}).get('usage')})

    async def _receive_loop(self):
        async for message in self.ws:
            if isinstance(message, str):
                continue
            frame = decode_frame(message)
            if frame['msg_type'] == MSG_ERROR:
                detail = frame['payload'].decode('utf-8', 'replace')[:300]
                raise VolcDialogError(f'服务端错误帧 code={frame["code"]}: {detail}')
            await self._handle_frame(frame)

    async def _send_loop(self):
        await asyncio.wait_for(self.ready.wait(), timeout=20)
        self.audio.start()
        async for chunk in self.audio.chunks():
            pcm16 = self.downsampler.process(chunk)
            if pcm16:
                await self.send(encode_frame(MSG_CLIENT_AUDIO, event=EV_TASK_REQUEST,
                                             session_id=self.session_id, payload=pcm16,
                                             serialization=SER_RAW))

    async def run(self):
        self.check_credentials()
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidStatus

        try:
            await self._session(connect, ConnectionClosed)
        except InvalidStatus as exc:
            response = getattr(exc, 'response', None)
            status = getattr(response, 'status_code', '?')
            body = ''
            try:
                body = (response.body or b'').decode('utf-8', 'replace')[:300]
            except Exception:
                pass
            hint = ''
            if 'grant' in body:
                hint = ('\n→ 鉴权头格式正确，但这个 Access Key 在火山 SaaS 里查不到授权：'
                        '多半是把「密钥名称」当成了密钥值。请回控制台点开该密钥复制真正的值。')
            elif 'no token' in body:
                hint = '\n→ 服务端没读到 X-Api-Access-Key，请确认环境变量已导出到当前进程。'
            raise VolcDialogError(f'握手被拒绝 HTTP {status}: {body or exc}{hint}') from None

    async def _session(self, connect, ConnectionClosed):
        async with connect(self.url, additional_headers=self.headers(),
                           max_size=8_000_000, open_timeout=20, ping_interval=20) as ws:
            self.ws = ws
            log_id = (ws.response.headers.get('X-Tt-Logid') if ws.response else None)
            self.log({'event': 'websocket_open', 'url': self.url, 'logid': log_id,
                      'model': self.model})
            await self.send_event(EV_START_CONNECTION, {})
            receiver = asyncio.create_task(self._receive_loop())
            try:
                await asyncio.wait_for(self.ready.wait(), timeout=20)
            except asyncio.TimeoutError:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
                raise VolcDialogError('会话建立超时（20 秒内没收到 SessionStarted）')
            sender = asyncio.create_task(self._send_loop())
            try:
                done, _ = await asyncio.wait({receiver, sender}, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    exc = task.exception()
                    if exc is None:
                        continue
                    if isinstance(exc, ConnectionClosed):
                        break          # 服务端主动关闭：属于正常收尾
                    raise exc
            finally:
                sender.cancel()
                receiver.cancel()
                await asyncio.gather(sender, receiver, return_exceptions=True)
                try:
                    await self.send_event(EV_FINISH_SESSION, {}, session_id=self.session_id)
                except Exception:
                    pass
                self.audio.close()
