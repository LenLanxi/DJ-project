"""Video queue regressions: fake reconnect paths plus real synthetic H264/UDP.

No aircraft, camera, browser or cloud is used. The loopback experiment measures
the known generation time embedded in pixels; FFmpeg raw H264 need not have PTS.
Official units/policy: https://ffmpeg.org/ffmpeg-protocols.html#udp
"""
from fractions import Fraction
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from drone_sdk import _driver as driver


class VideoTests(unittest.TestCase):
    def test01_explicit_fifo_has_documented_units_and_fatal_overrun(self):
        options = parse_qs(driver.VIDEO_URL_OPTIONS.lstrip('?'))
        units = int(options['fifo_size'][0])
        self.assertEqual(units, 7 * 4096)
        self.assertEqual(units * 188, 5390336)
        self.assertLess(units * 188, 2**31)
        self.assertEqual(options['overrun_nonfatal'], ['0'])

    def test02_open_failure_never_retries_an_unbounded_plain_url(self):
        decoder = driver._VideoDecoder('udp://127.0.0.1:19999')
        def fail(*args, **kwargs):
            decoder._halt.set()
            raise OSError('synthetic open failure')
        with patch('av.open', side_effect=fail) as opened:
            decoder.run()
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(opened.call_args.args[0], decoder.url + driver.VIDEO_URL_OPTIONS)
        self.assertEqual(opened.call_args.kwargs['timeout'], (5, 2))
        self.assertTrue(any('open(bounded_udp)' in error for error in decoder.errors))

    def test03_decode_overrun_is_visible_then_reconnects_under_the_same_policy(self):
        decoder = driver._VideoDecoder('udp://127.0.0.1:19999')
        closed = []
        class Frame:
            def to_ndarray(self, format):
                self.format = format
                return np.zeros((16, 16, 3), np.uint8)
        class Container:
            def __init__(self, fails): self.fails = fails
            def decode(self, video):
                if self.fails: raise OSError('Circular buffer overrun')
                yield Frame()
                decoder._halt.set()
            def close(self): closed.append(self.fails)
        with patch('av.open', side_effect=[Container(True), Container(False)]) as opened:
            decoder.run()
        self.assertEqual(opened.call_count, 2)
        self.assertTrue(all(call.args[0].endswith(driver.VIDEO_URL_OPTIONS) for call in opened.call_args_list))
        self.assertEqual(closed, [True, False])
        self.assertEqual(decoder.reconnects, 1)
        self.assertEqual(decoder.count, 1)
        self.assertTrue(any('Circular buffer overrun' in error for error in decoder.errors))

    def test04_real_h264_queue_can_be_old_even_when_decode_timestamp_is_new(self):
        import av
        encoder = av.CodecContext.create('libx264', 'w')
        encoder.width, encoder.height, encoder.pix_fmt = 256, 128, 'yuv420p'
        encoder.time_base, encoder.framerate = Fraction(1, 30), Fraction(30, 1)
        encoder.options = {'preset': 'ultrafast', 'tune': 'zerolatency', 'crf': '23',
                           'x264-params': 'keyint=15:scenecut=0:repeat-headers=1'}
        encoder.open()
        packets = []
        random = np.random.default_rng(7)
        for number in range(120):
            image = random.integers(0, 256, (128, 256, 3), dtype=np.uint8)
            for bit in range(8):
                image[:32, bit*32:(bit+1)*32, :] = 240 if (number >> bit) & 1 else 16
            frame = av.VideoFrame.from_ndarray(image, format='bgr24')
            frame.pts = number
            packets.append(b''.join(bytes(packet) for packet in encoder.encode(frame)))
        packets[-1] += b''.join(bytes(packet) for packet in encoder.encode(None))
        # An ephemeral loopback port is the only network destination.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserved:
            reserved.bind(('127.0.0.1', 0))
            port = reserved.getsockname()[1]
        sent, halt, errors = {}, threading.Event(), []
        def sender():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                    halt.wait(.15)
                    origin = time.monotonic()
                    for number, data in enumerate(packets):
                        if halt.wait(max(0., origin + number/30 - time.monotonic())): break
                        sent[number] = time.monotonic()
                        for start in range(0, len(data), 1200):
                            udp.sendto(data[start:start+1200], ('127.0.0.1', port))
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=sender, daemon=True)
        thread.start()
        container = None
        try:
            # The small probe only shortens fixture startup. No low-latency
            # tuning is being adopted in the production driver by this test.
            container = av.open(f'udp://127.0.0.1:{port}' + driver.VIDEO_URL_OPTIONS,
                                format='h264', options={'probesize': '32768', 'analyzeduration': '0'},
                                timeout=(3, 1))
            frames = container.decode(video=0)
            next(frames)
            time.sleep(1.1)
            pixels = next(frames).to_ndarray(format='bgr24')
            decoded_at = time.monotonic()
            number = sum(1 << bit for bit in range(8)
                         if pixels[4:28, bit*32+4:(bit+1)*32-4].mean() > 128)
            self.assertIn(number, sent)
            # This is an intentional counterexample, not a freshness pass:
            # source is >0.5s old although driver-style decode age is near zero.
            self.assertGreater(decoded_at - sent[number], .8)
            self.assertLess(time.monotonic() - decoded_at, .5)
            self.assertGreater(max(sent) - number, 20)
            self.assertFalse(errors)
        finally:
            halt.set()
            thread.join(2)
            if container is not None: container.close()
            self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
