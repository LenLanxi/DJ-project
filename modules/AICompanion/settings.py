"""Non-secret settings file; API keys stay in environment variables."""
import json
from pathlib import Path

DEFAULT = {'record_seconds': 5, 'input_device': None, 'output_device': None,
           'log_path': None, 'speak_replies': False}

def load(path=None):
    values = dict(DEFAULT)
    if path:
        custom = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(custom, dict) or set(custom) - set(DEFAULT):
            raise ValueError('配置只能包含config.example.json中的字段')
        values.update(custom)
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
