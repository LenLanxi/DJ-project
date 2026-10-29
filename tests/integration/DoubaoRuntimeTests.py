"""RTC admission and cancellation contracts, without cloud or aircraft."""
import asyncio
import json
import types
import unittest
from unittest.mock import AsyncMock, patch
from providers.RTCBridge import RTCBridge
from app.Tools import validate_action, validate_tool, OMNI_TOOLS
from app.Results import tool_result


IDENTITY = dict(sessionId='session1',roomId='room1',taskId='task1',botId='bot1',userId='user1')


class Runtime:
    def __init__(self):
        self.generation = 0
        self.running = False
        self.executed = []
        self.stops = []
        self.gate = None
    def cancel(self): self.generation += 1
    def begin_interrupt(self,land=False,expected_generation=None):
        if expected_generation is not None and expected_generation != self.generation: return None
        self.cancel()
        if self.gate is not None: self.gate.set()
        return types.SimpleNamespace(generation=self.generation,land=land)
    async def finish_interrupt(self,receipt):
        self.stops.append(receipt.land)
        return {'ok':True,'status':'completed'}
    async def run(self,code,expected_generation=None):
        if expected_generation != self.generation: return {'ok':False,'status':'cancelled'}
        self.executed.append(code)
        self.running = True
        try:
            if self.gate is not None: await self.gate.wait()
            return {'ok':True,'status':'completed','journal':[]}
        finally: self.running = False


def call(cid='call1',name='execute_action',args=None):
    return {'id':cid,'function':{'name':name,'arguments':json.dumps(
        args if args is not None else {'action':'rotate','args':[360]})}}


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = Runtime()
        self.results = []
        async def result(identity,cid,value): self.results.append((cid,value))
        self.bridge = RTCBridge(self.runtime,None,lambda _:None,result_sink=result)
        self.bridge.bind(IDENTITY)
    async def settle(self):
        while self.bridge.tasks: await asyncio.gather(*tuple(self.bridge.tasks))
    async def tools(self,calls):
        await self.bridge.message(dict(IDENTITY,type='cloud_event',event={
            'type':'tool','value':{'tool_calls':calls}}))
    async def test_tool_executes_without_any_subtitle(self):
        await self.tools([call()])
        await self.settle()
        self.assertEqual(self.runtime.executed,[{'action':'rotate','args':[360]}])
        self.assertTrue(self.results[0][1]['ok'])
    async def test_duplicate_call_never_replays_motion(self):
        await self.tools([call()])
        await self.settle()
        await self.tools([call()])
        await self.settle()
        self.assertEqual(len(self.runtime.executed),1)
        self.assertEqual(len(self.results),1)

    async def test_action_result_waits_for_post_action_frame_before_cloud_delivery(self):
        self.runtime.run = AsyncMock(return_value={'ok':True, 'status':'completed',
            'journal':[{'method':'rotate', 'result':{'ok':True}}]})
        self.bridge.send = AsyncMock()
        await self.tools([call()])
        while self.bridge.pending_frame is None: await asyncio.sleep(0)
        cid, after, _ = self.bridge.pending_frame
        self.assertEqual(self.results, [])
        await self.bridge.message(dict(IDENTITY, type='frame_published', request_id=cid,
                                      frame_id=1, timestamp=after-1))
        self.assertEqual(self.results, [])
        with patch('providers.RTCBridge.time.time', return_value=after+.2):
            await self.bridge.message(dict(IDENTITY, type='frame_published', request_id=cid,
                                          frame_id=2, timestamp=after+.1))
        await self.settle()
        camera = self.results[0][1]['camera_observation']
        self.assertEqual(camera['frame_id'], 2)
        self.assertFalse(camera['cloud_verified'])

    async def test_frame_wait_timeout_keeps_session_and_action_result(self):
        self.bridge.send = AsyncMock()
        result = await self.bridge.post_action_frame('missing-frame')
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(self.bridge.revoked)
        self.assertIsNone(self.bridge.pending_frame)

    async def test_interrupt_during_frame_wait_supersedes_old_result(self):
        self.runtime.run = AsyncMock(return_value={'ok':True, 'status':'completed',
            'journal':[{'method':'rotate', 'result':{'ok':True}}]})
        self.bridge.send = AsyncMock()
        await self.tools([call()])
        while self.bridge.pending_frame is None: await asyncio.sleep(0)
        await self.tools([call('stop', name='interrupt', args={'land':True})])
        await self.settle()
        self.assertEqual(self.runtime.stops, [True])
        self.assertEqual(self.results[0][1]['status'], 'cancelled')
    async def test_subtitle_and_barge_in_do_not_stop_program(self):
        for kind,value in [('subv',{'data':[{'userId':'user1','text':'看看画面'}]}),
                           ('conv',{'Stage':{'Code':4}})]:
            await self.bridge.message(dict(IDENTITY,type='cloud_event',event={'type':kind,'value':value}))
        self.assertFalse(self.bridge.revoked)
        self.assertEqual(self.runtime.stops,[])
    async def test_model_interrupt_keeps_session_usable(self):
        await self.tools([call(name='interrupt',args={'land':False})])
        await self.settle()
        self.assertFalse(self.bridge.revoked)
        await self.tools([call('call2')])
        await self.settle()
        self.assertEqual(len(self.runtime.executed),1)
        self.assertEqual(self.runtime.stops,[False])
    async def test_operator_interrupt_rejects_late_motion(self):
        await self.bridge.message({'type':'interrupt','land':False})
        await asyncio.wait_for(self.bridge.wait(),.1)
        with self.assertRaisesRegex(RuntimeError,'not_authorized'):
            await self.tools([call()])
        self.assertEqual(self.runtime.executed,[])
    async def test_disconnect_failure_surfaces_to_application_for_cloud_shutdown(self):
        self.runtime.finish_interrupt = AsyncMock(return_value={'ok':False,'reason':'stop_unconfirmed'})
        await self.bridge.revoke('rtc_client_disconnected')
        with self.assertRaisesRegex(RuntimeError,'stop_unconfirmed'):
            await self.bridge.wait()
    async def test_external_stop_does_not_reauthorize_on_bind_retry(self):
        self.runtime.cancel()
        self.bridge.bind(IDENTITY)
        with self.assertRaisesRegex(RuntimeError,'authority_changed'):
            await self.tools([call()])
    async def test_new_tool_cancels_old_program_then_executes(self):
        self.runtime.gate = asyncio.Event()
        await self.tools([call()])
        await asyncio.sleep(0)
        await self.tools([call('call2',args={'action':'get_status','args':[]})])
        await self.settle()
        self.assertEqual(self.runtime.executed,[{'action':'rotate','args':[360]}, {'action':'get_status','args':[]}])
        self.assertEqual(self.results[0][1]['status'],'cancelled')
    async def test_invalid_arguments_do_not_stop_running_program(self):
        self.runtime.gate = asyncio.Event()
        await self.tools([call()])
        await asyncio.sleep(0)
        malformed = call('bad')
        malformed['function']['arguments'] = '{"action":"cut'
        await self.tools([malformed])
        await asyncio.sleep(0)
        self.assertEqual(self.runtime.stops,[])
        self.runtime.gate.set()
        await self.settle()
        self.assertTrue(any('JSONDecodeError' in value.get('reason','') for _,value in self.results))

    async def test_old_python_tool_is_rejected_without_stopping_current_action(self):
        self.runtime.gate = asyncio.Event()
        await self.tools([call()])
        await asyncio.sleep(0)
        await self.tools([call('legacy', name='run_program', args={'code':'drone.land()'})])
        await asyncio.sleep(0)
        self.assertEqual(self.runtime.stops, [])
        self.runtime.gate.set()
        await self.settle()
        self.assertEqual(len(self.runtime.executed), 1)
        self.assertTrue(any('host_tool_not_exposed' in value.get('reason','') for _,value in self.results))
    async def test_temporary_missing_frame_does_not_revoke(self):
        self.bridge.device = types.SimpleNamespace(get_frame=lambda: (_ for _ in ()).throw(RuntimeError('no frame')))
        self.bridge.send = AsyncMock()
        pump = asyncio.create_task(self.bridge.frames())
        await asyncio.sleep(.15)
        pump.cancel()
        await asyncio.gather(pump,return_exceptions=True)
        self.assertFalse(self.bridge.revoked)
        self.assertEqual(self.runtime.stops,[])
        self.bridge.send.assert_awaited_with({'type':'invalid_frame','reason':'sdk_frame_unavailable'})


class ActionContractTests(unittest.TestCase):
    def test_action_boundary_rejects_code_extra_fields_and_invalid_values(self):
        invalid = [None, [], {'code':'drone.land()'}, {'action':'__getattribute__','args':[]},
                   {'action':'observe','args':[]}, {'action':'land','args':[1]},
                   {'action':'rotate','args':[0]}, {'action':'rotate','args':[True]},
                   {'action':'move_forward','args':[19]}, {'action':'move_forward','args':[501]},
                   {'action':'start_velocity','args':[0,20,0]},
                   {'action':'start_velocity','args':[0,101,0,0]},
                   {'action':'get_status','args':[], 'code':'anything'},
                   {'action':'move_forward','args':[20], 'adjust_follow_reference':'vertical'}]
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                validate_action(request)

    def test_matching_reference_adjustment_and_fixed_tool_schema(self):
        request = {'action':'move_forward','args':[20], 'adjust_follow_reference':'depth'}
        self.assertIs(validate_tool('execute_action', request), request)
        self.assertEqual([tool['name'] for tool in OMNI_TOOLS], ['execute_action', 'interrupt'])
        self.assertEqual(validate_action({'action':'start_velocity','args':[0,20,0,0]})['args'], [0,20,0,0])

    def test_query_boolean_and_detailed_error_survive_cloud_result(self):
        self.assertIs(tool_result({'ok':True, 'action_result':False})['action_result'], False)
        evidence = {'details': {'wire_sent':True, 'outcome_unknown':True}, 'reason':'response_timeout'}
        result = tool_result({'ok':False, 'action_error':evidence, 'action_result': evidence})
        self.assertEqual(result['action_error'], evidence)
        self.assertEqual(result['action_result'], evidence)
