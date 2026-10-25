"""免按键监听的 VAD 收尾逻辑：起音后立刻静音也必须正常返回，不能抛 UnboundLocalError。

这个 bug 是实测撞出来的：噪声尖峰触发起音，随后一直安静 —— 老代码里 last_voice
只在"说话还在继续"的分支里赋值，于是收尾时直接 UnboundLocalError，语音台一开就崩。
"""
import threading
import time
import unittest

import numpy as np

import audio_io


class FakeStream:
    def stop(self):
        pass

    def close(self):
        pass


def block(amplitude, frames=800):
    return np.full((frames, 1), amplitude, dtype='int16')


def run_listen(script, block_ms=50, **kwargs):
    """用合成音频驱动 listen_once：script 是 [(振幅, 块数), ...]。"""
    calls = {}

    def fake_open(_sd, _device, rate, block_ms_arg, callback):
        def feed():
            time.sleep(0.02)
            for amplitude, count in script:
                for _ in range(count):
                    callback(block(amplitude), block_ms_arg, None, None)
                    time.sleep(0.02)
            calls['fed'] = True
        threading.Thread(target=feed, daemon=True).start()
        return FakeStream(), rate, int(rate * block_ms_arg / 1000)

    original = audio_io._open_input
    audio_io._open_input = fake_open
    try:
        return audio_io.listen_once(None, silence_s=0.15, max_s=5.0,
                                    wait_s=3.0, **kwargs)
    finally:
        audio_io._open_input = original


class VADTailTests(unittest.TestCase):
    def test_onset_then_silence_returns_instead_of_crashing(self):
        # 起音 4 块（>150ms onset）后立刻安静：这就是曾经崩掉的路径
        wav, info = run_listen([(8000, 4), (1, 6)])
        self.assertIsNotNone(wav)
        self.assertEqual(info['reason'], 'ok')
        self.assertGreater(info['speech_s'], 0.0)

    def test_continuous_speech_keeps_voice_tail(self):
        wav, info = run_listen([(8000, 10), (1, 5)])
        self.assertIsNotNone(wav)
        self.assertGreater(info['speech_s'], 0.0)

    def test_pure_silence_times_out_without_wav(self):
        wav, info = run_listen([(1, 30)])
        self.assertIsNone(wav)
        self.assertEqual(info['reason'], 'timeout')

    def test_pause_event_cancels_capture(self):
        pause = threading.Event()
        threading.Timer(0.05, pause.set).start()
        wav, info = run_listen([(8000, 40)], pause=pause)
        self.assertIsNone(wav)
        self.assertEqual(info['reason'], 'paused')


if __name__ == '__main__':
    unittest.main()
