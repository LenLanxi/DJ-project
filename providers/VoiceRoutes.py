"""Explicit startup-only voice selection; credentials never enter browser config.

Both providers retain the same local program, navigation and interruption path.
Loading this module opens no connection and does not alter os.environ.
"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from types import MappingProxyType
from app.Tools import PROGRAM_MAX_BYTES


ARK_URL = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'
DOUBAO_FIELDS = {
    'RTC_APP_ID': ('RTC', 'AppId'),
    'RTC_APP_KEY': ('RTC', 'AppKey'),
    'VOLCENGINE_ACCESS_KEY_ID': ('OpenAPI', 'AccessKeyId'),
    'VOLCENGINE_SECRET_ACCESS_KEY': ('OpenAPI', 'SecretAccessKey'),
    'VOLC_S2S_APP_ID': ('Speech', 'AppId'),
    'VOLC_S2S_TOKEN': ('Speech', 'AccessToken'),
    'RTC_VLM_MODEL': ('Ark', 'ModelName'),
    'RTC_VLM_API_KEY': ('Ark', 'APIKey'),
}
SECRET_FIELDS = ('RTC_APP_KEY', 'VOLCENGINE_ACCESS_KEY_ID',
                 'VOLCENGINE_SECRET_ACCESS_KEY', 'VOLC_S2S_TOKEN', 'RTC_VLM_API_KEY')


def register_secrets(log, values):
    """Add file credentials to the existing logger's in-memory redaction set."""
    if log is not None and hasattr(log, 'secrets'):
        log.secrets = tuple(dict.fromkeys((*log.secrets,
                            *(value for value in values if isinstance(value, str) and value))))


def _credentials_path(value, variable):
    selected = value or os.environ.get(variable)
    if not isinstance(selected, str) or not selected.strip():
        raise ValueError('缺少服务端凭据文件路径：' + variable)
    return Path(selected).expanduser()


def _doubao_values(path):
    try:
        if path.stat().st_size > 65536:
            raise ValueError()
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError()
    except (OSError, ValueError):
        raise ValueError('豆包服务端凭据文件不存在、不可读或格式无效') from None
    result = {}
    for name, (group, key) in DOUBAO_FIELDS.items():
        section = value.get(group)
        item = section.get(key) if isinstance(section, dict) else None
        if isinstance(item, (str, int)) and not isinstance(item, bool):
            result[name] = str(item).strip()
    return result


@dataclass(frozen=True)
class VoiceRoute:
    provider: str
    config: object = field(default=None, repr=False)
    rtc_environment: object = field(default_factory=dict, repr=False)
    vision_key: str = field(default='', repr=False)
    vision_model: str = ''
    code_limit: int = PROGRAM_MAX_BYTES

    def vision_client(self, log):
        if self.provider == 'alibaba':
            from providers.AlibabaVision import AlibabaVisionClient
            return AlibabaVisionClient(self.config,on_usage=log,max_output_tokens=512)
        from providers.model_client import ModelClient
        return ModelClient(on_usage=log, key=self.vision_key, model=self.vision_model, url=ARK_URL,
                           max_output_tokens=512)


def load_voice_route(provider, *, alibaba_credentials=None, doubao_credentials=None, log=None):
    """Validate local configuration before hardware. Never auto-switch/replay.

    Alibaba uses its own realtime model for fresh observe() navigation too.
    The explicitly selected historical Doubao route retains its own Ark client.
    """
    if provider not in ('alibaba', 'doubao'):
        raise ValueError('语音服务仅支持alibaba或doubao；不会自动切换')
    config = None
    if provider == 'alibaba':
        from providers.AlibabaRealtime import AlibabaConfig, AlibabaError
        try:
            path = _credentials_path(alibaba_credentials, 'ALIBABA_CREDENTIALS_FILE')
            config = AlibabaConfig.from_credentials(path)
        except (ValueError, TypeError, AttributeError) as exc:
            # Adapter errors contain local codes only; never raw provider data.
            reason = str(exc) if isinstance(exc, AlibabaError) else 'credentials_invalid_or_missing'
            raise ValueError('阿里云语音配置无效：' + reason
                             + '；未连接飞机。如需豆包备选，请显式使用--voice-provider doubao并准备其凭据') from None
        register_secrets(log, (config.api_key,))
        return VoiceRoute(provider=provider,config=config,vision_model=config.model,
                          rtc_environment=MappingProxyType({}),code_limit=PROGRAM_MAX_BYTES)
    try:
        values = _doubao_values(_credentials_path(doubao_credentials, 'DOUBAO_CREDENTIALS_FILE'))
    except ValueError as exc:
        raise ValueError(str(exc) + '；豆包备选路线需要该文件；未连接飞机') from None
    register_secrets(log, (values.get(name) for name in SECRET_FIELDS))
    names = tuple(DOUBAO_FIELDS)
    missing = [name for name in names if not values.get(name)]
    if missing:
        raise ValueError('豆包服务端凭据缺少配置：' + ', '.join(missing)
                         + '；没有模拟或自动换服务降级；未连接飞机')
    return VoiceRoute(provider=provider, config=config,
                      rtc_environment=MappingProxyType(values),
                      vision_key=values['RTC_VLM_API_KEY'], vision_model=values['RTC_VLM_MODEL'],
                      code_limit=PROGRAM_MAX_BYTES)
