import os
import math
import threading
import time
import uuid
from http_transport import post_json


def accounting_usage(value):
    """Only numeric accounting fields, never arbitrary provider text/payloads."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, count in value.items():
        if not isinstance(key, str):
            continue
        if type(count) in (int, float) and math.isfinite(count) and count >= 0:
            result[key] = count
        elif isinstance(count, dict):
            nested = accounting_usage(count)
            if nested:
                result[key] = nested
    return result


class ModelClient:
    def __init__(self, on_usage=None):
        self.key = os.environ.get('AI_API_KEY') or os.environ.get('RTC_VLM_API_KEY', '')
        self.model = os.environ.get('AI_MODEL') or os.environ.get('RTC_VLM_MODEL', '')
        self.url = os.environ.get('AI_CHAT_URL') or ('https://ark.cn-beijing.volces.com/api/v3/chat/completions' if os.environ.get('RTC_VLM_API_KEY') else '')
        # Seed 2.1 Lite defaults to long hidden reasoning. This project's
        # real-time visual task needs direct answers instead. Other providers
        # retain their existing request schema unless explicitly configured.
        self.thinking = os.environ.get('AI_THINKING') or (
            'disabled' if self.model == 'doubao-seed-2-1-lite-260915' else None)
        if self.thinking is not None and self.thinking not in ('enabled', 'disabled', 'auto'):
            raise ValueError('AI_THINKING须为enabled、disabled或auto')
        self.last_usage = {}
        self.on_usage = on_usage
        if on_usage is not None and not callable(on_usage):
            raise TypeError('on_usage must be callable')
        self._usage_lock = threading.Lock()
        self._request_sequence = 0
        if not self.url.startswith('https://') or not self.key or not self.model:
            raise ValueError('请配置AI_CHAT_URL、AI_API_KEY、AI_MODEL')

    def complete(self, messages):
        request_id = uuid.uuid4().hex
        started_at, started = time.time(), time.monotonic()
        with self._usage_lock:
            self._request_sequence += 1
            sequence = self._request_sequence
            # Compatibility diagnostic only. Cost evidence is per-request below.
            self.last_usage = {}
        base = dict(request_id=request_id, model=self.model, started_at=started_at,
                    attempt=1, retried=False, billing_verified=False)
        if self.on_usage:
            try:
                self.on_usage(dict(base, event='model_request_start', status='started'))
            except Exception:
                raise RuntimeError('模型用量日志不可写，未发送请求；未重试') from None
        usage = {}
        provider_usage = {}
        try:
            body = {'model': self.model, 'messages': messages}
            if self.thinking is not None:
                body['thinking'] = {'type': self.thinking}
            result = post_json(self.url, self.key, body)
            if isinstance(result, dict) and isinstance(result.get('usage'), dict):
                provider_usage = result['usage']
                usage = accounting_usage(provider_usage)
            try:
                content = result['choices'][0]['message']['content']
                if not isinstance(content, str):
                    raise TypeError()
            except (KeyError, IndexError, TypeError, AttributeError):
                raise RuntimeError('模型返回格式不兼容') from None
        except Exception as exc:
            if self.on_usage:
                try:
                    self.on_usage(dict(base, event='model_request_end', status='failed',
                        ended_at=time.time(), duration_ms=round((time.monotonic()-started)*1000, 3),
                        usage=usage, usage_unknown=not bool(usage), error_type=type(exc).__name__))
                except Exception:
                    raise RuntimeError('模型请求失败，且用量日志写入失败；未重试') from None
            raise
        with self._usage_lock:
            if sequence == self._request_sequence:
                self.last_usage = provider_usage
        if self.on_usage:
            try:
                self.on_usage(dict(base, event='model_request_end', status='completed',
                    ended_at=time.time(), duration_ms=round((time.monotonic()-started)*1000, 3),
                    usage=usage, usage_unknown=not bool(usage)))
            except Exception:
                raise RuntimeError('模型已返回，但用量日志写入失败；未重试') from None
        return content
