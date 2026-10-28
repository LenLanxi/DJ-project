"""Comparable turn latency summaries; control-plane probes never enter rankings."""
import math
import statistics


def exact_acknowledgement(text):
    """One shared wording criterion, separate from actual media delivery."""
    return isinstance(text,str) and ''.join(c for c in text if c.isalnum()) == '测试收到'


def summarize(samples):
    """Keep failures in denominators; compare only matching media/turn scenarios."""
    groups = {}
    for sample in samples:
        key = (sample['provider'], sample['task'], sample['turn_mode'])
        groups.setdefault(key, []).append(sample)
    rows = []
    for (provider, task, mode), attempts in sorted(groups.items()):
        good = [s for s in attempts if s.get('ok') is True and s.get('media_verified') is True]
        row = dict(provider=provider, task=task, turn_mode=mode, attempts=len(attempts),
                   successful=len(good), failures=len(attempts)-len(good), metrics={})
        for metric in ('first_text_ms', 'first_audio_ms', 'tool_call_ms',
                       'tool_result_sent_ms', 'completed_ms'):
            values = []
            for sample in good:
                value = sample.get('latency', {}).get(metric)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    values.append(value)
            if values:
                row['metrics'][metric] = dict(n=len(values), median=round(statistics.median(values), 3),
                                             minimum=round(min(values), 3), maximum=round(max(values), 3))
        rows.append(row)
    return rows


def rank(rows, *, task, mode, metric, providers):
    """Rank only when every requested route has comparable successful media."""
    selected = [r for r in rows if r['task'] == task and r['turn_mode'] == mode
                and r['provider'] in providers]
    covered = {r['provider'] for r in selected
               if r['successful'] == r['attempts'] and r['successful'] >= 3
               and r['metrics'].get(metric, {}).get('n') == r['successful']}
    if covered != set(providers):
        return {'conclusive': False, 'reason': 'need_three_successful_comparable_media_turns_per_provider',
                'missing_or_failed': sorted(set(providers)-covered)}
    ordered = sorted(selected, key=lambda r: r['metrics'][metric]['median'])
    return {'conclusive': True, 'metric': metric, 'task': task, 'turn_mode': mode,
            'order': [r['provider'] for r in ordered],
            'scope': 'this_host_network_fixture_and_client_only_not_field_latency'}
