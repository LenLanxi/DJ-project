"""Decision regressions: a fast but incomplete/wrong-round reply cannot win."""
from copy import deepcopy
import asyncio
import json
from types import SimpleNamespace
import pytest
from RouteAnalysis import analyze, input_complete, video_tokens, decompose
from RouteSelection import ARCHIVE, cleanup_ok, main, plan, run


def test_color_answer_does_not_compensate_for_missing_user_question():
    sample = dict(provider='AlibabaRealtime', task='vision', ok=True,
        transcription_items=[dict(item_id='last', text='只回答颜色。')])
    assert not input_complete(sample)
    sample['transcription_items'][0]['text'] = '画面里的帐篷是什么颜色？只回答颜色。'
    assert input_complete(sample)


def test_concatenating_two_committed_items_cannot_hide_premature_endpoint():
    sample = dict(provider='AlibabaRealtime', task='status', transcription_items=[
        dict(item_id='first', text='请查询模拟无人机电量。'),
        dict(item_id='next', text='不要操作飞机。')])
    assert not input_complete(sample)


def test_doubao_matching_subtitle_words_require_one_completed_user_round():
    sample = dict(provider='DoubaoRTC', task='status', events=[
        dict(type='conv', payload=dict(UserID='User')),
        dict(type='subv', payload=dict(data=[
            dict(userId='User', roundId=1, definite=True, paragraph=False, text='请查询电量。'),
            dict(userId='User', roundId=1, definite=True, paragraph=True, text='不要操作飞机。'),
            dict(userId='Bot', roundId=1, definite=True, text='80')]))])
    assert input_complete(sample)
    altered = deepcopy(sample)
    altered['events'][1]['payload']['data'][1]['roundId'] = 2
    assert not input_complete(altered)
    altered = deepcopy(sample)
    altered['events'][1]['payload']['data'][2]['roundId'] = 2
    assert not input_complete(altered)
    altered = deepcopy(sample)
    altered['events'][1]['payload']['data'][1]['paragraph'] = False
    assert not input_complete(altered)


def test_image_sent_and_video_consumed_are_distinct_evidence():
    assert video_tokens(dict(image_sent=True, usage=[dict(input_tokens_details=dict(audio_tokens=21))])) == 0
    assert video_tokens(dict(usage=[dict(input_tokens_details=dict(video_tokens=220))])) == 220


def test_tool_runtime_is_not_misattributed_to_cloud_inference():
    row = decompose(dict(latency=dict(first_audio_ms=5000, tool_call_ms=1800,
        tool_result_sent_ms=3800), events=[]))
    assert row['local_tool_and_return_ms'] == 2000
    assert row['post_tool_first_audio_ms'] == 1200


def test_retired_provider_cannot_pass_active_scheduler_cleanup():
    sample = dict(provider='TencentTRTC', native_returncode=0, room_exit_confirmed=True,
        short_token_config_removed=True, cleanup=dict(ok=True, cloud_stop_confirmed=True))
    assert not cleanup_ok(sample)
    assert not cleanup_ok(dict(sample, provider='unknown'))


def test_retired_speech_route_is_analysis_only():
    trials=plan('speech')
    assert len(trials)==6
    assert {s['provider'] for s in trials}=={'Alibaba','Doubao'}
    with pytest.raises(ValueError,match='unknown_stage'):
        plan('vad')
    with pytest.raises(ValueError,match='unsupported_active_provider'):
        asyncio.run(run(dict(provider='Tencent')))


def test_archived_route_report_cannot_be_live_output():
    with pytest.raises(ValueError,match='archived_evidence_is_read_only'):
        asyncio.run(main(SimpleNamespace(real=True,output_folder=ARCHIVE,stage='speech')))


def test_copied_archived_design_is_rejected_before_live_calls(tmp_path):
    path=tmp_path/'Speech.json'
    path.write_text(json.dumps(dict(design=[dict(provider='Tencent')],attempts=[])),encoding='utf-8')
    with pytest.raises(ValueError,match='archived_provider_report_is_read_only'):
        asyncio.run(main(SimpleNamespace(real=True,output_folder=tmp_path,stage='speech')))


def test_historical_speech_and_vad_remain_readable_without_driver(tmp_path):
    sample=dict(provider='TencentTRTC',task='speech',delivery_verified=True,asr_rounds=1,
                latency=dict(first_audio_ms=100),events=[])
    value=dict(attempts=[dict(spec=dict(id='Historical'),sample=sample,cleanup_confirmed=True)])
    for stage in ('Speech','Vad'):
        (tmp_path/(stage+'.json')).write_text(json.dumps(value),encoding='utf-8')
    result=analyze(tmp_path)
    assert len(result['rows'])==2
    assert all(r['qualified'] for r in result['rows'])
    assert result['groups'][0]['provider']=='TencentTRTC'


def test_different_protocol_schemas_fail_closed_in_the_scheduler():
    assert not cleanup_ok(dict(provider='AlibabaRealtime', cleanup=dict(connection_closed=True)))
    assert not cleanup_ok(dict(provider='DoubaoRTC', cleanup=dict(cloud_stop=dict(ok=True))))


def test_screen_and_confirmation_are_not_pooled_and_pairs_include_both_colors():
    assert len(plan('screen')) == 6
    trials = plan('confirm')
    assert len(trials) == 18
    assert len({s['id'] for s in trials}) == len(trials)
    for provider in ('Alibaba', 'Doubao'):
        assert sum(s['provider'] == provider and s['image'] == 'RedTent' for s in trials) == 3
        assert sum(s['provider'] == provider and s['image'] == 'BlueTent' for s in trials) == 3
        assert sum(s['provider'] == provider and s['task'] == 'status' for s in trials) == 3
