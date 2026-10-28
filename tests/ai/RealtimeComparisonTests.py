import json
from CompareRealtime import compare


def test_missing_active_receipts_cannot_be_a_comparison_win(tmp_path):
    value={'samples':[dict(provider='AlibabaRealtime',task='speech',turn_mode='automatic',
                           ok=True,media_verified=True,latency={'first_audio_ms':1}) for _ in range(3)]}
    (tmp_path/'AlibabaMedia.json').write_text(json.dumps(value),encoding='utf-8')
    result=compare(tmp_path)
    assert not result['speech_first_audio']['conclusive']
    assert result['missing_receipts']==['DoubaoMedia.json']
    assert result['archived_providers']==[]


def test_two_active_providers_can_complete_without_retired_driver_or_receipts(tmp_path):
    for provider,name in [('AlibabaRealtime','AlibabaMedia.json'),('DoubaoRTC','DoubaoMedia.json')]:
        value={'samples':[dict(provider=provider,task='speech',turn_mode='automatic',ok=True,
            media_verified=True,delivery_verified=True,latency={'first_audio_ms':500}) for _ in range(3)]}
        (tmp_path/name).write_text(json.dumps(value),encoding='utf-8')
    result=compare(tmp_path)
    assert result['media_first_audio']['conclusive']
    assert result['missing_receipts']==[]
    assert set(result['media_first_audio']['order'])=={'AlibabaRealtime','DoubaoRTC'}


def test_historical_tencent_receipts_remain_offline_comparable(tmp_path):
    value={'samples':[dict(provider='TencentTRTC',task='speech',turn_mode='automatic',ok=True,
        media_verified=True,delivery_verified=True,latency={'first_audio_ms':500})]}
    path=tmp_path/'TencentMedia.json'
    raw=json.dumps(value)
    path.write_text(raw,encoding='utf-8')
    result=compare(tmp_path)
    assert result['archived_providers']==['TencentTRTC']
    assert result['summary'][0]['provider']=='TencentTRTC'
    assert path.read_text(encoding='utf-8')==raw


def test_one_repeated_color_is_not_visual_understanding_evidence(tmp_path):
    value={'samples':[dict(provider='AlibabaRealtime',task='vision',turn_mode='manual',
                           ok=True,media_verified=True,expected_color='红',latency={}) for _ in range(2)]}
    (tmp_path/'AlibabaMedia.json').write_text(json.dumps(value),encoding='utf-8')
    assert not compare(tmp_path)['capabilities'][0]['counterfactual_vision_verified']
    value['samples'][1]['expected_color']='蓝'
    (tmp_path/'AlibabaMedia.json').write_text(json.dumps(value),encoding='utf-8')
    assert compare(tmp_path)['capabilities'][0]['counterfactual_vision_verified']


def test_development_debugging_does_not_enter_formal_ranking(tmp_path):
    value={'samples':[dict(provider='AlibabaRealtime',task='speech',turn_mode='automatic',
                           phase='development',ok=True,media_verified=True,latency={'first_audio_ms':1})]}
    (tmp_path/'AlibabaMedia.json').write_text(json.dumps(value),encoding='utf-8')
    assert not compare(tmp_path)['samples']


def test_real_delivery_and_exact_wording_have_separate_denominators(tmp_path):
    value={'samples':[dict(provider='AlibabaRealtime',task='speech',turn_mode='automatic',
                           ok=False,media_verified=True,delivery_verified=True,
                           latency={'first_audio_ms':500})]}
    (tmp_path/'AlibabaMedia.json').write_text(json.dumps(value),encoding='utf-8')
    result=compare(tmp_path)
    assert result['summary'][0]['successful']==0
    assert result['delivery_summary'][0]['successful']==1


def test_manual_diagnostic_never_repairs_automatic_capability_claims(tmp_path):
    value={'samples':[dict(provider='AlibabaRealtime',task='status',turn_mode='manual',
                           phase='diagnostic',ok=True,media_verified=True,latency={})]}
    (tmp_path/'AlibabaMedia.json').write_text(json.dumps(value),encoding='utf-8')
    result=compare(tmp_path)
    assert not result['samples']
    assert len(result['diagnostic_samples'])==1
    assert not result['capabilities'][0]['tool_result_roundtrip_verified']
