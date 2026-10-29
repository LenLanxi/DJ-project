"""Shared Alibaba configuration and wire-protocol definitions."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


MODEL = 'qwen3.8-omni-flash-realtime'
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BASE64_BYTES = 256 * 1024
MAX_TOOL_BYTES = 64 * 1024
SOURCES = [
    'https://help.aliyun.com/zh/model-studio/realtime',
    'https://help.aliyun.com/zh/model-studio/client-events',
    'https://help.aliyun.com/zh/model-studio/server-events',
]


class AlibabaError(ValueError):
    """A safe local error code, never a raw HTTP response or credential."""


def strict_json(value: str) -> Any:
    def invalid(_value):
        raise AlibabaError('nonfinite_json')
    return json.loads(value, parse_constant=invalid)


@dataclass(frozen=True)
class AlibabaConfig:
    api_key: str = field(repr=False)
    workspace_id: str = field(repr=False)
    url: str = field(repr=False)
    model: str = MODEL
    voice: str = 'Tina'

    def __post_init__(self):
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise AlibabaError('missing_api_key')
        if self.model != MODEL:
            raise AlibabaError('unsupported_model_no_fallback')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,62}', self.workspace_id):
            raise AlibabaError('invalid_workspace_id')
        parsed = urlsplit(self.url)
        expected = self.workspace_id + '.cn-beijing.maas.aliyuncs.com'
        if (parsed.scheme != 'wss' or parsed.hostname != expected or parsed.port is not None
                or parsed.username or parsed.password or parsed.fragment
                or parsed.path != '/api-ws/v1/realtime'):
            raise AlibabaError('invalid_workspace_endpoint')
        query = parse_qs(parsed.query, keep_blank_values=True)
        if query and query != {'model': [self.model]}:
            raise AlibabaError('invalid_endpoint_query')
        if not isinstance(self.voice, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', self.voice):
            raise AlibabaError('invalid_voice')
        object.__setattr__(self, 'url', urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                                  urlencode({'model': self.model}), '')))

    @classmethod
    def from_credentials(cls, path: str | Path) -> 'AlibabaConfig':
        # No global environment mutation and no provider SDK that reads other
        # credentials implicitly. Only this explicit file is consulted.
        try:
            value = strict_json(Path(path).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            raise AlibabaError('credentials_unreadable') from None
        if not isinstance(value, dict) or value.get('Region') != 'cn-beijing':
            raise AlibabaError('unsupported_region')
        result = cls(api_key=value.get('ApiKey', ''), workspace_id=value.get('WorkspaceId', ''),
                     url=value.get('WebSocketUrl', ''), model=value.get('Model', ''))
        allowed = value.get('AllowedModels')
        if allowed is not None and (not isinstance(allowed, list) or result.model not in allowed):
            raise AlibabaError('model_not_allowed_by_credentials')
        return result


def normalize_tools(tools: list[dict]) -> list[dict]:
    if not isinstance(tools, list) or len(tools) > 16:
        raise AlibabaError('invalid_tools')
    normalized, names = [], set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get('type') != 'function':
            raise AlibabaError('only_local_function_tools_supported')
        function = tool.get('function', tool)
        name = function.get('name')
        parameters = function.get('parameters')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', name) or name in names:
            raise AlibabaError('invalid_or_duplicate_tool_name')
        if not isinstance(parameters, dict) or parameters.get('type') != 'object':
            raise AlibabaError('tool_parameters_must_be_object')
        names.add(name)
        normalized.append({'type': 'function', 'function': {
            'name': name, 'description': str(function.get('description', ''))[:2000],
            'parameters': parameters}})
    # Check definitions are finite and serializable before opening a connection.
    try:
        if len(json.dumps(normalized, allow_nan=False).encode()) > MAX_TOOL_BYTES:
            raise AlibabaError('tool_schema_too_large')
    except (TypeError, ValueError):
        raise AlibabaError('invalid_tool_schema') from None
    return normalized


def validate_arguments(arguments: Any, schema: dict) -> None:
    """Validate the common local-dispatch schema subset without a dependency.

    Unknown assertion keywords fail closed; annotations are permitted. Complex
    schemas should be handled by the caller rather than silently half-validated.
    """
    supported = {'type', 'properties', 'required', 'additionalProperties', 'items', 'enum',
                 'minimum', 'maximum', 'minLength', 'maxLength', 'minItems', 'maxItems',
                 'description', 'title', 'default', '$schema', 'examples'}
    if set(schema) - supported:
        raise AlibabaError('unsupported_argument_schema')
    kind = schema.get('type')
    valid = {'object': isinstance(arguments, dict), 'array': isinstance(arguments, list),
             'string': isinstance(arguments, str), 'boolean': type(arguments) is bool,
             'integer': type(arguments) is int,
             'number': type(arguments) in (int, float) and math.isfinite(arguments),
             'null': arguments is None}
    if kind not in valid or not valid[kind]:
        raise AlibabaError('tool_argument_type')
    if 'enum' in schema and not any(type(arguments) is type(item) and arguments == item for item in schema['enum']):
        raise AlibabaError('tool_argument_enum')
    if isinstance(arguments, dict):
        properties = schema.get('properties', {})
        if any(key not in arguments for key in schema.get('required', [])):
            raise AlibabaError('tool_argument_required')
        if schema.get('additionalProperties') is False and set(arguments) - set(properties):
            raise AlibabaError('tool_argument_extra')
        for name, value in arguments.items():
            if name in properties:
                validate_arguments(value, properties[name])
    if isinstance(arguments, list):
        if not schema.get('minItems', 0) <= len(arguments) <= schema.get('maxItems', 10000):
            raise AlibabaError('tool_argument_length')
        if 'items' in schema:
            for item in arguments:
                validate_arguments(item, schema['items'])
    if isinstance(arguments, str) and not schema.get('minLength', 0) <= len(arguments) <= schema.get('maxLength', MAX_TOOL_BYTES):
        raise AlibabaError('tool_argument_length')
    if type(arguments) in (float, int) and not schema.get('minimum', -math.inf) <= arguments <= schema.get('maximum', math.inf):
        raise AlibabaError('tool_argument_range')


def session_update(config: AlibabaConfig, instructions: str, tools: list[dict], *, mode='manual', silence_ms=800) -> dict:
    if mode not in ('manual', 'server_vad', 'semantic_vad', 'semantic_vad_ec'):
        raise AlibabaError('invalid_turn_mode')
    if type(silence_ms) is not int or not 200 <= silence_ms <= 6000:
        raise AlibabaError('invalid_vad_silence')
    return {'type': 'session.update', 'session': {
        'modalities': ['text', 'audio'], 'instructions': instructions,
        'audio': {'input': {'format': {'type': 'pcm', 'sample_rate': 16000,
                  'sample_format': 's16le', 'channels': 1, 'packing': 'interleaved', 'channel_layout': 'mono'}},
                  'output': {'voice': config.voice, 'format': {'type': 'pcm', 'sample_rate': 24000}}},
        'turn_detection': None if mode == 'manual' else {'type': mode, 'silence_duration_ms': silence_ms},
        'input_audio_transcription': {'model': 'qwen3-asr-flash-realtime'},
        'tools': tools, 'enable_search': False, 'max_tokens': 512}}
