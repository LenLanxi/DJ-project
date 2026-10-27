"""Tencent public control-plane contracts; no paid APIs or device capture."""
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'modules'/'ai'))
from TencentConversation import (CAPABILITIES, HOST, LLMConfig, TencentApiError,
    TencentConfig, TencentConversation, json_bytes, probe, signed_headers, user_sig)


class TencentTests(unittest.TestCase):
    def setUp(self):
        self.config = TencentConfig(1400000000, 'sdkFixtureSecret', 'AKIDFixture', 'cloudFixtureSecret')
        self.llm = LLMConfig('doubao-seed-2-1-lite-260915', 'arkFixtureSecret')

    def route(self, responses):
        calls = []
        def transport(url, raw, headers, timeout):
            calls.append((headers['X-TC-Action'], json.loads(raw)))
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return {'Response': {'RequestId': 'fixtureRequest', **response}}
        route = TencentConversation(self.config, self.llm, transport=transport)
        return route, calls

    def test01_user_sig_is_short_lived_official_tls_hmac_not_cloud_key(self):
        token = user_sig(1400000000, 'sdkFixtureSecret', 'UserFixture', timestamp=100, seconds=120)
        record = json.loads(zlib.decompress(base64.b64decode(token.replace('*','+').replace('-','/').replace('_','='))))
        self.assertEqual(record['TLS.ver'], '2.0')
        self.assertEqual(record['TLS.expire'], 120)
        content = 'TLS.identifier:UserFixture\nTLS.sdkappid:1400000000\nTLS.time:100\nTLS.expire:120\n'
        expected = base64.b64encode(hmac.new(b'sdkFixtureSecret', content.encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(record['TLS.sig'], expected)
        self.assertNotIn('sdkFixtureSecret', token)
        for duration in (0, 181, 3600):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                user_sig(1, 'fixture', 'user', seconds=duration)

    def test02_cloud_and_sdk_secrets_are_hidden_from_repr(self):
        self.assertNotIn('Fixture', repr(self.config))
        self.assertNotIn('arkFixtureSecret', repr(self.llm))

    def test03_tc3_signs_exact_utf8_body_and_action(self):
        raw = json_bytes({'Text': '停止'})
        headers = signed_headers(self.config, 'StopAIConversation', raw, timestamp=1551113065)
        self.assertEqual(headers['X-TC-Version'], '2019-07-22')
        self.assertIn('/2019-02-25/trtc/tc3_request', headers['Authorization'])
        self.assertIn('SignedHeaders=content-type;host;x-tc-action', headers['Authorization'])
        self.assertNotEqual(headers['Authorization'], signed_headers(self.config, 'StopAIConversation', raw+b' ', timestamp=1551113065)['Authorization'])
        self.assertNotEqual(headers['Authorization'], signed_headers(self.config, 'StartAIConversation', raw, timestamp=1551113065)['Authorization'])
        with self.assertRaises(ValueError): signed_headers(self.config, 'DeleteEverything', raw)

    def test04_public_voice_config_is_honest_and_uses_latest_tts_topic(self):
        route, _ = self.route([])
        body = route.start_body()
        self.assertEqual(body['RoomIdType'], 1)
        self.assertNotEqual(body['AgentConfig']['UserId'], body['AgentConfig']['TargetUserId'])
        self.assertFalse(body['AgentConfig']['FilterOneWord'])
        self.assertEqual(body['AgentConfig']['SubtitleMode'], 1)
        self.assertGreaterEqual(body['AgentConfig']['MaxIdleTime'], 60)
        self.assertEqual(body['STTConfig']['VadSilenceTime'], 1000)
        llm = json.loads(body['LLMConfig'])
        self.assertEqual(llm['ExtraBody']['thinking']['type'], 'disabled')
        self.assertTrue(llm['Streaming'])
        self.assertNotIn('VisionConfig', llm)
        self.assertNotIn('Tools', llm)
        self.assertEqual(json.loads(body['TTSConfig'])['Model'], 'flow_02_turbo')
        self.assertFalse(CAPABILITIES['video_understanding_verified'])

    def test05_start_and_status_never_claim_full_media_readiness(self):
        route, calls = self.route([{'TaskId': 'taskFixture'}, {'Status': 'InProgress'}])
        self.assertEqual(route.start()['status'], 'submitted')
        self.assertFalse(route.describe()['ready'])
        self.assertEqual(calls[1][1]['TaskId'], 'taskFixture')
        with self.assertRaises(RuntimeError): route.start()

    def test06_failed_stop_retains_task_for_retry(self):
        route, calls = self.route([{'TaskId': 'taskFixture'}, TimeoutError(), {}])
        route.start()
        self.assertEqual(route.stop()['status'], 'cleanup_pending')
        self.assertTrue(route.needs_stop)
        self.assertEqual(route.stop()['status'], 'stopped')
        self.assertEqual(calls[-1], ('StopAIConversation', {'TaskId': 'taskFixture'}))
        self.assertFalse(route.needs_stop)

    def test07_unknown_start_result_recovers_task_using_session_id_and_stops(self):
        route, calls = self.route([TimeoutError(), {'TaskId': 'recoveredTask', 'Status': 'InProgress'}, {}])
        with self.assertRaises(TencentApiError): route.start()
        self.assertEqual(route.stop()['status'], 'stopped')
        self.assertEqual(calls[1][1]['SessionId'], route.session_id)
        self.assertEqual(calls[2][1]['TaskId'], 'recoveredTask')

    def test08_rejected_start_does_not_create_a_false_stop_confirmation(self):
        route, calls = self.route([{'Error': {'Code': 'FailedOperation.NotAbility', 'Message': 'secret'}}])
        with self.assertRaises(TencentApiError): route.start()
        self.assertEqual(route.stop()['status'], 'no_cloud_task')
        self.assertEqual(len(calls), 1)
        self.assertNotIn('secret', json.dumps(route.events))

    def test09_control_uses_current_documented_command_objects(self):
        route, calls = self.route([{'TaskId': 'taskFixture'}, {}, {}])
        route.start()
        route.control('InvokeLLM', '你好')
        self.assertEqual(calls[-1][1]['InvokeLLM'], {'Content': '你好', 'Interrupt': True})
        route.control('ServerPushText', '测试')
        self.assertEqual(calls[-1][1]['ServerPushText'], {'Text': '测试', 'Interrupt': True})
        with self.assertRaises(ValueError): route.control('chat', 'legacy')

    def test10_unknown_transport_error_is_sanitized_and_never_retried(self):
        route, calls = self.route([RuntimeError('secret should not leak')])
        with self.assertRaisesRegex(TencentApiError, 'network_or_transport_error') as caught:
            route.start()
        self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(len(calls), 1)
        self.assertTrue(route.needs_stop)
        self.assertNotIn('secret', json.dumps(route.events))

    def test11_empty_success_is_unknown_not_readiness(self):
        route, _ = self.route([{}])
        with self.assertRaisesRegex(TencentApiError, 'missing_task_id'):
            route.start()
        self.assertTrue(route.needs_stop)

    def test12_probe_finally_stops_and_does_not_report_api_time_as_speech_latency(self):
        route, calls = self.route([{'TaskId': 'taskFixture'}, {'Status': 'Preparing'}, {}])
        with patch('TencentConversation.TencentConversation', return_value=route):
            result = probe(self.config, self.llm)
        self.assertTrue(result['ok'])
        self.assertIsNone(result['full_pipeline_latency_ms'])
        self.assertFalse(result['media_published'])
        self.assertEqual([name for name, _ in calls], ['StartAIConversation','DescribeAIConversation','StopAIConversation'])
        self.assertNotIn('FixtureSecret', json.dumps(result))

    def test13_logger_failure_prevents_start_network_call(self):
        calls = []
        def reject(_): raise OSError('log full')
        route = TencentConversation(self.config, self.llm, on_event=reject,
                                    transport=lambda *args: calls.append(args))
        with self.assertRaisesRegex(TencentApiError, 'audit_unavailable'): route.start()
        self.assertEqual(calls, [])

    def test14_actual_public_subtitles_are_bound_to_exact_bot_and_round(self):
        from TencentBenchmark import subtitle, response_finished
        event = {'event':'custom_message','payload':{'type':10000,'sender':'bot',
            'payload':{'text':'测试收到','end':True,'roundid':'round1'}}}
        self.assertEqual(subtitle(event,'human','bot'), {'role':'assistant','text':'测试收到',
            'final':True,'round_id':'round1'})
        self.assertIsNone(subtitle(event,'human','differentBot'))
        self.assertTrue(response_finished({'event':'custom_message','payload':{'type':10001,
            'payload':{'state':5}}}))

    def test15_prefix_and_sentence_subtitles_preserve_exact_answer(self):
        from TencentBenchmark import combine_text, recompact
        items = [{'text':'测试'},{'text':'测试收到'},{'text':'测试收到。'}]
        self.assertEqual(recompact(combine_text(items)), '测试收到')
        self.assertNotEqual(recompact('没听见测试收到'), '测试收到')

    def test16_completion_requires_matching_full_asr_round_and_input_end(self):
        from TencentBenchmark import completion_matches
        def event(round_id):
            return {'event':'custom_message','payload':{'type':10001,'payload':{'state':5,'roundid':round_id}}}
        self.assertFalse(completion_matches(event('old'), 'new', True))
        self.assertFalse(completion_matches(event('new'), 'new', False))
        self.assertFalse(completion_matches(event('new'), None, True))
        self.assertTrue(completion_matches(event('new'), 'new', True))

    def test17_broken_audit_never_blocks_priority_stop(self):
        route, calls = self.route([{'TaskId':'taskFixture'},{}])
        route.start()
        def broken(_): raise OSError('full')
        route.on_event = broken
        self.assertTrue(route.stop()['ok'])
        self.assertEqual(calls[-1][0], 'StopAIConversation')
        self.assertGreater(route.audit_failures, 0)

    def test18_first_audio_rejects_old_round_and_requires_two_continuous_windows(self):
        from TencentBenchmark import RoundAudio
        tracker = RoundAudio()
        def audio(stamp, rms=100):
            return {'event':'output_audio_frame','monotonic_ms':stamp,'rms':rms}
        self.assertIsNone(tracker.feed(audio(110), 'new', 'old', 100))
        self.assertIsNone(tracker.feed(audio(120), 'new', 'old', 100))
        self.assertIsNone(tracker.feed(audio(130), 'new', 'new', 100))
        self.assertIsNone(tracker.feed(audio(150, 0), 'new', 'new', 100))
        self.assertIsNone(tracker.feed(audio(170), 'new', 'new', 100))
        self.assertEqual(tracker.feed(audio(190), 'new', 'new', 100), 70)

    def test19_wrong_verbatim_answer_still_has_measurable_verified_delivery(self):
        from TencentBenchmark import finalize_delivery
        event = lambda stamp, round_id: {'event':'custom_message','monotonic_ms':stamp,
            'payload':{'type':10001,'payload':{'state':5,'roundid':round_id}}}
        result = {'response_round_id':'current','voiced_endpoint_ms':100,
            'latency':{'first_text_ms':15,'first_audio_ms':30,'completed_ms':None},
            'audio_latency_source':'native_20ms_pcm_same_complete_asr_round_two_nonzero_frames',
            'asr_verified':True,'input_completed':True,'answer_verified':False,
            'reply_text':'收到，测试通过','cleanup':{'ok':True,'cloud_stop_confirmed':True},
            'room_exit_confirmed':True,'native_returncode':0,
            'events':[event(110,'current'),{'event':'pcm_eof','monotonic_ms':120},
                      event(140,'old'),event(180,'current')]}
        finalize_delivery(result)
        self.assertEqual(result['latency']['completed_ms'], 80)
        self.assertTrue(result['delivery_verified'])
        self.assertFalse(result['answer_verified'])
        self.assertFalse(result['ok'])

    def test20_delivery_rejects_protocol_errors_even_when_text_and_pcm_arrived(self):
        from TencentBenchmark import finalize_delivery
        result = {'response_round_id':'current','voiced_endpoint_ms':100,
            'latency':{'first_audio_ms':30},'asr_verified':True,'input_completed':True,
            'audio_latency_source':'native_20ms_pcm_same_complete_asr_round_two_nonzero_frames',
            'answer_verified':True,'cleanup':{'ok':True,'cloud_stop_confirmed':True},
            'room_exit_confirmed':True,'native_returncode':0,
            'events':[{'event':'pcm_eof'},{'event':'malformed_message'}]}
        finalize_delivery(result)
        self.assertFalse(result['delivery_verified'])
        self.assertFalse(result['ok'])

    def test21_appending_keeps_prior_failed_formal_sample_and_trial_number(self):
        from TencentBenchmark import append_sample, read_report
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'TencentMedia.json'
            append_sample(path, {'ok':False,'delivery_verified':True}, False)
            append_sample(path, {'ok':True,'delivery_verified':True}, False)
            result = read_report(path, False)
            self.assertEqual([s['ok'] for s in result['samples']], [False,True])
            self.assertEqual([s['trial'] for s in result['samples']], [1,2])
            self.assertEqual(len(list(Path(folder).iterdir())), 1)
            with self.assertRaises(ValueError): read_report(path, True)

    def test22_actual_queued_sample_anchor_reconstructs_same_round_metrics(self):
        from TencentBenchmark import recompute_latency
        def state(stamp, code):
            return {'event':'custom_message','monotonic_ms':stamp,'payload':{'sender':'bot',
                'type':10001,'payload':{'state':code,'roundid':'round'}}}
        result = {'response_round_id':'round','full_input_fixture':{'speech_end_samples':320,'sample_rate':16000},
            'events':[{'event':'audio_input_started','monotonic_ms':100},
                {'event':'audio_chunk','last_sample':480,'monotonic_ms':150},
                {'event':'pcm_eof','monotonic_ms':160},state(170,3),
                {'event':'custom_message','monotonic_ms':180,'payload':{'sender':'bot','type':10000,
                    'payload':{'roundid':'round','text':'收到'}}},
                {'event':'output_audio_frame','monotonic_ms':190,'rms':100},
                {'event':'output_audio_frame','monotonic_ms':210,'rms':100},state(230,5)]}
        self.assertTrue(recompute_latency(result))
        self.assertEqual(result['nominal_voiced_endpoint_ms'], 120)
        self.assertEqual(result['voiced_endpoint_ms'], 140)
        self.assertEqual(result['latency'], {'first_text_ms':40,'first_audio_ms':50,'completed_ms':90})

    def test23_unknown_start_and_three_not_found_lookups_keep_cleanup_for_late_task(self):
        missing = {'Error':{'Code':'FailedOperation.TaskNotExist'}}
        route, calls = self.route([TimeoutError(), missing, missing, missing,
                                   {'TaskId':'lateTask','Status':'InProgress'}, {}])
        with self.assertRaises(TencentApiError): route.start()
        session_id = route.session_id
        with patch('TencentConversation.time.sleep') as sleep:
            first = route.stop()
        self.assertFalse(first['ok'])
        self.assertEqual(first['status'], 'cleanup_pending')
        self.assertEqual(first['lookup_attempts'], 3)
        self.assertFalse(first['cloud_stop_confirmed'])
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(route.needs_stop)
        self.assertTrue(route.start_outcome_unknown)
        self.assertEqual(route.session_id, session_id)
        self.assertTrue(route.stop()['ok'])
        self.assertFalse(route.needs_stop)
        self.assertFalse(route.start_outcome_unknown)
        self.assertEqual(calls[-1], ('StopAIConversation', {'TaskId':'lateTask'}))
        self.assertTrue(all(body.get('SessionId') == session_id for name, body in calls
                            if name == 'DescribeAIConversation'))

    def test24_bounded_lookup_can_find_a_task_created_after_the_first_not_found(self):
        route, calls = self.route([TimeoutError(), {'Error':{'Code':'FailedOperation.TaskNotExist'}},
                                   {'TaskId':'lateTask'}, {}])
        with self.assertRaises(TencentApiError): route.start()
        with patch('TencentConversation.time.sleep'):
            result = route.stop()
        self.assertTrue(result['cloud_stop_confirmed'])
        self.assertEqual([name for name, _ in calls], ['StartAIConversation',
            'DescribeAIConversation','DescribeAIConversation','StopAIConversation'])

    def test25_definite_start_rejection_does_not_claim_unknown_task_or_confirmed_stop(self):
        route, calls = self.route([{'Error':{'Code':'InvalidParameter'}}])
        with self.assertRaises(TencentApiError): route.start()
        self.assertFalse(route.start_outcome_unknown)
        result = route.stop()
        self.assertEqual(result['status'], 'no_cloud_task')
        self.assertFalse(result['cloud_stop_confirmed'])
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
