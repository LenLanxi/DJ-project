"""Explicit PortAudio endpoints shared by the application and headset self-test."""
from dataclasses import dataclass, replace
import math

RATE = 24000
BLOCK_FRAMES = 480


def list_audio_devices(sd):
    print('索引 | 输入通道 | 输出通道 | 音频后端 | 设备名称')
    for index, info in enumerate(sd.query_devices()):
        host = sd.query_hostapis(info['hostapi'])['name']
        print(f"{index:4} | {info['max_input_channels']:8} | "
              f"{info['max_output_channels']:8} | {host} | {info['name']}")


@dataclass(frozen=True)
class Endpoint:
    index: int
    name: str
    hostapi: str
    channels: int
    extra_settings: object = None
    rate: int = RATE

    def stream_kwargs(self):
        return dict(device=self.index, samplerate=self.rate, channels=self.channels,
                    dtype='int16', extra_settings=self.extra_settings)


def resolve_endpoints(sd, config):
    endpoints = []
    for direction in ('input', 'output'):
        index = getattr(config, direction + '_device')
        if index is None:
            raise ValueError('必须明确指定输入和输出设备；先运行 '
                             'python test_audio_device.py --list，再选择索引；不使用系统默认设备')
        try:
            info = sd.query_devices(index)
            host = sd.query_hostapis(info['hostapi'])['name']
        except Exception as exc:
            raise ValueError(f'{direction} 设备索引 {index} 不可用，请重新列出设备') from exc
        name = info['name']
        if info['max_' + direction + '_channels'] < 1:
            raise ValueError(f'设备 [{index}] {name} 不支持 {direction}')
        # These Windows endpoints follow system defaults, even with a numeric index.
        if name.casefold().startswith(('microsoft sound mapper', 'primary sound',
                                      '主声音', '主音频')):
            raise ValueError(f'[{index}] {name} 是默认设备映射，请选择具体耳机端点')
        for field, actual in (('name', name), ('hostapi', host)):
            expected = getattr(config, direction + '_' + field)
            if expected is not None and expected != actual:
                raise ValueError(f'{direction} 设备身份不匹配：索引 {index} 当前为 {name} / {host}；'
                                 '请重新选择并保存配置，不会自动切换到其他设备')
        # Stereo headphone output avoids mono-only stream failures on common USB devices.
        channels = 1 if direction == 'input' else (config.output_channels or min(2, info['max_output_channels']))
        extra = None
        if 'wasapi' in host.casefold():
            extra = sd.WasapiSettings(exclusive=False, auto_convert=config.wasapi_auto_convert)
        rate=getattr(config,direction+'_samplerate') or info.get('default_samplerate',RATE)
        if type(rate) not in (int,float) or not math.isfinite(rate) or not 8000<=rate<=192000:
            raise ValueError('invalid_device_sample_rate')
        rate=int(round(rate))
        endpoint = Endpoint(index, name, host, channels, extra,rate)
        checker = sd.check_input_settings if direction == 'input' else sd.check_output_settings
        try:
            checker(**endpoint.stream_kwargs())
        except Exception as exc:
            raise ValueError(f'{direction} [{index}] {name} 无法使用 {rate}Hz PCM16/{channels}通道：'
                             f'{exc}。Windows 可尝试同一耳机的 WASAPI 索引；不自动改用其他设备') from exc
        endpoints.append(endpoint)
    return tuple(endpoints)


def pinned_config(config, endpoints):
    values = {}
    for direction, endpoint in zip(('input', 'output'), endpoints):
        values.update({direction + '_device': endpoint.index,
                       direction + '_name': endpoint.name,
                       direction + '_hostapi': endpoint.hostapi})
    return replace(config, **values)


def describe_endpoints(endpoints):
    for label, endpoint in zip(('输入麦克风', '输出耳机'), endpoints):
        print(f'{label}: [{endpoint.index}] {endpoint.name} / {endpoint.hostapi} '
              f'({endpoint.channels}通道, 设备{endpoint.rate}Hz; API {RATE}Hz)')
