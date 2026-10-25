"""Signal and backlog tests; no real Bluetooth hardware or cloud claims."""
import asyncio
import math
import queue
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
try:
    import numpy as np
    import soxr
except ImportError:
    raise unittest.SkipTest('requires optional audio dependencies numpy and soxr')
from audio_streaming import CaptureQueue, PCMResampler, OutputAdapter
from duplex_audio import PlaybackBuffer
from realtime import RealtimeSession

class QueueTests(unittest.TestCase):
    def test_variable_callbacks_keep_only_latest_200ms(self):
        q=CaptureQueue(16000)
        q.put(b'\x01\0'*1600,now=0) # 100ms
        q.put(b'\x02\0'*3200,now=.01) # 200ms, not "one 20ms block"
        self.assertEqual(q.size,6400)
        pcm,generation=q.get_nowait(now=.02)
        self.assertEqual(pcm,b'\x02\0'*3200)
        self.assertEqual(q.dropped_samples,1600)
        self.assertGreater(generation,0)
    def test_one_oversize_burst_is_trimmed_in_samples(self):
        q=CaptureQueue(8000)
        q.put(b'\x01\0'*8000,now=0)
        self.assertEqual(q.size,3200)
        self.assertEqual(q.dropped_samples,6400)
    def test_old_queue_expires_even_without_further_callbacks(self):
        q=CaptureQueue(48000)
        q.put(b'\0\0'*480,now=0)
        with self.assertRaises(queue.Empty): q.get_nowait(now=.3)
        self.assertEqual(q.dropped_samples,480)
    def test_overflow_counts_discontinuity_without_crashing_queue(self):
        q=CaptureQueue(24000)
        q.put(b'\0\0'*480,now=0,overflow=True)
        self.assertEqual(q.diagnostics()['input_overflows'],1)
        self.assertEqual(len(q.get_nowait(now=0)[0]),960)

class ConversionTests(unittest.TestCase):
    def test_continuous_phase_and_duration_across_arbitrary_chunk_boundaries(self):
        for source,target in ((8000,24000),(16000,24000),(44100,24000),(48000,24000),(24000,48000),(24000,16000)):
            with self.subTest(source=source,target=target):
                wave=(np.sin(np.arange(source)*2*np.pi*440/source)*10000).astype('<i2')
                c=PCMResampler(source,target)
                output=b''.join(c.process(wave[i:i+137].tobytes()) for i in range(0,len(wave),137))
                output+=c.process(b'',last=True)
                self.assertEqual(len(output)//2,target)
                reference=PCMResampler(source,target).process(wave.tobytes(),last=True)
                error=np.abs(np.frombuffer(output,dtype='<i2').astype(float)-np.frombuffer(reference,dtype='<i2'))
                self.assertLessEqual(error.max(),1)
    def test_downsample_filters_aliasing(self):
        source=48000; target=16000
        wave=(np.sin(np.arange(source)*2*np.pi*12000/source)*10000).astype('<i2')
        pcm=PCMResampler(source,target).process(wave.tobytes(),last=True)
        result=np.frombuffer(pcm,dtype='<i2').astype(float)[1000:-1000]
        self.assertLess(math.sqrt(np.mean(result*result)),100)
    def test_render_length_and_interrupt_drop_resampled_tail(self):
        b=PlaybackBuffer(); b.append('old',0,(np.ones(24000)*1000).astype('<i2').tobytes())
        out=OutputAdapter(b,48000)
        self.assertEqual(len(out.render(960,20)),1920)
        b.interrupt()
        # The converter's previous buffered audio must not leak after an interrupt.
        next_audio=np.frombuffer(out.render(960,20),dtype='<i2')
        self.assertTrue(np.all(next_audio==0))
        self.assertGreaterEqual(b.latency_ms,20)

class DiscontinuityTests(unittest.IsolatedAsyncioTestCase):
    async def test_loss_stops_before_blocked_network_and_disables_motion(self):
        runner=SimpleNamespace(running=True,cancel=MagicMock(),
             gateway=SimpleNamespace(interrupt=AsyncMock(return_value={'ok':True})),run=AsyncMock())
        audio=SimpleNamespace(loss_generation=1,speaker=SimpleNamespace(interrupt=MagicMock()))
        s=RealtimeSession(runner,audio)
        s.send=AsyncMock()
        s.note_input_loss()
        await asyncio.gather(*list(s.tool_tasks))
        runner.cancel.assert_called_once()
        runner.gateway.interrupt.assert_awaited_once_with(False)
        await s.call_tool({'name':'run_program','call_id':'c','arguments':'{"code":"drone.takeoff()"}'},s.epoch)
        runner.run.assert_not_awaited()
        self.assertTrue(s.input_degraded)
        self.assertIn('restart_required',s.send.call_args_list[0].args[0]['item']['output'])


class DeviceRateTests(unittest.TestCase):
    def device_api(self):
        sd=MagicMock()
        devices=[dict(name='Headset Microphone',hostapi=0,max_input_channels=1,max_output_channels=0,default_samplerate=16000.),
                 dict(name='Headphones',hostapi=0,max_input_channels=0,max_output_channels=2,default_samplerate=48000.)]
        sd.query_devices.side_effect=lambda index:devices[index]
        sd.query_hostapis.return_value={'name':'Windows WASAPI'}
        return sd
    def test_native_rates_are_independent_and_explicit_override_checked(self):
        from audio_config import AudioConfig
        from audio_devices import resolve_endpoints
        sd=self.device_api()
        first,second=resolve_endpoints(sd,AudioConfig(input_device=0,output_device=1))
        self.assertEqual((first.rate,second.rate),(16000,48000))
        self.assertEqual(sd.check_input_settings.call_args.kwargs['samplerate'],16000)
        self.assertEqual(sd.check_output_settings.call_args.kwargs['samplerate'],48000)
        first,_=resolve_endpoints(sd,AudioConfig(input_device=0,output_device=1,input_samplerate=8000))
        self.assertEqual(first.rate,8000)
        _,second=resolve_endpoints(sd,AudioConfig(input_device=0,output_device=1,output_channels=1))
        self.assertEqual(second.channels,1)
    def test_callbacks_allow_burst_loss_and_open_native_rates(self):
        from duplex_audio import DuplexAudio
        from unittest.mock import patch
        from audio_config import AudioConfig
        sd=self.device_api(); a=DuplexAudio(config=AudioConfig(input_device=0,output_device=1))
        try:
            with patch.dict('sys.modules',{'sounddevice':sd}): a.start()
            self.assertEqual(sd.RawInputStream.call_args.kwargs['blocksize'],0)
            self.assertEqual(sd.RawOutputStream.call_args.kwargs['samplerate'],48000)
            callback=sd.RawInputStream.call_args.kwargs['callback']
            callback(b'\x01\0'*8000,8000,None,SimpleNamespace(input_overflow=False))
            self.assertIsNone(a.fault)
            self.assertEqual(a.input.size,6400)
            self.assertEqual(a.input.dropped_samples,4800)
        finally: a.close()

if __name__=='__main__': unittest.main()
