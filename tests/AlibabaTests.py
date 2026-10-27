"""Offline Alibaba protocol tests. Synthetic PCM/JPEG; loopback sockets only."""
import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timezone
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'modules' / 'ai'))
from AlibabaRealtime import (AlibabaConfig, AlibabaError, AlibabaRealtime, MODEL,
                             PcmActivity, normalize_tools, session_update, speech_end_position,
                             validate_arguments)


CONFIG = AlibabaConfig('private-test-key-never-print', 'workspace1',
                       'wss://workspace1.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime',
                       free_tier_only=True, free_quota_tokens=1000000,
                       free_quota_expires='2099-01-01T00:00:00Z')
TOOLS = [{'type': 'function', 'name': 'get_status', 'description': 'Query fake status',
          'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}]
PCM = struct.pack('<3200h', *([500] * 3200))


class ConfigTests(unittest.TestCase):
    def test01_exact_model_and_workspace_endpoint_without_secret_repr(self):
        self.assertTrue(CONFIG.validate().endswith('?model=' + MODEL))
        self.assertNotIn(CONFIG.api_key, repr(CONFIG))
        self.assertEqual(replace(CONFIG, url=CONFIG.validate()).validate(), CONFIG.validate())

    def test02_no_remote_endpoint_or_legacy_model_fallback(self):
        invalid = [replace(CONFIG, model='qwen3-omni-flash-realtime'),
                   replace(CONFIG, url='wss://evil.example/api-ws/v1/realtime'),
                   replace(CONFIG, url=CONFIG.url + '?key=secret'),
                   replace(CONFIG, url=CONFIG.url.replace('wss:', 'ws:')),
                   replace(CONFIG, url=CONFIG.url.replace('workspace1', 'otherworkspace'))]
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(AlibabaError): config.validate()

    def test03_free_only_guard_is_required_and_expired_quota_is_rejected(self):
        for config in (replace(CONFIG, free_tier_only=False), replace(CONFIG, free_quota_tokens=0),
                       replace(CONFIG, free_quota_expires='2020-01-01T00:00:00Z')):
            with self.assertRaises(AlibabaError): config.validate()

    def test04_explicit_credentials_file_does_not_mutate_environment(self):
        import os
        before = dict(os.environ)
        value = {'Region': 'cn-beijing', 'ApiKey': CONFIG.api_key,
                 'WorkspaceId': CONFIG.workspace_id, 'WebSocketUrl': CONFIG.url, 'Model': MODEL,
                 'StopWhenFreeQuotaExhausted': True, 'FreeQuotaTokens': 1000,
                 'FreeQuotaExpires': CONFIG.free_quota_expires, 'AllowedModels': [MODEL]}
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
    async def run_server(self, handler, *, tool_handler=None, audio=PCM, image=None, timeout=2, **options):
        async with serve(handler, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            def local_connector(_url, **kwargs):
                return connect(f'ws://127.0.0.1:{port}', **kwargs)
            adapter = AlibabaRealtime(CONFIG, instructions='Use the real tool result', tools=TOOLS,
                                       tool_handler=tool_handler, connect=local_connector)
            return await adapter.run_turn(audio, image, realtime_pacing=False,
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
            await asyncio.wait_for(entered.wait(), 2)
            await adapter.close()
            with self.assertRaises(asyncio.CancelledError): await work
        self.assertTrue(cancelled.is_set())
        self.assertFalse(any(event['type'] == 'conversation.item.create' for event in sent))

    async def test13_oversized_audio_image_and_missing_free_guard_fail_before_connect(self):
        def fail(*args, **kwargs): self.fail('must not connect')
        adapter = AlibabaRealtime(CONFIG, instructions='', tools=[], connect=fail)
        for audio, image in ((b'odd', None), (PCM*100, None), (PCM, b'not jpeg')):
            with self.assertRaises(AlibabaError): await adapter.run_turn(audio, image)
        adapter = AlibabaRealtime(replace(CONFIG, free_tier_only=False), instructions='', tools=[], connect=fail)
        with self.assertRaises(AlibabaError): await adapter.run_turn(PCM)


if __name__ == '__main__':
    unittest.main()
