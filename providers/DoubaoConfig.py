"""One server-side credential file for RTC voice and Ark vision."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path

FIELDS = {
    'RTC_APP_ID': ('RTC', 'AppId'), 'RTC_APP_KEY': ('RTC', 'AppKey'),
    'VOLCENGINE_ACCESS_KEY_ID': ('OpenAPI', 'AccessKeyId'),
    'VOLCENGINE_SECRET_ACCESS_KEY': ('OpenAPI', 'SecretAccessKey'),
    'VOLC_S2S_APP_ID': ('Speech', 'AppId'), 'VOLC_S2S_TOKEN': ('Speech', 'AccessToken'),
    'RTC_VLM_MODEL': ('Ark', 'ModelName'), 'RTC_VLM_API_KEY': ('Ark', 'APIKey'),
}


@dataclass(frozen=True)
class DoubaoConfig:
    environment: dict = field(repr=False)

    @property
    def vision_key(self): return self.environment['RTC_VLM_API_KEY']

    @property
    def vision_model(self): return self.environment['RTC_VLM_MODEL']


def load_config(path=None, log=None):
    source = Path(path or os.environ.get('DOUBAO_CREDENTIALS_FILE') or
                  Path.home()/'Desktop'/'tmp'/'DoubaoRealtime'/'Credentials.json')
    try:
        data = json.loads(source.read_text(encoding='utf-8-sig'))
        values = {name: str(data[group][key]).strip() for name, (group,key) in FIELDS.items()
                  if isinstance(data.get(group, {}).get(key), (str,int))}
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        raise ValueError('豆包凭据文件不可读或格式无效：'+str(source)) from None
    missing = [name for name in FIELDS if not values.get(name)]
    if missing: raise ValueError('豆包凭据缺少字段：'+', '.join(missing))
    if log is not None and hasattr(log, 'secrets'):
        log.secrets = tuple(dict.fromkeys((*log.secrets, *values.values())))
    return DoubaoConfig(values)
