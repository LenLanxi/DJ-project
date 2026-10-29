"""Offline Alibaba protocol tests. Synthetic PCM/JPEG; loopback sockets only."""
import asyncio
import base64
from dataclasses import replace
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

import cv2
import numpy as np
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parents[2]
from providers.AlibabaRealtime import (AlibabaConfig, AlibabaError, MODEL, normalize_tools,
                             session_update, validate_arguments)
from AlibabaProbe import AlibabaRealtime, PcmActivity, speech_end_position


CONFIG = AlibabaConfig('private-test-key-never-print', 'workspace1',
                       'wss://workspace1.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime')
TOOLS = [{'type': 'function', 'name': 'get_status', 'description': 'Query fake status',
          'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}]
PCM = struct.pack('<3200h', *([500] * 3200))


class ConfigTests(unittest.TestCase):
    def test01_exact_model_and_workspace_endpoint_without_secret_repr(self):
        self.assertTrue(CONFIG.url.endswith('?model=' + MODEL))
        self.assertNotIn(CONFIG.api_key, repr(CONFIG))
        self.assertEqual(replace(CONFIG, url=CONFIG.url).url, CONFIG.url)

    def test02_no_remote_endpoint_or_legacy_model_fallback(self):
        invalid = [dict(model='qwen3-omni-flash-realtime'),
                   dict(url='wss://evil.example/api-ws/v1/realtime'),
                   dict(url=CONFIG.url + '&key=secret'),
                   dict(url=CONFIG.url.replace('wss:', 'ws:')),
                   dict(url=CONFIG.url.replace('workspace1', 'otherworkspace'))]
        for values in invalid:
            with self.subTest(fields=tuple(values)), self.assertRaises(AlibabaError):
                replace(CONFIG, **values)

    def test03_missing_credentials_and_invalid_voice_are_rejected_at_construction(self):
        for values in (dict(api_key=''), dict(workspace_id='invalid/workspace'),
                       dict(voice='invalid/voice')):
            with self.assertRaises(AlibabaError): replace(CONFIG, **values)

    def test04_explicit_credentials_file_does_not_mutate_environment(self):
        import os
        before = dict(os.environ)
        value = {'Region': 'cn-beijing', 'ApiKey': CONFIG.api_key,
                 'WorkspaceId': CONFIG.workspace_id, 'WebSocketUrl': CONFIG.url, 'Model': MODEL,
                 'AllowedModels': [MODEL]}
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'Credentials.json'
            file.write_text(json.dumps(value), encoding='utf-8')
            config = AlibabaConfig.from_credentials(file)
        self.assertEqual(config.api_key, CONFIG.api_key)
        self.assertEqual(dict(os.environ), before)

    def test05_session_uses_new_audio_schema_and_native_local_functions(self):
        update = session_update(CONFIG, 'test', normalize_tools(TOOLS))['session']
        self.assertIsNone(update['turn_detection'])
        self.assertEqual(update['audio']['input']['format']['sample_rate'], 16000)
        self.assertEqual(update['audio']['output']['voice'], 'Tina')
        self.assertEqual(update['input_audio_transcription'], {'model': 'qwen3-asr-flash-realtime'})
        self.assertFalse(update['enable_search'])
        self.assertEqual(update['tools'][0]['function']['name'], 'get_status')
        self.assertNotIn('input_audio_format', update)

    def test06_argument_validation_rejects_unknown_extra_boolean_and_nonfinite(self):
        schema = {'type': 'object', 'properties': {'number': {'type': 'integer', 'minimum': 0}},
                  'required': ['number'], 'additionalProperties': False}
        for arguments in ({'number': True}, {'number': -1}, {'number': 1, 'extra': 0}, {}):
            with self.assertRaises(AlibabaError): validate_arguments(arguments, schema)
        with self.assertRaises(AlibabaError): validate_arguments(float('nan'), {'type': 'number'})
        with self.assertRaises(AlibabaError): validate_arguments('x', {'type': 'string', 'pattern': 'x'})

    def test07_speech_endpoint_excludes_zero_padding(self):
        self.assertAlmostEqual(speech_end_position(PCM + b'\0\0' * 16000), .2)
        with self.assertRaises(AlibabaError): speech_end_position(b'\0\0' * 320)

    def test08_activity_requires_twenty_milliseconds_not_a_nonempty_silent_packet(self):
        activity = PcmActivity()
        self.assertIsNone(activity.push(b'\0\0' * 480, 1.))
        loud = struct.pack('<240h', *([500] * 240))
        self.assertIsNone(activity.push(loud, 10.))
        detected = activity.push(loud, 20.)
        self.assertEqual(detected['first_window_received_ms'], 10.)
        self.assertEqual(detected['confirmed_received_ms'], 20.)
        self.assertEqual(detected['sample_offset'], 480)

    def test09_activity_handles_split_windows_and_resets_after_silence(self):
        activity = PcmActivity()
        loud = struct.pack('<240h', *([500] * 240))
        self.assertIsNone(activity.push(loud[:240], 1.))
        self.assertIsNone(activity.push(loud[240:], 2.))
        self.assertIsNone(activity.push(b'\0\0' * 240, 3.))
        self.assertIsNone(activity.push(loud, 4.))
        detected = activity.push(loud, 5.)
        self.assertEqual(detected['first_window_received_ms'], 4.)
        self.assertEqual(detected['sample_offset'], 480)

    def test10_semantic_checks_require_understood_audio_and_actual_nonzero_output(self):
        from AlibabaBenchmark import semantic_check, latency
        valid = {'ok': True, 'reply_text': '测试收到。', 'asr_text': '请回答测试收到',
                 'output_audio_bytes': 960, 'metrics': {'first_nonzero_audio_ms': 10}}
        self.assertTrue(semantic_check('speech', valid)[0])
        self.assertFalse(semantic_check('speech', dict(valid, reply_text='好的测试收到'))[0])
        self.assertFalse(semantic_check('speech', dict(valid, asr_text=''))[0])
        self.assertFalse(semantic_check('speech', dict(valid, metrics={}))[0])
        self.assertFalse(semantic_check('vision', dict(valid, image_sent=True, reply_text='红色'), '蓝')[0])
        measured = latency({'input_end_send_ms': 100., 'input_end_packet_overshoot_ms': 5.,
                            'speech_end_anchor_ms': 95., 'first_audio_ms': 110.})
        self.assertEqual(measured['first_audio_ms'], 15.)

    def test11_media_delivery_is_independent_from_an_inexact_repeated_answer(self):
        from AlibabaBenchmark import semantic_check, delivery_check
        delivered = {'ok': True, 'reply_text': '收到，测试正常。', 'asr_text': '请只回答一句话，测试收到。',
                     'output_audio_bytes': 960, 'metrics': {'first_nonzero_audio_ms': 10, 'asr_final_ms': 8}}
        clean = {'connection_closed': True, 'sender_finished': True, 'receiver_finished': True}
        self.assertTrue(delivery_check(delivered, clean))
        self.assertFalse(semantic_check('speech', delivered)[0])
        self.assertFalse(delivery_check(dict(delivered, asr_text=''), clean))
        self.assertFalse(delivery_check(dict(delivered, asr_text='请只回答一句话'), clean))
        self.assertFalse(delivery_check(delivered, dict(clean, connection_closed=False)))

    def test12_existing_receipt_revalidation_preserves_metrics_and_requires_acoustic_proof(self):
        from AlibabaBenchmark import recompute_delivery_evidence
        sample = {'task': 'speech', 'transport_ok': True, 'transcripts': ['请回答测试收到'],
                  'output_audio_bytes': 960, 'timing': {'raw_metrics': {
                      'asr_final_ms': 8., 'first_nonzero_audio_ms': 10., 'first_audio_ms': 10.}},
                  'audio_activity': {'rms_threshold': 64, 'window_ms': 10,
                      'consecutive_windows': 2, 'first_window_received_ms': 10.},
                  'first_audio_definition': 'first_received_window_RMS64_two_consecutive_10ms_not_playback',
                  'cleanup': {'connection_closed': True, 'sender_finished': True, 'receiver_finished': True}}
        original = json.dumps(sample, sort_keys=True)
        evidence = recompute_delivery_evidence(sample)
        self.assertTrue(evidence['delivery_verified'])
        self.assertTrue(evidence['delivery_evidence']['recomputed_from_existing_evidence'])
        self.assertEqual(json.dumps(sample, sort_keys=True), original)
        self.assertFalse(recompute_delivery_evidence(dict(sample, audio_activity=None))['delivery_verified'])


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def run_server(self, handler, *, tool_handler=None, audio=PCM, image=None, timeout=2,
                         sleep=asyncio.sleep, **options):
        async with serve(handler, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            def local_connector(_url, **kwargs):
                return connect(f'ws://127.0.0.1:{port}', **kwargs)
            adapter = AlibabaRealtime(CONFIG, instructions='Use the real tool result', tools=TOOLS,
                                       tool_handler=tool_handler, connect=local_connector, sleep=sleep)
            return await adapter.run_turn(audio, image, realtime_pacing=options.pop('realtime_pacing', False),
                                          timeout_seconds=timeout, **options)

    async def test08_pcm_image_tool_result_continuation_and_usage_roundtrip(self):
        seen, tools = [], []
        picture = cv2.imencode('.jpg', np.zeros((16, 16, 3), np.uint8))[1].tobytes()
        async def tool(name, args, call_id):
            tools.append((name, args, call_id))
            return {'ok': True, 'simulated': True, 'battery': 73, 'nonce': 'actualResult7'}
        async def handler(ws):
            await ws.send(json.dumps({'type': 'session.created'}))
            self.assertIn('Bearer ', ws.request.headers['Authorization'])
            update = json.loads(await ws.recv())
            self.assertEqual(update['session']['tools'][0]['function']['name'], 'get_status')
            await ws.send(json.dumps({'type': 'session.updated'}))
            phase = 0
            async for raw in ws:
                event = json.loads(raw); seen.append(event)
                if event['type'] == 'input_image_buffer.append':
                    self.assertTrue(any(item['type'] == 'input_audio_buffer.append' for item in seen))
                    self.assertEqual(base64.b64decode(event['image']), picture)
                if event['type'] == 'input_audio_buffer.commit':
                    await ws.send(json.dumps({'type': 'input_audio_buffer.committed'}))
                    await ws.send(json.dumps({'type': 'conversation.item.input_audio_transcription.completed',
                                              'transcript': '请查询状态'}))
                elif event['type'] == 'response.create' and phase == 0:
                    phase = 1
                    call = {'name': 'get_status', 'arguments': '{}', 'call_id': 'oneCall'}
                    await ws.send(json.dumps({'type': 'response.created', 'response': {'id': 'one'}}))
                    await ws.send(json.dumps({'type': 'response.function_call_arguments.delta', 'delta': 'bad partial'}))
                    await ws.send(json.dumps({'type': 'response.function_call_arguments.done', **call}))
                    await ws.send(json.dumps({'type': 'response.done', 'response': {'status': 'completed',
                        'output': [{'type': 'function_call', **call}], 'usage': {'input_tokens': 100}}}))
                elif event['type'] == 'conversation.item.create':
                    self.assertEqual(event['item']['call_id'], 'oneCall')
                    self.assertEqual(json.loads(event['item']['output'])['nonce'], 'actualResult7')
                elif event['type'] == 'response.create' and phase == 1:
                    phase = 2
                    for message in ({'type': 'response.audio_transcript.delta', 'delta': '模拟电量'},
                                    {'type': 'response.audio.delta', 'delta': base64.b64encode(struct.pack('<480h', *([500]*480))).decode()},
                                    {'type': 'response.audio_transcript.done', 'transcript': '模拟电量73，actualResult7'},
                                    {'type': 'response.done', 'response': {'status': 'completed', 'output': [],
                                                                          'usage': {'output_tokens': 9}}}):
                        await ws.send(json.dumps(message))
        result = await self.run_server(handler, tool_handler=tool, image=picture)
        self.assertTrue(result['ok'], result)
        self.assertEqual(tools, [('get_status', {}, 'oneCall')])
        audio = b''.join(base64.b64decode(item['audio']) for item in seen if item['type']=='input_audio_buffer.append')
        self.assertEqual(audio, PCM)
        self.assertEqual(result['asr_text'], '请查询状态')
        self.assertIn('actualResult7', result['reply_text'])
        self.assertTrue(result['image_sent'])
        self.assertEqual(result['output_audio_bytes'], 960)
        self.assertEqual(result['usage'], [{'input_tokens': 100}, {'output_tokens': 9}])
        for metric in ('input_end_send_ms', 'commit_ms', 'first_text_ms', 'first_audio_ms', 'tool_call_ms',
                       'result_sent_ms', 'post_result_first_text_ms', 'post_result_first_audio_ms', 'response_done_ms'):
            self.assertIn(metric, result['metrics'])
        self.assertNotIn(CONFIG.api_key, json.dumps(result))

    async def test09_provider_error_is_redacted_without_api_or_model_retry(self):
        async def handler(ws):
            await ws.send(json.dumps({'type': 'error', 'error': {'code': 'AllocationQuota.FreeTierOnly',
                                         'message': 'secret ' + CONFIG.api_key}}))
        result = await self.run_server(handler)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'AllocationQuota.FreeTierOnly')
        self.assertNotIn(CONFIG.api_key, json.dumps(result))

    async def test16_error_without_code_preserves_bounded_redacted_official_fields(self):
        # The real automatic Status failure delivered only an error event to
        # the old logger. Missing code must not erase its other diagnostics.
        async def handler(ws):
            await ws.send(json.dumps({'type': 'error', 'error': {
                'type': 'invalid_request_error', 'message': 'echo ' + CONFIG.api_key + '\n' + 'x'*900,
                'param': 'response.create', 'untrusted_request_body': CONFIG.api_key}}))
        result = await self.run_server(handler)
        self.assertEqual(result['error_code'], 'provider_error')
        self.assertEqual(result['error_details']['type'], 'invalid_request_error')
        self.assertEqual(result['error_details']['param'], 'response.create')
        self.assertLessEqual(len(result['error_details']['message']), 512)
        self.assertNotIn('\n', result['error_details']['message'])
        self.assertIn('[REDACTED]', result['error_details']['message'])
        self.assertNotIn('untrusted_request_body', result['error_details'])
        self.assertNotIn(CONFIG.api_key, json.dumps(result))
        self.assertTrue(result['cleanup']['connection_closed'])

    async def test10_deadline_closes_socket_and_leaves_no_receiver_or_sender(self):
        closed = asyncio.Event()
        async def handler(ws):
            try:
                await ws.send(json.dumps({'type': 'session.created'}))
                await ws.recv()
                await ws.send(json.dumps({'type': 'session.updated'}))
                async for _ in ws: pass
            finally: closed.set()
        result = await self.run_server(handler, timeout=1)
        self.assertEqual(result['error_code'], 'deadline_exceeded')
        self.assertIn('session_ready_ms', result['metrics'])
        self.assertTrue(closed.is_set())

    async def test11_unknown_tool_and_bad_json_cannot_dispatch(self):
        for call in ({'name': 'takeoff', 'arguments': '{}', 'call_id': 'c'},
                     {'name': 'get_status', 'arguments': '{"x":NaN}', 'call_id': 'c'},
                     {'name': 'get_status', 'arguments': '{"extra":true}', 'call_id': 'c'}):
            dispatched = []
            async def tool(*args): dispatched.append(args)
            async def handler(ws):
                await ws.send(json.dumps({'type': 'session.created'})); await ws.recv()
                await ws.send(json.dumps({'type': 'session.updated'}))
                async for raw in ws:
                    if json.loads(raw)['type'] == 'input_audio_buffer.commit':
                        await ws.send(json.dumps({'type': 'input_audio_buffer.committed'}))
                        await ws.send(json.dumps({'type': 'response.function_call_arguments.done', **call}))
            result = await self.run_server(handler, tool_handler=tool)
            self.assertFalse(result['ok'])
            self.assertEqual(dispatched, [])

    async def test12_tool_exception_cannot_echo_secret(self):
        async def tool(*args): raise RuntimeError(CONFIG.api_key)
        async def handler(ws):
            await ws.send(json.dumps({'type': 'session.created'})); await ws.recv()
            await ws.send(json.dumps({'type': 'session.updated'}))
            async for raw in ws:
                if json.loads(raw)['type'] == 'input_audio_buffer.commit':
                    await ws.send(json.dumps({'type': 'input_audio_buffer.committed'}))
                    await ws.send(json.dumps({'type': 'response.function_call_arguments.done',
                                              'name': 'get_status', 'arguments': '{}', 'call_id': 'c'}))
                    await ws.send(json.dumps({'type': 'response.done', 'response': {'status': 'completed'}}))
        result = await self.run_server(handler, tool_handler=tool)
        self.assertEqual(result['error_code'], 'RuntimeError')
        self.assertNotIn(CONFIG.api_key, json.dumps(result))

    async def test14_proactive_tool_before_committed_audio_is_never_executed(self):
        calls = []
        async def tool(*args): calls.append(args)
        async def handler(ws):
            await ws.send(json.dumps({'type': 'session.created'})); await ws.recv()
            await ws.send(json.dumps({'type': 'session.updated'}))
            await ws.send(json.dumps({'type': 'response.function_call_arguments.done',
                                      'name': 'get_status', 'arguments': '{}', 'call_id': 'early'}))
        result = await self.run_server(handler, tool_handler=tool)
        self.assertEqual(result['error_code'], 'tool_without_committed_audio')
        self.assertEqual(calls, [])

    async def test15_closing_a_tool_await_cancels_it_without_returning_stale_result(self):
        entered, cancelled, sent = asyncio.Event(), asyncio.Event(), []
        async def tool(*args):
            entered.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        async def handler(ws):
            await ws.send(json.dumps({'type': 'session.created'})); await ws.recv()
            await ws.send(json.dumps({'type': 'session.updated'}))
            async for raw in ws:
                event = json.loads(raw); sent.append(event)
                if event['type'] == 'input_audio_buffer.commit':
                    await ws.send(json.dumps({'type': 'input_audio_buffer.committed'}))
                    await ws.send(json.dumps({'type': 'response.function_call_arguments.done',
                                              'name': 'get_status', 'arguments': '{}', 'call_id': 'c'}))
                    await ws.send(json.dumps({'type': 'response.done', 'response': {'status': 'completed'}}))
        async with serve(handler, '127.0.0.1', 0) as server:
            def local(_url, **kwargs):
                return connect(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}', **kwargs)
            adapter = AlibabaRealtime(CONFIG, instructions='', tools=TOOLS, tool_handler=tool, connect=local)
            work = asyncio.create_task(adapter.run_turn(PCM, realtime_pacing=False))
            await asyncio.wait_for(entered.wait(), 5)
            await adapter.close()
            with self.assertRaises(asyncio.CancelledError): await work
        self.assertTrue(cancelled.is_set())
        self.assertFalse(any(event['type'] == 'conversation.item.create' for event in sent))

    async def test13_oversized_audio_image_and_invalid_credentials_fail_before_connect(self):
        def fail(*args, **kwargs): self.fail('must not connect')
        adapter = AlibabaRealtime(CONFIG, instructions='', tools=[], connect=fail)
        for audio, image in ((b'odd', None), (PCM*100, None), (PCM, b'not jpeg')):
            with self.assertRaises(AlibabaError): await adapter.run_turn(audio, image)
        with self.assertRaises(AlibabaError):
            AlibabaRealtime(replace(CONFIG, api_key=''), instructions='', tools=[], connect=fail)

    async def test16_automatic_probe_preserves_distinct_items_and_effective_vad(self):
        async def handler(ws):
            await ws.send(json.dumps({'type': 'session.created'}))
            update = json.loads(await ws.recv())
            self.assertEqual(update['session']['turn_detection'],
                             {'type': 'server_vad', 'silence_duration_ms': 1200})
            await ws.send(json.dumps({'type': 'session.updated', 'session': {
                'turn_detection': update['session']['turn_detection']}}))
            for item_id, text in [('first', '查询电量'), ('second', '不要操作飞机')]:
                await ws.send(json.dumps({'type': 'conversation.item.input_audio_transcription.completed',
                                          'item_id': item_id, 'transcript': text}))
            await ws.send(json.dumps({'type': 'response.done', 'response': {'status': 'completed'}}))
            async for _ in ws: pass
        result = await self.run_server(handler, mode='server_vad', silence_ms=1200, vad_padding_seconds=0)
        self.assertEqual(result['transcriptions'], [dict(item_id='first', text='查询电量'),
                                                  dict(item_id='second', text='不要操作飞机')])
        self.assertEqual(result['effective_session']['turn_detection']['silence_duration_ms'], 1200)

    async def test17_frame_stream_refreshes_context_without_exceeding_one_hertz(self):
        seen = []
        picture = cv2.imencode('.jpg', np.zeros((16,16,3), np.uint8))[1].tobytes()
        async def handler(ws):
            await ws.send(json.dumps({'type':'session.created'})); await ws.recv()
            await ws.send(json.dumps({'type':'session.updated'}))
            async for raw in ws:
                event = json.loads(raw); seen.append(event)
                if event['type'] == 'input_audio_buffer.commit':
                    await ws.send(json.dumps({'type':'input_audio_buffer.committed'}))
                if event['type'] == 'response.create':
                    await ws.send(json.dumps({'type':'response.done','response':{'status':'completed'}}))
        result = await self.run_server(handler, audio=PCM*12, image=picture, image_interval_seconds=1)
        self.assertTrue(result['ok'])
        samples = 0
        positions = []
        for e in seen:
            if e['type'] == 'input_audio_buffer.append': samples += len(base64.b64decode(e['audio']))//2
            if e['type'] == 'input_image_buffer.append': positions.append(samples)
        self.assertEqual(len(positions),3)
        self.assertGreater(positions[0],0)
        self.assertTrue(all(b-a >= 16000 for a,b in zip(positions,positions[1:])))

    async def test18_invalid_image_rate_and_vad_wait_never_connect(self):
        def fail(*args, **kwargs): self.fail('must not connect')
        adapter = AlibabaRealtime(CONFIG, instructions='', tools=[], connect=fail)
        for options in ({'image_interval_seconds':.5}, {'image_interval_seconds':float('nan')},
                        {'silence_ms':True}, {'silence_ms':199}, {'silence_ms':6001}):
            with self.assertRaises(AlibabaError): await adapter.run_turn(PCM, **options)

    async def test19_early_cloud_commit_cannot_execute_tool_before_full_input_sent(self):
        calls = []
        pacing_calls = 0
        remaining_input = asyncio.Event()
        async def pace(_delay):
            nonlocal pacing_calls
            pacing_calls += 1
            # Hold the second packet until turn cleanup cancels the sender.
            # A real-time 200ms gap cannot guarantee this ordering under load.
            if pacing_calls > 1: await remaining_input.wait()
        async def tool(*args): calls.append(args)
        async def handler(ws):
            await ws.send(json.dumps({'type':'session.created'})); await ws.recv()
            await ws.send(json.dumps({'type':'session.updated'}))
            first = json.loads(await ws.recv())
            self.assertEqual(first['type'],'input_audio_buffer.append')
            self.assertEqual(len(base64.b64decode(first['audio'])),640)
            await ws.send(json.dumps({'type':'input_audio_buffer.committed'}))
            await ws.send(json.dumps({'type':'response.function_call_arguments.done',
                'name':'get_status','arguments':'{}','call_id':'early'}))
            async for _ in ws: pass
        result = await self.run_server(handler, tool_handler=tool, realtime_pacing=True,
                                       mode='server_vad', vad_padding_seconds=0, sleep=pace)
        self.assertEqual(result['error_code'],'tool_before_input_finished')
        self.assertEqual(calls,[])
        self.assertEqual(pacing_calls,2)
        self.assertNotIn('input_end_send_ms',result['metrics'])
        self.assertNotIn('all_audio_sent_ms',result['metrics'])
        self.assertTrue(result['cleanup']['sender_finished'])

    async def test20_new_speech_cancels_slow_tool_without_stale_output_or_continuation(self):
        entered, cancelled, sent = asyncio.Event(), asyncio.Event(), []
        async def tool(*args):
            entered.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        async def handler(ws):
            await ws.send(json.dumps({'type':'session.created'})); await ws.recv()
            await ws.send(json.dumps({'type':'session.updated'}))
            async for raw in ws:
                e = json.loads(raw); sent.append(e)
                if e['type'] == 'input_audio_buffer.commit':
                    await ws.send(json.dumps({'type':'input_audio_buffer.committed'}))
                elif e['type'] == 'response.create':
                    await ws.send(json.dumps({'type':'response.function_call_arguments.done',
                        'name':'get_status','arguments':'{}','call_id':'old'}))
                    await ws.send(json.dumps({'type':'response.done','response':{'status':'completed'}}))
                    await entered.wait()
                    await ws.send(json.dumps({'type':'input_audio_buffer.speech_started','item_id':'new'}))
        result = await self.run_server(handler, tool_handler=tool, timeout=5)
        self.assertEqual(result['error_code'],'input_interrupted_during_tool')
        self.assertTrue(cancelled.is_set())
        self.assertFalse(any(e['type']=='conversation.item.create' for e in sent))
        self.assertEqual(sum(e['type']=='response.create' for e in sent),1)


if __name__ == '__main__':
    unittest.main()
