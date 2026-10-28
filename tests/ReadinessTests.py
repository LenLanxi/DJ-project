"""Local evidence settlement checks; no cloud, Docker or physical devices."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ReadinessScenario import Audit, BoundedArk


class ReadinessSettlement(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_waiter_retains_worker_usage_and_raw_before_settlement(self):
        started=threading.Event();release=threading.Event()

        class FakeClient:
            def __init__(self,on_usage,**kwargs):
                self.on_usage=on_usage;self.model=kwargs['model']
            def complete_structured(self,messages,schema):
                self.on_usage({'event':'model_request_start','request_id':'WorkerRequest'})
                started.set()
                if not release.wait(1):raise RuntimeError('test_worker_not_released')
                self.on_usage({'event':'model_request_end','request_id':'WorkerRequest',
                               'usage':{'total_tokens':7},'status':'completed'})
                return '{invalid JSON retained'

        audit=Audit()
        with tempfile.TemporaryDirectory() as folder:
            credentials=Path(folder)/'Credentials.json'
            credentials.write_text(json.dumps({'Ark':{'APIKey':'fake-test-secret',
                                                     'ModelName':'fake-test-model'}}),encoding='utf-8')
            with patch('model_client.ModelClient',FakeClient):cloud=BoundedArk(credentials,audit)
        task=asyncio.create_task(asyncio.to_thread(cloud.complete_structured,[],{'type':'object'}))
        try:
            async with asyncio.timeout(1):
                while not started.is_set():await asyncio.sleep(.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(cloud.active,1)
            pending=asyncio.create_task(cloud.wait_settled(timeout=.5))
            await asyncio.sleep(.01)
            self.assertFalse(pending.done())
            release.set()
            result=await pending
            self.assertEqual(result['status'],'settled')
            self.assertEqual(result['completed_calls'],1)
            self.assertEqual(result['active_remaining'],0)
            kinds=[event['event'] for event in audit.events]
            self.assertLess(kinds.index('model_request_end'),kinds.index('readiness_Ark_settlement'))
            self.assertLess(kinds.index('readiness_Ark_raw'),kinds.index('readiness_Ark_settlement'))
            raw=next(event for event in audit.events if event['event']=='readiness_Ark_raw')
            self.assertEqual(raw['request_id'],'WorkerRequest')
            self.assertEqual(raw['raw_response'],'{invalid JSON retained')
            self.assertEqual(raw['validation_error_type'],'JSONDecodeError')
            with self.assertRaisesRegex(RuntimeError,'readiness_Ark_cleanup_started'):
                cloud.complete_structured([],{'type':'object'})
            self.assertNotIn('fake-test-secret',json.dumps(audit.events))
        finally:
            release.set()


if __name__=='__main__':unittest.main()
