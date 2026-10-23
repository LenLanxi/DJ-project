"""Non-secret settings file; API keys stay in environment variables."""
import json
import os
from pathlib import Path

DEFAULT = {'record_seconds': 5, 'input_device': None, 'output_device': None,
           'log_path': None, 'speak_replies': False}

DEVICE_ENV = {'input_device': 'AI_INPUT_DEVICE', 'output_device': 'AI_OUTPUT_DEVICE'}


def _device_from_env(name):
    raw = (os.getenv(DEVICE_ENV[name]) or '').strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError('%s 必须是设备编号整数，当前为 %r' % (DEVICE_ENV[name], raw)) from None


def load(path=None):
    values = dict(DEFAULT)
    if path:
        custom = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(custom, dict) or set(custom) - set(DEFAULT):
            raise ValueError('配置只能包含config.example.json中的字段')
        values.update(custom)
    # 环境变量优先：现场换耳机时不必改 config.local.json
    for name in DEVICE_ENV:
        device = _device_from_env(name)
        if device is not None:
            values[name] = device
    if type(values['record_seconds']) not in (int, float) or not 1 <= values['record_seconds'] <= 20:
        raise ValueError('record_seconds须为1至20秒')
    for k in ('input_device', 'output_device'):
        if values[k] is not None and type(values[k]) is not int:
            raise ValueError(k + '须为设备编号或null')
    if values['log_path'] is not None and not isinstance(values['log_path'], str):
        raise ValueError('log_path须为字符串或null')
    if type(values['speak_replies']) is not bool:
        raise ValueError('speak_replies须为true或false')
    return values
