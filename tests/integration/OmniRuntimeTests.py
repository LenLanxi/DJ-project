"""Alibaba authority/loopback transport tests. No cloud, UI or aircraft access."""
import asyncio
import base64
import copy
import json
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.request import urlopen

import numpy as np
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

ROOT = Path(__file__).resolve().parents[2]
from providers.AlibabaRealtime import AlibabaConfig
from providers.OmniSession import OmniSession, MAX_RESPONSE_TOKENS
from providers.OmniRuntime import OmniService
from app import Application as main
from app.Runtime import UnifiedRuntime
from app import Tools
from drone_sdk.contract import FrameResult, CommandResult


CONFIG = AlibabaConfig('privateOmniKeyValue','workspace1',
    'wss://workspace1.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime')


class LocalRuntime:
    def __init__(self):
        self.generation=0
        self.running=False
        self.calls=[]
        self.stops=[]
        self.land_requested=False
        self.result={'ok':True,'status':'completed','journal':[]}
    def cancel(self): self.generation+=1
    def begin_interrupt(self,land=False,expected_generation=None):
        if expected_generation is not None and expected_generation != self.generation:
            return None
        self.land_requested = self.land_requested or bool(land)
        self.cancel()
        return types.SimpleNamespace(generation=self.generation,effective_land=self.land_requested)
    async def finish_interrupt(self,receipt):
        self.stops.append(receipt.effective_land)
        return {'ok':True,'status':'interrupted'}
    async def interrupt(self,land=False):
        return await self.finish_interrupt(self.begin_interrupt(land))
    async def run(self,code,expected_generation=None):
        if expected_generation != self.generation:
            return {'ok':False,'status':'cancelled'}
        self.calls.append(code)
        return self.result


class CloudWire:
    def __init__(self): self.events=[]
    async def send(self,value): self.events.append(json.loads(value))
    async def close(self): return


class RuntimeLandingPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_sender_error_is_not_reported_as_cloud_vad_timeout(self):
        from OmniLive import phase
        async def sender(): raise KeyError('speech_end_samples')
        send_task=asyncio.create_task(sender())
        receive_task=asyncio.create_task(asyncio.Event().wait())
        try:
            with self.assertRaisesRegex(KeyError,'speech_end_samples'):
                await phase(asyncio.Event(),receive_task,.2,workers=(send_task,))
        finally:
            receive_task.cancel()
            await asyncio.gather(send_task,receive_task,return_exceptions=True)

    def make_runtime(self):
        device=types.SimpleNamespace(priority_interrupt=Mock(return_value={'ok':True}))
        follow=types.SimpleNamespace(stop=Mock(return_value=CommandResult(True,'completed')))
        records=[]
        return UnifiedRuntime(device,follow,log=records.append),device,records

    async def test25_land_stays_requested_when_a_later_cleanup_asks_only_to_stop(self):
        runtime,gateway,records=self.make_runtime()
        await runtime.interrupt(False)
        await runtime.interrupt(True)
        await runtime.interrupt(False)
        self.assertEqual([call.args[0] for call in gateway.priority_interrupt.call_args_list],[False,True,True])
        self.assertTrue(runtime.land_requested)
        self.assertFalse(records[-1]['requested_land'])
        self.assertTrue(records[-1]['effective_land'])
        fresh,_,_=self.make_runtime()
        self.assertFalse(fresh.land_requested)

    async def test26_pending_or_failed_land_does_not_permit_a_stop_downgrade(self):
        runtime,gateway,_=self.make_runtime()
        entered,release=threading.Event(),threading.Event()
        calls=[]
        def pending(land):
            calls.append(land)
            if len(calls)==1:
                entered.set()
                release.wait(2)
            return {'ok':False,'reason':'land_unconfirmed'}
        gateway.priority_interrupt=pending
        first=asyncio.create_task(runtime.interrupt(True))
        self.assertTrue(await asyncio.to_thread(entered.wait,1))
        await runtime.interrupt(False)
        release.set()
        await first
        await runtime.interrupt(False)
        self.assertEqual(calls,[True,True,True])
        self.assertTrue(runtime.land_requested)


class OmniAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime=LocalRuntime()
        self.client=[]
        async def send(value): self.client.append(value)
        self.session=OmniSession(self.runtime,CONFIG,Tools.OMNI_INSTRUCTIONS,Tools.OMNI_TOOLS,send,lambda e:None)
        self.session.ws=CloudWire()
        self.session.fault=None

    async def asyncTearDown(self): await self.session.close()

    async def requested(self):
        for _ in range(40):
            await asyncio.sleep(0)
            if self.session.pending_response == self.session.epoch: return
        self.fail('manual response request was not sent')

    async def turn(self,*,item='Input1',rid='Response1',asr=True):
        await self.session.handle({'type':'input_audio_buffer.speech_started','item_id':item})
        await self.session.handle({'type':'input_audio_buffer.committed','item_id':item})
        if asr:
            await self.session.handle({'type':'conversation.item.input_audio_transcription.completed',
                                       'item_id':item,'transcript':'查询状态'})
            await self.requested()
            await self.session.handle({'type':'response.created','response':{'id':rid}})

    async def program(self,cid='Call1',rid='Response1',code='print(drone.get_status())'):
        item={'type':'function_call','call_id':cid,'name':'run_program','arguments':json.dumps({'code':code},ensure_ascii=False)}
        await self.session.handle({**item,'type':'response.function_call_arguments.done','response_id':rid})
        await self.session.handle({'type':'response.done','response':{'id':rid,'status':'completed','output':[item]}})

    async def settle(self):
        for _ in range(20):
            await asyncio.sleep(0)
            if not self.session.tasks: break

    async def finish_rejected(self, item, *, status='completed', extra=None):
        await self.session.handle({'type':'response.done','response':{
            'id':'Response1','status':status,'output':[item],**(extra or {})}})
        if self.session.execution_tasks:
            await asyncio.gather(*tuple(self.session.execution_tasks),return_exceptions=True)
        await self.settle()

    async def test_truncated_json_returns_error_then_model_can_submit_corrected_program(self):
        records=[]
        self.session.log=records.append
        await self.turn()
        item={'type':'function_call','call_id':'BrokenCall','name':'run_program',
              'arguments':'{"code": "import time\\n# 移动到'}
        await self.session.handle({**item,'type':'response.function_call_arguments.done','response_id':'Response1'})
        self.assertIsNone(self.session.fault)
        self.assertEqual(self.runtime.calls,[])
        await self.finish_rejected(item,status='incomplete',extra={
            'status_details':{'type':'incomplete','reason':'max_output_tokens'},
            'usage':{'input_tokens':123,'output_tokens':2048,'total_tokens':2171}})
        self.assertEqual(self.runtime.calls,[])
        self.assertEqual(self.runtime.stops,[])
        self.assertEqual(self.runtime.generation,0)
        self.assertFalse(self.session.land_requested)
        self.assertFalse(self.session.closed)
        self.assertIsNone(self.session.fault)
        self.assertIsNone(self.session.active_response)
        self.assertEqual(self.session.pending_response,self.session.epoch)
        result=self.session.tool_outputs['BrokenCall']
        self.assertEqual(result['reason'],'invalid_tool_arguments')
        self.assertIn('Unterminated string',result['debug'])
        end=next(e for e in records if e['event']=='omni_response_end')
        self.assertEqual(end['status_details']['reason'],'max_output_tokens')
        self.assertEqual(end['usage']['output_tokens'],2048)
        self.assertTrue(any('错误已返回模型处理' in e.get('text','') for e in self.client))
        outputs=[e for e in self.session.ws.events if e['type']=='conversation.item.create']
        self.assertEqual(len(outputs),1)
        self.assertIn('Unterminated string',json.loads(outputs[0]['item']['output'])['debug'])
        requests=sum(e['type']=='response.create' for e in self.session.ws.events)
        # Duplicate completion cannot publish the failure or request a reply twice.
        await self.finish_rejected(item)
        self.assertEqual(sum(e['type']=='response.create' for e in self.session.ws.events),requests)
        self.assertEqual(len([e for e in self.session.ws.events if e['type']=='conversation.item.create']),1)
        await self.session.handle({'type':'response.created','response':{'id':'CorrectedReply'}})
        await self.program(cid='CorrectedCall',rid='CorrectedReply',code='print(drone.get_status())')
        await self.settle()
        self.assertEqual(self.runtime.calls,['print(drone.get_status())'])
        self.assertEqual(self.runtime.stops,[])
        self.assertTrue(self.session.tool_outputs['CorrectedCall']['ok'])

    async def test_completed_response_with_invalid_schema_rejects_entire_batch(self):
        await self.turn()
        good={'type':'function_call','call_id':'GoodCall','name':'run_program',
              'arguments':json.dumps({'code':'drone.takeoff()'})}
        bad={'type':'function_call','call_id':'BadCall','name':'interrupt',
             'arguments':json.dumps({'land':'yes'})}
        await self.session.handle({'type':'response.done','response':{
            'id':'Response1','status':'completed','output':[good,bad]}})
        await self.settle()
        self.assertEqual(self.runtime.calls,[])
        self.assertEqual(self.runtime.stops,[])
        self.assertFalse(self.session.tool_outputs['GoodCall']['ok'])
        self.assertIn('invalid_interrupt_arguments',self.session.tool_outputs['BadCall']['debug'])

    async def test_model_can_correct_arguments_after_previous_program_completed(self):
        await self.turn()
        await self.program(code='print(1)')
        await self.settle()
        await self.session.handle({'type':'response.created','response':{'id':'MalformedReply'}})
        item={'type':'function_call','call_id':'BadCall','name':'run_program','arguments':'{"code": "'}
        await self.session.handle({'type':'response.done','response':{
            'id':'MalformedReply','status':'completed','output':[item]}})
        await self.settle()
        self.assertIn('Unterminated string',self.session.tool_outputs['BadCall']['debug'])
        await self.session.handle({'type':'response.created','response':{'id':'Correction'}})
        await self.program(cid='CorrectedCall',rid='Correction',code='print(2)')
        await self.settle()
        self.assertEqual(self.runtime.calls,['print(1)','print(2)'])
        self.assertEqual(self.runtime.stops,[])
        self.assertTrue(self.session.tool_outputs['CorrectedCall']['ok'])

    async def test_incomplete_valid_program_never_executes(self):
        await self.turn()
        item={'type':'function_call','call_id':'Call1','name':'run_program',
              'arguments':json.dumps({'code':'drone.takeoff()'})}
        await self.finish_rejected(item,status='incomplete')
        self.assertEqual(self.runtime.calls,[])
        self.assertEqual(self.session.tool_outputs['Call1']['reason'],'cloud_response_incomplete')
        self.assertIsNone(self.session.fault)

    async def test_rejection_leaves_running_program_untouched(self):
        release,sequence=await self.blocking_program()
        await self.turn(item='Input2',rid='Response1New')
        item={'type':'function_call','call_id':'BrokenCall','name':'run_program','arguments':'{"code": "'}
        await self.session.handle({'type':'response.done','response':{
            'id':'Response1New','status':'completed','output':[item]}})
        await self.settle()
        self.assertFalse(release.is_set())
        self.assertEqual(self.runtime.calls,['old_program'])
        self.assertEqual(self.runtime.stops,[])
        self.assertTrue(self.runtime.running)
        self.assertIn('BrokenCall',self.session.tool_outputs)
        self.assertIsNone(self.session.fault)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tuple(self.session.execution_tasks)),2)

    async def test_model_can_explicitly_request_landing_after_parameter_error(self):
        await self.turn()
        item={'type':'function_call','call_id':'BadCall','name':'run_program','arguments':'{"code": "'}
        await self.finish_rejected(item)
        self.assertEqual(self.runtime.stops,[])
        await self.session.handle({'type':'response.created','response':{'id':'ModelDecision'}})
        await self.session.handle({'type':'response.done','response':{
            'id':'ModelDecision','status':'completed','output':[{
                'type':'function_call','call_id':'LandCall','name':'interrupt','arguments':'{"land":true}'}]}})
        await self.settle()
        self.assertEqual(self.runtime.stops,[True])
        self.assertTrue(self.session.land_requested)
        self.assertEqual(self.runtime.calls,[])
        self.assertIsNone(self.session.fault)

    async def test_invalid_arguments_without_response_end_has_bounded_fail_safe(self):
        records=[]
        self.session.log=records.append
        await self.turn()
        with patch('providers.OmniSession.INVALID_RESPONSE_GRACE_SECONDS',.01):
            await self.session.handle({'type':'response.function_call_arguments.done',
                'response_id':'Response1','call_id':'BadCall','name':'run_program','arguments':'{"code": "'})
            await asyncio.wait_for(asyncio.gather(*tuple(self.session.tasks),return_exceptions=True),1)
            await asyncio.sleep(0)
        self.assertIsInstance(self.session.fault,TimeoutError)
        self.assertGreater(self.runtime.generation,0)
        self.assertTrue(any(e['event']=='omni_response_end_missing' for e in records))
        self.assertEqual(self.runtime.calls,[])

    async def test_superseded_invalid_response_cannot_land_for_new_epoch(self):
        await self.turn()
        bad={'type':'function_call','call_id':'BadCall','name':'run_program','arguments':'{"code": "'}
        await self.session.handle({**bad,'type':'response.function_call_arguments.done','response_id':'Response1'})
        for event in (
            {'type':'input_audio_buffer.speech_started','item_id':'Input2'},
            {'type':'input_audio_buffer.committed','item_id':'Input2'},
            {'type':'conversation.item.input_audio_transcription.completed','item_id':'Input2','transcript':'查询状态'}):
            await self.session.handle(event)
        await self.finish_rejected(bad)
        self.assertEqual(self.runtime.stops,[])
        self.assertEqual(self.session.tool_outputs['BadCall']['status'],'cancelled')
        self.assertIsNone(self.session.fault)

    async def test_response_end_diagnostics_are_bounded_and_redacted(self):
        records=[]
        cleaner=main.SessionLog.__new__(main.SessionLog)
        cleaner.secrets=(CONFIG.api_key,)
        self.session.log=lambda e:records.append(cleaner.clean(e))
        await self.turn()
        await self.session.handle({'type':'response.done','response':{
            'id':'Response1','status':'failed','output':[],
            'status_details':{'type':'failed','reason':'provider_failure',
                              'error':{'message':CONFIG.api_key+'x'*3000,'code':'bad_output'}},
            'headers':{'Authorization':CONFIG.api_key},'usage':{'output_tokens':12}}})
        await self.settle()
        end=next(e for e in records if e['event']=='omni_response_end')
        self.assertNotIn(CONFIG.api_key,json.dumps(records))
        self.assertNotIn('headers',end)
        self.assertLessEqual(len(end['error']['message']),1000)
        self.assertEqual(end['usage']['output_tokens'],12)
        self.assertIsNone(self.session.fault)

    async def test01_program_waits_for_matching_completed_asr(self):
        await self.turn(asr=False)
        await self.program()
        await asyncio.sleep(0)
        self.assertEqual(self.runtime.calls,[])
        await self.session.handle({'type':'conversation.item.input_audio_transcription.completed',
                                   'item_id':'OldInput','transcript':'去前面'})
        self.assertFalse(any(e['type']=='response.create' for e in self.session.ws.events))
        self.assertFalse(any(e['type']=='user_text' for e in self.client))
        await self.session.handle({'type':'conversation.item.input_audio_transcription.completed',
                                   'item_id':'Input1','transcript':'查询状态'})
        await self.requested()
        await self.session.handle({'type':'response.created','response':{'id':'Response1'}})
        await self.program(cid='ValidCall')
        await self.settle()
        self.assertEqual(self.runtime.calls,['print(drone.get_status())'])

    async def test_paused_capture_discards_partial_input_and_late_asr_without_revoking_programs(self):
        self.session.ready.set()
        await self.session.handle({'type':'input_audio_buffer.speech_started','item_id':'PartialInput'})
        before=self.runtime.generation
        await self.session.clear_audio()
        self.assertEqual(self.session.ws.events[-1]['type'],'input_audio_buffer.clear')
        for event in (
            {'type':'input_audio_buffer.committed','item_id':'PartialInput'},
            {'type':'conversation.item.input_audio_transcription.completed','item_id':'PartialInput','transcript':'旧半句'}):
            await self.session.handle(event)
        self.assertFalse(any(value['type']=='response.create' for value in self.session.ws.events))
        self.assertEqual(self.runtime.generation,before)
        await self.turn(item='NewInput',rid='NewResponse')
        await self.program(rid='NewResponse')
        await self.settle()
        self.assertEqual(self.runtime.calls,['print(drone.get_status())'])

    async def test_own_interrupt_and_parallel_video_pump_keep_authority_consistent(self):
        frame_seen=asyncio.Event()
        backend=types.SimpleNamespace(get_frame=lambda:FrameResult(
            np.zeros((16,16,3),dtype=np.uint8),1,time.time(),False))
        service=OmniService(self.runtime,backend,lambda e:None,CONFIG,'test',Tools.OMNI_TOOLS,seconds=5)
        service.deadline=time.time()+5
        service.last_heartbeat=time.monotonic()
        service.cloud=self.session
        async def send(value):
            if value['type']=='frame':
                frame_seen.set()
                service.handle_closing=True
        service.send=send
        # With wait_for, the interrupt coroutine is queued behind this already
        # runnable video pump, exposing expected generation 1 while runtime is 0.
        interrupt=asyncio.create_task(self.session.own_interrupt(True))
        pump=asyncio.create_task(service.frames())
        result,video=await asyncio.gather(interrupt,pump,return_exceptions=True)
        self.assertIsNone(video,repr(video))
        self.assertTrue(frame_seen.is_set())
        self.assertTrue(result['ok'])
        self.assertTrue(self.session.authority_current())
        self.assertEqual(self.runtime.stops,[True])

    async def test_external_cancel_before_own_interrupt_starts_prevents_dispatch(self):
        async def operator_cancel(): self.runtime.cancel()
        operator=asyncio.create_task(operator_cancel())
        interrupt=asyncio.create_task(self.session.own_interrupt(True))
        _,result=await asyncio.gather(operator,interrupt)
        self.assertEqual(result,{'ok':False,'reason':'external_authority_changed'})
        self.assertEqual(self.runtime.stops,[])
        self.assertFalse(self.session.authority_current())

    async def test_external_cancel_during_own_interrupt_is_not_adopted(self):
        entered,release=asyncio.Event(),asyncio.Event()
        original=self.runtime.finish_interrupt
        async def blocked(receipt):
            result=await original(receipt)
            entered.set()
            await release.wait()
            return result
        self.runtime.finish_interrupt=blocked
        interrupt=asyncio.create_task(self.session.own_interrupt(True))
        await asyncio.wait_for(entered.wait(),2)
        self.runtime.cancel()
        release.set()
        result=await asyncio.wait_for(interrupt,2)
        self.assertEqual(result,{'ok':False,'reason':'external_authority_changed'})
        self.assertEqual(self.runtime.stops,[True])
        self.assertFalse(self.session.authority_current())

    async def test_own_interrupt_caller_cancellation_remains_cancelled(self):
        entered=asyncio.Event()
        cancelled=[]
        original=self.runtime.finish_interrupt
        async def blocked(receipt):
            await original(receipt)
            entered.set()
            try: await asyncio.Event().wait()
            finally: cancelled.append(True)
        self.runtime.finish_interrupt=blocked
        interrupt=asyncio.create_task(self.session.own_interrupt(True))
        await asyncio.wait_for(entered.wait(),2)
        interrupt.cancel()
        with self.assertRaises(asyncio.CancelledError): await interrupt
        self.assertEqual(cancelled,[True])
        self.assertEqual(self.runtime.stops,[True])

    async def test02_f12_while_tool_waits_for_asr_cannot_borrow_current_generation(self):
        await self.turn(asr=False)
        await self.program()
        await asyncio.sleep(0)
        self.runtime.cancel()  # Same synchronous revocation used by F12.
        with self.assertRaisesRegex(RuntimeError,'authority_changed'):
            await self.session.handle({'type':'conversation.item.input_audio_transcription.completed',
                                       'item_id':'Input1','transcript':'查询状态'})
        await self.settle()
        self.assertEqual(self.runtime.calls,[])
        self.assertFalse(self.session.authority_current())

    async def test03_new_speech_cannot_reset_an_external_interrupt_authorization(self):
        await self.turn()
        self.runtime.cancel()
        with self.assertRaisesRegex(RuntimeError,'authority_changed'):
            await self.session.handle({'type':'input_audio_buffer.speech_started','item_id':'Input2'})
        self.assertEqual(self.runtime.calls,[])

    async def test04_duplicate_tool_id_is_once_and_model_can_continue_same_turn(self):
        await self.turn()
        await self.program()
        await self.settle()
        await self.program()
        await self.settle()
        self.assertEqual(len(self.runtime.calls),1)
        for index in range(2,6):
            rid='Response'+str(index)
            cid='Call'+str(index)
            await self.session.handle({'type':'response.created','response':{'id':rid}})
            await self.program(cid=cid,rid=rid,code=f'print({index})')
            await self.settle()
            self.assertTrue(self.session.tool_outputs[cid]['ok'])
        self.assertEqual(self.runtime.calls,['print(drone.get_status())']+[f'print({i})' for i in range(2,6)])
        self.assertEqual(self.session.epoch,1)
        self.assertEqual(self.runtime.stops,[])

    async def test05_response_requires_committed_user_input_and_old_response_is_ignored(self):
        await self.session.handle({'type':'response.created','response':{'id':'Unbound'}})
        await self.turn()
        await self.session.handle({'type':'input_audio_buffer.speech_started','item_id':'Input2'})
        await self.session.handle({'type':'response.function_call_arguments.done','response_id':'Unbound',
            'call_id':'LateCall','name':'run_program','arguments':json.dumps({'code':'print(1)'})})
        self.assertEqual(self.runtime.calls,[])
        self.assertTrue(self.session.authority_current())

    async def test06_overlapping_sound_does_not_revoke_or_close_session(self):
        await self.turn()
        before=self.runtime.generation
        await self.session.handle({'type':'input_audio_buffer.speech_started','item_id':'Input2'})
        self.assertEqual(self.runtime.generation,before)
        self.assertEqual(self.session.input_id,'Input2')
        self.assertEqual(self.session.epoch,1)
        self.assertTrue(self.session.authority_current())
        self.assertFalse(self.session.closed)

    async def test07_sound_while_program_runs_does_not_grant_a_new_program(self):
        self.runtime.running=True
        await self.session.handle({'type':'input_audio_buffer.speech_started','item_id':'Input1'})
        self.assertEqual(self.session.input_id,'Input1')
        self.assertEqual(self.session.epoch,0)
        self.assertTrue(self.session.authority_current())
        self.assertIsNone(self.session.pending_response)

    async def test_noise_and_empty_or_failed_transcription_leave_running_program_untouched(self):
        await self.turn()
        self.runtime.running=True
        before=self.runtime.generation
        for index in range(45):
            item='Noise'+str(index)
            await self.session.handle({'type':'input_audio_buffer.speech_started','item_id':item})
            await self.session.handle({'type':'input_audio_buffer.committed','item_id':item})
            if index%2:
                await self.session.handle({'type':'conversation.item.input_audio_transcription.failed','item_id':item})
            else:
                await self.session.handle({'type':'conversation.item.input_audio_transcription.completed',
                                           'item_id':item,'transcript':'  '})
        self.assertEqual(self.runtime.generation,before)
        self.assertEqual(self.runtime.stops,[])
        self.assertEqual(self.session.epoch,1)
        self.assertEqual(len([e for e in self.client if e['type']=='user_text']),1)
        self.assertLessEqual(len(self.session.inputs),32)
        self.assertFalse(self.session.closed)
        self.assertIsNone(self.session.fault)

    async def blocking_program(self):
        started,release=asyncio.Event(),asyncio.Event()
        sequence=[]
        async def run(code,expected_generation=None):
            self.runtime.calls.append(code)
            self.runtime.running=True
            sequence.append(('start',code,expected_generation))
            try:
                if code=='old_program':
                    started.set(); await release.wait()
                return {'ok':True,'status':'completed'}
            finally:
                sequence.append(('finish',code,expected_generation))
                self.runtime.running=False
        async def stop(receipt):
            land=receipt.effective_land
            self.runtime.stops.append(land)
            sequence.append(('stop',land,self.runtime.generation))
            release.set()
            return {'ok':True,'status':'interrupted'}
        self.runtime.run=run
        self.runtime.finish_interrupt=stop
        await self.turn()
        await self.program(code='old_program')
        await asyncio.wait_for(started.wait(),1)
        return release,sequence

    async def test_confirmed_new_program_stops_old_program_and_waits_for_its_cleanup(self):
        release,sequence=await self.blocking_program()
        await self.turn(item='Input2',rid='Plan2')
        self.assertEqual(self.runtime.stops,[])  # ASR alone is not an action.
        self.assertTrue(self.runtime.running)
        await self.program(cid='NewCall',rid='Plan2',code='new_program')
        await asyncio.wait_for(asyncio.gather(*tuple(self.session.execution_tasks)),2)
        self.assertTrue(release.is_set())
        self.assertEqual(self.runtime.calls,['old_program','new_program'])
        self.assertEqual(self.runtime.stops,[False])
        self.assertEqual([entry[:2] for entry in sequence],
                         [('start','old_program'),('stop',False),('finish','old_program'),
                          ('start','new_program'),('finish','new_program')])
        self.assertEqual(sequence[-2][2],self.runtime.generation)
        self.assertNotEqual(sequence[0][2],sequence[-2][2])
        self.assertEqual(self.session.tool_outputs['Call1']['status'],'cancelled')
        self.assertTrue(self.session.tool_outputs['NewCall']['ok'])
        self.assertTrue(self.session.authority_current())
        self.assertIsNone(self.session.fault)
        self.assertFalse(self.session.closed)

    async def test_new_conversation_without_a_tool_does_not_cancel_running_program(self):
        release,sequence=await self.blocking_program()
        await self.turn(item='Greeting',rid='GreetingReply')
        await self.session.handle({'type':'response.text.delta','response_id':'GreetingReply','delta':'你好'})
        await self.session.handle({'type':'response.done','response':{
            'id':'GreetingReply','status':'completed','output':[]}})
        self.assertTrue(self.runtime.running)
        self.assertEqual(self.runtime.stops,[])
        self.assertEqual(self.runtime.generation,0)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tuple(self.session.execution_tasks)),2)
        self.assertTrue(self.session.tool_outputs['Call1']['ok'])
        self.assertFalse(self.session.closed)

    async def test_late_created_old_reply_cannot_use_new_input_authority(self):
        await self.session.handle({'type':'input_audio_buffer.speech_started','item_id':'OldInput'})
        await self.session.handle({'type':'input_audio_buffer.committed','item_id':'OldInput'})
        await self.session.handle({'type':'conversation.item.input_audio_transcription.completed',
                                   'item_id':'OldInput','transcript':'旧指令'})
        await self.requested()
        await self.session.handle({'type':'input_audio_buffer.speech_started','item_id':'NewInput'})
        await self.session.handle({'type':'input_audio_buffer.committed','item_id':'NewInput'})
        await self.session.handle({'type':'conversation.item.input_audio_transcription.completed',
                                   'item_id':'NewInput','transcript':'只查询状态'})
        await self.session.handle({'type':'response.created','response':{'id':'DelayedOld'}})
        self.assertEqual(self.session.responses['DelayedOld'],1)
        item={'type':'function_call','call_id':'LateAction','name':'run_program',
              'arguments':json.dumps({'code':'drone.move_forward(20)'})}
        await self.session.handle({'type':'response.done','response':{
            'id':'DelayedOld','status':'completed','output':[item]}})
        await self.requested()
        await self.session.handle({'type':'response.created','response':{'id':'NewPlan'}})
        await self.program(cid='NewQuery',rid='NewPlan',code='print(drone.get_status())')
        await self.settle()
        self.assertEqual(self.runtime.calls,['print(drone.get_status())'])
        self.assertEqual(self.session.tool_outputs['LateAction']['status'],'cancelled')
        self.assertIsNone(self.session.fault)
        self.assertFalse(self.session.closed)

    async def test_old_response_cancel_ack_is_drained_before_new_response_request(self):
        await self.turn()
        for event in (
            {'type':'input_audio_buffer.speech_started','item_id':'NewInput'},
            {'type':'input_audio_buffer.committed','item_id':'NewInput'},
            {'type':'conversation.item.input_audio_transcription.completed','item_id':'NewInput',
             'transcript':'只查询状态'}):
            await self.session.handle(event)
        for _ in range(60):
            await asyncio.sleep(0)
            if any(e['type']=='response.cancel' for e in self.session.ws.events): break
        self.assertTrue(any(e['type']=='response.cancel' for e in self.session.ws.events))
        self.assertIsNone(self.session.pending_response)
        before=len(self.client)
        await self.session.handle({'type':'response.text.delta','response_id':'Response1','delta':'旧回复'})
        self.assertEqual(len(self.client),before)
        await self.session.handle({'type':'response.done','response':{
            'id':'Response1','status':'cancelled','output':[]}})
        await self.requested()
        await self.session.handle({'type':'response.created','response':{'id':'NewReply'}})
        await self.program(cid='OnlyNewCall',rid='NewReply')
        await self.settle()
        self.assertEqual(self.runtime.calls,['print(drone.get_status())'])
        self.assertIsNone(self.session.fault)
        self.assertFalse(self.session.closed)

    async def test_unconfirmed_stop_blocks_programs_but_keeps_voice_and_land_available(self):
        async def failed_stop(receipt):
            land=receipt.effective_land
            self.runtime.stops.append(land)
            self.runtime.running=False
            return {'ok':False,'reason':'stop_send_failed'}
        self.runtime.finish_interrupt=failed_stop
        self.runtime.running=True
        await self.turn()
        await self.program()
        await self.settle()
        self.assertEqual(self.runtime.calls,[])
        self.assertEqual(self.session.blocked_reason,'previous_stop_unconfirmed')
        self.assertFalse(self.session.closed)
        await self.session.handle({'type':'response.created','response':{'id':'BlockedReply'}})
        await self.session.handle({'type':'response.done','response':{
            'id':'BlockedReply','status':'completed','output':[]}})
        await self.turn(item='AnotherCommand',rid='AnotherPlan')
        await self.program(cid='BlockedAgain',rid='AnotherPlan')
        await self.settle()
        self.assertEqual(self.runtime.calls,[])
        self.assertEqual(self.runtime.stops,[False])
        self.assertIsNone(self.session.fault)

    async def test_land_acknowledged_as_accepted_is_not_reported_as_completed(self):
        async def accepted(receipt):
            self.runtime.stops.append(receipt.effective_land)
            return {'ok':True,'status':'accepted'}
        self.runtime.finish_interrupt=accepted
        await self.turn()
        await self.session.handle({'type':'response.done','response':{
            'id':'Response1','status':'completed','output':[{'type':'function_call',
            'call_id':'Landing','name':'interrupt','arguments':json.dumps({'land':True})}]}})
        await self.settle()
        self.assertEqual(self.session.tool_outputs['Landing']['status'],'accepted')
        self.assertFalse(self.session.closed)
        self.assertEqual(self.session.pending_response,self.session.epoch)
        self.assertIsNone(self.session.fault)

    async def test08_complete_python_over_rtc_limit_is_supported_but_utf8_limit_is_enforced(self):
        await self.turn()
        await self.program(code='#'+('a'*1000)+'\nprint(drone.get_status())')
        await self.settle()
        self.assertEqual(len(self.runtime.calls),1)
        await self.session.handle({'type':'response.created','response':{'id':'Response2'}})
        await self.program(cid='Call2',rid='Response2',code='#'+'中'*21000)
        await self.settle()
        self.assertEqual(len(self.runtime.calls),1)
        self.assertIsNone(self.session.fault)
        self.assertFalse(self.session.tool_outputs['Call2']['ok'])
        self.assertIn('code_bytes=63001',self.session.tool_outputs['Call2']['debug'])

    async def test09_tool_reply_omits_images_and_keys_but_retains_failure_evidence(self):
        self.runtime.result={'ok':False,'status':'failed','reason':'not_visible',
            'api_key':CONFIG.api_key,'debug':CONFIG.api_key,
            'journal':[{'method':'get_frame','result':{'image_url':'data:image/jpeg;base64,SECRET'}}]}
        await self.turn()
        await self.program()
        await self.settle()
        raw=json.dumps(self.session.ws.events)
        self.assertNotIn(CONFIG.api_key,raw)
        self.assertNotIn('data:image',raw)
        self.assertIn('not_visible',raw)

    async def test_follow_cleanup_survives_provider_tool_output(self):
        self.runtime.result={'ok':True,'status':'completed','follow_cleanup':{
            'status':'follow_resumed','reference_adjusted':'horizontal'}}
        await self.turn()
        await self.program()
        await self.settle()
        output=next(event['item'] for event in self.session.ws.events
                    if event['type']=='conversation.item.create')
        follow=json.loads(output['output'])['follow_cleanup']
        self.assertEqual(follow,{'status':'follow_resumed','reference_adjusted':'horizontal'})

    async def test_provider_error_diagnostics_are_bounded_and_secret_redacted(self):
        records=[]
        cleaner=main.SessionLog.__new__(main.SessionLog)
        cleaner.secrets=(CONFIG.api_key,)
        self.session.log=lambda event:records.append(cleaner.clean(event))
        with self.assertRaisesRegex(RuntimeError,'omni_cloud_error'):
            await self.session.handle({'type':'error','error':{
                'code':'invalid_request','message':'bad '+CONFIG.api_key+' '+('x'*3000),
                'param':'call_id','request':{'Authorization':'Bearer '+CONFIG.api_key}}})
        details=next(record for record in records if record['event']=='omni_cloud_error_details')
        self.assertEqual(details['error']['code'],'invalid_request')
        self.assertEqual(details['error']['param'],'call_id')
        self.assertNotIn('request',details['error'])
        self.assertNotIn(CONFIG.api_key,json.dumps(records))
        self.assertLessEqual(len(details['error']['message']),2000)

    async def test_cloud_processing_cause_is_saved_before_socket_cleanup(self):
        await self.turn()
        records=[]
        cleaner=main.SessionLog.__new__(main.SessionLog)
        cleaner.secrets=(CONFIG.api_key,)
        self.session.log=lambda event:records.append(cleaner.clean(event))
        session=self.session
        class BrokenWire(CloudWire):
            async def recv(self): return json.dumps({'type':'session.created'})
            def __aiter__(self): return self.events_to_read()
            async def events_to_read(self):
                yield json.dumps({'type':'response.function_call_arguments.done',
                    'response_id':'Response1','call_id':'Call1',
                    'arguments':CONFIG.api_key+'x'*3000})
            async def __aenter__(self): return self
            async def __aexit__(self,*args):
                self.cause_at_close=session.fault
                self.generation_at_close=session.runtime.generation
        wire=BrokenWire()
        session.connect=lambda *args,**kwargs:wire
        with self.assertRaisesRegex(ValueError,'omni_invalid_tool_request'):
            await session.run()
        self.assertIsInstance(wire.cause_at_close,ValueError)
        self.assertGreater(wire.generation_at_close,0)
        failure=next(e for e in records if e['event']=='omni_cloud_processing_failed')
        self.assertEqual(failure['cloud_event_type'],'response.function_call_arguments.done')
        self.assertLessEqual(len(failure['tool']['arguments']),2000)
        self.assertNotIn(CONFIG.api_key,json.dumps(records))
        self.assertEqual(self.runtime.calls,[])

    async def test_images_require_current_speech_audio_and_stop_after_commit(self):
        jpeg=b'\xff\xd8\xff\xd9'
        self.session.ready.set()
        await self.session.audio(bytes(640))
        self.assertFalse(await self.session.image(jpeg,time.time()))
        await self.session.handle({'type':'input_audio_buffer.speech_started','item_id':'Input1'})
        self.assertFalse(await self.session.image(jpeg,time.time()))
        await self.session.audio(bytes(640))
        self.assertTrue(await self.session.image(jpeg,time.time()))
        await self.session.handle({'type':'input_audio_buffer.speech_stopped','item_id':'Input1'})
        await self.session.audio(bytes(640))
        self.assertFalse(await self.session.image(jpeg,time.time()))
        await self.session.handle({'type':'input_audio_buffer.committed','item_id':'Input1'})
        await self.session.audio(bytes(640))
        self.assertFalse(await self.session.image(jpeg,time.time()))
        await self.session.handle({'type':'input_audio_buffer.speech_started','item_id':'Input2'})
        self.assertFalse(await self.session.image(jpeg,time.time()))
        await self.session.audio(bytes(640))
        self.assertTrue(await self.session.image(jpeg,time.time()))

    async def test_image_waiting_for_socket_lock_cannot_cross_speech_stop(self):
        self.session.ready.set()
        await self.session.handle({'type':'input_audio_buffer.speech_started','item_id':'Input1'})
        await self.session.audio(bytes(640))
        await self.session.lock.acquire()
        pending=asyncio.create_task(self.session.image(b'\xff\xd8\xff\xd9',time.time()))
        await asyncio.sleep(0)
        await self.session.handle({'type':'input_audio_buffer.speech_stopped','item_id':'Input1'})
        self.session.lock.release()
        self.assertFalse(await pending)
        self.assertFalse(any(event['type']=='input_image_buffer.append' for event in self.session.ws.events))

    async def test_image_expiring_while_waiting_for_socket_is_not_published(self):
        self.session.ready.set()
        await self.session.handle({'type':'input_audio_buffer.speech_started','item_id':'Input1'})
        await self.session.audio(bytes(640))
        stamp=time.time()
        await self.session.lock.acquire()
        pending=asyncio.create_task(self.session.image(b'\xff\xd8\xff\xd9',stamp))
        await asyncio.sleep(0)
        with patch('providers.OmniSession.time.time',return_value=stamp+1.1):
            self.session.lock.release()
            self.assertFalse(await pending)
        self.assertFalse(any(event['type']=='input_image_buffer.append' for event in self.session.ws.events))

    async def test_completed_tool_history_keeps_identity_without_retaining_programs_or_old_results(self):
        code='#'+'a'*10000
        await self.turn()
        await self.program(code=code)
        await self.settle()
        record=self.session.calls['Call1']
        self.assertIsNone(record['args'])
        self.assertEqual(len(record['signature']),32)
        self.assertNotIn(code,str(record))
        await self.session.handle({'type':'response.created','response':{'id':'FinalOldReply'}})
        await self.session.handle({'type':'response.done','response':{
            'id':'FinalOldReply','status':'completed','output':[]}})
        await self.turn(item='NextInput',rid='NextResponse')
        self.assertIsNone(self.session.tool_outputs['Call1'])
        await self.session.handle({'type':'response.function_call_arguments.done','response_id':'Response1',
            'call_id':'Call1','name':'run_program','arguments':json.dumps({'code':code})})
        await self.program(cid='NewCall',rid='NextResponse',code='print(drone.get_status())')
        await self.settle()
        self.assertEqual(self.runtime.calls,[code,'print(drone.get_status())'])

    async def test_inflight_old_audio_does_not_reopen_committed_image_buffer(self):
        self.session.ready.set()
        await self.session.handle({'type':'input_audio_buffer.speech_started','item_id':'Input1'})
        entered,release=asyncio.Event(),asyncio.Event()
        async def blocked(raw):
            entered.set();await release.wait()
        self.session.ws.send=blocked
        pending=asyncio.create_task(self.session.audio(bytes(640)))
        await entered.wait()
        await self.session.handle({'type':'input_audio_buffer.committed','item_id':'Input1'})
        release.set();await pending
        self.assertFalse(self.session.audio_started)
        self.assertFalse(await self.session.image(b'\xff\xd8\xff\xd9',time.time()))

    async def test10_visual_answer_does_not_create_any_navigation_authority(self):
        await self.turn()
        await self.session.handle({'type':'response.text.delta','response_id':'Response1','delta':'画面中有红帐篷'})
        await self.session.handle({'type':'response.done','response':{'id':'Response1','status':'completed','output':[]}})
        self.assertEqual(self.runtime.calls,[])
        self.assertEqual(self.runtime.calls,[])
        self.assertTrue(any(x.get('type')=='reply_text' for x in self.client))

    async def test17_late_created_response_from_previous_input_cannot_authorize_new_input(self):
        await self.session.handle({'type':'input_audio_buffer.speech_started','item_id':'Input1'})
        await self.session.handle({'type':'input_audio_buffer.committed','item_id':'Input1'})
        await self.session.handle({'type':'input_audio_buffer.speech_started','item_id':'Input2'})
        await self.session.handle({'type':'input_audio_buffer.committed','item_id':'Input2'})
        await self.session.handle({'type':'conversation.item.input_audio_transcription.completed',
                                   'item_id':'Input2','transcript':'不要前进，只查状态'})
        epoch=self.session.epoch
        await self.session.handle({'type':'conversation.item.input_audio_transcription.completed',
                                   'item_id':'Input1','transcript':'前进'})
        self.assertEqual(self.session.epoch,epoch)
        await self.requested()
        await self.session.handle({'type':'response.created','response':{'id':'CurrentOnly'}})
        await self.program(cid='CurrentQuery',rid='CurrentOnly')
        await self.settle()
        self.assertEqual(self.runtime.calls,['print(drone.get_status())'])

    async def test18_fully_completed_sequential_turns_reuse_connection(self):
        await self.turn()
        await self.program()
        await self.settle()
        await self.session.handle({'type':'response.created','response':{'id':'Final1'}})
        await self.session.handle({'type':'response.done','response':{'id':'Final1','status':'completed','output':[]}})
        self.assertFalse(self.session.turn_open)
        await self.turn(item='Input2',rid='Response2')
        await self.program(cid='Call2',rid='Response2')
        await self.settle()
        self.assertEqual(len(self.runtime.calls),2)
        self.assertTrue(self.session.authority_current())

    async def test19_ready_requires_effective_cloud_audio_model_and_vad_configuration(self):
        value={'model':CONFIG.model,'turn_detection':{'type':'server_vad','silence_duration_ms':1200,
               'create_response':False,'interrupt_response':False},
               'audio':{'input':{'format':{'type':'pcm','sample_rate':16000}},
                        'output':{'format':{'type':'pcm','sample_rate':24000}}},
               'input_audio_transcription':{'model':'qwen3-asr-flash-realtime'},'max_tokens':MAX_RESPONSE_TOKENS}
        invalid=[]
        wrong=copy.deepcopy(value); wrong['model']='another-model'; invalid.append(wrong)
        wrong=copy.deepcopy(value); wrong['audio']['input']['format']['sample_rate']=48000; invalid.append(wrong)
        wrong=copy.deepcopy(value); wrong['audio']['output']['format']['sample_rate']=16000; invalid.append(wrong)
        wrong=copy.deepcopy(value); wrong['turn_detection']['silence_duration_ms']=800; invalid.append(wrong)
        wrong=copy.deepcopy(value); wrong['turn_detection']['create_response']=True; invalid.append(wrong)
        wrong=copy.deepcopy(value); wrong['turn_detection']['interrupt_response']=True; invalid.append(wrong)
        wrong=copy.deepcopy(value); wrong['input_audio_transcription']=None; invalid.append(wrong)
        for tokens in (None,True,512,513,16384):
            wrong=copy.deepcopy(value); wrong['max_tokens']=tokens; invalid.append(wrong)
        for effective in invalid:
            with self.subTest(effective=effective),self.assertRaisesRegex(RuntimeError,'effective_config_unconfirmed'):
                await self.session.handle({'type':'session.updated','session':effective})
            self.assertFalse(self.session.ready.is_set())
        logged=[]
        self.session.log=logged.append
        value['turn_detection']['provider_payload']='doNotLogPayload'
        value['audio']['input']['format']['provider_payload']='doNotLogPayload'
        await self.session.handle({'type':'session.updated','session':value})
        self.assertTrue(self.session.ready.is_set())
        self.assertEqual(self.client[-1],{'type':'ready'})
        self.assertNotIn('doNotLogPayload',json.dumps(logged))

    async def test22_blocked_cloud_interrupt_has_a_deadline_and_does_not_resume(self):
        await self.turn()
        cancelled=asyncio.Event()
        budgets=[]
        original_timeout=asyncio.timeout
        async def blocked(receipt):
            self.runtime.stops.append(receipt.effective_land)
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        def bounded(seconds):
            budgets.append(seconds)
            return original_timeout(.03)
        self.runtime.finish_interrupt=blocked
        item={'type':'function_call','call_id':'LandCall','name':'interrupt',
              'arguments':json.dumps({'land':True})}
        with patch('providers.OmniSession.asyncio.timeout',new=bounded):
            await self.session.handle({'type':'response.done','response':{
                'id':'Response1','status':'completed','output':[item]}})
            await asyncio.wait_for(asyncio.gather(*tuple(self.session.tasks),return_exceptions=True),2)
        await asyncio.sleep(0)
        self.assertEqual(budgets,[3])
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(self.session.fault)
        self.assertFalse(self.session.closed)
        self.assertEqual(self.session.blocked_reason,'previous_stop_unconfirmed')
        self.assertTrue(self.session.land_requested)
        self.assertEqual(self.runtime.calls,[])

    async def test24_f11_during_cloud_notification_cannot_be_overridden_by_the_old_stop(self):
        device=types.SimpleNamespace(priority_interrupt=Mock(return_value={'ok':True}))
        follow=types.SimpleNamespace(stop=Mock(return_value=CommandResult(True,'completed')))
        runtime=UnifiedRuntime(device,follow)
        self.session.runtime=runtime
        self.runtime=runtime
        await self.turn()
        entered,release=asyncio.Event(),asyncio.Event()
        async def blocked_notification(value):
            if value.get('type')=='interrupted':
                entered.set()
                await release.wait()
            self.client.append(value)
        self.session.send_client=blocked_notification
        item={'type':'function_call','call_id':'StopCall','name':'interrupt',
              'arguments':json.dumps({'land':False})}
        await self.session.handle({'type':'response.done','response':{
            'id':'Response1','status':'completed','output':[item]}})
        await asyncio.wait_for(entered.wait(),2)
        await runtime.interrupt(True)  # Independent operator F11 while notification is blocked.
        release.set()
        await asyncio.wait_for(asyncio.gather(*tuple(self.session.tasks),return_exceptions=True),2)
        await asyncio.sleep(0)
        self.assertEqual([call.args[0] for call in device.priority_interrupt.call_args_list],[True])
        self.assertTrue(runtime.land_requested)


class LocalCloud:
    def __init__(self,runtime,config,instructions,tools,send,log,connect=None):
        self.runtime,self.send=runtime,send
        self.generation=runtime.generation
        self.fault=None
        self.closed=False
    def authority_current(self): return self.generation==self.runtime.generation
    async def run(self):
        await self.send({'type':'ready'})
        await asyncio.Event().wait()
    async def image(self,jpeg,timestamp): return False
    async def audio(self,pcm):
        if len(pcm)!=640: raise ValueError('omni_requires_20ms_pcm16_16k')
    async def close(self): self.closed=True


class OmniTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime=LocalRuntime()
        self.frame_id=0
        def frame():
            self.frame_id+=1
            return FrameResult(np.zeros((16,16,3),dtype=np.uint8),self.frame_id,time.time(),False)
        self.backend=types.SimpleNamespace(get_frame=frame)
        self.service=OmniService(self.runtime,self.backend,lambda e:None,CONFIG,'test',Tools.OMNI_TOOLS,seconds=5)
        self.factory=patch('providers.OmniRuntime.OmniSession',LocalCloud)
        self.factory.start()
        await self.service.start()

    async def asyncTearDown(self):
        result=await self.service.close()
        self.assertEqual(result['ok'],not getattr(self,'expect_cleanup_failure',False))
        self.factory.stop()

    async def until(self,ws,kind):
        async def receive():
            while True:
                value=json.loads(await ws.recv())
                if value.get('type')==kind: return value
        return await asyncio.wait_for(receive(),2)

    async def test_invalid_tool_returns_error_over_cloud_wire_without_changing_flight_state(self):
        with patch('providers.OmniRuntime.OmniSession',OmniSession):
            async with connect(self.service.ws_url,origin=self.service.url,proxy=None) as socket:
                await self.until(socket,'frame')
                cloud=self.service.cloud
                cloud.ws=CloudWire()
                for event in (
                    {'type':'input_audio_buffer.speech_started','item_id':'MoveInput'},
                    {'type':'input_audio_buffer.committed','item_id':'MoveInput'},
                    {'type':'conversation.item.input_audio_transcription.completed',
                     'item_id':'MoveInput','transcript':'飞到前方的桌子上方'}):
                    await cloud.handle(event)
                for _ in range(40):
                    if cloud.pending_response is not None: break
                    await asyncio.sleep(0)
                item={'type':'function_call','call_id':'BadCall','name':'run_program',
                      'arguments':'{"code": "import time\\n# 移动到'}
                await cloud.handle({'type':'response.created','response':{'id':'MoveResponse'}})
                await cloud.handle({**item,'type':'response.function_call_arguments.done',
                                    'response_id':'MoveResponse'})
                await cloud.handle({'type':'response.done','response':{
                    'id':'MoveResponse','status':'incomplete','output':[item]}})
                notice=await self.until(socket,'state')
                self.assertIn('错误已返回模型处理',notice['text'])
                self.assertFalse(self.service.finished.is_set())
                self.assertFalse(cloud.closed)
                self.assertIsNone(cloud.fault)
                self.assertEqual(self.runtime.calls,[])
                self.assertEqual(self.runtime.stops,[])
                self.assertFalse(cloud.land_requested)
                self.assertIn('Unterminated string',cloud.tool_outputs['BadCall']['debug'])
                for _ in range(40):
                    if cloud.pending_response is not None: break
                    await asyncio.sleep(0)
                self.assertEqual(cloud.pending_response,cloud.epoch)
                await socket.send(json.dumps({'type':'stop'}))
                with self.assertRaises(ConnectionClosed):
                    while True: await asyncio.wait_for(socket.recv(),2)
        await asyncio.wait_for(self.service.wait(),2)
        self.assertEqual(self.runtime.stops,[False])  # Explicit client stop only.
        self.assertFalse(self.service.land_requested)

    async def test11_http_session_exposes_only_ephemeral_loopback_authority(self):
        def request():
            with urlopen(self.service.url+'/api/session',timeout=2) as response:
                return response.read().decode()
        raw=await asyncio.to_thread(request)
        value=json.loads(raw)
        self.assertEqual(value['provider'],'alibaba')
        self.assertTrue(value['url'].startswith('ws://127.0.0.1:'))
        self.assertNotIn(CONFIG.api_key,raw)
        self.assertNotIn(CONFIG.workspace_id,raw)

    async def test_fixed_web_port_is_reused_with_new_session_authority(self):
        url=self.service.url
        port=self.service.http.server_port
        old_token=self.service.token
        def request():
            with urlopen(url+'/api/session',timeout=2) as response:
                return json.loads(response.read())
        self.assertIn(old_token,(await asyncio.to_thread(request))['url'])
        await self.service.close()
        replacement=OmniService(self.runtime,self.backend,lambda e:None,CONFIG,'test',
                                Tools.OMNI_TOOLS,seconds=5,http_port=port)
        try:
            self.assertEqual(await replacement.start(),url)
            self.assertNotEqual(replacement.token,old_token)
            session=await asyncio.to_thread(request)
            self.assertIn(replacement.token,session['url'])
            self.assertNotIn(old_token,session['url'])
        finally: await replacement.close()

    async def test_occupied_fixed_port_fails_without_dynamic_fallback_or_ws_leak(self):
        port=self.service.http.server_port
        duplicate=OmniService(self.runtime,self.backend,lambda e:None,CONFIG,'test',
                             Tools.OMNI_TOOLS,seconds=5,http_port=port)
        try:
            with self.assertRaisesRegex(RuntimeError,'不会自动换端口'):
                await duplicate.start()
        finally:
            if duplicate.http is not None: await duplicate.close()
        self.assertIsNone(duplicate.server)
        self.assertIsNone(duplicate.http)
        self.assertIsNone(duplicate.thread)
        self.assertFalse(self.service.closed)

    def test_production_web_port_default_and_service_validation(self):
        self.assertEqual(main.make_parser().parse_args([]).web_port,18848)
        self.assertEqual(main.make_parser().parse_args(['--web-port','8849']).web_port,8849)
        for invalid in (-1,65536,True,'18848'):
            with self.subTest(port=invalid),self.assertRaisesRegex(ValueError,'http_port'):
                OmniService(self.runtime,self.backend,lambda e:None,CONFIG,'test',
                            Tools.OMNI_TOOLS,http_port=invalid)

    async def test_primary_cloud_failure_survives_secondary_audio_send_failure(self):
        trigger,closing=asyncio.Event(),asyncio.Event()
        records=[]
        self.service.log=records.append
        class FailingCloud(LocalCloud):
            async def run(self):
                await self.send({'type':'ready'})
                await trigger.wait()
                self.fault=ValueError('original_tool_validation_failure')
                closing.set()
                await asyncio.Event().wait()  # Socket cleanup is still pending.
            async def audio(self,pcm):
                trigger.set()
                await closing.wait()
                raise RuntimeError('secondary_socket_already_closed')
        async def blocked_frames(): await asyncio.Event().wait()
        with patch('providers.OmniRuntime.OmniSession',FailingCloud),patch.object(self.service,'frames',blocked_frames):
            async with connect(self.service.ws_url,origin=self.service.url,proxy=None) as socket:
                await socket.send(json.dumps({'type':'start'}))
                await self.until(socket,'ready')
                await socket.send(json.dumps({'type':'audio','audio':base64.b64encode(bytes(640)).decode()}))
                await self.until(socket,'error')
        with self.assertRaisesRegex(RuntimeError,'session_failed'):
            await asyncio.wait_for(self.service.wait(),2)
        failure=next(e for e in records if e['event']=='omni_transport_failed')
        self.assertEqual(failure['stage'],'cloud_session')
        self.assertEqual(failure['error'],'original_tool_validation_failure')
        self.assertNotIn('secondary_socket',failure['traceback'])
        self.assertEqual(self.runtime.calls,[])
        self.assertEqual(self.runtime.stops,[False])

    async def test12_invalid_token_or_origin_cannot_acquire_control(self):
        for url,origin in ((self.service.ws_url.replace(self.service.token,'wrong'),self.service.url),
                           (self.service.ws_url,'https://untrusted.example')):
            async with connect(url,origin=origin,proxy=None) as socket:
                with self.assertRaises(ConnectionClosed): await socket.recv()
                self.assertEqual(socket.close_code,1008)
        self.assertIsNone(self.service.client)
        self.assertEqual(self.runtime.stops,[])

    async def test13_only_one_client_and_disconnect_stops_shared_runtime(self):
        async with connect(self.service.ws_url,origin=self.service.url,proxy=None) as first:
            await self.until(first,'frame')
            async with connect(self.service.ws_url,origin=self.service.url,proxy=None) as second:
                with self.assertRaises(ConnectionClosed): await second.recv()
                self.assertEqual(second.close_code,1008)
            await first.send(json.dumps({'type':'start'}))
            await self.until(first,'ready')
        await asyncio.wait_for(self.service.wait(),2)
        self.assertEqual(self.runtime.stops,[False])
        self.assertTrue(self.service.cloud.closed)

    async def test14_stale_sdk_frame_is_not_sent_and_voice_recovers_with_new_frames(self):
        self.backend.get_frame=lambda:FrameResult(np.zeros((16,16,3),dtype=np.uint8),1,time.time()-2,True)
        async with connect(self.service.ws_url,origin=self.service.url,proxy=None) as socket:
            await self.until(socket,'invalid_frame')
            await socket.send(json.dumps({'type':'start'}))
            await self.until(socket,'ready')
            self.assertFalse(self.service.finished.is_set())
            self.assertEqual(self.runtime.stops,[])
            self.backend.get_frame=lambda:FrameResult(np.zeros((16,16,3),dtype=np.uint8),2,time.time(),False)
            frame=await self.until(socket,'frame')
            self.assertEqual(frame['id'],2)

    async def test15_user_interrupt_revokes_before_slow_cloud_cleanup(self):
        async with connect(self.service.ws_url,origin=self.service.url,proxy=None) as socket:
            await self.until(socket,'frame')
            await socket.send(json.dumps({'type':'interrupt','land':True}))
            await self.until(socket,'interrupted')
        await asyncio.wait_for(self.service.wait(),2)
        self.assertIn(True,self.runtime.stops)
        self.assertTrue(self.runtime.stops[-1])  # Cleanup must preserve a land request.
        self.assertGreater(self.runtime.generation,0)

    async def test_client_playback_stop_reason_is_bounded_redacted_and_logged_before_cleanup(self):
        records=[]
        cleaner=main.SessionLog.__new__(main.SessionLog)
        cleaner.secrets=(CONFIG.api_key,)
        self.service.log=lambda event:records.append(cleaner.clean(event))
        async with connect(self.service.ws_url,origin=self.service.url,proxy=None) as socket:
            await self.until(socket,'frame')
            await socket.send(json.dumps({'type':'stop','source':'client_error',
                'reason':'耳机播放积压 '+CONFIG.api_key+'x'*600,
                'diagnostics':{'scheduled_audio_seconds':5.5,'pending_audio_seconds':4,
                               'frame_age_ms':float('inf'),'audio_state':'running',
                               'headers':{'Authorization':CONFIG.api_key}}}))
            with self.assertRaises(ConnectionClosed):
                while True: await socket.recv()
        await asyncio.wait_for(self.service.wait(),2)
        report=next(event for event in records if event['event']=='omni_client_stop')
        self.assertEqual(report['source'],'client_error')
        self.assertLessEqual(len(report['reason']),512)
        self.assertNotIn(CONFIG.api_key,json.dumps(report))
        self.assertEqual(report['diagnostics'],{
            'scheduled_audio_seconds':5.5,'pending_audio_seconds':4,'audio_state':'running'})
        self.assertEqual(self.runtime.stops,[False])

    async def test16_cloud_close_failure_still_closes_local_session_and_surfaces_cleanup(self):
        async with connect(self.service.ws_url,origin=self.service.url,proxy=None) as socket:
            await self.until(socket,'frame')
            self.service.cloud.close=AsyncMock(side_effect=RuntimeError('cloud_cleanup_failed'))
            await socket.send(json.dumps({'type':'stop'}))
            with self.assertRaises(ConnectionClosed):
                while True: await socket.recv()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(self.service.wait(),2)
        self.assertIsNone(self.service.client)
        self.assertTrue(self.service.finished.is_set())
        self.assertTrue(self.service.cleanup_pending)
        self.service.cloud.close=AsyncMock()
        self.expect_cleanup_failure=True

    async def test_receiver_that_consumes_cancellation_is_woken_by_socket_close(self):
        entered=asyncio.Event()
        consumed=asyncio.Event()
        class RacingCloud(LocalCloud):
            async def audio(self,pcm):
                entered.set()
                try: await asyncio.Event().wait()
                except asyncio.CancelledError: consumed.set()
        def broken_frame(): raise RuntimeError('fixture_frame_failure')
        with patch('providers.OmniRuntime.OmniSession',RacingCloud):
            async with connect(self.service.ws_url,origin=self.service.url,proxy=None) as socket:
                await socket.send(json.dumps({'type':'start'}))
                await self.until(socket,'ready')
                await socket.send(json.dumps({'type':'audio','audio':base64.b64encode(bytes(640)).decode()}))
                await asyncio.wait_for(entered.wait(),2)
                self.backend.get_frame=broken_frame
                await self.until(socket,'invalid_frame')
                self.assertFalse(self.service.finished.is_set())
                self.assertEqual(self.runtime.stops,[])
                self.service.cloud.fault=RuntimeError('fixture_cloud_failure')
                await self.until(socket,'error')
                with self.assertRaises(ConnectionClosed):
                    while True: await asyncio.wait_for(socket.recv(),2)
            with self.assertRaisesRegex(RuntimeError,'session_failed'):
                await asyncio.wait_for(self.service.wait(),2)
        self.assertTrue(consumed.is_set())
        self.assertTrue(self.service.cloud.closed)
        self.assertIsNone(self.service.client)
        self.assertEqual(self.runtime.stops,[False])

    async def test20_repeated_source_frame_cannot_be_refreshed_by_a_new_timestamp(self):
        calls=0
        image=np.zeros((16,16,3),dtype=np.uint8)
        def relabelled_frame():
            nonlocal calls
            calls+=1
            return FrameResult(image,7,time.time(),False)
        self.backend.get_frame=relabelled_frame
        async with connect(self.service.ws_url,origin=self.service.url,proxy=None) as socket:
            await self.until(socket,'frame')
            await self.until(socket,'invalid_frame')
            self.assertFalse(self.service.finished.is_set())
            self.assertEqual(self.runtime.stops,[])
        await asyncio.wait_for(self.service.wait(),2)
        self.assertGreaterEqual(calls,2)
        self.assertEqual(self.runtime.stops,[False])
        self.assertIsNone(self.service.client)
        self.assertTrue(self.service.finished.is_set())

    async def test21_cloud_tool_land_is_preserved_through_actual_local_transport_cleanup(self):
        with patch('providers.OmniRuntime.OmniSession',OmniSession):
            async with connect(self.service.ws_url,origin=self.service.url,proxy=None) as socket:
                await self.until(socket,'frame')
                cloud=self.service.cloud
                cloud.ws=CloudWire()  # Recorded cloud writes, no provider connection.
                for event in (
                    {'type':'input_audio_buffer.speech_started','item_id':'LandInput'},
                    {'type':'input_audio_buffer.committed','item_id':'LandInput'},
                    {'type':'conversation.item.input_audio_transcription.completed',
                     'item_id':'LandInput','transcript':'请降落'}):
                    await cloud.handle(event)
                for _ in range(40):
                    if cloud.pending_response is not None: break
                    await asyncio.sleep(0)
                for event in (
                    {'type':'response.created','response':{'id':'LandResponse'}},
                    {'type':'response.done','response':{'id':'LandResponse','status':'completed',
                     'output':[{'type':'function_call','call_id':'LandCall','name':'interrupt',
                                'arguments':json.dumps({'land':True})}]}}):
                    await cloud.handle(event)
                for _ in range(80):
                    if self.runtime.stops: break
                    await asyncio.sleep(0)
                self.assertEqual(self.runtime.stops,[True])
                self.assertFalse(cloud.closed)
                self.assertFalse(self.service.finished.is_set())
                await socket.send(json.dumps({'type':'stop'}))
                with self.assertRaises(ConnectionClosed):
                    while True: await asyncio.wait_for(socket.recv(),2)
        await asyncio.wait_for(self.service.wait(),2)
        self.assertTrue(cloud.closed)
        self.assertTrue(cloud.land_requested)
        self.assertTrue(self.service.land_requested)
        self.assertEqual(self.runtime.stops,[True,True])
        self.assertEqual(self.runtime.calls,[])
        self.assertIsNone(self.service.client)

    async def test23_blocked_user_land_interrupt_times_out_and_cleanup_keeps_land(self):
        budgets=[]
        cancellations=[]
        original_wait_for=asyncio.wait_for
        async def blocked(land):
            self.runtime.cancel()
            self.runtime.stops.append(bool(land))
            try: await asyncio.Event().wait()
            finally: cancellations.append(bool(land))
        def bounded(awaitable,seconds):
            if getattr(getattr(awaitable,'cr_code',None),'co_name',None)=='blocked':
                budgets.append(seconds)
                return original_wait_for(awaitable,.03)
            return original_wait_for(awaitable,seconds)
        self.runtime.interrupt=blocked
        with patch('providers.OmniRuntime.asyncio.wait_for',new=bounded):
            async with connect(self.service.ws_url,origin=self.service.url,proxy=None) as socket:
                await self.until(socket,'frame')
                await socket.send(json.dumps({'type':'interrupt','land':True}))
                await self.until(socket,'error')
            with self.assertRaisesRegex(RuntimeError,'session_failed'):
                await original_wait_for(self.service.wait(),2)
        self.assertEqual(budgets,[3,3])
        self.assertEqual(cancellations,[True,True])
        self.assertEqual(self.runtime.stops,[True,True])
        self.assertTrue(self.service.cleanup_pending)
        self.assertTrue(self.service.finished.is_set())
        self.expect_cleanup_failure=True
