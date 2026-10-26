"""Offline protocol/ordering tests. Only a loopback socket and fake audio are used."""
import asyncio
import base64
import json
import struct
import unittest
from unittest.mock import patch

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from DuplexAudio import PlaybackBuffer
import VoiceProtocol as vd


class FakeRunner:
    def __init__(self):
        self.calls, self.interrupts, self.order = [], [], []
        self.cancel_count = 0
        self.gate = None
        self.failure = None

    async def run(self, code):
        self.calls.append(code)
        self.order.append('run')
        if self.gate:
            await self.gate.wait()
        if self.failure:
            raise self.failure
        return {'ok': True, 'status': 'completed', 'simulated': True}

    def cancel(self):
        self.cancel_count += 1
        self.order.append('cancel')

    async def interrupt(self, land=False):
        self.interrupts.append(land)
        self.order.append('land' if land else 'stop')
        if self.gate:
            self.gate.set()
        return {'ok': True, 'status': 'interrupted', 'land': land, 'simulated': True}


class FakeAudio:
    def __init__(self):
        self.speaker = PlaybackBuffer(rate=24000)
        self.started = self.closed = False
        self.queued_at_close = 0
        self.block = struct.pack('<480h', *([300] * 480))

    def start(self):
        self.started = True

    def close(self):
        self.queued_at_close = max(self.queued_at_close, self.speaker.size)
        self.closed = True

    async def chunks(self):
        while True:
            yield self.block
            await asyncio.sleep(.001)  # Client must pace even an over-eager producer.


class MemorySocket:
    def __init__(self):
        self.sent = []
        self.block = None
        self.fail = None

    async def send(self, message):
        if self.block:
            await self.block.wait()
        if self.fail:
            raise self.fail
        self.sent.append(json.loads(message))


def tool(name='run_program', call_id='c1', **arguments):
    return {'call_id': call_id, 'name': name,
            'arguments': json.dumps(arguments or {'code': 'drone.takeoff()'})}


class SessionPayload(unittest.TestCase):
    def test_current_official_session_contract(self):
        result = vd.build_session(instructions='助手', tools=[{'type': 'function', 'name': 'x'}])
        self.assertEqual(result['type'], 'session.create')
        self.assertEqual(result['session']['model'], '1.2.6.1')
        self.assertEqual(result['session']['audio']['input']['format'], {'type': 'pcm', 'rate': 16000})
        self.assertEqual(result['session']['audio']['output']['format'], {'type': 'pcm_s16le', 'rate': 24000})
        self.assertNotIn('turn_detection', json.dumps(result))
        self.assertNotIn('type', result['session'])  # Not an OpenAI session object.

    def test_unknown_voice_fails_instead_of_silent_change(self):
        with self.assertRaises(ValueError):
            vd.build_session(instructions='', voice='alloy')

    def test_old_binary_endpoint_rejected(self):
        with self.assertRaisesRegex(vd.VolcDialogError, '二进制'):
            vd.validate_url('wss://openspeech.bytedance.com/api/v3/realtime/dialogue')

    def test_no_remote_plaintext_or_secret_in_url(self):
        for url in ('ws://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue',
                    'wss://example.com/api/v3/duplex/realtime/dialogue',
                    vd.DEFAULT_URL + '?key=secret'):
            with self.subTest(url=url), self.assertRaises(vd.VolcDialogError):
                vd.validate_url(url)

    def test_missing_key_does_not_take_environment_fallback(self):
        with patch.dict('os.environ', {'VOLC_API_KEY': 'must-not-use'}):
            dialog = vd.VolcDuplex(FakeRunner(), FakeAudio(), instructions='', api_key='')
            with self.assertRaises(vd.VolcDialogError):
                dialog.check_credentials()

    def test_connection_ids_are_unique_without_logging_key(self):
        dialog = vd.VolcDuplex(FakeRunner(), FakeAudio(), instructions='', api_key='test-secret')
        first, second = dialog.headers(), dialog.headers()
        self.assertEqual(first['X-Api-Key'], 'test-secret')
        self.assertNotEqual(first['X-Api-Connect-Id'], second['X-Api-Connect-Id'])


class Resampling(unittest.TestCase):
    def test_fixed_packet_and_split_continuity(self):
        pcm = struct.pack('<480h', *range(480))
        entire = vd.Downsampler().process(pcm)
        stream = vd.Downsampler()
        parts = b''.join(stream.process(pcm[i:i + 74]) for i in range(0, len(pcm), 74))
        self.assertEqual(entire, parts)
        self.assertEqual(len(entire), 640)

    def test_odd_pcm_fails(self):
        with self.assertRaises(ValueError):
            vd.Downsampler().process(b'1')


class Pacing(unittest.IsolatedAsyncioTestCase):
    async def test_burst_producer_is_paced_at_actual_sender_boundary(self):
        class Clock:
            now = 10.
            def __init__(self): self.sleeps = []
            def read(self): return self.now
            async def sleep(self, seconds):
                self.sleeps.append(seconds)
                self.now += seconds
        class BurstAudio(FakeAudio):
            async def chunks(self):
                # Three complete packets are available immediately. Pacing
                # must come from the sender, independently of producer waits.
                yield self.block * 3
        clock = Clock()
        sent_at = []
        class TimedSocket(MemorySocket):
            async def send(self, message):
                sent_at.append(clock.now)
                await super().send(message)
        audio = BurstAudio()
        dialog = vd.VolcDuplex(FakeRunner(), audio, instructions='', api_key='fake')
        dialog.ws = TimedSocket()
        try:
            with patch.object(vd.time, 'perf_counter', new=clock.read), \
                    patch.object(vd.asyncio, 'sleep', new=clock.sleep):
                await dialog._send_audio()
            self.assertEqual(len(sent_at), 3)
            self.assertEqual(len(clock.sleeps), 3)
            for index, timestamp in enumerate(sent_at):
                self.assertAlmostEqual(timestamp - 10., index * .02, places=9)
            self.assertTrue(all(event['type'] == vd.EV_AUDIO_APPEND for event in dialog.ws.sent))
            self.assertEqual([len(base64.b64decode(event['audio'])) for event in dialog.ws.sent],
                             [vd.INPUT_PACKET_BYTES] * 3)
        finally:
            await dialog.close()


class Ordering(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runner, self.audio, self.logs = FakeRunner(), FakeAudio(), []
        self.dialog = vd.VolcDuplex(self.runner, self.audio, instructions='', api_key='fake',
                                    on_log=self.logs.append)
        self.dialog.ws = MemorySocket()
        await self.turn()

    async def asyncTearDown(self):
        await self.dialog.close()

    async def turn(self, text='起飞'):
        await self.dialog._handle({'type': vd.EV_ASR_STARTED})
        await self.dialog._handle({'type': vd.EV_ASR_COMPLETED, 'transcript': text})

    async def flush(self):
        if self.dialog.tool_tasks:
            await asyncio.gather(*list(self.dialog.tool_tasks))

    async def call(self, items, **extra):
        await self.dialog._handle({'type': vd.EV_TOOL_CALL, 'items': items, **extra})
        await self.flush()

    def results(self):
        return [json.loads(x['items'][0]['content'][0]['text'])
                for x in self.dialog.ws.sent if x['type'] == vd.EV_ITEM_CREATE]

    async def test_async_program_and_documented_tool_result(self):
        await self.call([tool()])
        self.assertEqual(self.runner.calls, ['drone.takeoff()'])
        reply = [x for x in self.dialog.ws.sent if x['type'] == vd.EV_ITEM_CREATE][0]
        self.assertEqual(reply['items'][0]['call_id'], 'c1')
        self.assertEqual(reply['items'][0]['role'], 'tool')
        self.assertTrue(self.results()[0]['ok'])

    async def test_duplicate_call_executes_once(self):
        await self.call([tool(), tool()])
        await self.call([tool()])
        self.assertEqual(len(self.runner.calls), 1)

    async def test_parallel_programs_are_rejected_not_queued(self):
        self.runner.gate = asyncio.Event()
        await self.dialog._handle({'type': vd.EV_TOOL_CALL,
                                   'items': [tool(), tool(call_id='c2')]})
        await asyncio.sleep(0)
        self.runner.gate.set()
        await self.flush()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertIn('busy', [r['status'] for r in self.results()])

    async def test_argument_objects_nan_extra_keys_and_boolean_strings_rejected(self):
        bad = [dict(tool(call_id='a'), arguments='[]'),
               dict(tool(call_id='b'), arguments='{"code":NaN}'),
               tool(call_id='c', code='x', extra=True),
               tool('interrupt', call_id='d', land='false'),
               dict(tool(call_id='e'), arguments={'code': 'x'})]
        await self.call(bad)
        self.assertFalse(self.runner.calls)
        self.assertFalse(self.runner.interrupts)
        self.assertTrue(all(r['ok'] is False for r in self.results()))

    async def test_cancel_before_scheduled_tool_prevents_execution(self):
        await self.dialog._handle({'type': vd.EV_TOOL_CALL, 'items': [tool()]})
        await self.turn('停止')  # Before the queued tool coroutine starts.
        await self.flush()
        self.assertFalse(self.runner.calls)
        self.assertFalse(self.results())

    async def test_speech_interrupt_stops_locally_before_blocked_network(self):
        self.runner.gate = asyncio.Event()
        await self.dialog._handle({'type': vd.EV_TOOL_CALL, 'items': [tool()]})
        await asyncio.sleep(0)
        self.dialog.ws.block = asyncio.Event()
        await self.turn('停止')
        await asyncio.sleep(0)
        self.assertEqual(self.runner.interrupts, [False])
        self.assertEqual(self.audio.speaker.size, 0)
        self.dialog.ws.block.set()
        await self.flush()
        self.assertFalse(self.results())  # Obsolete run result cannot enter a new turn.

    async def test_emergency_uses_unified_runtime_without_demo_owner_bug(self):
        self.dialog.ws.block = asyncio.Event()
        result = await asyncio.wait_for(self.dialog.emergency(land=True), .2)
        self.assertTrue(result['ok'])
        self.assertEqual(self.runner.interrupts, [True])
        self.dialog.ws.block.set()
        await self.flush()

    async def test_old_response_and_untagged_output_cannot_resume_after_bargein(self):
        delta = {'type': vd.EV_AUDIO_DELTA, 'response_id': 'old',
                 'delta': base64.b64encode(b'\0\0' * 30).decode()}
        await self.dialog._handle(delta)
        self.assertGreater(self.audio.speaker.size, 0)
        await self.turn('新要求')
        await self.dialog._handle(delta)
        await self.dialog._handle(dict(delta, response_id='new'))
        self.assertEqual(self.audio.speaker.size, 0)
        await self.dialog._handle({'type': vd.EV_RESPONSE_CANCELED})
        await self.dialog._handle(delta)
        self.assertEqual(self.audio.speaker.size, 0)
        await self.dialog._handle(dict(delta, response_id='new'))
        self.assertGreater(self.audio.speaker.size, 0)
        await self.flush()

    async def test_stale_response_tool_rejected(self):
        self.dialog.response_epochs['old'] = self.dialog.epoch - 1
        await self.call([tool()], response_id='old')
        self.assertFalse(self.runner.calls)

    async def test_tool_interrupt_blocks_further_programs_in_same_turn(self):
        await self.call([tool('interrupt', land=False)])
        await self.call([tool(call_id='late')])
        self.assertEqual(self.runner.interrupts, [False])
        self.assertFalse(self.runner.calls)
        self.assertEqual(self.results()[-1]['status'], 'cancelled')

    async def test_no_tool_execution_without_actual_completed_input(self):
        self.dialog.turn_complete = False
        await self.call([tool()])
        self.assertFalse(self.runner.calls)

    async def test_audio_rejects_corruption_instead_of_truncating(self):
        for raw in ('!', base64.b64encode(b'x').decode()):
            with self.subTest(raw=raw), self.assertRaises(vd.VolcDialogError):
                await self.dialog._handle({'type': vd.EV_AUDIO_DELTA, 'delta': raw})

    async def test_latency_name_describes_measured_boundary(self):
        await self.dialog._handle({'type': vd.EV_AUDIO_DELTA,
                                   'delta': base64.b64encode(b'\0\0').decode()})
        latency = [x for x in self.logs if 'first_audio' in x['event']]
        self.assertEqual(len(latency), 1)
        self.assertEqual(latency[0]['event'], 'asr_completed_to_first_audio_received_ms')
        self.assertIn('not_audible', latency[0]['scope'])

    async def test_usage_callback_receives_actual_event(self):
        received = []
        self.dialog.on_usage = received.append
        await self.dialog._handle({'type': vd.EV_RESPONSE_DONE, 'usage': {'input_tokens': 12}})
        self.assertEqual(received, [{'input_tokens': 12}])

    async def test_real_service_cumulative_asr_and_authoritative_completed_text(self):
        received = []
        self.dialog.on_user_text = received.append
        await self.dialog._handle({'type': vd.EV_ASR_STARTED, 'item_id': 'input-real', 'round_id': 'round-real'})
        for hypothesis in ('please', 'please check', 'please check the drones',
                           'please check the drone status'):
            await self.dialog._handle({'type': vd.EV_ASR_DELTA, 'item_id': 'input-real',
                                       'content_index': 0, 'delta': hypothesis})
        await self.dialog._handle({'type': vd.EV_ASR_COMPLETED, 'item_id': 'input-real',
                                   'content_index': 0, 'text': 'Please check the drone status.'})
        self.assertEqual(received, ['Please check the drone status.'])
        self.assertEqual([x for x in self.logs if x['event'] == 'asr_final'][-1]['text'], received[0])
        await self.flush()

    async def test_late_completed_asr_cannot_reopen_cancelled_input(self):
        await self.dialog._handle({'type': vd.EV_ASR_STARTED, 'item_id': 'old-input', 'round_id': 'old-round'})
        await self.dialog._handle({'type': vd.EV_ASR_STARTED, 'item_id': 'new-input', 'round_id': 'new-round'})
        await self.dialog._handle({'type': vd.EV_ASR_COMPLETED, 'item_id': 'old-input', 'text': '起飞'})
        self.assertFalse(self.dialog.turn_complete)
        await self.dialog._handle({'type': vd.EV_ASR_COMPLETED, 'item_id': 'new-input', 'text': '查询状态'})
        self.assertTrue(self.dialog.turn_complete)
        await self.dialog._handle({'type': vd.EV_RESPONSE_CANCELED})
        await self.call([tool()], round_id='old-round')
        self.assertFalse(self.runner.calls)

    async def test_muting_announces_protocol_events(self):
        await self.dialog.mute(True)
        await self.dialog.mute(True)
        await self.dialog.mute(False)
        self.assertEqual([x['type'] for x in self.dialog.ws.sent],
                         [vd.EV_AUDIO_MUTE, vd.EV_AUDIO_UNMUTE])

    async def test_failed_device_result_is_not_success(self):
        async def failed(code):
            return {'ok': False, 'status': 'failed', 'reason': 'send_failed'}
        self.runner.run = failed
        await self.call([tool()])
        self.assertFalse(self.results()[0]['ok'])

    async def test_exception_does_not_echo_api_key(self):
        self.runner.failure = RuntimeError('secret=' + self.dialog.api_key)
        await self.call([tool()])
        serialized = json.dumps(self.dialog.ws.sent)
        self.assertNotIn('secret=' + self.dialog.api_key, serialized)
        self.assertIn('[redacted]', serialized)

    async def test_close_is_idempotent_and_failed_stop_is_visible(self):
        self.dialog.closing = False
        async def failed_stop(land=False):
            return {'ok': False, 'reason': 'send_failed'}
        self.runner.interrupt = failed_stop
        with self.assertRaises(vd.VolcDialogError):
            await self.dialog.close()
        self.assertTrue(self.audio.closed)
        await self.dialog.close()


class Loopback(unittest.IsolatedAsyncioTestCase):
    async def test_session_audio_tool_response_and_cleanup(self):
        seen = {'packets': [], 'tool': None}
        async def handler(ws):
            seen['headers'] = dict(ws.request.headers)
            async for message in ws:
                event = json.loads(message)
                if event['type'] == vd.EV_SESSION_CREATE:
                    seen['session'] = event
                    await ws.send(json.dumps({'type': vd.EV_SESSION_CREATED, 'session': {'id': 's1'}}))
                elif event['type'] == vd.EV_AUDIO_APPEND:
                    seen['packets'].append(base64.b64decode(event['audio'], validate=True))
                    if len(seen['packets']) == 3:
                        for payload in ({'type': vd.EV_ASR_STARTED},
                                        {'type': vd.EV_ASR_COMPLETED, 'transcript': '起飞'},
                                        {'type': vd.EV_TOOL_CALL, 'items': [tool()]}):
                            await ws.send(json.dumps(payload))
                elif event['type'] == vd.EV_ITEM_CREATE:
                    seen['tool'] = event
                    await ws.send(json.dumps({'type': vd.EV_TEXT_DONE, 'text': '结果已收到'}))
                    await ws.send(json.dumps({'type': vd.EV_RESPONSE_DONE, 'usage': {'total_tokens': 10}}))
                    await ws.send(json.dumps({'type': vd.EV_SESSION_CLOSED}))
                    return
        async with serve(handler, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            audio, runner, replies = FakeAudio(), FakeRunner(), []
            dialog = vd.VolcDuplex(runner, audio, instructions='助手', api_key='fake',
                                    url=f'ws://127.0.0.1:{port}', on_reply_text=replies.append)
            await asyncio.wait_for(dialog.run(), 5)
        # Receiver scheduling can coalesce independently paced sends. Check
        # framing/content here; Pacing tests the sender with a virtual clock.
        self.assertGreaterEqual(len(seen['packets']), 3)
        self.assertEqual({len(packet) for packet in seen['packets']}, {640})
        self.assertTrue(all(packet == struct.pack('<320h', *([300] * 320))
                            for packet in seen['packets']))
        self.assertEqual(runner.calls, ['drone.takeoff()'])
        self.assertEqual(runner.interrupts, [False])
        self.assertEqual(replies, ['结果已收到'])
        self.assertTrue(audio.started and audio.closed)
        self.assertEqual(json.loads(seen['tool']['items'][0]['content'][0]['text'])['ok'], True)

    async def test_startup_error_is_propagated_without_twenty_second_wait(self):
        async def handler(ws):
            await ws.recv()
            await ws.send(json.dumps({'type': vd.EV_ERROR, 'error': {'code': 'bad_session'}}))
            await asyncio.sleep(.1)
        async with serve(handler, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            audio = FakeAudio()
            dialog = vd.VolcDuplex(FakeRunner(), audio, instructions='', api_key='fake',
                                    url=f'ws://127.0.0.1:{port}')
            with self.assertRaisesRegex(vd.VolcDialogError, 'bad_session'):
                await asyncio.wait_for(dialog.run(), 2)
        self.assertFalse(audio.started)
        self.assertTrue(audio.closed)

    async def test_background_tool_result_send_error_is_not_swallowed(self):
        async def handler(ws):
            await ws.recv()
            await ws.send(json.dumps({'type': vd.EV_SESSION_CREATED, 'session': {'id': 's'}}))
            for payload in ({'type': vd.EV_ASR_STARTED},
                            {'type': vd.EV_ASR_COMPLETED, 'transcript': '起飞'},
                            {'type': vd.EV_TOOL_CALL, 'items': [tool()]}):
                await ws.send(json.dumps(payload))
            try:
                async for _ in ws:
                    pass
            except ConnectionClosed:
                pass
        class FailingSend(vd.VolcDuplex):
            async def send(self, event):
                if event['type'] == vd.EV_ITEM_CREATE:
                    raise RuntimeError('tool_result_send_failed')
                await super().send(event)
        async with serve(handler, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            audio = FakeAudio()
            dialog = FailingSend(FakeRunner(), audio, instructions='', api_key='fake',
                                 url=f'ws://127.0.0.1:{port}')
            with self.assertRaisesRegex(RuntimeError, 'tool_result_send_failed'):
                await asyncio.wait_for(dialog.run(), 3)
        self.assertTrue(audio.closed)

    async def test_session_deadline_cancels_runtime_and_audio(self):
        async def handler(ws):
            await ws.recv()
            await ws.send(json.dumps({'type': vd.EV_SESSION_CREATED, 'session': {'id': 's'}}))
            try:
                async for _ in ws:
                    pass
            except ConnectionClosed:
                pass
        async with serve(handler, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            runner, audio = FakeRunner(), FakeAudio()
            dialog = vd.VolcDuplex(runner, audio, instructions='', api_key='fake',
                                    url=f'ws://127.0.0.1:{port}', max_session_seconds=.5)
            with self.assertRaisesRegex(vd.VolcDialogError, '时限'):
                await dialog.run()
        self.assertEqual(runner.interrupts, [False])
        self.assertTrue(audio.closed)


if __name__ == '__main__':
    unittest.main(verbosity=2)
