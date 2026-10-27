"""Tencent TRTC public voice-conversation control plane, without device capture.

Public API: https://cloud.tencent.com/document/api/647/108514 (2019-07-22).
TC3 and TLS UserSig are implemented from the official signature specifications:
https://cloud.tencent.com/document/product/1278/48732
https://github.com/Tencent-RTC/tls-sig-api-v2-python

Start acknowledgement is not bot readiness or measured speech latency. This
adapter deliberately does not invent public VisionConfig/Tools fields: the
documented function-calling example uses an externally hosted LLM proxy.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid
import zlib

HOST = 'trtc.tencentcloudapi.com'
VERSION = '2019-07-22'
REGIONS = {'ap-beijing', 'ap-guangzhou', 'ap-shanghai', 'ap-singapore', 'ap-tokyo',
           'na-ashburn', 'na-siliconvalley'}
ACTIONS = {'StartAIConversation', 'DescribeAIConversation', 'ControlAIConversation', 'StopAIConversation'}
CAPABILITIES = {'voice_rtc': True, 'client_tools_verified': False,
                'video_understanding_verified': False, 'full_media_round_trip_verified': False,
                'public_api_configuration': 'voice_cascade', 'hardware_verified': False}
ARK_URL = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def safe_code(value):
    return re.sub(r'[^A-Za-z0-9_.-]', '', str(value or 'unknown'))[:100]


class TencentApiError(RuntimeError):
    def __init__(self, code, request_id=None, *, unknown_outcome=False):
        self.code = safe_code(code)
        self.request_id = request_id if isinstance(request_id, str) else None
        self.unknown_outcome = bool(unknown_outcome)
        # Provider error text may echo credentials or configuration. Never retain it.
        super().__init__('Tencent API: ' + self.code)


@dataclass(frozen=True)
class TencentConfig:
    sdk_app_id: int
    sdk_secret_key: str = field(repr=False)
    secret_id: str = field(repr=False)
    secret_key: str = field(repr=False)
    region: str = 'ap-guangzhou'
    timeout: float = 8.0

    def __post_init__(self):
        if type(self.sdk_app_id) is not int or self.sdk_app_id <= 0:
            raise ValueError('SdkAppId must be a positive integer')
        for name in ('sdk_secret_key', 'secret_id', 'secret_key'):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError('Missing Tencent credential: ' + name)
        if self.region not in REGIONS:
            raise ValueError('Unsupported Tencent TRTC region')
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 15:
            raise ValueError('Tencent timeout must be 0..15 seconds')

    @classmethod
    def from_file(cls, path):
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if data.get('ApiEndpoint', HOST) not in (HOST, 'https://' + HOST):
            raise ValueError('Tencent credentials specify a nonofficial endpoint')
        if data.get('ApiVersion', VERSION) != VERSION:
            raise ValueError('Unsupported Tencent API version')
        return cls(data['SdkAppId'], data['SdkSecretKey'], data['SecretId'], data['SecretKey'],
                   data.get('Region', 'ap-guangzhou'))


@dataclass(frozen=True)
class LLMConfig:
    model: str
    api_key: str = field(repr=False)
    api_url: str = ARK_URL

    def __post_init__(self):
        if not self.model or not self.api_key:
            raise ValueError('LLM model and API key are required')
        if not self.api_url.startswith('https://') or any(ch in self.api_url for ch in '\r\n'):
            raise ValueError('LLM API URL must use HTTPS')

    @classmethod
    def from_ark_file(cls, path):
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))['Ark']
        return cls(data['ModelName'], data['APIKey'])


def user_sig(sdk_app_id, sdk_secret_key, user_id, *, seconds=180, timestamp=None):
    """Short-lived TLS 2.0 UserSig; not room-scoped. Never log or persist it."""
    if not isinstance(user_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', user_id):
        raise ValueError('TRTC UserId must be 1..32 ASCII identifier characters')
    if type(sdk_app_id) is not int or sdk_app_id <= 0 or not sdk_secret_key:
        raise ValueError('Invalid TRTC application credentials')
    if type(seconds) is not int or not 1 <= seconds <= 180:
        raise ValueError('UserSig lifetime must be 1..180 seconds')
    now = int(time.time()) if timestamp is None else int(timestamp)
    content = ('TLS.identifier:%s\nTLS.sdkappid:%s\nTLS.time:%s\nTLS.expire:%s\n'
               % (user_id, sdk_app_id, now, seconds))
    signature = base64.b64encode(hmac.new(sdk_secret_key.encode(), content.encode(), hashlib.sha256).digest()).decode()
    record = {'TLS.ver': '2.0', 'TLS.identifier': user_id, 'TLS.sdkappid': sdk_app_id,
              'TLS.expire': seconds, 'TLS.time': now, 'TLS.sig': signature}
    return base64.b64encode(zlib.compress(json_bytes(record))).decode().replace('+', '*').replace('/', '-').replace('=', '_')


def signed_headers(config, action, body, *, timestamp=None):
    if action not in ACTIONS:
        raise ValueError('Unsupported Tencent API action')
    if not isinstance(body, bytes):
        raise TypeError('Sign the exact UTF-8 request bytes')
    now = int(time.time()) if timestamp is None else int(timestamp)
    date = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')
    content_type = 'application/json; charset=utf-8'
    canonical_headers = ('content-type:%s\nhost:%s\nx-tc-action:%s\n' % (content_type, HOST, action.lower()))
    signed = 'content-type;host;x-tc-action'
    canonical = '\n'.join(('POST', '/', '', canonical_headers, signed, hashlib.sha256(body).hexdigest()))
    scope = date + '/trtc/tc3_request'
    string_to_sign = '\n'.join(('TC3-HMAC-SHA256', str(now), scope, hashlib.sha256(canonical.encode()).hexdigest()))
    digest = lambda key, value: hmac.new(key, value.encode(), hashlib.sha256).digest()
    signing_key = digest(digest(digest(('TC3' + config.secret_key).encode(), date), 'trtc'), 'tc3_request')
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = ('TC3-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s'
                     % (config.secret_id, scope, signed, signature))
    return {'Authorization': authorization, 'Content-Type': content_type, 'Host': HOST,
            'X-TC-Action': action, 'X-TC-Version': VERSION, 'X-TC-Timestamp': str(now),
            'X-TC-Region': config.region}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise TencentApiError('redirect_refused', unknown_outcome=True)


def https_transport(url, body, headers, timeout):
    if url != 'https://' + HOST + '/':
        raise ValueError('Only the official Tencent TRTC endpoint is allowed')
    request = Request(url, data=body, headers=headers, method='POST')
    try:
        response = build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read(131073)
    if len(raw) > 131072:
        raise TencentApiError('response_too_large', unknown_outcome=True)
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise TencentApiError('invalid_json', unknown_outcome=True) from None


class TencentConversation:
    """Reusable bounded voice route. The media client remains a separate SDK peer.

    Methods are synchronous and serialized; async callers should use to_thread.
    Cloud task identifiers are retained after a failed stop for explicit retry.
    """
    def __init__(self, config, llm, *, on_event=None, transport=None, seconds=60, vad_silence_ms=1000):
        if type(seconds) is not int or not 1 <= seconds <= 180:
            raise ValueError('Tencent session must be 1..180 seconds')
        self.config, self.llm = config, llm
        self.on_event = on_event or (lambda _: None)
        self.transport = transport or https_transport
        self.seconds = seconds
        if type(vad_silence_ms) is not int or not 240 <= vad_silence_ms <= 2000:
            raise ValueError('Tencent VAD silence must be 240..2000 ms')
        self.vad_silence_ms = vad_silence_ms
        self.session_id = 'Session' + uuid.uuid4().hex
        self.room_id = 'Room' + uuid.uuid4().hex
        self.user_id = 'User' + uuid.uuid4().hex[:24]
        self.bot_id = 'Bot' + uuid.uuid4().hex[:24]
        self.task_id = None
        self.needs_stop = False
        self.start_outcome_unknown = False
        self.closed = False
        self.started_at = None
        self.deadline = None
        self.events = []
        self.audit_failures = 0
        self.lock = threading.RLock()

    def _event(self, event, *, required=False):
        record = dict(event, timestamp=time.time(), session_id=self.session_id,
                      measured_scope='control_plane_only', billed_cost_verified=False)
        self.events.append(record)
        try:
            self.on_event(record)
        except Exception:
            self.audit_failures += 1
            record['audit_incomplete'] = True
            if required:
                raise TencentApiError('audit_unavailable') from None

    def call(self, action, body):
        raw = json_bytes(body)
        headers = signed_headers(self.config, action, raw)
        started = time.monotonic()
        self._event({'event': 'tencent_api_start', 'action': action, 'attempt': 1, 'retried': False},
                    required=action in ('StartAIConversation', 'ControlAIConversation'))
        try:
            data = self.transport('https://' + HOST + '/', raw, headers, self.config.timeout)
            response = data.get('Response') if isinstance(data, dict) else None
            if not isinstance(response, dict) or not isinstance(response.get('RequestId'), str):
                raise TencentApiError('invalid_response', unknown_outcome=True)
            error = response.get('Error')
            if error:
                raise TencentApiError(error.get('Code') if isinstance(error, dict) else 'provider_error', response['RequestId'])
        except Exception as exc:
            error = exc if isinstance(exc, TencentApiError) else TencentApiError('network_or_transport_error', unknown_outcome=True)
            self._event({'event': 'tencent_api_end', 'action': action, 'ok': False,
                         'error_code': error.code, 'request_id': error.request_id,
                         'outcome_unknown': error.unknown_outcome,
                         'control_plane_ms': round((time.monotonic()-started)*1000, 3)})
            raise error from None
        self._event({'event': 'tencent_api_end', 'action': action, 'ok': True,
                     'request_id': response['RequestId'],
                     'control_plane_ms': round((time.monotonic()-started)*1000, 3)})
        return response

    def client_parameters(self):
        """Temporary SDK room-entry data. Keep in memory; never write to evidence."""
        return {'sdkAppId': self.config.sdk_app_id, 'roomId': self.room_id, 'strRoomId': self.room_id,
                'roomIdType': 1, 'userId': self.user_id, 'botId': self.bot_id,
                'sessionId': self.session_id,
                'userSig': user_sig(self.config.sdk_app_id, self.config.sdk_secret_key, self.user_id, seconds=self.seconds)}

    def start_body(self, system_prompt='用中文简短回答；不能声称执行了设备动作。'):
        llm = {'LLMType': 'openai', 'Model': self.llm.model, 'APIKey': self.llm.api_key,
               'APIUrl': self.llm.api_url, 'Streaming': True, 'SystemPrompt': system_prompt,
               'Timeout': 10.0, 'History': 0, 'MaxTokens': 128}
        if self.llm.model == 'doubao-seed-2-1-lite-260915':
            llm['ExtraBody'] = {'thinking': {'type': 'disabled'}}
        return {'SdkAppId': self.config.sdk_app_id, 'RoomId': self.room_id, 'RoomIdType': 1,
                'SessionId': self.session_id,
                'AgentConfig': {'UserId': self.bot_id, 'UserSig': user_sig(self.config.sdk_app_id,
                    self.config.sdk_secret_key, self.bot_id, seconds=self.seconds),
                    # Backend validation requires >=60, even for a shorter
                    # local experiment. The caller's finally-stop bounds cost.
                    'TargetUserId': self.user_id, 'MaxIdleTime': max(60, self.seconds),
                    'WelcomeMessage': '', 'InterruptMode': 0, 'FilterOneWord': False,
                    'SubtitleMode': 1},
                'STTConfig': {'Language': 'zh', 'VadSilenceTime': self.vad_silence_ms},
                'LLMConfig': json.dumps(llm, ensure_ascii=False, separators=(',', ':')),
                # Latest TTS topic (2026-07), newer than the Start API example.
                'TTSConfig': json.dumps({'TTSType': 'flow', 'VoiceId': 'v-female-R2s4N9qJ',
                    'Model': 'flow_02_turbo', 'Speed': 1.0, 'Volume': 1.0, 'Language': 'zh'}, separators=(',', ':'))}

    def start(self, system_prompt='用中文简短回答；不能声称执行了设备动作。'):
        with self.lock:
            if self.closed or self.needs_stop or self.task_id:
                raise RuntimeError('Tencent session cannot be started twice or reused after stop')
            self.started_at = time.time()
            self.deadline = time.monotonic() + self.seconds
            self.needs_stop = True
            self.start_outcome_unknown = True
            try:
                response = self.call('StartAIConversation', self.start_body(system_prompt))
            except TencentApiError as exc:
                if not exc.unknown_outcome:
                    self.needs_stop = False  # explicit rejection; no acknowledgement claimed
                    self.start_outcome_unknown = False
                    self.closed = True
                raise
            if not isinstance(response.get('TaskId'), str) or not response['TaskId']:
                raise TencentApiError('missing_task_id', response.get('RequestId'), unknown_outcome=True)
            self.task_id = response['TaskId']
            self.start_outcome_unknown = False
            return {'ok': True, 'status': 'submitted', 'task_id': self.task_id,
                    'request_id': response['RequestId'], 'ready': False, **CAPABILITIES}

    def describe(self):
        with self.lock:
            body = {'SdkAppId': self.config.sdk_app_id}
            body['TaskId' if self.task_id else 'SessionId'] = self.task_id or self.session_id
            response = self.call('DescribeAIConversation', body)
            if response.get('TaskId'):
                self.task_id = response['TaskId']
                self.start_outcome_unknown = False
            return {'status': response.get('Status'), 'task_id': response.get('TaskId'),
                    'request_id': response['RequestId'], 'ready': False,
                    'note': 'InProgress alone does not prove audio, image, tool or TTS delivery'}

    def control(self, command, text=''):
        with self.lock:
            if self.closed or not self.task_id or not self.needs_stop:
                raise RuntimeError('Tencent task is not active')
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise RuntimeError('Tencent session deadline reached; stop the task')
            if command not in ('ServerPushText', 'InvokeLLM') or not isinstance(text, str) or len(text.encode()) > 4096:
                raise ValueError('Invalid Tencent control command')
            field = 'Text' if command == 'ServerPushText' else 'Content'
            body = {'TaskId': self.task_id, 'Command': command,
                    command: {field: text, 'Interrupt': True}}
            response = self.call('ControlAIConversation', body)
            return {'ok': True, 'status': 'submitted', 'request_id': response['RequestId']}

    def stop(self):
        with self.lock:
            self.closed = True
            if not self.needs_stop:
                return {'ok': True, 'status': 'no_cloud_task', 'cloud_stop_confirmed': False,
                        'billing_stopped_verified': False}
            if not self.task_id:
                # A timed-out Start may still create a task after the first
                # lookup. A not-found lookup never proves that Start did not
                # happen. Keep this identity and responsibility for later retry.
                for attempt in range(3):
                    try:
                        self.describe()  # recover by stable SessionId
                        break
                    except TencentApiError as exc:
                        if exc.code == 'FailedOperation.TaskNotExist' and attempt < 2:
                            time.sleep(.25)
                            continue
                        return {'ok': False, 'status': 'cleanup_pending', 'error_code': exc.code,
                                'session_id': self.session_id, 'lookup_attempts': attempt+1,
                                'start_outcome_unknown': self.start_outcome_unknown,
                                'cloud_stop_confirmed': False, 'billing_stopped_verified': False}
            if not self.task_id:
                return {'ok': False, 'status': 'cleanup_pending', 'session_id': self.session_id,
                        'billing_stopped_verified': False}
            try:
                response = self.call('StopAIConversation', {'TaskId': self.task_id})
            except TencentApiError as exc:
                if exc.code != 'FailedOperation.TaskNotExist':
                    return {'ok': False, 'status': 'cleanup_pending', 'task_id': self.task_id,
                            'error_code': exc.code, 'billing_stopped_verified': False}
                response = {'RequestId': exc.request_id}
            self.needs_stop = False
            return {'ok': True, 'status': 'stopped', 'task_id': self.task_id,
                    'request_id': response['RequestId'], 'cloud_stop_confirmed': True,
                    'billing_stopped_verified': False}


def probe(config, llm):
    route = TencentConversation(config, llm, seconds=20)
    result = {'provider': 'tencent_trtc', 'schema_version': 1, **CAPABILITIES,
              'full_pipeline_latency_ms': None, 'media_published': False,
              'note': 'Control-plane qualification only; no microphone, camera, browser, aircraft or user media input.'}
    try:
        result['start'] = route.start()
        result['describe'] = route.describe()
    except TencentApiError as exc:
        result['error_code'] = exc.code
        result['request_id'] = exc.request_id
        result['outcome_unknown'] = exc.unknown_outcome
    finally:
        result['cleanup'] = route.stop()
        result['events'] = route.events
    result['ok'] = bool(result.get('start', {}).get('ok') and result['cleanup'].get('ok'))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials', required=True)
    parser.add_argument('--ark-credentials', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--real', action='store_true', help='Explicitly allow one bounded paid cloud task; no media')
    args = parser.parse_args(argv)
    if not args.real:
        parser.error('--real is required; no implicit cloud request')
    config = TencentConfig.from_file(args.credentials)
    llm = LLMConfig.from_ark_file(args.ark_credentials)
    result = probe(config, llm)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'ok': result['ok'], 'error_code': result.get('error_code'),
                      'cleanup': result['cleanup']['status'], 'output': str(path.resolve()),
                      'full_pipeline_latency_ms': None}, ensure_ascii=False))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
