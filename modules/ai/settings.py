"""Non-secret settings file; API keys stay in environment variables.

设备索引统一由 audio_config.json 管理（与实时语音入口共用），
不再使用 AI_INPUT_DEVICE / AI_OUTPUT_DEVICE 环境变量。
"""
import json
from pathlib import Path

from audio_config import load_audio_config

DEFAULT = {'record_seconds': 5, 'input_device': None, 'output_device': None,
           'log_path': None, 'speak_replies': False}


def load(path=None):
    values = dict(DEFAULT)
    if path:
        custom = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(custom, dict) or set(custom) - set(DEFAULT):
            raise ValueError('配置只能包含config.example.json中的字段')
        values.update(custom)
    # 设备索引来自 audio_config.json（存在时）；命令行/配置文件显式值优先
    audio = load_audio_config()
    if values['input_device'] is None:
        values['input_device'] = audio.input_device
    if values['output_device'] is None:
        values['output_device'] = audio.output_device
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
