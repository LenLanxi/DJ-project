"""Startup-only routing and credential boundaries. No cloud or aircraft access."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[2]
from app import Application as main
from app import Tools
from app.Results import close_result
from providers.AlibabaRealtime import MODEL
from providers.VoiceRoutes import ARK_URL, DOUBAO_FIELDS, load_voice_route
from providers.RTCRuntime import RTCProcess, validate_environment


def credentials(folder, *, free=True):
    ali = Path(folder)/'Alibaba.json'
    dou = Path(folder)/'Doubao.json'
    ali.write_text(json.dumps({'Region':'cn-beijing', 'ApiKey':'privateAlibabaSecretValue',
        'WorkspaceId':'workspace1', 'WebSocketUrl':'wss://workspace1.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime',
        'Model':MODEL, 'StopWhenFreeQuotaExhausted':free, 'FreeQuotaTokens':1000000,
        'FreeQuotaExpires':'2099-01-01T00:00:00Z'}), encoding='utf-8')
    dou.write_text(json.dumps({'RTC':{'AppId':'testApp','AppKey':'privateRtcKeyValue'},
        'OpenAPI':{'AccessKeyId':'privateOpenIdValue','SecretAccessKey':'privateOpenSecretValue'},
        'Speech':{'AppId':'testSpeech','AccessToken':'privateSpeechTokenValue'},
        'Ark':{'ModelName':'doubao-seed-2-1-lite-260915','APIKey':'privateArkKeyValue'}}), encoding='utf-8')
    return ali, dou


class VoiceRoutesTests(unittest.IsolatedAsyncioTestCase):

    def test02_explicit_file_selection_overrides_unrelated_global_models_without_mutating_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            ali, dou = credentials(folder)
            with patch.dict(os.environ, {'AI_API_KEY':'wrongGlobalKey','AI_MODEL':'wrongModel',
                                         'AI_CHAT_URL':'https://unrelated.example/chat'}):
                before = dict(os.environ)
                route = load_voice_route('alibaba', alibaba_credentials=str(ali), doubao_credentials=str(dou))
                client = route.vision_client(lambda event:None)
                self.assertEqual(client.config.api_key, 'privateAlibabaSecretValue')
                self.assertEqual(client.model, MODEL)
                self.assertEqual(client.url, route.config.url)
                doubao = load_voice_route('doubao', doubao_credentials=str(dou))
                doubao_client = doubao.vision_client(lambda event: None)
                self.assertEqual(doubao_client.key, 'privateArkKeyValue')
                self.assertEqual(doubao_client.url, ARK_URL)
                self.assertEqual(dict(os.environ), before)
            self.assertNotIn('private', repr(route))
            self.assertNotIn('private', repr(route.config))


    async def test04_missing_alibaba_file_never_connects_aircraft_or_starts_docker(self):
        args = main.make_parser().parse_args(['voice','--real-drone','--alibaba-credentials','MissingAlibaba.json'])
        with patch.object(main.DeviceSession,'open',new=AsyncMock()) as hardware, \
             patch('execution.sandbox_runner.SandboxRunner.check',new=AsyncMock()) as docker:
            with self.assertRaisesRegex(ValueError,'voice-provider doubao'):
                await main.Session(args,lambda event:None).run()
            hardware.assert_not_awaited()
            docker.assert_not_awaited()

    async def test05_missing_ark_blocks_doubao_before_hardware_but_alibaba_is_independent(self):
        with tempfile.TemporaryDirectory() as folder:
            ali, dou = credentials(folder)
            value = json.loads(dou.read_text(encoding='utf-8'))
            value['Ark'].pop('APIKey')
            dou.write_text(json.dumps(value), encoding='utf-8')
            alibaba = load_voice_route('alibaba', alibaba_credentials=str(ali),
                                      doubao_credentials=str(dou))
            self.assertEqual(alibaba.vision_client(None).config.api_key, 'privateAlibabaSecretValue')
            for provider in ('doubao',):
                args = main.make_parser().parse_args(['voice','--real-drone','--voice-provider',provider,
                    '--alibaba-credentials',str(ali),'--doubao-credentials',str(dou)])
                with patch.object(main.DeviceSession,'open',new=AsyncMock()) as hardware:
                    with self.assertRaisesRegex(ValueError,'RTC_VLM_API_KEY'):
                        await main.Session(args,lambda event:None).run()
                    hardware.assert_not_awaited()

    def test06_file_keys_are_in_memory_redacted_before_use_and_never_browser_schema_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            ali, dou = credentials(folder)
            log = main.SessionLog(Path(folder)/'Session.jsonl')
            try:
                route = load_voice_route('alibaba', alibaba_credentials=str(ali), doubao_credentials=str(dou),log=log)
                doubao = load_voice_route('doubao', doubao_credentials=str(dou), log=log)
                log({'event':'test','error':route.config.api_key+' '+doubao.vision_key})
                self.assertNotIn(route.config.api_key, log.clean(route.config.api_key))
                self.assertNotIn(doubao.vision_key, log.clean(doubao.vision_key))
            finally: log.close()
            self.assertNotIn('private', (Path(folder)/'Session.jsonl').read_text(encoding='utf-8'))
            schema = json.dumps(Tools.OMNI_TOOLS)+json.dumps(Tools.RTC_TOOLS)
            self.assertNotIn('private',schema)


    async def test08_doubao_spawn_has_keys_only_in_child_environment_not_argv_or_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            _, dou = credentials(folder)
            route = load_voice_route('doubao',doubao_credentials=str(dou))
            log = main.SessionLog(Path(folder)/'Session.jsonl')
            process = RTCProcess(log, env=route.rtc_environment,node='node',web_port=18848)
            before = dict(os.environ)
            child = types.SimpleNamespace(returncode=None,pid=1)
            async def spawn(*args,**kwargs):
                self.assertEqual(args[0], 'node')
                self.assertEqual(kwargs['env']['RTC_PORT'],'18848')
                self.assertNotIn('private',json.dumps(args))
                self.assertEqual(kwargs['env']['RTC_APP_KEY'],'privateRtcKeyValue')
                self.assertEqual(kwargs['env']['RTC_VLM_API_KEY'],'privateArkKeyValue')
                process.output.write_text(json.dumps({'event':'rtc_listening','url':'http://127.0.0.1:1'}),encoding='utf-8')
                return child
            try:
                with patch('providers.RTCRuntime.validate_environment',return_value='node'), \
                     patch('providers.RTCRuntime.asyncio.create_subprocess_exec',side_effect=spawn), \
                     patch.object(process,'request',return_value={'configured':True}):
                    await process.start(types.SimpleNamespace(url='ws://127.0.0.1:9',token='ephemeral'),180,
                                        Tools.RTC_INSTRUCTIONS,Tools.RTC_TOOLS)
                self.assertEqual(dict(os.environ), before)
            finally: log.close()

    def test09_doubao_validation_uses_explicit_env_not_global_secret_fallback(self):
        with patch.dict(os.environ, {name:'unrelatedAmbientValue' for name in DOUBAO_FIELDS}):
            with self.assertRaisesRegex(ValueError,'缺少配置'):
                validate_environment(180,{})

    async def test10_alibaba_uses_shared_gateway_and_omni_without_starting_rtc_process(self):
        with tempfile.TemporaryDirectory() as folder:
            ali, dou = credentials(folder)
            args = main.make_parser().parse_args(['voice','--real-drone','--alibaba-credentials',str(ali),
                                                  '--doubao-credentials',str(dou)])
            events=[]
            order=[]
            log=main.SessionLog(Path(folder)/'Session.jsonl')
            class Service:
                def __init__(self,runtime,backend,logger,config,instructions,tools,*,seconds,http_port):
                    self.http_port=http_port
                    self.runtime,self.device,self.deadline=runtime,backend,time.time()+seconds
                    self.close=AsyncMock(side_effect=lambda:order.append('media-close') or close_result())
                    self.revoke=Mock(side_effect=lambda land:order.append('revoke'))
                    events.append(self)
                async def start(self): return 'http://127.0.0.1:1'
                async def wait(self): return
            async def controls(*args): await asyncio.Event().wait()
            try:
                with patch.dict(sys.modules,{'providers.OmniRuntime':types.SimpleNamespace(OmniService=Service)}), \
                     patch.object(main.DeviceSession,'open',new=AsyncMock(return_value=types.SimpleNamespace(simulated=False))), \
                     patch.object(main,'operator_controls',side_effect=controls), \
                     patch.object(main.DeviceSession,'close',new=AsyncMock(side_effect=lambda *args:order.append('aircraft-close'))) as shutdown, \
                     patch('execution.sandbox_runner.SandboxRunner.check',new=AsyncMock()), \
                     patch('providers.RTCRuntime.RTCProcess') as rtc:
                    self.assertEqual(await main.Session(args,log).run(),0)
                self.assertEqual(len(events),1)
                self.assertEqual(events[0].http_port,18848)
                self.assertIs(events[0].runtime.device,events[0].device)
                self.assertTrue(events[0].runtime.closed)
                events[0].close.assert_awaited_once()
                events[0].revoke.assert_called_once_with(True)
                shutdown.assert_awaited_once()
                self.assertEqual(order,['revoke','media-close','aircraft-close'])
                rtc.assert_not_called()
            finally: log.close()



    async def test13_active_provider_failure_does_not_instantiate_fallback_or_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            ali, dou = credentials(folder)
            args=main.make_parser().parse_args(['voice','--real-drone','--alibaba-credentials',str(ali),
                                               '--doubao-credentials',str(dou)])
            log=main.SessionLog(Path(folder)/'Session.jsonl')
            closed=AsyncMock(side_effect=RuntimeError('cleanup_unconfirmed'))
            class Service:
                def __init__(self,*args,**kwargs): self.close,self.revoke=closed,Mock()
                async def start(self): raise RuntimeError('cloud_start_failed')
            async def controls(*args): await asyncio.Event().wait()
            try:
                with patch.dict(sys.modules,{'providers.OmniRuntime':types.SimpleNamespace(OmniService=Service)}), \
                     patch.object(main.DeviceSession,'open',new=AsyncMock(return_value=types.SimpleNamespace(simulated=False))), \
                     patch.object(main,'operator_controls',side_effect=controls), \
                     patch.object(main.DeviceSession,'close',new=AsyncMock()) as shutdown, \
                     patch('execution.sandbox_runner.SandboxRunner.check',new=AsyncMock()), \
                     patch('providers.RTCRuntime.RTCProcess') as rtc:
                    with self.assertRaisesRegex(RuntimeError,'cloud_start_failed'):
                        await main.Session(args,log).run()
                closed.assert_awaited_once()
                shutdown.assert_awaited_once()
                rtc.assert_not_called()
            finally: log.close()

    async def test14_explicit_doubao_uses_rtc_bridge_and_shared_gateway(self):
        with tempfile.TemporaryDirectory() as folder:
            _, dou = credentials(folder)
            args=main.make_parser().parse_args(['voice','--real-drone','--voice-provider','doubao',
                                               '--doubao-credentials',str(dou)])
            log=main.SessionLog(Path(folder)/'Session.jsonl')
            bridges,services=[],[]
            class Bridge:
                def __init__(self,runtime,*args,**kwargs):
                    self.device=args[0]
                    self.runtime,self.token,self.url=runtime,'ephemeralBridgeToken','ws://127.0.0.1:9'
                    self.close=AsyncMock(return_value=close_result())
                    bridges.append(self)
                async def start(self): return
            class Service:
                def __init__(self,logger,*,env,node,web_port):
                    self.web_port,self.node=web_port,node
                    self.env,self.close,self.deadline=dict(env),AsyncMock(return_value=close_result()),time.time()+180
                    services.append(self)
                async def start(self,*args): return 'http://127.0.0.1:1'
                async def wait(self): return
            async def controls(*args): await asyncio.Event().wait()
            try:
                with patch('providers.RTCBridge.RTCBridge',Bridge),patch('providers.RTCRuntime.RTCProcess',Service), \
                     patch('providers.RTCRuntime.validate_environment',return_value='node'), \
                     patch.object(main.DeviceSession,'open',new=AsyncMock(return_value=types.SimpleNamespace(simulated=False))), \
                     patch.object(main,'operator_controls',side_effect=controls), \
                     patch.object(main.DeviceSession,'close',new=AsyncMock()), \
                     patch('execution.sandbox_runner.SandboxRunner.check',new=AsyncMock()):
                    self.assertEqual(await main.Session(args,log).run(),0)
                self.assertEqual(len(bridges),1)
                self.assertIs(bridges[0].runtime.device,bridges[0].device)
                self.assertEqual(services[0].env['RTC_APP_KEY'],'privateRtcKeyValue')
                self.assertEqual(services[0].web_port,18848)
                self.assertEqual(services[0].node,'node')
                bridges[0].close.assert_awaited_once()
                services[0].close.assert_awaited_once()
            finally: log.close()
