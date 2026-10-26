"""Device/PCM/lifecycle tests migrated to the current production modules."""
import asyncio
import json
import math
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from AudioConfig import AudioConfig, load_audio_config, save_audio_config
from AudioDevices import pinned_config, resolve_endpoints
from DuplexAudio import DuplexAudio, output_pcm
from AudioDevice import levels, record_three_seconds, play_recording
def fake_sd():
    sd = MagicMock()
    sd.query_devices.side_effect = [
        dict(name='USB Mic', hostapi=0, max_input_channels=1, max_output_channels=0),
        dict(name='USB Headphones', hostapi=0, max_input_channels=0, max_output_channels=2),
    ]
    sd.query_hostapis.return_value = dict(name='Windows WASAPI')
    return sd


class DeviceTests(unittest.TestCase):
    def test_no_system_defaults(self):
        sd = fake_sd()
        with self.assertRaisesRegex(ValueError, '必须明确指定'):
            resolve_endpoints(sd, AudioConfig())
        sd.query_devices.assert_not_called()

    def test_explicit_indices_wasapi_conversion_and_stereo(self):
        sd = fake_sd()
        first, second = resolve_endpoints(sd, AudioConfig(input_device=4, output_device=8))
        self.assertEqual([call.args for call in sd.query_devices.call_args_list], [(4,), (8,)])
        self.assertEqual((first.channels, second.channels), (1, 2))
        sd.WasapiSettings.assert_called_with(exclusive=False, auto_convert=True)
        self.assertEqual(sd.check_input_settings.call_args.kwargs['device'], 4)
        self.assertEqual(sd.check_output_settings.call_args.kwargs['device'], 8)

    def test_stale_index_identity_fails_without_fallback(self):
        for metadata in ({'input_name': 'Other Mic'}, {'input_hostapi': 'Other Backend'}):
            sd = fake_sd()
            with self.subTest(metadata=metadata), self.assertRaisesRegex(ValueError, '身份不匹配'):
                resolve_endpoints(sd, AudioConfig(input_device=4, output_device=8, **metadata))
            self.assertEqual(sd.query_devices.call_count, 1)

    def test_disappeared_device_and_wrong_direction_fail(self):
        sd = fake_sd()
        sd.query_devices.side_effect = RuntimeError('invalid device')
        with self.assertRaisesRegex(ValueError, '不可用'):
            resolve_endpoints(sd, AudioConfig(input_device=4, output_device=8))
        sd = fake_sd()
        sd.query_devices.side_effect = None
        sd.query_devices.return_value = dict(name='USB Mic', hostapi=0,
                                            max_input_channels=1, max_output_channels=0)
        with self.assertRaisesRegex(ValueError, '不支持 output'):
            resolve_endpoints(sd, AudioConfig(input_device=4, output_device=4))

    def test_default_mapper_rejected(self):
        sd = fake_sd()
        sd.query_devices.side_effect = None
        sd.query_devices.return_value = dict(name='Microsoft Sound Mapper - Input', hostapi=0,
                                            max_input_channels=1, max_output_channels=0)
        with self.assertRaisesRegex(ValueError, '默认设备映射'):
            resolve_endpoints(sd, AudioConfig(input_device=0, output_device=1))

    def test_unsupported_format_never_opens_other_device(self):
        sd = fake_sd()
        sd.check_input_settings.side_effect = RuntimeError('Invalid sample rate')
        with self.assertRaisesRegex(ValueError, '24kHz'):
            resolve_endpoints(sd, AudioConfig(input_device=4, output_device=8))
        self.assertEqual(sd.query_devices.call_count, 1)
        sd.RawInputStream.assert_not_called()

    def test_non_wasapi_does_not_receive_wasapi_settings(self):
        sd = fake_sd()
        sd.query_hostapis.return_value = dict(name='Core Audio')
        endpoints = resolve_endpoints(sd, AudioConfig(input_device=4, output_device=8))
        sd.WasapiSettings.assert_not_called()
        self.assertTrue(all(x.extra_settings is None for x in endpoints))

    def test_input_closed_if_output_open_fails(self):
        sd = fake_sd()
        sd.RawOutputStream.side_effect = RuntimeError('headphone unplugged')
        audio = DuplexAudio(4, 8)
        with patch.dict('sys.modules', {'sounddevice': sd}), self.assertRaises(RuntimeError):
            audio.start()
        sd.RawInputStream.return_value.close.assert_called_once()
        self.assertEqual(audio.streams, [])

    def test_close_even_when_abort_raises(self):
        audio = DuplexAudio(4, 8)
        stream = MagicMock()
        stream.abort.side_effect = RuntimeError('already disconnected')
        audio.streams = [stream]
        audio.close()
        stream.close.assert_called_once()

    def test_stereo_preserves_samples_and_mono_accounting(self):
        mono = struct.pack('<hhhh', 1, -2, 32767, -32768)
        expected = struct.pack('<hhhhhhhh', 1, 1, -2, -2, 32767, 32767, -32768, -32768)
        self.assertEqual(output_pcm(mono, 2), expected)
        self.assertEqual(output_pcm(mono, 1), mono)


class AudioSettingsTests(unittest.TestCase):
    def test_save_and_load_identity_and_cli_override(self):
        config = AudioConfig(input_device=4, output_device=8, vad_threshold=0.8)
        config = pinned_config(config, resolve_endpoints(fake_sd(), config))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audio.json'
            save_audio_config(path, config)
            self.assertEqual(load_audio_config(path), config)
            override = load_audio_config(path, input_device=3)
        self.assertIsNone(override.input_name)
        self.assertEqual(override.input_device, 3)
        self.assertEqual(override.output_name, 'USB Headphones')
        self.assertEqual(override.vad_threshold, 0.8)

    def test_invalid_and_misspelled_settings_fail(self):
        invalid = [{'input_device': True}, {'output_device': -1}, {'vad_threshold': 2},
                   {'vad_threshold': math.nan}, {'silence_duration_ms': -1},
                   {'prefix_padding_ms': 0.5}, {'noise_reduction': 'aec'},
                   {'wasapi_auto_convert': 'false'}]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                AudioConfig(**data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audio.json'
            path.write_text(json.dumps({'vad_treshold': 0.7}))
            with self.assertRaisesRegex(ValueError, '未知'):
                load_audio_config(path)



class AudioLifecycleTests(unittest.IsolatedAsyncioTestCase):

    async def test_stopped_and_stalled_streams_fail_instead_of_hanging(self):
        for active in (False, True):
            audio = DuplexAudio(4, 8)
            audio.streams = [MagicMock(active=active), MagicMock(active=active)]
            audio.last_capture = audio.last_render = time.monotonic() - 3
            with self.subTest(active=active), self.assertRaises(RuntimeError):
                await anext(audio.chunks())

    async def test_finished_callback_reports_disconnect_but_not_normal_close(self):
        sd = fake_sd()
        audio = DuplexAudio(4, 8)
        with patch.dict('sys.modules', {'sounddevice': sd}):
            audio.start()
        done = sd.RawInputStream.call_args.kwargs['finished_callback']
        done()
        with self.assertRaisesRegex(RuntimeError, '设备流已停止'):
            await anext(audio.chunks())
        audio.close()
        audio.fault = None
        done()
        self.assertIsNone(audio.fault)

    async def test_capture_exact_three_seconds_and_report_level(self):
        class Audio:
            async def chunks(self):
                for _ in range(151):
                    yield struct.pack('<h', 16384) * 480
        recording = await record_three_seconds(Audio())
        self.assertEqual(len(recording), 24000 * 2 * 3)
        rms, peak = levels(recording)
        self.assertAlmostEqual(rms, -6.02, places=2)
        self.assertAlmostEqual(peak, -6.02, places=2)

    async def test_self_test_drains_capture_while_playing(self):
        class Audio:
            def __init__(self):
                from DuplexAudio import PlaybackBuffer
                self.speaker = PlaybackBuffer()
                self.captures = 0
            async def chunks(self):
                while True:
                    self.captures += 1
                    self.speaker.consume(480)
                    # Windows asyncio 的 sleep 最小粒度约 15ms，用 sleep(0) 保持节奏确定
                    await asyncio.sleep(0)
                    yield b'\x00\x00' * 480
        audio = Audio()
        await asyncio.wait_for(play_recording(audio, b'\x01\x00' * 24000 * 3), 2)
        self.assertGreaterEqual(audio.captures, 150)
        self.assertEqual(audio.speaker.size, 0)

