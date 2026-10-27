from copy import deepcopy

import pytest

from DoubaoBenchmark import delivery_verified, exact_acknowledgement


def delivered_speech():
    return dict(task='speech', transcripts=['请只回答一句话，测试收到。'],
        replies=['收到，测试正常。'], media_verified=True, answer_correct=False, ok=False,
        latency={'first_audio_ms': 250},
        events=[{'event': 'pcm_eof'},
            {'event': 'tlv', 'type': 'conv', 'payload': {'UserID': 'User1', 'RoundID': 1}},
            {'event': 'tlv', 'type': 'subv', 'payload': {'data': [
                {'userId': 'User1', 'roundId': 1, 'definite': True, 'paragraph': True},
                {'userId': 'Bot1', 'roundId': 1, 'definite': True}]}}],
        cleanup={'cloud_stop': {'ok': True}, 'native_exit_code': 0,
                 'node_exit_code': 0, 'short_token_file_deleted': True})


def test_actual_delivery_does_not_claim_exact_answer_compliance():
    sample = delivered_speech()
    assert delivery_verified(sample)
    assert not sample['answer_correct'] and not sample['ok']


def test_speech_requires_the_same_exact_acknowledgement_as_other_routes():
    assert exact_acknowledgement('测试，收到。')
    assert not exact_acknowledgement('好的测试收到')
    assert not exact_acknowledgement('收到，测试正常。')


@pytest.mark.parametrize('missing', ['audio', 'asr', 'eof', 'stop', 'token', 'round', 'protocol', 'paragraph', 'definite'])
def test_delivery_requires_independent_media_and_cleanup_evidence(missing):
    sample = deepcopy(delivered_speech())
    if missing == 'audio':
        sample['latency']['first_audio_ms'] = None
    elif missing == 'asr':
        sample['transcripts'] = ['另外一个问题']
    elif missing == 'eof':
        sample['events'].pop(0)
    elif missing == 'stop':
        sample['cleanup']['cloud_stop']['ok'] = False
    elif missing == 'token':
        sample['cleanup']['short_token_file_deleted'] = False
    elif missing == 'round':
        sample['events'][2]['payload']['data'][1]['roundId'] = 2
    elif missing in ('paragraph', 'definite'):
        sample['events'][2]['payload']['data'][0][missing] = False
    else:
        sample['events'].append({'event': 'malformed_tlv'})
    assert not delivery_verified(sample)


def test_status_and_vision_do_not_relax_content_success():
    for task in ('status', 'vision'):
        sample = delivered_speech()
        sample['task'] = task
        assert not delivery_verified(sample)
        sample['ok'] = True
        assert delivery_verified(sample)
