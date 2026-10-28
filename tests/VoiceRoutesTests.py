"""Startup-only routing and credential boundaries. No cloud or aircraft access."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'modules'/'ai'))
import main
from AlibabaRealtime import AlibabaConfig, AlibabaError, MODEL
from VoiceRoutes import ARK_URL, DOUBAO_FIELDS, load_voice_route
from RTCRuntime import RTCProcess, validate_environment


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
    def test01_default_and_explicit_alternative_are_only_startup_choices(self):
        args = main.make_parser().parse_args([])
        self.assertEqual(args.voice_provider, 'alibaba')
        self.assertEqual(args.api_budget_rmb, 0)
        self.assertEqual(main.make_parser().parse_args(['--voice-provider','doubao']).voice_provider, 'doubao')
        self.assertEqual(main.OMNI_TOOLS[0]['parameters']['properties']['code']['maxLength'], 60000)
        self.assertEqual(main.RTC_TOOLS[0]['parameters']['properties']['code']['maxLength'], 600)
        self.assertNotIn('1KB', main.OMNI_INSTRUCTIONS)

    def test02_explicit_file_selection_overrides_unrelated_global_models_without_mutating_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            ali, dou = credentials(folder)
            with patch.dict(os.environ, {'AI_API_KEY':'wrongGlobalKey','AI_MODEL':'wrongModel',
                                         'AI_CHAT_URL':'https://unrelated.example/chat'}):
                before = dict(os.environ)
                route = load_voice_route('alibaba', alibaba_credentials=str(ali), doubao_credentials=str(dou),paid_budget_rmb=15)
                client = route.vision_client(lambda event:None)
                self.assertEqual(client.config.api_key, 'privateAlibabaSecretValue')
                self.assertEqual(client.model, MODEL)
                self.assertEqual(client.url, route.config.validate())
                doubao = load_voice_route('doubao', doubao_credentials=str(dou), paid_budget_rmb=15)
                doubao_client = doubao.vision_client(lambda event: None)
                self.assertEqual(doubao_client.key, 'privateArkKeyValue')
                self.assertEqual(doubao_client.url, ARK_URL)
                self.assertEqual(dict(os.environ), before)
            self.assertNotIn('private', repr(route))
            self.assertNotIn('private', repr(route.config))

    def test03_environment_contains_file_paths_not_a_requirement_to_export_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            ali, dou = credentials(folder)
            with patch.dict(os.environ, {'ALIBABA_CREDENTIALS_FILE':str(ali), 'DOUBAO_CREDENTIALS_FILE':str(dou)}):
                route = load_voice_route('alibaba',paid_budget_rmb=15)
                self.assertEqual(route.config.model, MODEL)
                args = main.make_parser().parse_args([])
                self.assertEqual(args.alibaba_credentials, str(ali))

    async def test04_missing_alibaba_file_never_connects_aircraft_or_starts_docker(self):
        args = main.make_parser().parse_args(['--real-drone','--alibaba-credentials','MissingAlibaba.json','--api-budget-rmb','15'])
        with patch.object(main,'initialize_real',new=AsyncMock()) as hardware, \
             patch('sandbox_runner.SandboxRunner.check',new=AsyncMock()) as docker:
            with self.assertRaisesRegex(ValueError,'voice-provider doubao'):
                await main.run(args,lambda event:None)
            hardware.assert_not_awaited()
            docker.assert_not_awaited()

    async def test05_missing_ark_blocks_doubao_before_hardware_but_alibaba_is_independent(self):
        with tempfile.TemporaryDirectory() as folder:
            ali, dou = credentials(folder)
            value = json.loads(dou.read_text(encoding='utf-8'))
            value['Ark'].pop('APIKey')
            dou.write_text(json.dumps(value), encoding='utf-8')
            alibaba = load_voice_route('alibaba', alibaba_credentials=str(ali),
                                      doubao_credentials=str(dou), paid_budget_rmb=15)
            self.assertEqual(alibaba.vision_client(None).config.api_key, 'privateAlibabaSecretValue')
            for provider in ('doubao',):
                args = main.make_parser().parse_args(['--real-drone','--voice-provider',provider,
                    '--alibaba-credentials',str(ali),'--doubao-credentials',str(dou),'--api-budget-rmb','15'])
                with patch.object(main,'initialize_real',new=AsyncMock()) as hardware:
                    with self.assertRaisesRegex(ValueError,'RTC_VLM_API_KEY'):
                        await main.run(args,lambda event:None)
                    hardware.assert_not_awaited()

    def test06_file_keys_are_in_memory_redacted_before_use_and_never_browser_schema_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            ali, dou = credentials(folder)
            log = main.SessionLog(Path(folder)/'Session.jsonl')
            try:
                route = load_voice_route('alibaba', alibaba_credentials=str(ali), doubao_credentials=str(dou),paid_budget_rmb=15,log=log)
                doubao = load_voice_route('doubao', doubao_credentials=str(dou), paid_budget_rmb=15, log=log)
                log({'event':'test','error':route.config.api_key+' '+doubao.vision_key})
                self.assertNotIn(route.config.api_key, log.clean(route.config.api_key))
                self.assertNotIn(doubao.vision_key, log.clean(doubao.vision_key))
            finally: log.close()
            self.assertNotIn('private', (Path(folder)/'Session.jsonl').read_text(encoding='utf-8'))
            schema = json.dumps(main.OMNI_TOOLS)+json.dumps(main.RTC_TOOLS)
            self.assertNotIn('private',schema)

    def test07_doubao_maps_its_own_file_and_does_not_require_alibaba(self):
        with tempfile.TemporaryDirectory() as folder:
            _, dou = credentials(folder)
            route = load_voice_route('doubao', alibaba_credentials='DoesNotExist.json', doubao_credentials=str(dou),paid_budget_rmb=15)
            self.assertIsNone(route.config)
            self.assertEqual(set(route.rtc_environment), set(DOUBAO_FIELDS))
            self.assertEqual(route.rtc_environment['RTC_APP_ID'],'testApp')
            self.assertEqual(route.rtc_environment['VOLC_S2S_TOKEN'],'privateSpeechTokenValue')
            self.assertEqual(route.code_limit,600)
            with self.assertRaises(TypeError): route.rtc_environment['RTC_APP_ID'] = 'different'

    async def test08_doubao_spawn_has_keys_only_in_child_environment_not_argv_or_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            _, dou = credentials(folder)
            route = load_voice_route('doubao',doubao_credentials=str(dou),paid_budget_rmb=15)
            log = main.SessionLog(Path(folder)/'Session.jsonl')
            process = RTCProcess(log, env=route.rtc_environment,web_port=18848)
            before = dict(os.environ)
            child = types.SimpleNamespace(returncode=None,pid=1)
            async def spawn(*args,**kwargs):
                self.assertEqual(kwargs['env']['RTC_PORT'],'18848')
                self.assertNotIn('private',json.dumps(args))
                self.assertEqual(kwargs['env']['RTC_APP_KEY'],'privateRtcKeyValue')
                self.assertEqual(kwargs['env']['RTC_VLM_API_KEY'],'privateArkKeyValue')
                process.output.write_text(json.dumps({'event':'rtc_listening','url':'http://127.0.0.1:1'}),encoding='utf-8')
                return child
            try:
                with patch('RTCRuntime.validate_environment',return_value='node'), \
                     patch('RTCRuntime.asyncio.create_subprocess_exec',side_effect=spawn), \
                     patch.object(process,'request',return_value={'configured':True}):
                    await process.start(types.SimpleNamespace(url='ws://127.0.0.1:9',token='ephemeral'),180,
                                        main.RTC_INSTRUCTIONS,main.RTC_TOOLS)
                self.assertEqual(dict(os.environ), before)
            finally: log.close()

    def test09_doubao_validation_uses_explicit_env_not_global_secret_fallback(self):
        with patch.dict(os.environ, {name:'unrelatedAmbientValue' for name in DOUBAO_FIELDS}):
            with self.assertRaisesRegex(ValueError,'缺少配置'):
                validate_environment(180,{})

    async def test10_alibaba_uses_shared_gateway_and_omni_without_starting_rtc_process(self):
        with tempfile.TemporaryDirectory() as folder:
            ali, dou = credentials(folder)
            args = main.make_parser().parse_args(['--real-drone','--alibaba-credentials',str(ali),
                                                  '--doubao-credentials',str(dou),'--api-budget-rmb','15'])
            events=[]
            order=[]
            log=main.SessionLog(Path(folder)/'Session.jsonl')
            class Service:
                def __init__(self,runtime,backend,logger,config,instructions,tools,*,seconds,http_port):
                    self.http_port=http_port
                    self.runtime,self.deadline=runtime,time.time()+seconds
                    self.close=AsyncMock(side_effect=lambda:order.append('media-close'))
                    self.revoke=Mock(side_effect=lambda land:order.append('revoke'))
                    events.append(self)
                async def start(self): return 'http://127.0.0.1:1'
                async def wait(self): return
            async def controls(*args): await asyncio.Event().wait()
            try:
                with patch.dict(sys.modules,{'OmniRuntime':types.SimpleNamespace(OmniService=Service)}), \
                     patch.object(main,'initialize_real',new=AsyncMock(return_value=types.SimpleNamespace(simulated=False))), \
                     patch.object(main,'operator_controls',side_effect=controls), \
                     patch.object(main,'close_real',new=AsyncMock(side_effect=lambda *args:order.append('aircraft-close'))) as shutdown, \
                     patch('sandbox_runner.SandboxRunner.check',new=AsyncMock()), \
                     patch('RTCRuntime.RTCProcess') as rtc:
                    self.assertEqual(await main.run(args,log),0)
                self.assertEqual(len(events),1)
                self.assertEqual(events[0].http_port,18848)
                self.assertIs(events[0].runtime.gateway,events[0].runtime.runner.gateway)
                self.assertTrue(events[0].runtime.closed)
                events[0].close.assert_awaited_once()
                events[0].revoke.assert_called_once_with(True)
                shutdown.assert_awaited_once()
                self.assertEqual(order,['revoke','media-close','aircraft-close'])
                rtc.assert_not_called()
            finally: log.close()

    def test11_paid_is_explicit_and_never_weakens_a_free_only_file(self):
        with tempfile.TemporaryDirectory() as folder:
            ali, dou = credentials(folder,free=False)
            with self.assertRaisesRegex(AlibabaError,'free_quota_guard'):
                AlibabaConfig.from_credentials(ali)
            config = AlibabaConfig.from_credentials(ali,paid_budget_rmb=15)
            self.assertFalse(config.free_tier_only)
            self.assertEqual(config.paid_budget_rmb,15)
            for bad in (-1,15.01,float('nan'),float('inf'),True):
                with self.subTest(budget=bad), self.assertRaises(AlibabaError):
                    AlibabaConfig.from_credentials(ali,paid_budget_rmb=bad)
            free = replace(config,free_tier_only=True,free_quota_expires='2020-01-01T00:00:00Z')
            with self.assertRaises(AlibabaError): free.validate()

    def test12_launcher_dryrun_only_exposes_paths_and_explicit_startup_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            ali, dou = credentials(folder)
            command = ['pwsh','-NoProfile','-File',str(ROOT/'start_flight.ps1'),'-DryRun',
                       '-AlibabaCredentials',str(ali),'-DoubaoCredentials',str(dou)]
            result = subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('--voice-provider alibaba',result.stdout)
            self.assertIn('--api-budget-rmb 15',result.stdout)
            self.assertIn('--web-port 18848',result.stdout)
            self.assertIn(str(ali),result.stdout)
            self.assertNotIn('private',result.stdout+result.stderr)
            result = subprocess.run(command+['-VoiceProvider','doubao','-WebPort','8849'],cwd=ROOT,capture_output=True,
                                    text=True,encoding='utf-8',timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('--voice-provider doubao',result.stdout)
            self.assertIn('--web-port 8849',result.stdout)

    async def test13_active_provider_failure_does_not_instantiate_fallback_or_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            ali, dou = credentials(folder)
            args=main.make_parser().parse_args(['--real-drone','--alibaba-credentials',str(ali),
                                               '--doubao-credentials',str(dou),'--api-budget-rmb','15'])
            log=main.SessionLog(Path(folder)/'Session.jsonl')
            closed=AsyncMock(side_effect=RuntimeError('cleanup_unconfirmed'))
            class Service:
                def __init__(self,*args,**kwargs): self.close,self.revoke=closed,Mock()
                async def start(self): raise RuntimeError('cloud_start_failed')
            async def controls(*args): await asyncio.Event().wait()
            try:
                with patch.dict(sys.modules,{'OmniRuntime':types.SimpleNamespace(OmniService=Service)}), \
                     patch.object(main,'initialize_real',new=AsyncMock(return_value=types.SimpleNamespace(simulated=False))), \
                     patch.object(main,'operator_controls',side_effect=controls), \
                     patch.object(main,'close_real',new=AsyncMock()) as shutdown, \
                     patch('sandbox_runner.SandboxRunner.check',new=AsyncMock()), \
                     patch('RTCRuntime.RTCProcess') as rtc:
                    with self.assertRaisesRegex(RuntimeError,'cloud_start_failed'):
                        await main.run(args,log)
                closed.assert_awaited_once()
                shutdown.assert_awaited_once()
                rtc.assert_not_called()
            finally: log.close()

    async def test14_explicit_doubao_uses_rtc_bridge_and_shared_gateway(self):
        with tempfile.TemporaryDirectory() as folder:
            _, dou = credentials(folder)
            args=main.make_parser().parse_args(['--real-drone','--voice-provider','doubao',
                                               '--doubao-credentials',str(dou),'--api-budget-rmb','15'])
            log=main.SessionLog(Path(folder)/'Session.jsonl')
            bridges,services=[],[]
            class Bridge:
                def __init__(self,runtime,*args,**kwargs):
                    self.runtime,self.token,self.url=runtime,'ephemeralBridgeToken','ws://127.0.0.1:9'
                    self.close=AsyncMock()
                    bridges.append(self)
                async def start(self): return
            class Service:
                def __init__(self,logger,*,env,web_port):
                    self.web_port=web_port
                    self.env,self.close,self.deadline=dict(env),AsyncMock(),time.time()+180
                    services.append(self)
                async def start(self,*args): return 'http://127.0.0.1:1'
                async def wait(self): return
            async def controls(*args): await asyncio.Event().wait()
            try:
                with patch('RTCBridge.RTCBridge',Bridge),patch('RTCRuntime.RTCProcess',Service), \
                     patch('RTCRuntime.validate_environment',return_value='node'), \
                     patch.object(main,'initialize_real',new=AsyncMock(return_value=types.SimpleNamespace(simulated=False))), \
                     patch.object(main,'operator_controls',side_effect=controls), \
                     patch.object(main,'close_real',new=AsyncMock()), \
                     patch('sandbox_runner.SandboxRunner.check',new=AsyncMock()):
                    self.assertEqual(await main.run(args,log),0)
                self.assertEqual(len(bridges),1)
                self.assertIs(bridges[0].runtime.gateway,bridges[0].runtime.runner.gateway)
                self.assertEqual(services[0].env['RTC_APP_KEY'],'privateRtcKeyValue')
                self.assertEqual(services[0].web_port,18848)
                bridges[0].close.assert_awaited_once()
                services[0].close.assert_awaited_once()
            finally: log.close()

    async def test15_zero_budget_rejects_both_routes_before_any_hardware_or_cloud(self):
        with tempfile.TemporaryDirectory() as folder:
            ali, dou=credentials(folder)
            for provider in ('alibaba','doubao'):
                args=main.make_parser().parse_args(['--real-drone','--voice-provider',provider,
                    '--alibaba-credentials',str(ali),'--doubao-credentials',str(dou)])
                with patch.object(main,'initialize_real',new=AsyncMock()) as hardware:
                    with self.assertRaisesRegex(ValueError,'0不授权调用'):
                        await main.run(args,lambda event:None)
                    hardware.assert_not_awaited()

    def test16_both_visual_routes_send_the_explicit_output_cap_and_reject_invalid_caps(self):
        from model_client import ModelClient
        with tempfile.TemporaryDirectory() as folder:
            ali,dou=credentials(folder)
            for provider in ('alibaba','doubao'):
                route=load_voice_route(provider,alibaba_credentials=str(ali),doubao_credentials=str(dou),
                                       paid_budget_rmb=15)
                client=route.vision_client(lambda event:None)
                if provider == 'alibaba':
                    events = iter([
                        {'type':'session.created'},
                        {'type':'session.updated','session':{'modalities':['text'],'turn_detection':None,'max_tokens':512}},
                        {'type':'conversation.item.created','item':{'type':'message','role':'user',
                            'content':[{'type':'input_text','text':'test'}]}},
                        {'type':'response.created','response':{'id':'r1'}},
                        {'type':'response.text.done','response_id':'r1','text':'{}'},
                        {'type':'response.done','response':{'id':'r1','status':'completed','output':[]}}
                    ])
                    sent = []
                    class Socket:
                        async def __aenter__(self): return self
                        async def __aexit__(self, *args): pass
                        async def send(self, raw): sent.append(json.loads(raw))
                        async def recv(self): return json.dumps(next(events))
                    client.connect = lambda *args, **kwargs: Socket()
                    self.assertEqual(client.complete([{'role':'user','content':'test'}]), '{}')
                    self.assertEqual(sent[0]['session']['max_tokens'], 512)
                    continue
                with patch('model_client.post_json',return_value={
                    'choices':[{'message':{'content':'{}'}}]}) as request:
                    self.assertEqual(client.complete([]),'{}')
                self.assertEqual(request.call_args.args[2]['max_tokens'],512)
            with patch('model_client.post_json') as request:
                for limit in (0,-1,True,1.5,4097):
                    with self.subTest(limit=limit),self.assertRaisesRegex(ValueError,'output_token_budget'):
                        ModelClient(key='fake',model='fake',url='https://example.invalid/chat',max_output_tokens=limit)
                request.assert_not_called()
