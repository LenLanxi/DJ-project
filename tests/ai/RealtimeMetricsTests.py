from RealtimeMetrics import summarize, rank, exact_acknowledgement


def test_exact_acknowledgement_never_uses_a_provider_specific_substring_rule():
    assert exact_acknowledgement('测试收到。')
    assert exact_acknowledgement(' 测试收到！ ')
    assert not exact_acknowledgement('好的，测试收到')
    assert not exact_acknowledgement('收到，测试正常')


def sample(provider='a', **extra):
    return dict(provider=provider, task='speech', turn_mode='vad', ok=True,
                media_verified=True, latency={'first_audio_ms': 250}, **extra)


def test_control_plane_success_never_enters_media_ranking():
    values = [sample() for _ in range(3)]
    values[1]['media_verified'] = False
    rows = summarize(values)
    assert rows[0]['attempts'] == 3 and rows[0]['successful'] == 2
    assert not rank(rows, task='speech', mode='vad', metric='first_audio_ms', providers=['a'])['conclusive']


def test_one_route_missing_must_not_invent_winner():
    rows = summarize([sample() for _ in range(3)])
    result = rank(rows, task='speech', mode='vad', metric='first_audio_ms', providers=['a', 'b', 'c'])
    assert result['missing_or_failed'] == ['b', 'c']


def test_manual_and_vad_are_distinct_comparisons():
    values = [sample() for _ in range(3)]
    for value in values:
        value['turn_mode'] = 'manual'
    assert not rank(summarize(values), task='speech', mode='vad', metric='first_audio_ms', providers=['a'])['conclusive']


def test_failed_attempt_remains_in_denominator():
    values = [sample() for _ in range(4)]
    values[-1]['ok'] = False
    rows = summarize(values)
    assert rows[0]['failures'] == 1
    assert not rank(rows, task='speech', mode='vad', metric='first_audio_ms', providers=['a'])['conclusive']


def test_median_preserves_overlap_and_ignores_invalid_timestamps():
    values = [sample() for _ in range(4)]
    for item, latency in zip(values, [-10, 40, 200, float('nan')]):
        item['latency']['first_audio_ms'] = latency
    row = summarize(values)[0]
    assert row['metrics']['first_audio_ms'] == dict(n=3, median=40, minimum=-10, maximum=200)
    assert not rank([row], task='speech', mode='vad', metric='first_audio_ms', providers=['a'])['conclusive']


def test_ranking_requires_same_successful_media_metric():
    values = [sample(p) for p in ['a', 'b', 'c'] for _ in range(3)]
    for value in values:
        value['latency']['first_audio_ms'] = {'a': 300, 'b': 100, 'c': 200}[value['provider']]
    result = rank(summarize(values), task='speech', mode='vad', metric='first_audio_ms', providers=['a', 'b', 'c'])
    assert result['conclusive'] and result['order'] == ['b', 'c', 'a']
