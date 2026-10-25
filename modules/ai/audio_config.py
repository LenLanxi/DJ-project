"""Validated headset/VAD settings. No credentials are stored in this file.

默认配置位置：先找当前目录的 audio_config.json，再找仓库根目录的同名文件。
这样从仓库根目录（modules/ai 入口）和 docs/v4-reference（实时语音入口）启动时
都能读到同一份配置。
"""
from dataclasses import asdict, dataclass, fields, replace
import json
from pathlib import Path


def default_config_paths():
    here = Path(__file__).resolve()
    repository = here.parents[2] if len(here.parents) > 2 else here.parent
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
    input_samplerate: int | None = None
    output_samplerate: int | None = None
    output_channels: int | None = None
    capture_buffer_ms: int = 200
    diagnostics_interval_s: int = 5
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
        if self.output_channels is not None and (type(self.output_channels) is not int or self.output_channels not in (1,2)):
            raise ValueError('output_channels 必须是1、2或null')
        for name in ('input_samplerate','output_samplerate'):
            v=getattr(self,name)
            if v is not None and (type(v) is not int or not 8000<=v<=192000):
                raise ValueError(name+' 必须是8000至192000的整数或null')
        for name,low,high in (('capture_buffer_ms',40,1000),('diagnostics_interval_s',1,60)):
            v=getattr(self,name)
            if type(v) is not int or not low<=v<=high: raise ValueError('invalid '+name)
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
