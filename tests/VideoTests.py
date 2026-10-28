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
        self.assertLessEqual(units, 2048)
        self.assertGreaterEqual(units, 1024)
        self.assertLessEqual(units * 188, 385024)
        self.assertLess(units * 188, 2**31)
        self.assertEqual(options['overrun_nonfatal'], ['0'])
        self.assertEqual(options['buffer_size'], ['65536'])

    def test02_open_failure_never_retries_an_unbounded_plain_url(self):
        decoder = driver._VideoDecoder('udp://127.0.0.1:19999')
        def fail(*args, **kwargs):
            decoder._halt.set()
            raise OSError('synthetic open failure')
        with patch('av.open', side_effect=fail) as opened:
            decoder.run()
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(opened.call_args.args[0], decoder.url + driver.VIDEO_URL_OPTIONS)
        self.assertEqual(opened.call_args.kwargs['timeout'], (1, 1))
        self.assertEqual(opened.call_args.kwargs['format'], 'h264')
        self.assertLessEqual(int(opened.call_args.kwargs['options']['probesize']), 32768)
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
            def demux(self, video):
                if self.fails: raise OSError('Circular buffer overrun')
                class Packet:
                    def decode(self): return [Frame()]
                yield Packet()
                yield Packet()
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

    def test_stop_during_open_closes_on_worker_without_starting_demux(self):
        decoder = driver._VideoDecoder('udp://127.0.0.1:19999')
        entered = threading.Event()
        closed = []
        class Container:
            def demux(self, **kwargs):
                raise AssertionError('must not start another native read after stop')
            def close(self):
                closed.append(threading.get_ident())
        def opened(*args, **kwargs):
            entered.set()
            if not decoder._halt.wait(2):
                raise AssertionError('stop was not requested')
            return Container()
        with patch('av.open', side_effect=opened):
            decoder.start()
            try:
                self.assertTrue(entered.wait(2))
            finally:
                decoder.stop()
        self.assertFalse(decoder.is_alive())
        self.assertEqual(closed, [decoder.ident])
        self.assertIsNone(decoder.frame)

    def test_stop_timeout_is_reported_instead_of_silent_success(self):
        decoder = driver._VideoDecoder('udp://127.0.0.1:19999')
        with patch.object(decoder, 'is_alive', return_value=True), patch.object(decoder, 'join'):
            with self.assertRaisesRegex(TimeoutError, 'video decoder did not stop'):
                decoder.stop(timeout=.01)

    def test_streamoff_success_does_not_hide_decoder_stop_failure(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        reader = SimpleNamespace(stop=Mock(side_effect=[TimeoutError('still opening'), None]))
        tello = SimpleNamespace(streamoff=Mock())
        with patch.object(driver, '_video', reader), patch.object(driver, '_tello', tello), \
             patch.object(driver, '_call_real', return_value=(True, None)), \
             patch.object(driver, '_record'), patch.object(driver.time, 'sleep'):
            self.assertFalse(driver.stop_video())
            self.assertIs(driver._video, reader)
            self.assertTrue(driver.stop_video())
            self.assertIsNone(driver._video)

    def test_real_silent_udp_open_stops_and_releases_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserved:
            reserved.bind(('127.0.0.1', 0))
            port = reserved.getsockname()[1]
        decoder = driver._VideoDecoder(f'udp://127.0.0.1:{port}')
        entered = threading.Event()
        import av
        original = av.open
        def opened(*args, **kwargs):
            entered.set()
            return original(*args, **kwargs)
        with patch('av.open', side_effect=opened):
            decoder.start()
            try:
                self.assertTrue(entered.wait(2))
                time.sleep(.1)  # enter the actual native open/probe with no sender
            finally:
                decoder.stop()
        self.assertFalse(decoder.is_alive())
        self.assertIsNone(decoder.frame)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as released:
            released.bind(('127.0.0.1', port))

    def test_invalid_h264_packet_does_not_reopen_before_next_valid_packet(self):
        import av
        decoder = driver._VideoDecoder('udp://127.0.0.1:19999')
        class Packet:
            def __init__(self, bad): self.bad = bad
            def decode(self):
                if self.bad: raise av.error.InvalidDataError(1094995529, 'missing references')
                return ['fresh']
        class Container:
            def demux(self, video): return iter([Packet(True),Packet(False)])
        decoder._frame = np.zeros((16,16,3),np.uint8)
        frames = decoder._decode_packets(Container())
        self.assertEqual(next(frames), 'fresh')
        self.assertIsNone(decoder.frame)
        self.assertEqual(decoder.reconnects, 0)
        self.assertTrue(any('invalid_packet' in error for error in decoder.errors))

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
            # Keep the previous 5.39MB policy explicit as a historical negative
            # control. Production now intentionally rejects this queued stream.
            baseline = '?overrun_nonfatal=0&fifo_size=28672'
            container = av.open(f'udp://127.0.0.1:{port}' + baseline,
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

    def test05_real_production_decoder_discards_post_stall_queue_before_publication(self):
        from VideoProbe import PROFILES, encode, run_decoder
        result=run_decoder(PROFILES['small'],encode(PROFILES['small']),stall=True)
        self.assertTrue(result['decoder_finished'])
        self.assertTrue(result['sender_finished'])
        self.assertFalse(result['sender_errors'])
        self.assertTrue(result['summary']['gap_rejected'],result)
        self.assertGreaterEqual(result['summary']['reconnects'],1)
        self.assertGreaterEqual(result['summary']['count'],10)
        self.assertEqual(result['summary']['unknown_source_count'],0,result)
        self.assertEqual(result['summary']['published_stale_count'],0,result)
        self.assertIsNone(result['stats']['age'])  # stopped/failed readers expose no cached frame

    def test06_real_720p_keyframe_bursts_still_decode_under_production_policy(self):
        from VideoProbe import PROFILES, encode, run_decoder
        result=run_decoder(PROFILES['large'],encode(PROFILES['large']),stall=False)
        self.assertTrue(result['decoder_finished'])
        self.assertTrue(result['sender_finished'])
        self.assertFalse(result['sender_errors'])
        self.assertGreaterEqual(result['summary']['count'],60,result)
        self.assertEqual(result['summary']['reconnects'],0,result)
        self.assertEqual(result['summary']['unknown_source_count'],0,result)
        self.assertEqual(result['summary']['published_stale_count'],0,result)

    def test07_continuous_local_processing_overload_never_publishes_the_queued_old_frames(self):
        from VideoProbe import PROFILES, encode, run_decoder
        result=run_decoder(PROFILES['small'],encode(PROFILES['small']),stall=False,processing_delay=.1)
        self.assertTrue(result['decoder_finished'],result)
        self.assertTrue(result['sender_finished'])
        self.assertTrue(result['summary']['processing_rejected'],result)
        self.assertEqual(result['summary']['count'],0,result)
        self.assertEqual(result['summary']['published_stale_count'],0,result)
        self.assertIsNone(result['stats']['shape'])


if __name__ == '__main__':
    unittest.main()
