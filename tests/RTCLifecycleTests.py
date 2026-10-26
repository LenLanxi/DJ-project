"""RTC supervisor lifecycle, using loopback only and no cloud/aircraft/audio."""
import asyncio
from pathlib import Path
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'modules'/'ai'))
import main
from RTCRuntime import RTCProcess, REQUIRED, validate_environment


class RTCLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_rtc_credentials_are_detected_before_any_hardware_connection(self):
        args=main.make_parser().parse_args(['--real-drone'])
        with patch.dict(os.environ,{name:'' for name in REQUIRED}), \
             patch.object(main,'initialize_real',new=AsyncMock()) as initialize:
            with self.assertRaisesRegex(ValueError,'RTC正式语音缺少配置'):
                await main.run(args,lambda event:None)
            initialize.assert_not_awaited()

    async def test_direct_speech_credentials_do_not_silently_replace_rtc(self):
        with patch.dict(os.environ,{**{name:'' for name in REQUIRED},'VOLC_API_KEY':'speechOnlySecret'}):
            with self.assertRaisesRegex(ValueError,'没有旧语音或模拟降级'):
                validate_environment(180)

    async def test_hidden_local_service_starts_and_shuts_down_without_opening_a_cloud_session(self):
        values={name:'testCredentialValue' for name in REQUIRED}
        values['RTC_APP_ID']='testApp'
        values['RTC_VLM_MODEL']='testModel'
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,values):
            log=main.SessionLog(Path(tmp)/'Session.jsonl')
            process=RTCProcess(log)
            bridge=types.SimpleNamespace(url='ws://127.0.0.1:9/?token=testNonceValue',token='testNonceValue')
            try:
                url=await process.start(bridge,180,main.RTC_INSTRUCTIONS,main.RTC_TOOLS)
                self.assertTrue(url.startswith('http://127.0.0.1:'))
                status=await asyncio.to_thread(process.request,'/api/status')
                self.assertTrue(status['configured'])
                self.assertIsNone(status['sessionId'])
                await process.close()
                self.assertEqual(process.proc.returncode,0)
                self.assertFalse(process.cleanup_pending)
                text=process.output.read_text(encoding='utf-8')
                self.assertNotIn('testCredentialValue',text)
                self.assertNotIn('testNonceValue',text)
            finally:
                if process.proc and process.proc.returncode is None:
                    # No session was created in this test; terminating a failed
                    # local-only fixture cannot strand a cloud task.
                    process.proc.terminate()
                    await process.proc.wait()
                log.close()

    async def test_unconfirmed_cloud_stop_keeps_the_cleanup_process_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=main.SessionLog(Path(tmp)/'Session.jsonl')
            process=RTCProcess(log)
            process.url='http://127.0.0.1:1234'
            process.proc=types.SimpleNamespace(returncode=None,pid=12345,terminate=Mock(),wait=AsyncMock(return_value=0))
            async def timeout(awaitable,seconds):
                await awaitable
                raise asyncio.TimeoutError()
            try:
                with patch.object(process,'request',return_value={'ok':False,'status':'cleanup_pending'}), \
                     patch('RTCRuntime.asyncio.wait_for',side_effect=timeout):
                    await process.close()
                self.assertTrue(process.cleanup_pending)
                process.proc.terminate.assert_not_called()
            finally:log.close()
