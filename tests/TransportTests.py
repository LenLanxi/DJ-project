"""Retained HTTP/model contracts migrated from the retired voice/demo tests.

All HTTP operations are substituted in memory; no credentials or network needed.
"""
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import unittest
import threading
from unittest.mock import MagicMock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'modules' / 'ai'))
from model_client import ModelClient
from http_transport import NoRedirect, post, post_json


class TransportTests(unittest.TestCase):
    def test_plain_http_embedded_credentials_fragment_missing_host_rejected(self):
        for url in ('http://example.com', 'https://user:pass@example.com',
                    'https://example.com/#frag', 'https:///missing'):
            with self.subTest(url=url), patch('http_transport.urllib.request.build_opener') as opener:
                with self.assertRaises(ValueError):
                    post(url, 'test-key', b'')
                opener.assert_not_called()

    def test_missing_api_key_rejected_before_request(self):
        with patch('http_transport.urllib.request.build_opener') as opener:
            with self.assertRaises(ValueError):
                post('https://example.com', '', b'')
            opener.assert_not_called()

    def test_redirect_handler_never_forwards_bearer_key(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_post_uses_bounded_read_timeout_and_single_request(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch('http_transport.urllib.request.build_opener', return_value=opener):
            self.assertEqual(post('https://example.com/chat', 'fake-key', b'payload', limit=100), b'{}')
        response.read.assert_called_once_with(101)
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer fake-key')
        self.assertEqual(request.data, b'payload')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 30)

    def test_oversized_response_rejected(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'x' * 11
        opener = MagicMock()
        opener.open.return_value = response
        with patch('http_transport.urllib.request.build_opener', return_value=opener):
            with self.assertRaisesRegex(RuntimeError, '过大'):
                post('https://example.com', 'fake', b'', limit=10)

    def test_http_error_not_retried_or_body_echoed(self):
        for status in (302, 401, 429, 500):
            opener = MagicMock()
            opener.open.side_effect = urllib.error.HTTPError('https://example.com', status,
                                                              'server echoed secret', {},
                                                              io.BytesIO(b'fake-secret'))
            with self.subTest(status=status), patch('http_transport.urllib.request.build_opener', return_value=opener):
                with self.assertRaises(RuntimeError) as caught:
                    post('https://example.com', 'fake-secret', b'')
                self.assertIn(str(status), str(caught.exception))
                self.assertNotIn('fake-secret', str(caught.exception))
                opener.open.assert_called_once()

    def test_network_timeout_not_retried(self):
        for error in (TimeoutError('secret'), urllib.error.URLError('secret')):
            opener = MagicMock()
            opener.open.side_effect = error
            with self.subTest(error=type(error).__name__), patch('http_transport.urllib.request.build_opener', return_value=opener):
                with self.assertRaises(RuntimeError) as caught:
                    post('https://example.com', 'fake-secret', b'')
                self.assertNotIn('secret', str(caught.exception))
                opener.open.assert_called_once()

    def test_json_response_rejects_invalid_json_and_encoding(self):
        for payload in (b'not json', b'\xff'):
            with self.subTest(payload=payload), patch('http_transport.post', return_value=payload):
                with self.assertRaises(RuntimeError):
                    post_json('https://example.com', 'fake', {})

    def test_json_request_preserves_unicode_without_mutating_input(self):
        request = {'messages': [{'role': 'user', 'content': '红色帐篷'}]}
        with patch('http_transport.post', return_value=b'{"ok":true}') as sent:
            self.assertEqual(post_json('https://example.com', 'fake', request), {'ok': True})
        self.assertEqual(json.loads(sent.call_args.args[2]), request)


class ModelTests(unittest.TestCase):
    def test_structured_request_uses_strict_schema_and_preserves_raw_for_caller_validation(self):
        events=[]
        client=self.client(events.append)
        schema={'type':'object','properties':{'visible':{'type':'boolean'}},
                'required':['visible'],'additionalProperties':False}
        messages=[{'role':'user','content':'private input'}]
        # Invalid JSON is deliberately returned intact so the diagnostic
        # wrapper can persist it before the business validator rejects it.
        with patch('model_client.post_json',return_value={
                'choices':[{'message':{'content':'provider malformed JSON'}}],
                'usage':{'total_tokens':9}}) as sent:
            self.assertEqual(client.complete_structured(messages,schema),'provider malformed JSON')
        body=sent.call_args.args[2]
        self.assertEqual(body,{'model':'configured-model','messages':messages,
            'response_format':{'type':'json_schema','json_schema':{
                'name':'navigation_observation','strict':True,'schema':schema}}})
        self.assertIsNot(body['response_format']['json_schema']['schema'],schema)
        self.assertEqual(client.last_usage,{'total_tokens':9})
        self.assertEqual(len(events),2)
        self.assertEqual(events[-1]['usage'],{'total_tokens':9})
        self.assertEqual(events[-1]['response_format'],'json_schema')
        self.assertNotIn('provider malformed JSON',json.dumps(events))
        self.assertNotIn('private input',json.dumps(events))

    def test_structured_http_rejection_has_one_failed_audit_without_text_fallback(self):
        events=[]
        client=self.client(events.append)
        with patch('model_client.post_json',side_effect=RuntimeError('HTTP 400')) as sent:
            with self.assertRaisesRegex(RuntimeError,'HTTP 400'):
                client.complete_structured([],{'type':'object'})
        sent.assert_called_once()
        self.assertEqual(events[-1]['status'],'failed')
        self.assertTrue(events[-1]['usage_unknown'])
        self.assertFalse(events[-1]['retried'])

    def test_invalid_structured_schema_is_rejected_before_any_request(self):
        invalid=(None,[],{'type':'array'},{'type':'object','value':float('nan')},
                 {'type':'object','value':object()})
        for schema in invalid:
            with self.subTest(schema=schema),patch('model_client.post_json') as sent:
                with self.assertRaises(ValueError): self.client().complete_structured([],schema)
                sent.assert_not_called()

    def test_structured_seed_request_keeps_disabled_thinking_and_output_budget(self):
        with patch.dict('os.environ',{'AI_THINKING':''}):
            client=ModelClient(key='fake',model='doubao-seed-2-1-lite-260915',
                url='https://example.com/chat',max_output_tokens=500)
        with patch('model_client.post_json',return_value={'choices':[{'message':{'content':'{}'}}]}) as sent:
            client.complete_structured([],{'type':'object'})
        self.assertEqual(sent.call_args.args[2]['thinking'],{'type':'disabled'})
        self.assertEqual(sent.call_args.args[2]['max_tokens'],500)

    def test_seed_visual_requests_disable_default_hidden_reasoning(self):
        with patch.dict('os.environ', {'AI_CHAT_URL':'https://ark.cn-beijing.volces.com/api/v3/chat/completions',
                'AI_API_KEY':'fixture','AI_MODEL':'doubao-seed-2-1-lite-260915','AI_THINKING':''}):
            client=ModelClient()
        with patch('model_client.post_json',return_value={'choices':[{'message':{'content':'{}'}}]}) as sent:
            client.complete([])
        self.assertEqual(sent.call_args.args[2]['thinking'],{'type':'disabled'})

    def test_reasoning_override_is_validated_before_sending(self):
        with patch.dict('os.environ', {'AI_CHAT_URL':'https://example.com/chat',
                'AI_API_KEY':'fixture','AI_MODEL':'m','AI_THINKING':'invalid'}), \
                patch('model_client.post_json') as sent:
            with self.assertRaises(ValueError): ModelClient()
            sent.assert_not_called()

    def client(self, on_usage=None):
        with patch.dict('os.environ', {'AI_CHAT_URL': 'https://example.com/chat',
                                       'AI_API_KEY': 'fake', 'AI_MODEL': 'configured-model'}):
            return ModelClient(on_usage=on_usage)

    def test_requires_explicit_cloud_configuration(self):
        for values in ({'AI_CHAT_URL': '', 'AI_API_KEY': 'fake', 'AI_MODEL': 'm'},
                       {'AI_CHAT_URL': 'https://example.com', 'AI_API_KEY': '', 'AI_MODEL': 'm'},
                       {'AI_CHAT_URL': 'https://example.com', 'AI_API_KEY': 'fake', 'AI_MODEL': ''}):
            with self.subTest(values=values), patch.dict('os.environ', values):
                with self.assertRaises(ValueError):
                    ModelClient()

    def test_request_and_usage_are_actual_model_result(self):
        client = self.client()
        messages = [{'role': 'user', 'content': '查询状态'}]
        with patch('model_client.post_json', return_value={
                'choices': [{'message': {'content': 'actual answer'}}],
                'usage': {'total_tokens': 10}}) as request:
            self.assertEqual(client.complete(messages), 'actual answer')
        self.assertEqual(client.last_usage, {'total_tokens': 10})
        self.assertEqual(request.call_args.args,
                         ('https://example.com/chat', 'fake',
                          {'model': 'configured-model', 'messages': messages}))

    def test_malformed_provider_response_not_fictional_reply(self):
        client = self.client()
        invalid = (None, [], {}, {'choices': []}, {'choices': [None]},
                   {'choices': [{'message': {'content': None}}]},
                   {'choices': [{'message': {'content': {'code': 'drone.takeoff()'}}}]})
        for response in invalid:
            with self.subTest(response=response), patch('model_client.post_json', return_value=response):
                with self.assertRaises(RuntimeError):
                    client.complete([])

    def test_model_timeout_propagates_without_canned_fallback(self):
        client = self.client()
        with patch('model_client.post_json', side_effect=RuntimeError('model_timeout')):
            with self.assertRaisesRegex(RuntimeError, 'model_timeout'):
                client.complete([])

    def test_failed_request_cannot_reuse_previous_usage(self):
        client = self.client()
        client.last_usage = {'total_tokens': 123}
        with patch('model_client.post_json', side_effect=RuntimeError('timeout')):
            with self.assertRaises(RuntimeError):
                client.complete([])
        self.assertEqual(client.last_usage, {})

    def test_each_request_audits_actual_usage_without_request_payloads_or_credentials(self):
        events=[]
        client=self.client(events.append)
        with patch('model_client.post_json',return_value={
                'choices':[{'message':{'content':'answer'}}],
                'usage':{'input_tokens':3,'output_tokens':4,'total_tokens':7}}) as request:
            self.assertEqual(client.complete([{'role':'user','content':'private-image-or-message'}]),'answer')
        request.assert_called_once()
        self.assertEqual([event['event'] for event in events],['model_request_start','model_request_end'])
        self.assertEqual(events[0]['request_id'],events[1]['request_id'])
        self.assertEqual(events[1]['model'],'configured-model')
        self.assertEqual(events[1]['usage']['total_tokens'],7)
        self.assertFalse(events[1]['usage_unknown'])
        self.assertEqual(events[1]['attempt'],1)
        self.assertFalse(events[1]['retried'])
        self.assertFalse(events[1]['billing_verified'])
        self.assertGreaterEqual(events[1]['ended_at'],events[0]['started_at'])
        self.assertNotIn('messages',json.dumps(events))
        self.assertNotIn('private-image-or-message',json.dumps(events))
        self.assertNotIn('fake',json.dumps(events))

    def test_failed_request_is_audited_as_usage_unknown_and_is_not_retried(self):
        events=[]
        client=self.client(events.append)
        with patch('model_client.post_json',side_effect=RuntimeError('timeout with private data')) as request:
            with self.assertRaisesRegex(RuntimeError,'timeout'):
                client.complete([])
        request.assert_called_once()
        self.assertEqual(events[-1]['status'],'failed')
        self.assertTrue(events[-1]['usage_unknown'])
        self.assertEqual(events[-1]['usage'],{})
        self.assertFalse(events[-1]['retried'])
        self.assertNotIn('private data',json.dumps(events))

    def test_late_request_completion_does_not_overwrite_new_request_usage_or_mix_audit_ids(self):
        events=[]
        client=self.client(events.append)
        entered,release=threading.Event(),threading.Event()
        def response(_url,_key,body):
            old=body['messages'][0]['content']=='old'
            if old:
                entered.set()
                if not release.wait(2):
                    raise AssertionError('test barrier timeout')
            return {'choices':[{'message':{'content':'old answer' if old else 'new answer'}}],
                    'usage':{'total_tokens':101 if old else 7}}
        with patch('model_client.post_json',side_effect=response), ThreadPoolExecutor(max_workers=2) as pool:
            old=pool.submit(client.complete,[{'role':'user','content':'old'}])
            try:
                self.assertTrue(entered.wait(1))
                new=pool.submit(client.complete,[{'role':'user','content':'new'}])
                self.assertEqual(new.result(timeout=1),'new answer')
                self.assertEqual(client.last_usage,{'total_tokens':7})
            finally:
                release.set()
            self.assertEqual(old.result(timeout=1),'old answer')
        self.assertEqual(client.last_usage,{'total_tokens':7})
        starts=[event for event in events if event['event']=='model_request_start']
        ends={event['request_id']:event for event in events if event['event']=='model_request_end'}
        self.assertEqual(len(starts),2)
        self.assertNotEqual(starts[0]['request_id'],starts[1]['request_id'])
        self.assertEqual(ends[starts[0]['request_id']]['usage'],{'total_tokens':101})
        self.assertEqual(ends[starts[1]['request_id']]['usage'],{'total_tokens':7})

    def test_usage_logger_failure_does_not_silently_allow_an_unrecorded_model_program(self):
        def fail_at_start(_event): raise OSError('log disk unavailable')
        with patch('model_client.post_json') as request:
            with self.assertRaisesRegex(RuntimeError,'未发送请求'):
                self.client(fail_at_start).complete([])
            request.assert_not_called()
        def fail_at_end(event):
            if event['event']=='model_request_end': raise OSError('log disk unavailable')
        with patch('model_client.post_json',return_value={'choices':[{'message':{'content':'drone.takeoff()'}}]}) as request:
            with self.assertRaisesRegex(RuntimeError,'模型已返回，但用量日志写入失败'):
                self.client(fail_at_end).complete([])
            request.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
