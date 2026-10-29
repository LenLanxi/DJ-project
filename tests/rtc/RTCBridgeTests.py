"""Host-owned RTC event admission and direct cloud result delivery; no cloud/devices."""
import asyncio
import json
import unittest
from RTCFixture import Camera, Runtime
from providers.RTCBridge import RTCBridge

IDENTITY = dict(sessionId='s',roomId='r',taskId='t',botId='b',userId='u')

def subtitle(round_id=1, text='query', **changes):
    item=dict(userId='u',roundId=round_id,text=text,paragraph=True,definite=True)
    item.update(changes)
    return dict(type='subv',value=dict(data=[item]))

def tool(code='print(drone.get_status())', call_id='c'):
    return dict(type='tool',value=dict(tool_calls=[dict(id=call_id,
        function=dict(name='run_program',arguments=json.dumps(dict(code=code))))]))

class RTCBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime=Runtime(); self.outputs=[]; self.display=[]
        async def sink(identity, call_id, result): self.outputs.append((identity,call_id,result))
        self.bridge=RTCBridge(self.runtime,Camera(),lambda _:None,result_sink=sink)
        async def send(message): self.display.append(message)
        self.bridge.send=send
        self.bridge.bind(IDENTITY)

    async def event(self,event):
        await self.bridge.message(dict(IDENTITY,type='cloud_event',event=event))

    async def settle(self):
        while self.bridge.tasks:
            await asyncio.gather(*list(self.bridge.tasks), return_exceptions=True)

    async def test_completed_subtitle_authorizes_direct_result_and_replay_once(self):
        await self.event(subtitle())
        await self.event(tool()); await self.settle()
        await self.event(tool()); await self.settle()
        self.assertEqual(len([x for x in self.runtime.calls if x[0]=='run']),1)
        self.assertEqual(len(self.outputs),2)
        self.assertEqual(self.outputs[0],self.outputs[1])
        self.assertTrue(self.outputs[0][2]['ok'])
        self.assertFalse(any(x['type']=='turn' for x in self.display))
        with self.assertRaisesRegex(RuntimeError,'different_payload'):
            await self.event(tool('drone.takeoff()'))

    async def test_partial_invalid_and_empty_new_round_never_authorize(self):
        for event in (subtitle(definite=False,paragraph=False),subtitle(round_id=True),
                      subtitle(round_id=2,text='')):
            await self.event(event)
            with self.assertRaises(RuntimeError): await self.event(tool())
        self.assertEqual(self.runtime.calls,[])

    async def test_empty_final_uses_only_same_round_text(self):
        await self.event(subtitle(text='query',definite=False,paragraph=False))
        await self.event(subtitle(text=''))
        await self.event(tool()); await self.settle()
        self.assertEqual(len(self.outputs),1)

    async def test_new_speech_revokes_planning_and_old_tools(self):
        await self.event(subtitle())
        await self.event(subtitle(2,'new request',definite=False,paragraph=False))
        with self.assertRaises(RuntimeError): await self.event(tool())
        self.assertTrue(any(x[0]=='cancel' for x in self.runtime.calls))

    async def test_cancel_in_flight_never_publishes_old_completion(self):
        self.runtime.release.clear()
        await self.event(subtitle()); await self.event(tool())
        await asyncio.sleep(0)
        await self.bridge.message(dict(type='interrupt',land=False,source='operator'))
        await self.settle()
        self.assertEqual(self.outputs,[])

    async def test_identity_and_runtime_epoch_reject_old_events(self):
        with self.assertRaises(RuntimeError):
            await self.bridge.message(dict(IDENTITY,type='cloud_event',event=subtitle(),sessionId='old'))
        await self.event(subtitle())
        self.runtime.cancel()
        with self.assertRaises(RuntimeError): await self.event(tool())
        self.assertFalse(any(x[0]=='run' for x in self.runtime.calls))

    async def test_result_transport_failure_revokes_authority(self):
        async def fail(*_): raise OSError('offline')
        self.bridge.result_sink=fail
        await self.event(subtitle()); await self.event(tool()); await self.settle()
        self.assertIn('s',self.bridge.revoked)

    async def test_busy_program_and_decoded_budget_are_checked_by_host(self):
        self.runtime.release.clear()
        await self.event(subtitle()); await self.event(tool())
        await self.event(tool(call_id='other'))
        await asyncio.sleep(0)
        self.runtime.release.set(); await self.settle()
        self.assertEqual(len([x for x in self.runtime.calls if x[0]=='run']),1)
        self.assertTrue(any(x[2].get('status')=='busy' for x in self.outputs))
        await self.event(tool('x'*60001,'large')); await self.settle()
        self.assertFalse(self.outputs[-1][2]['ok'])

    async def test_display_delivery_failure_does_not_revoke_delivered_cloud_result(self):
        async def fail(_): raise OSError('display unavailable')
        self.bridge.send = fail
        await self.event(subtitle()); await self.event(tool()); await self.settle()
        self.assertEqual(len(self.outputs), 1)
        self.assertNotIn('s', self.bridge.revoked)
