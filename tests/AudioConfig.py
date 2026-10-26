"""Validated headset/VAD settings. No credentials are stored in this file.

仅供独立 PortAudio 诊断。正式 RTC 使用浏览器设备选择，不读取这个配置。
"""
from dataclasses import asdict, dataclass, fields, replace
import json
from pathlib import Path


def default_config_paths():
    here = Path(__file__).resolve()
    repository = here.parents[1]
    return (Path.cwd() / 'audio_config.json', repository / 'audio_config.json')


def find_audio_config():
    for candidate in default_config_paths():
        if candidate.exists():
            return candidate
    return None


@dataclass(frozen=True)
class AudioConfig:
    input_device: int | None = None
    output_device: int | None = None
    input_name: str | None = None
    output_name: str | None = None
    input_hostapi: str | None = None
    output_hostapi: str | None = None
    wasapi_auto_convert: bool = True
    noise_reduction: str | None = 'near_field'
    vad_threshold: float = 0.7
    prefix_padding_ms: int = 300
    silence_duration_ms: int = 600

    def __post_init__(self):
        for field in ('input_device', 'output_device'):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f'{field} 必须是非负整数设备索引')
        for field in ('input_name', 'output_name', 'input_hostapi', 'output_hostapi'):
            value = getattr(self, field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f'{field} 必须是非空字符串或 null')
        if type(self.wasapi_auto_convert) is not bool:
            raise ValueError('wasapi_auto_convert 必须是 true 或 false')
        if self.noise_reduction not in ('near_field', 'far_field', None):
            raise ValueError('noise_reduction 必须是 near_field、far_field 或 null')
        if type(self.vad_threshold) not in (int, float) or not 0 <= self.vad_threshold <= 1:
            raise ValueError('vad_threshold 必须在 0 至 1 之间')
        for field, low, high in (('prefix_padding_ms', 0, 1000),
                                 ('silence_duration_ms', 100, 2000)):
            value = getattr(self, field)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{field} 必须为 {low} 至 {high} 的整数毫秒')


def load_audio_config(path=None, input_device=None, output_device=None):
    """Explicit paths must exist; otherwise auto-load audio_config.json if present."""
    source = Path(path) if path is not None else find_audio_config()
    data = {}
    if path is not None or source is not None:
        if not source.exists():
            raise ValueError(f'音频配置不存在：{source}')
        data = json.loads(source.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict):
            raise ValueError('音频配置必须为 JSON 对象')
        unknown = set(data) - {f.name for f in fields(AudioConfig)}
        if unknown:
            raise ValueError('未知音频配置项：' + ', '.join(sorted(unknown)))
    config = AudioConfig(**data)
    # Explicit CLI selection replaces the corresponding saved identity, not the other side.
    for direction, index in (('input', input_device), ('output', output_device)):
        if index is not None:
            config = replace(config, **{direction + '_device': index,
                                       direction + '_name': None,
                                       direction + '_hostapi': None})
    return config


def save_audio_config(path, config):
    Path(path).write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2) + '\n',
                          encoding='utf-8')
