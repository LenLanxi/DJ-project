"""播放适配器：没有音频可播时必须输出静音，不能抛异常打死整条会话。

实测踩过：接豆包全双工，刚开流 / 被打断清空的那几个回调里 speaker 是空的，
render() 直接 raise RuntimeError('output_resampler_stalled')，回调置 fault，
会话当场退出（表现为"执行失败：音频回放失败：output_resampler_stalled"）。
"""
import unittest

from audio_streaming import OutputAdapter
from duplex_audio import PlaybackBuffer


class EmptySpeakerTests(unittest.TestCase):
    def test_empty_speaker_yields_silence(self):
        speaker = PlaybackBuffer(rate=24000)
        adapter = OutputAdapter(speaker, 44100)
        out = adapter.render(1024, 10.0)
        self.assertEqual(len(out), 1024 * 2)
        self.assertEqual(out, b'\x00' * (1024 * 2))

    def test_interrupt_between_callbacks_does_not_raise(self):
        speaker = PlaybackBuffer(rate=24000)
        adapter = OutputAdapter(speaker, 48000)
        for _ in range(5):
            speaker.interrupt()            # 模拟服务端打断/清空
            out = adapter.render(512, 10.0)
            self.assertEqual(len(out), 512 * 2)

    def test_audio_is_still_played_when_available(self):
        speaker = PlaybackBuffer(rate=24000)
        adapter = OutputAdapter(speaker, 24000)
        # 塞入 100ms 的 440Hz 24kHz 单声道音频
        import math
        import struct
        samples = [int(8000 * math.sin(2 * math.pi * 440 * i / 24000)) for i in range(2400)]
        speaker.append('item', 0, struct.pack('<%dh' % len(samples), *samples))
        out = adapter.render(1024, 10.0)
        self.assertEqual(len(out), 1024 * 2)
        self.assertNotEqual(out, b'\x00' * (1024 * 2))


if __name__ == '__main__':
    unittest.main()
