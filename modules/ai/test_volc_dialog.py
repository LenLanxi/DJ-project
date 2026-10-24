"""豆包全双工实时语音协议层离线测试。

不连真实服务：用本地回环 WebSocket 模拟豆包，验证事件编解码、音频通路与工具调用。
运行：python test_volc_dialog.py
"""
import asyncio
import base64
import json
import struct
import types
import unittest

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from duplex_audio import PlaybackBuffer
import volc_dialog as vd
from volc_dialog import (Downsampler, VolcDuplex, VolcDialogError, build_session)


class SessionPayload(unittest.TestCase):
    def test_structure_matches_duplex_contract(self):
        payload = build_session(instructions='你是助手', tools=[{'type': 'function',
                                                                'name': 'x', 'parameters': {}}])
        self.assertEqual(payload['type'], 'session.create')
        session = payload['session']
        self.assertEqual(session['type'], 'realtime')
        self.assertEqual(session['model'], vd.DEFAULT_MODEL)
        self.assertEqual(session['instructions'], '你是助手')
        self.assertEqual(session['audio']['input']['format'],
                         {'type': 'pcm', 'rate': 16000})
        self.assertEqual(session['audio']['output']['format'],
                         {'type': 'pcm_s16le', 'rate': 24000})
        self.assertEqual(session['audio']['output']['voice'], vd.DEFAULT_VOICE)
        self.assertEqual(len(session['tools']), 1)
        self.assertEqual(payload['extension'], {'asr': {}, 'tts': {}, 'dialog': {}})

    def test_unknown_voice_falls_back(self):
        payload = build_session(voice='alloy', instructions='')
        self.assertEqual(payload['session']['audio']['output']['voice'], vd.DEFAULT_VOICE)

    def test_no_turn_detection_field(self):
        """全双工由服务端原生 VAD 判停，不应传 turn_detection。"""
        payload = build_session(instructions='')
        self.assertNotIn('turn_detection', json.dumps(payload))


class Resampling(unittest.TestCase):
    def test_24k_to_16k_packet_size(self):
        pcm = struct.pack('<480h', *([1000] * 480))
        self.assertEqual(len(Downsampler().process(pcm)), 640)

    def test_continuity_across_chunks(self):
        pcm = struct.pack('<480h', *([500] * 480))
        one = Downsampler().process(pcm)
        split = Downsampler()
        two = split.process(pcm[:480]) + split.process(pcm[480:])
        self.assertEqual(len(two), len(one))


class FakeDrone:
    def __init__(self):
        self.calls = []

    def land(self):
        self.calls.append('land')
        return {'ok': True, 'message': '已降落'}

    def stop_motion(self):
        self.calls.append('stop_motion')
        return {'ok': True, 'message': '已停止'}


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.demo = types.SimpleNamespace(drone=FakeDrone())

    def run(self, code):
        self.calls.append(code)
        return {'accepted': True, 'ok': True, 'result': '[模拟] 起飞完成'}


class FakeAudio:
    def __init__(self):
        self.speaker = PlaybackBuffer(rate=24000)
        self.started = False
        self.closed = False
        self._block = struct.pack('<480h', *([300] * 480))

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    async def chunks(self):
        while True:
            yield self._block
            await asyncio.sleep(0.001)


class Loopback(unittest.TestCase):
    """模拟豆包服务端：校验 session.create → 回 ASR → 下发工具调用 → 回 TTS。"""

    def test_full_flow_with_tool_call(self):
        seen = {'headers': None, 'session': None, 'packets': [], 'tool_result': None}

        async def handler(ws):
            seen['headers'] = {k.lower(): v for k, v in dict(ws.request.headers).items()}
            replied = False
            try:
                async for message in ws:
                    event = json.loads(message)
                    kind = event.get('type')
                    if kind == vd.EV_SESSION_CREATE:
                        seen['session'] = event
                        await ws.send(json.dumps({'type': vd.EV_SESSION_CREATED,
                                                  'session': {'id': 'sess-1'}}))
                    elif kind == vd.EV_AUDIO_APPEND:
                        seen['packets'].append(len(base64.b64decode(event['audio'])))
                        if len(seen['packets']) >= 3 and not replied:
                            replied = True
                            await ws.send(json.dumps({'type': vd.EV_ASR_STARTED}))
                            await ws.send(json.dumps({'type': vd.EV_ASR_DELTA, 'delta': '起飞'}))
                            await ws.send(json.dumps({'type': vd.EV_ASR_COMPLETED,
                                                      'transcript': '起飞'}))
                            await ws.send(json.dumps({
                                'type': vd.EV_TOOL_CALL,
                                'items': [{'call_id': 'call-1', 'name': 'run_program',
                                           'arguments': '{"code":"drone.takeoff()"}'}]}))
                    elif kind == vd.EV_ITEM_CREATE:
                        seen['tool_result'] = event
                        await ws.send(json.dumps({'type': vd.EV_TEXT_DONE,
                                                  'text': '好的，已起飞', 'response_id': 'r1'}))
                        await ws.send(json.dumps({'type': vd.EV_AUDIO_STARTED,
                                                  'tts_type': 'default', 'response_id': 'r1'}))
                        await ws.send(json.dumps({
                            'type': vd.EV_AUDIO_DELTA, 'response_id': 'r1',
                            'delta': base64.b64encode(b'\x02\x00' * 2400).decode()}))
                        await ws.send(json.dumps({'type': vd.EV_AUDIO_DONE,
                                                  'response_id': 'r1'}))
                        await ws.send(json.dumps({'type': vd.EV_RESPONSE_DONE,
                                                  'response': {'usage': {'total_tokens': 10}}}))
                        await asyncio.sleep(0.1)
                        await ws.close()
            except ConnectionClosed:
                pass

        async def scenario():
            server = await serve(handler, '127.0.0.1', 0)
            port = server.sockets[0].getsockname()[1]
            audio, runner = FakeAudio(), FakeRunner()
            texts, replies, speech = [], [], []
            dialog = VolcDuplex(runner, audio, instructions='你是助手',
                                tools=[{'type': 'function', 'name': 'run_program',
                                        'parameters': {}}],
                                api_key='test-key',
                                url='ws://127.0.0.1:%d' % port,
                                on_user_text=texts.append, on_reply_text=replies.append,
                                on_speech_started=lambda: speech.append(True))
            try:
                await asyncio.wait_for(dialog.run(), timeout=15)
            finally:
                server.close()
                await server.wait_closed()
            return audio, runner, texts, replies, speech, dialog

        audio, runner, texts, replies, speech, dialog = asyncio.run(scenario())

        # 鉴权与握手
        self.assertEqual(seen['headers']['x-api-key'], 'test-key')
        self.assertNotIn('x-api-app-id', seen['headers'])

        # session.create 契约
        session = seen['session']['session']
        self.assertEqual(session['model'], vd.DEFAULT_MODEL)
        self.assertEqual(session['audio']['input']['format']['rate'], 16000)
        self.assertEqual(session['audio']['output']['format']['type'], 'pcm_s16le')
        self.assertEqual(len(session['tools']), 1)

        # 上行音频：每包必须是 640 字节（20ms @16k int16）
        self.assertTrue(seen['packets'])
        self.assertEqual(set(seen['packets']), {640})

        # 工具调用：真的执行了，并且按 role=tool 回传
        self.assertEqual(runner.calls, ['drone.takeoff()'])
        result = seen['tool_result']
        self.assertEqual(result['type'], 'conversation.item.create')
        item = result['items'][0]
        self.assertEqual(item['call_id'], 'call-1')
        self.assertEqual(item['role'], 'tool')
        self.assertEqual(item['content'][0]['type'], 'input_text')
        self.assertIn('起飞完成', item['content'][0]['text'])

        # 回调与音频
        self.assertEqual(texts, ['起飞'])
        self.assertEqual(replies, ['好的，已起飞'])
        self.assertTrue(speech)
        self.assertEqual(audio.speaker.size, 4800)
        self.assertTrue(audio.started)
        self.assertTrue(audio.closed)
        self.assertEqual(dialog.session_id, 'sess-1')

    def test_interrupt_tool_hits_drone(self):
        async def scenario():
            server = await serve(handler_interrupt, '127.0.0.1', 0)
            port = server.sockets[0].getsockname()[1]
            audio, runner = FakeAudio(), FakeRunner()
            dialog = VolcDuplex(runner, audio, instructions='', api_key='k',
                                url='ws://127.0.0.1:%d' % port)
            try:
                await asyncio.wait_for(dialog.run(), timeout=15)
            finally:
                server.close()
                await server.wait_closed()
            return runner

        runner = asyncio.run(scenario())
        self.assertEqual(runner.demo.drone.calls, ['land'])

    def test_missing_key_fails_fast(self):
        dialog = VolcDuplex(FakeRunner(), FakeAudio(), instructions='', api_key='',
                            url='ws://127.0.0.1:1')
        with self.assertRaises(VolcDialogError):
            asyncio.run(dialog.run())


async def handler_interrupt(ws):
    sent = False
    try:
        async for message in ws:
            event = json.loads(message)
            if event.get('type') == vd.EV_SESSION_CREATE:
                await ws.send(json.dumps({'type': vd.EV_SESSION_CREATED,
                                          'session': {'id': 's'}}))
            elif event.get('type') == vd.EV_AUDIO_APPEND and not sent:
                sent = True
                await ws.send(json.dumps({
                    'type': vd.EV_TOOL_CALL,
                    'items': [{'call_id': 'c2', 'name': 'interrupt',
                               'arguments': '{"land":true}'}]}))
            elif event.get('type') == vd.EV_ITEM_CREATE:
                await ws.send(json.dumps({'type': vd.EV_RESPONSE_DONE,
                                          'response': {'usage': {}}}))
                await asyncio.sleep(0.05)
                await ws.close()
    except ConnectionClosed:
        pass


if __name__ == '__main__':
    unittest.main(verbosity=2)
