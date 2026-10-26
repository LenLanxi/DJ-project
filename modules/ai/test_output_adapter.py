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
        adapter = OutputAdapter(speaker, 24000, prebuffer_ms=0)
        # 塞入 100ms 的 440Hz 24kHz 单声道音频
        import math
        import struct
        samples = [int(8000 * math.sin(2 * math.pi * 440 * i / 24000)) for i in range(2400)]
        speaker.append('item', 0, struct.pack('<%dh' % len(samples), *samples))
        out = adapter.render(1024, 10.0)
        self.assertEqual(len(out), 1024 * 2)
        self.assertNotEqual(out, b'\x00' * (1024 * 2))

    def test_jitter_buffer_holds_silence_until_enough_is_queued(self):
        # 服务器是 20ms 一块推的：不预缓冲就会"推一块播一块"，听感一卡一卡
        speaker = PlaybackBuffer(rate=24000)
        adapter = OutputAdapter(speaker, 48000, prebuffer_ms=150)
        speaker.append('item', 0, b'\x01\x00' * 480)     # 只有 20ms
        self.assertEqual(adapter.render(480, 10.0), b'\x00' * 960)

    def test_jitter_buffer_starts_once_prefill_reached(self):
        import struct

        speaker = PlaybackBuffer(rate=24000)
        adapter = OutputAdapter(speaker, 48000, prebuffer_ms=100)
        speaker.append('item', 0, struct.pack('<4800h', *([2000] * 4800)))   # 200ms
        out = adapter.render(480, 10.0)
        self.assertNotEqual(out, b'\x00' * 960)

    def test_after_first_gap_resume_needs_no_fresh_prebuffer(self):
        import struct

        speaker = PlaybackBuffer(rate=24000)
        adapter = OutputAdapter(speaker, 48000, prebuffer_ms=150)
        speaker.append('item', 0, struct.pack('<4800h', *([2000] * 4800)))     # 200ms
        self.assertNotEqual(adapter.render(480, 10.0), b'\x00' * 960)
        self.assertTrue(adapter.primed)
        for _ in range(300):                                    # 放空队列
            adapter.render(480, 10.0)
        self.assertEqual(speaker.size, 0)
        self.assertFalse(adapter.started)
        # 只来 20ms 数据：应当立刻出声，而不是再等 150ms
        speaker.append('item', 1, struct.pack('<480h', *([2000] * 480)))
        self.assertNotEqual(adapter.render(480, 10.0), b'\x00' * 960)

    def test_zero_prebuffer_keeps_old_behaviour(self):
        import struct

        speaker = PlaybackBuffer(rate=24000)
        adapter = OutputAdapter(speaker, 24000, prebuffer_ms=0)
        speaker.append('item', 0, struct.pack('<480h', *([2000] * 480)))
        self.assertNotEqual(adapter.render(240, 10.0), b'\x00' * 480)


if __name__ == '__main__':
    unittest.main()
