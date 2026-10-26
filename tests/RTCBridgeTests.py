"""RTC control bridge behavioral tests over real loopback WebSockets.

No cloud, browser, sound device, aircraft or PC webcam is opened. Frame fixtures
are generated arrays; fake runtimes record admission and cancellation outcomes.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT/'modules'/'ai', ROOT/'tests'):
    sys.path.insert(0, str(path))
import numpy as np
from RTCBridge import RTCBridge
from SafetyTests import NoNetwork


class Camera:
    simulated = True
    def __init__(self):
        self.stale = False
        self.index = 0
    def get_frame(self):
        self.index += 1
        return {'frame':np.zeros((16,16,3),dtype=np.uint8),'frame_id':self.index,
                'timestamp':time.time()-2 if self.stale else time.time(),'stale':self.stale}


class Runtime:
    def __init__(self):
        self.generation = 0
        self.running = False
        self.calls = []
        self.release = asyncio.Event()
        self.release.set()
        self.interrupt_error = False
    def cancel(self):
        self.generation += 1
        self.calls.append(('cancel',))
        self.release.set()
    async def interrupt(self, land=False):
        self.calls.append(('interrupt', land))
        if self.interrupt_error:
            raise OSError('fake failed priority send')
        self.cancel()
        return {'ok':True,'status':'accepted'}
    async def run(self, code, expected_generation=None):
        if expected_generation != self.generation:
            return {'ok':False,'status':'cancelled','reason':'stale_plan'}
        self.running = True
        self.calls.append(('run', code))
        try:
            await self.release.wait()
            return {'ok':True,'status':'completed','simulated':True,
                    'journal':[{'method':'get_status','args':[],'result':{'flight_state':'landed'}}],
                    'debug':'actual result fixture'}
        finally: self.running = False


def identity(number='1'):
    return {'sessionId':'session'+number,'roomId':'room'+number,'taskId':'task'+number,
            'botId':'bot'+number,'userId':'user'+number}


def turn(bridge, name='turn1', **changes):
    value = dict(type='user_turn', id=name, text='take off', completed=True, **bridge.session)
    value.update(changes)
    return value


def tool(bridge, call_id='call1', code='drone.get_status()', **changes):
    value = dict(type='tool', epoch=bridge.epoch, calls=[{'id':call_id,
                 'function':{'name':'run_program','arguments':json.dumps({'code':code})}}], **bridge.session)
    value.update(changes)
    return value


class RTCBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        NoNetwork.setUp(self)
        self.runtime = Runtime()
        self.camera = Camera()
        self.events, self.out = [], []
        self.bridge = RTCBridge(self.runtime, self.camera, self.events.append)
        async def send(value): self.out.append(value)
        self.bridge.send = send
        self.bridge.bind(identity())

    async def settle(self):
        pending = list(self.bridge.tasks)
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), 1)
        await asyncio.sleep(0)

    async def test01_partial_subtitle_and_bot_text_cannot_authorize_tools(self):
        for changes in ({'completed':False},{'userId':'bot1'},{'id':True}):
            with self.assertRaises((ValueError,RuntimeError)):
                await self.bridge.message(turn(self.bridge, **changes))
        self.assertIsNone(self.bridge.turn_id)
        with self.assertRaises(RuntimeError): await self.bridge.message(tool(self.bridge))
        self.assertFalse(any(call[0]=='run' for call in self.runtime.calls))

    async def test02_bound_identity_fields_cannot_be_changed(self):
        await self.bridge.message(turn(self.bridge))
        for changes in ({'sessionId':'old'},{'botId':'other'},{'roomId':'other'},{'taskId':'other'}):
            with self.assertRaises((ValueError,RuntimeError)):
                await self.bridge.message(tool(self.bridge, **changes))
        self.assertEqual(self.runtime.calls, [])

    async def test03_exact_rebind_does_not_reset_dedup_or_epoch(self):
        await self.bridge.message(turn(self.bridge))
        message = tool(self.bridge)
        await self.bridge.message(message)
        await self.settle()
        epoch = self.bridge.epoch
        self.bridge.bind(identity())
        self.assertEqual(self.bridge.epoch, epoch)
        await self.bridge.message(message)
        await self.settle()
        self.assertEqual(len([call for call in self.runtime.calls if call[0]=='run']),1)

    async def test04_same_call_id_with_changed_payload_is_rejected(self):
        await self.bridge.message(turn(self.bridge))
        await self.bridge.message(tool(self.bridge))
        await self.settle()
        with self.assertRaisesRegex(RuntimeError,'different_payload'):
            await self.bridge.message(tool(self.bridge, code='drone.takeoff()'))
        self.assertEqual(len([call for call in self.runtime.calls if call[0]=='run']),1)

    async def test05_one_complete_program_per_user_turn(self):
        await self.bridge.message(turn(self.bridge))
        await self.bridge.message(tool(self.bridge))
        await self.settle()
        await self.bridge.message(tool(self.bridge, call_id='call2'))
        await self.settle()
        self.assertEqual(len([call for call in self.runtime.calls if call[0]=='run']),1)
        self.assertEqual(self.out[-1]['result']['reason'],'turn_program_budget_exceeded')

    async def test06_parallel_program_is_busy_not_admitted(self):
        self.runtime.release.clear()
        await self.bridge.message(turn(self.bridge))
        await self.bridge.message(tool(self.bridge))
        await self.bridge.message(tool(self.bridge, call_id='call2'))
        await asyncio.sleep(.01)
        self.runtime.release.set()
        await self.settle()
        self.assertEqual(len([call for call in self.runtime.calls if call[0]=='run']),1)
        self.assertTrue(any(item.get('result',{}).get('status')=='busy' for item in self.out))

    async def test07_old_completed_turn_cannot_be_replayed_after_new_turn(self):
        await self.bridge.message(turn(self.bridge, 'turn1'))
        await self.bridge.message(turn(self.bridge, 'turn2'))
        epoch = self.bridge.epoch
        await self.bridge.message(turn(self.bridge, 'turn1'))
        self.assertEqual(self.bridge.epoch, epoch)
        self.assertEqual(self.bridge.turn_id, 'turn2')

    async def test08_interrupt_revokes_old_identity_and_requires_new_bind(self):
        await self.bridge.message(turn(self.bridge))
        old = tool(self.bridge)
        await self.bridge.revoke('test')
        with self.assertRaises(RuntimeError): await self.bridge.message(old)
        with self.assertRaises(RuntimeError): self.bridge.bind(identity())
        self.bridge.bind(identity('2'))
        await self.bridge.message(turn(self.bridge, 'turn2'))
        await self.bridge.message(tool(self.bridge, call_id='call2'))
        await self.settle()
        self.assertEqual(len([call for call in self.runtime.calls if call[0]=='run']),1)

    async def test09_hardware_interrupt_generation_rejects_late_cloud_tool(self):
        await self.bridge.message(turn(self.bridge))
        old = tool(self.bridge)
        self.runtime.cancel()
        with self.assertRaisesRegex(RuntimeError,'runtime_authority_changed'):
            await self.bridge.message(old)
        self.assertFalse(any(call[0]=='run' for call in self.runtime.calls))
        self.assertIn('session1', self.bridge.revoked)

    async def test10_incomplete_frame_revokes_and_blocks_rebind_tools(self):
        self.camera.stale = True
        self.bridge.last_heartbeat = time.monotonic()
        task = asyncio.create_task(self.bridge.frames())
        try:
            async with asyncio.timeout(1):
                while not self.bridge.frame_fault:
                    await asyncio.sleep(.005)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(self.bridge.frame_fault)
        self.assertIn('session1', self.bridge.revoked)
        self.assertTrue(any(call[0]=='interrupt' for call in self.runtime.calls))
        self.bridge.bind(identity('2'))
        with self.assertRaisesRegex(RuntimeError,'camera_unavailable'):
            await self.bridge.message(turn(self.bridge))

    async def test11_heartbeat_expiration_is_detected(self):
        self.bridge.last_heartbeat = time.monotonic()-10
        with self.assertRaisesRegex(TimeoutError,'heartbeat_expired'):
            await self.bridge.frames()

    async def test12_revoke_exception_still_revokes_and_reports_failure(self):
        self.runtime.interrupt_error = True
        result = await self.bridge.revoke('test')
        self.assertFalse(result['ok'])
        self.assertIn('session1', self.bridge.revoked)
        self.assertIsNone(self.bridge.turn_id)
        self.assertEqual(self.out[-1]['type'],'session_reset_required')

    async def test13_nested_images_secrets_and_nonfinite_values_never_leave_result(self):
        secret = 'fake-secret-that-must-not-appear'
        value = {'ok':True,'status':'completed','debug':'answer '+secret+' data:image/jpeg;base64,AAAA',
                 'journal':[{'method':'get_frame','args':[], 'result':{'nested':{'image_url':'data:image/jpeg;base64,AAAA','api_key':secret},'number':float('nan')}}]}
        with patch.dict(os.environ, {'TEST_API_KEY':secret}):
            result = RTCBridge.compact(value)
        encoded = json.dumps(result,allow_nan=False)
        self.assertNotIn(secret, encoded)
        self.assertNotIn('data:image', encoded)
        self.assertNotIn('AAAA', encoded)
        self.assertIn('[payload omitted]', encoded)

    async def test14_program_result_is_actual_runtime_result_not_canned_success(self):
        async def failed(code, expected_generation=None):
            self.runtime.calls.append(('run',code))
            return {'ok':False,'status':'failed','reason':'real execution refused'}
        self.runtime.run = failed
        await self.bridge.message(turn(self.bridge))
        await self.bridge.message(tool(self.bridge))
        await self.settle()
        result = self.out[-1]['result']
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'],'real execution refused')

    async def test15_speech_while_program_running_revokes_old_task(self):
        self.runtime.release.clear()
        await self.bridge.message(turn(self.bridge))
        await self.bridge.message(tool(self.bridge))
        await asyncio.sleep(.01)
        await self.bridge.message(turn(self.bridge,'turn2'))
        await self.settle()
        self.assertIn('session1',self.bridge.revoked)
        self.assertTrue(any(call[0]=='interrupt' for call in self.runtime.calls))

    async def test22_rtc_program_uses_utf8_budget_not_character_count(self):
        await self.bridge.message(turn(self.bridge))
        await self.bridge.message(tool(self.bridge, code='#' + '字'*201))
        await self.settle()
        self.assertFalse(any(call[0]=='run' for call in self.runtime.calls))
        self.assertFalse(self.out[-1]['result']['ok'])
        self.assertIn('invalid_program_arguments',self.out[-1]['result']['reason'])

    async def test23_repeated_bind_does_not_extend_session_deadline(self):
        deadline = self.bridge.session_deadline
        self.bridge.bind(identity())
        self.assertEqual(self.bridge.session_deadline,deadline)

    async def test24_expired_session_cannot_admit_a_late_tool(self):
        await self.bridge.message(turn(self.bridge))
        message = tool(self.bridge)
        self.bridge.session_deadline = time.monotonic()-1
        with self.assertRaisesRegex(RuntimeError,'session_deadline_exceeded'):
            await self.bridge.message(message)
        self.assertIn('session1',self.bridge.revoked)
        self.assertFalse(any(call[0]=='run' for call in self.runtime.calls))

    async def test26_tool_transport_exception_is_consumed_and_revokes(self):
        await self.bridge.message(turn(self.bridge))
        def failed_log(_event): raise OSError('fake failed log sink')
        self.bridge.log = failed_log
        await self.bridge.message(tool(self.bridge))
        async with asyncio.timeout(1):
            while self.bridge.tasks:
                await asyncio.sleep(.005)
        self.assertTrue(any(call[0]=='interrupt' for call in self.runtime.calls))
        self.assertIn('session1',self.bridge.revoked)


class RTCLoopbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self): NoNetwork.setUp(self)

    async def asyncSetUp(self):
        self.runtime, self.camera, self.logs = Runtime(), Camera(), []
        self.bridge = RTCBridge(self.runtime,self.camera,self.logs.append,heartbeat_seconds=2)
        self.url = await self.bridge.start()

    async def asyncTearDown(self): await self.bridge.close()

    async def receive_type(self, socket, kind):
        async with asyncio.timeout(2):
            while True:
                value = json.loads(await socket.recv())
                if value.get('type') == kind: return value

    async def wait_disconnected(self):
        async with asyncio.timeout(2):
            while self.bridge.client is not None:
                await asyncio.sleep(.01)

    async def test16_unauthorized_client_cannot_take_control(self):
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed
        async with connect(self.url.replace(self.bridge.token,'wrong')) as socket:
            with self.assertRaises(ConnectionClosed): await socket.recv()
            self.assertEqual(socket.close_code,1008)
        self.assertIsNone(self.bridge.client)
        self.assertEqual(self.runtime.calls, [])

    async def test17_only_one_client_and_disconnect_revokes(self):
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed
        async with connect(self.url) as first:
            await self.receive_type(first,'frame')
            await first.send(json.dumps(dict(type='bind',**identity())))
            await self.receive_type(first,'bound')
            async with connect(self.url) as second:
                with self.assertRaises(ConnectionClosed): await second.recv()
                self.assertEqual(second.close_code,1008)
        await self.wait_disconnected()
        self.assertIn('session1',self.bridge.revoked)
        self.assertIsNone(self.bridge.client)
        self.assertTrue(any(call[0]=='interrupt' for call in self.runtime.calls))

    async def test18_heartbeat_failure_stops_and_clears_client(self):
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed
        self.bridge.heartbeat_seconds = .15
        async with connect(self.url) as socket:
            await self.receive_type(socket,'frame')
            await socket.send(json.dumps(dict(type='bind',**identity())))
            await self.receive_type(socket,'bound')
            try:
                while True: await asyncio.wait_for(socket.recv(),1)
            except ConnectionClosed: pass
        self.assertIn('session1',self.bridge.revoked)
        self.assertIsNone(self.bridge.client)
        self.assertTrue(any(call[0]=='interrupt' for call in self.runtime.calls))

    async def test19_disconnect_still_cleans_when_interrupt_raises(self):
        from websockets.asyncio.client import connect
        self.runtime.interrupt_error = True
        async with connect(self.url) as socket:
            await self.receive_type(socket,'frame')
            await socket.send(json.dumps(dict(type='bind',**identity())))
            await self.receive_type(socket,'bound')
        await self.wait_disconnected()
        self.assertIsNone(self.bridge.client)
        self.assertIsNone(self.bridge.session)
        self.assertIn('session1',self.bridge.revoked)

    async def test20_loopback_completed_user_tool_returns_runtime_result(self):
        from websockets.asyncio.client import connect
        async with connect(self.url) as socket:
            await self.receive_type(socket,'frame')
            await socket.send(json.dumps(dict(type='bind',**identity())))
            await self.receive_type(socket,'bound')
            await socket.send(json.dumps(turn(self.bridge)))
            acknowledged = await self.receive_type(socket,'turn')
            await socket.send(json.dumps(tool(self.bridge)))
            reply = await self.receive_type(socket,'tool_result')
            self.assertEqual(reply['epoch'],acknowledged['epoch'])
            self.assertEqual(reply['result']['debug'],'actual result fixture')
            self.assertEqual(len([call for call in self.runtime.calls if call[0]=='run']),1)
            self.assertNotIn('jpeg',json.dumps(reply))

    async def test21_loopback_frame_expiry_revokes_authority(self):
        from websockets.asyncio.client import connect
        async with connect(self.url) as socket:
            await self.receive_type(socket,'frame')
            await socket.send(json.dumps(dict(type='bind',**identity())))
            await self.receive_type(socket,'bound')
            self.camera.stale = True
            reset = await self.receive_type(socket,'session_reset_required')
            self.assertEqual(reset['reason'],'sdk_frame_expired')
            self.assertIn('session1',self.bridge.revoked)
            self.assertTrue(any(call[0]=='interrupt' for call in self.runtime.calls))

    async def test25_host_session_deadline_applies_despite_client_heartbeats(self):
        from websockets.asyncio.client import connect
        self.bridge.max_session_seconds = .2
        async with connect(self.url) as socket:
            await self.receive_type(socket,'frame')
            await socket.send(json.dumps(dict(type='bind',**identity())))
            await self.receive_type(socket,'bound')
            async def heartbeats():
                while True:
                    await socket.send(json.dumps({'type':'heartbeat'}))
                    await asyncio.sleep(.02)
            heartbeat = asyncio.create_task(heartbeats())
            try:
                reset = await self.receive_type(socket,'session_reset_required')
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat,return_exceptions=True)
            self.assertEqual(reset['reason'],'rtc_session_deadline_exceeded')
        await self.wait_disconnected()
        self.assertIn('session1',self.bridge.revoked)
        self.assertTrue(any(call[0]=='interrupt' for call in self.runtime.calls))


if __name__ == '__main__':
    unittest.main(verbosity=2)
