"""豆包实时对话协议层离线测试。

不连真机、不连真实服务：全部用官方文档给出的字节示例 + 本地回环 WebSocket 验证。
运行：python test_volc_dialog.py
"""
import asyncio
import struct
import unittest

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from duplex_audio import PlaybackBuffer
import volc_dialog as vd
from volc_dialog import (Downsampler, EV_ASR_RESPONSE, EV_CHAT_RESPONSE, EV_CONNECTION_STARTED,
                         EV_SESSION_STARTED, EV_START_CONNECTION, EV_START_SESSION,
                         EV_TASK_REQUEST, EV_TTS_RESPONSE, EV_TTS_SENTENCE_START, EV_TTS_ENDED,
                         MSG_CLIENT_AUDIO, MSG_CLIENT_EVENT, MSG_SERVER_AUDIO, MSG_SERVER_EVENT,
                         SER_RAW, VolcDialog, build_start_session, decode_frame, encode_frame)


class ProtocolBytes(unittest.TestCase):
    def test_start_connection_matches_official_sample(self):
        """官方文档给出的 StartConnection 帧字节数组必须逐字节一致。"""
        official = bytes([17, 20, 16, 0, 0, 0, 0, 1, 0, 0, 0, 2, 123, 125])
        self.assertEqual(encode_frame(MSG_CLIENT_EVENT, event=EV_START_CONNECTION, payload={}),
                         official)

    def test_start_session_frame_layout(self):
        session_id = '75a6126e-427f-49a1-a2c1-621143cb9db3'
        frame = encode_frame(MSG_CLIENT_EVENT, event=EV_START_SESSION,
                             session_id=session_id, payload={'dialog': {'bot_name': '豆包'}})
        self.assertEqual(frame[0], 0x11)                       # version 1 / header 4 字节
        self.assertEqual(frame[1] >> 4, MSG_CLIENT_EVENT)
        self.assertEqual(frame[1] & 0x0F, vd.FLAG_EVENT)
        self.assertEqual(frame[2], 0x10)                       # JSON + 无压缩
        self.assertEqual(struct.unpack('>i', frame[4:8])[0], EV_START_SESSION)
        size = struct.unpack('>I', frame[8:12])[0]
        self.assertEqual(size, len(session_id))
        self.assertEqual(frame[12:12 + size].decode(), session_id)
        decoded = decode_frame(frame)
        self.assertEqual(decoded['session_id'], session_id)
        self.assertEqual(decoded['json'], {'dialog': {'bot_name': '豆包'}})

    def test_decode_official_session_sample(self):
        session_id = '75a6126e-427f-49a1-a2c1-621143cb9db3'
        payload = '{"dialog":{"bot_name":"豆包","dialog_id":"","extra":null}}'.encode('utf-8')
        frame = bytes([17, 20, 16, 0, 0, 0, 0, 100, 0, 0, 0, 36]) + session_id.encode()
        frame += struct.pack('>I', len(payload)) + payload
        decoded = decode_frame(frame)
        self.assertEqual(decoded['event'], 100)
        self.assertEqual(decoded['session_id'], session_id)
        self.assertEqual(decoded['json']['dialog']['bot_name'], '豆包')

    def test_connect_events_carry_no_session_id(self):
        for event in (EV_CONNECTION_STARTED, vd.EV_CONNECTION_FAILED, vd.EV_CONNECTION_FINISHED):
            frame = encode_frame(MSG_SERVER_EVENT, event=event, payload={})
            decoded = decode_frame(frame)
            self.assertIsNone(decoded['session_id'], f'event {event} 不应带 session id')

    def test_audio_frame_uses_raw_serialization(self):
        pcm = b'\x00\x01' * 320
        frame = encode_frame(MSG_CLIENT_AUDIO, event=EV_TASK_REQUEST, session_id='s-1',
                             payload=pcm, serialization=SER_RAW)
        self.assertEqual(frame[2] >> 4, SER_RAW)
        decoded = decode_frame(frame)
        self.assertEqual(decoded['payload'], pcm)
        self.assertIsNone(decoded['json'])

    def test_gzip_payload_roundtrip(self):
        payload = {'content': '你好' * 50}
        frame = encode_frame(MSG_CLIENT_EVENT, event=vd.EV_CHAT_TTS_TEXT, session_id='s-1',
                             payload=payload, compression=vd.COMP_GZIP)
        decoded = decode_frame(frame)
        self.assertEqual(decoded['json'], payload)

    def test_rejects_short_frame(self):
        with self.assertRaises(ValueError):
            decode_frame(b'\x11\x20')


class Resampling(unittest.TestCase):
    def test_24k_to_16k_packet_size(self):
        """480 帧 @24kHz（20ms）应重采样成 320 帧 @16kHz = 640 字节。"""
        pcm = struct.pack('<480h', *([1000] * 480))
        out = Downsampler().process(pcm)
        self.assertEqual(len(out), 640)

    def test_continuity_across_chunks(self):
        """分块调用不能丢样本：两半各 240 帧的结果应等于一次 480 帧。"""
        pcm = struct.pack('<480h', *([500] * 480))
        one_shot = Downsampler().process(pcm)
        split = Downsampler()
        two_part = split.process(pcm[:480]) + split.process(pcm[480:])
        self.assertEqual(len(two_part), len(one_shot))

    def test_preserves_signal_amplitude(self):
        pcm = struct.pack('<480h', *([2000, -2000] * 240))
        out = struct.unpack('<320h', Downsampler().process(pcm))
        self.assertGreater(max(abs(value) for value in out), 1000)


class StartSessionPayload(unittest.TestCase):
    def test_required_fields(self):
        payload = build_start_session(model='1.2.1.1', speaker='zh_female_vv_jupiter_bigtts',
                                      system_role='你是助手', end_smooth_ms=700)
        self.assertEqual(payload['dialog']['extra']['model'], '1.2.1.1')
        self.assertEqual(payload['dialog']['system_role'], '你是助手')
        self.assertEqual(payload['asr']['extra']['end_smooth_window_ms'], 700)
        # 必须显式要 16bit PCM，否则服务端默认回 OGG/Opus
        self.assertEqual(payload['tts']['audio_config']['format'], 'pcm_s16le')
        self.assertEqual(payload['tts']['audio_config']['sample_rate'], 24000)
        self.assertEqual(payload['tts']['speaker'], 'zh_female_vv_jupiter_bigtts')

    def test_falls_back_to_audio_config_silence(self):
        class Cfg:
            silence_duration_ms = 900
        payload = build_start_session(end_smooth_ms=None, audio_config=Cfg())
        self.assertEqual(payload['asr']['extra']['end_smooth_window_ms'], 900)


class FakeAudio:
    """替身：不走声卡，只提供 DuplexAudio 的形状。"""

    def __init__(self):
        self.speaker = PlaybackBuffer(rate=24000)
        self.started = False
        self.closed = False
        self._block = struct.pack('<480h', *([300] * 480))

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def level(self):
        return 9999.0

    async def chunks(self):
        while True:
            yield self._block
            await asyncio.sleep(0.001)


class LoopbackSession(unittest.TestCase):
    """本地假服务端：验证握手、上行音频、下行 TTS、ASR 文本回调整条状态机。"""

    def test_full_handshake_and_audio_path(self):
        seen = {'events': [], 'audio_bytes': 0, 'headers': None, 'replied': False}

        async def handler(ws):
            seen['headers'] = dict(ws.request.headers)
            session_id = None
            try:
                async for message in ws:
                    frame = decode_frame(message)
                    seen['events'].append(frame['event'])
                    if frame['event'] == EV_START_CONNECTION:
                        await ws.send(encode_frame(MSG_SERVER_EVENT, event=EV_CONNECTION_STARTED,
                                                   payload={}))
                    elif frame['event'] == EV_START_SESSION:
                        session_id = frame['session_id']
                        await ws.send(encode_frame(MSG_SERVER_EVENT, event=EV_SESSION_STARTED,
                                                   session_id=session_id,
                                                   payload={'dialog_id': 'demo-dialog'}))
                    elif frame['event'] == EV_TASK_REQUEST:
                        seen['audio_bytes'] += len(frame['payload'])
                        if seen['audio_bytes'] >= 640 * 3 and not seen['replied']:
                            seen['replied'] = True
                            await ws.send(encode_frame(MSG_SERVER_EVENT, event=vd.EV_ASR_INFO,
                                                       session_id=session_id,
                                                       payload={'question_id': 'q1'}))
                            await ws.send(encode_frame(MSG_SERVER_EVENT, event=EV_ASR_RESPONSE,
                                                       session_id=session_id,
                                                       payload={'results': [{'text': '起飞',
                                                                            'is_interim': False}]}))
                            await ws.send(encode_frame(MSG_SERVER_EVENT, event=EV_CHAT_RESPONSE,
                                                       session_id=session_id,
                                                       payload={'content': '好的',
                                                                'question_id': 'q1',
                                                                'reply_id': 'r1'}))
                            await ws.send(encode_frame(MSG_SERVER_EVENT, event=EV_TTS_SENTENCE_START,
                                                       session_id=session_id,
                                                       payload={'text': '好的',
                                                                'tts_type': 'default',
                                                                'reply_id': 'r1'}))
                            await ws.send(encode_frame(MSG_SERVER_AUDIO, event=EV_TTS_RESPONSE,
                                                       session_id=session_id, serialization=SER_RAW,
                                                       payload=b'\x02\x00' * 2400))
                            await ws.send(encode_frame(MSG_SERVER_EVENT, event=EV_TTS_ENDED,
                                                       session_id=session_id,
                                                       payload={'reply_id': 'r1'}))
                            await asyncio.sleep(0.1)
                            await ws.close()
            except ConnectionClosed:
                pass          # 客户端断开属于正常收尾，测试不应留下告警

        async def scenario():
            server = await serve(handler, '127.0.0.1', 0)
            port = server.sockets[0].getsockname()[1]
            audio = FakeAudio()
            texts, chats, speech = [], [], []
            dialog = VolcDialog(audio, url=f'ws://127.0.0.1:{port}',
                                app_id='test-app-id', access_key='test-access-key',
                                on_user_text=texts.append, on_chat_text=chats.append,
                                on_speech_started=lambda: speech.append(True))
            try:
                await asyncio.wait_for(dialog.run(), timeout=15)
            finally:
                server.close()
                await server.wait_closed()
            return audio, texts, chats, speech, dialog

        audio, texts, chats, speech, dialog = asyncio.run(scenario())

        headers = {k.lower(): v for k, v in seen['headers'].items()}
        self.assertEqual(headers['x-api-app-id'], 'test-app-id')
        self.assertEqual(headers['x-api-access-key'], 'test-access-key')
        self.assertEqual(headers['x-api-resource-id'], 'volc.speech.dialog')
        self.assertEqual(headers['x-api-app-key'], 'PlgvMymc7f3tQnJ6')

        self.assertEqual(seen['events'][:2], [EV_START_CONNECTION, EV_START_SESSION])
        self.assertIn(EV_TASK_REQUEST, seen['events'])
        self.assertGreaterEqual(seen['audio_bytes'], 640 * 3)
        self.assertEqual(texts, ['起飞'])
        self.assertEqual(chats, ['好的'])
        self.assertEqual(speech, [True])
        # 下行 2400 帧 @24kHz 必须全部进入播放缓冲
        self.assertEqual(audio.speaker.size, 4800)
        self.assertTrue(audio.started)
        self.assertTrue(audio.closed)
        self.assertEqual(dialog.model, vd.DEFAULT_MODEL)

    def test_missing_credentials_fail_fast(self):
        dialog = VolcDialog(FakeAudio(), url='ws://127.0.0.1:1',
                            app_id='', access_key='')
        with self.assertRaises(vd.VolcDialogError):
            asyncio.run(dialog.run())


if __name__ == '__main__':
    unittest.main(verbosity=2)
