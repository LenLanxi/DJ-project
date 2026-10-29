"""Independent text/JPEG requests through the official Ark SDK.

Each request owns its SDK client: Ark clients aren't shared across worker threads.
Vision validates observations; this module never executes model-generated code.
"""
import json
import threading
import time
import uuid

from navigation.Observation import (
    DESCRIBE_INSTRUCTIONS, OBSERVE_INSTRUCTIONS, OBSERVATION_SCHEMA, image_messages,
)


class DoubaoVisionClient:
    image_bounds = (640, 480)

    def __init__(self, config, on_usage=None, *, timeout_seconds=30,
                 max_output_tokens=None, client_factory=None):
        self.config, self.on_usage = config, on_usage
        self.model = config.vision_model
        self.timeout_seconds, self.max_output_tokens = timeout_seconds, max_output_tokens
        self.client_factory = client_factory
        self.last_usage = {}
        self._lock = threading.Lock()

    def complete(self, messages):
        return self._request(messages, self.timeout_seconds, {'type': 'json_object'})

    def describe_image(self, jpeg, question):
        return self._request(image_messages(jpeg, DESCRIBE_INSTRUCTIONS, question),
                             self.timeout_seconds)

    def observe_image(self, jpeg, target, *, timeout):
        schema = OBSERVATION_SCHEMA
        instructions = OBSERVE_INSTRUCTIONS + '\n只输出满足以下结构的JSON对象：' + json.dumps(
            schema, ensure_ascii=False)
        return self._request(image_messages(jpeg, instructions, '查找：' + target),
            min(timeout, self.timeout_seconds), {'type': 'json_schema', 'json_schema': {
                'name': 'navigation_observation', 'strict': True, 'schema': schema}})

    def _request(self, messages, timeout, response_format=None):
        factory = self.client_factory
        if factory is None:
            from volcenginesdkarkruntime import Ark
            factory = Ark
        started = time.monotonic()
        base = dict(request_id=uuid.uuid4().hex, provider='doubao', model=self.model,
                    attempt=1, retried=False, billing_verified=False)
        usage = {}
        if self.on_usage:
            self.on_usage(dict(base, event='model_request_start', status='started'))
        try:
            body = dict(model=self.model, messages=messages, thinking={'type': 'disabled'})
            if response_format is not None:
                body['response_format'] = response_format
            if self.max_output_tokens is not None:
                body['max_tokens'] = self.max_output_tokens
            with factory(api_key=self.config.vision_key, timeout=timeout, max_retries=0) as sdk:
                result = sdk.chat.completions.create(**body)
            if result.usage is not None:
                usage = result.usage.model_dump(exclude_none=True)
            content = result.choices[0].message.content
            if not isinstance(content, str) or not content.strip():
                raise ValueError('empty_doubao_visual_response')
            with self._lock:
                self.last_usage = usage
        except Exception as exc:
            if self.on_usage:
                self.on_usage(dict(base, event='model_request_end', status='failed',
                    error_type=type(exc).__name__, usage=usage, usage_unknown=not bool(usage),
                    duration_ms=round((time.monotonic()-started)*1000, 3)))
            raise
        if self.on_usage:
            self.on_usage(dict(base, event='model_request_end', status='completed', usage=usage,
                usage_unknown=not bool(usage), duration_ms=round((time.monotonic()-started)*1000, 3)))
        return content
