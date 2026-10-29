"""Explicit results at the cloud and resource ownership boundaries."""
import json
import math
import os
import re

def tool_result(value, *, secrets=()):
    hidden = tuple(secrets) + tuple(v for k, v in os.environ.items() if v and len(v) >= 8
                    and any(word in k.upper() for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')))
    def clean(item, key=''):
        if any(word in key.lower() for word in ('api_key','access_key','authorization','password','secret','token')):
            return '[redacted]'
        if key.lower() in ('image_url','frame','audio','pcm','jpeg'):
            return '[payload omitted]'
        if isinstance(item, dict):
            return {str(k):clean(v,str(k)) for k,v in list(item.items())[:100]}
        if isinstance(item, (list, tuple)):
            return [clean(v) for v in item[:100]]
        if isinstance(item, str):
            if item.startswith('data:'):
                return '[payload omitted]'
            for secret in hidden:
                item = item.replace(secret, '[redacted]')
            item = re.sub(r'''data:(?:image|audio)/[^\s"']+''', '[payload omitted]', item)
            return item[:8000]
        if isinstance(item, float) and not math.isfinite(item):
            return None
        if item is None or isinstance(item, (bool,int,float)):
            return item
        return '[object omitted]'
    result = {}
    for key in ('ok', 'simulated'):
        if key in value:
            result[key] = value[key] is True
    for key, limit in (('status',128), ('reason',1000)):
        if key in value:
            result[key] = str(value[key])[:limit]
    follow = value.get('follow_cleanup')
    if isinstance(follow, dict):
        result['follow_cleanup'] = {key: follow[key] for key in
            ('ok', 'status', 'reason', 'reference_adjusted') if key in follow}
    result['debug'] = str(value.get('debug',''))[:2000]
    if value.get('debug_truncated') or len(str(value.get('debug',''))) > 2000:
        result['debug_truncated'] = True
    if value.get('debug_warnings'):
        result['debug_warnings'] = value['debug_warnings']
    journal = []
    for item in value.get('journal', [])[-12:]:
        entry = {k:v for k,v in item.items() if k in ('method','args','error')}
        detail = item.get('result')
        if isinstance(detail,dict):
            entry['result'] = detail
        journal.append(entry)
    result['journal'] = journal
    result = clean(result)
    if len(json.dumps(result,ensure_ascii=False,allow_nan=False).encode()) > 12000:
        result['journal'] = []
        result['detail_omitted'] = True
    return result


def close_step(step, ok, reason=''):
    """One resource owner's confirmed close outcome, never a guessed shape."""
    result = {'step': step, 'ok': ok is True}
    if reason:
        result['reason'] = str(reason)[:1000]
    return result


def close_result(*steps):
    return {'ok': all(step['ok'] for step in steps), 'steps': list(steps)}
