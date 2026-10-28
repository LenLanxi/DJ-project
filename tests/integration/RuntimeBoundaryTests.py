"""Production-entry boundaries, independent of model credentials and hardware."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
import contextlib
import io
from unittest.mock import patch
from app import Application as main
from app.DeviceSession import validate_health
from app.Hud import draw_follow_overlay, follow_status_text


class RuntimeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_refuses_implicit_simulation(self):
        with self.assertRaisesRegex(ValueError,'real-drone'):
            await main.Session(main.make_parser().parse_args([]),lambda event:None).run()

    async def test_static_picture_cannot_guide_actual_aircraft(self):
        args=main.make_parser().parse_args(['--real-drone','--text','look','--image','image.png'])
        with self.assertRaisesRegex(ValueError,'image'):
            await main.Session(args,lambda event:None).run()

    async def test_plan_only_cannot_connect_aircraft(self):
        args=main.make_parser().parse_args(['--real-drone','--text','look','--plan-only'])
        with self.assertRaisesRegex(ValueError,'plan-only'):
            await main.Session(args,lambda event:None).run()

    async def test_old_production_routes_are_not_selectable(self):
        options=main.make_parser()._option_string_actions
        for name in ('--live','--offline-voice','--always-on','--realtime','--volc-dialog','--sdk-stub-root'):
            self.assertNotIn(name,options)

    async def test_voice_duration_over_30000_is_rejected_before_logging_or_startup(self):
        with contextlib.redirect_stderr(io.StringIO()), patch.object(main,'SessionLog') as log:
            with self.assertRaises(SystemExit) as caught:
                main.main(['--real-drone','--max-session-seconds','30001'])
        self.assertEqual(caught.exception.code,2)
        log.assert_not_called()

    async def test_fresh_health_is_required_for_takeoff_authorization(self):
        args=main.make_parser().parse_args(['--allow-real-takeoff'])
        health=dict(simulated=False,connected=True,link_ok=True,battery=80,
                    temperature_c=40,telemetry_fresh=True,video=True)
        validate_health(health,args)
        for field,bad in [('simulated',True),('telemetry_fresh',False),('temperature_c',95),
                          ('temperature_c',float('nan')),('battery',10),('video',False)]:
            with self.subTest(field=field,value=bad), self.assertRaises(RuntimeError):
                validate_health(dict(health,**{field:bad}),args)

    async def test_model_program_is_full_python(self):
        code='for _ in range(2):\n    print(drone.get_status())'
        fake=types.SimpleNamespace(complete=lambda messages:json.dumps({'code':code}))
        self.assertEqual(main.plan_program(fake,'status twice')['code'],code)

    async def test_logs_redact_credentials_and_image_audio_payloads(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'TEST_API_KEY':'secretValue12345'}):
            path=Path(tmp)/'Session.jsonl'
            log=main.SessionLog(path)
            log({'event':'test','error':'request secretValue12345','api_key':'secret',
                 'image_url':'data:image/jpeg;base64,abc','audio':'bytes'})
            log.close()
            text=path.read_text(encoding='utf-8')
            self.assertNotIn('secretValue12345',text)
            self.assertNotIn('base64',text)
            self.assertEqual(json.loads(text)['audio'],'[payload omitted]')

    async def test_token_usage_counts_survive_credential_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=main.SessionLog(Path(tmp)/'Session.jsonl')
            try:
                result=log.clean({'usage':{'input_tokens':3,'output_tokens':4,'total_tokens':7},
                                  'access_token':'credential','token':{'count':7}})
                self.assertEqual(result['usage'],{'input_tokens':3,'output_tokens':4,'total_tokens':7})
                self.assertEqual(result['access_token'],'[redacted]')
                self.assertEqual(result['token']['count'],7)
            finally:log.close()

    async def test_failed_operator_task_cancels_main_operation(self):
        cancelled=asyncio.Event()
        async def operation():
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        async def controls(): raise RuntimeError('operator task failed')
        with self.assertRaisesRegex(RuntimeError,'operator task failed'):
            await main.supervise(operation(),asyncio.create_task(controls()))
        self.assertTrue(cancelled.is_set())

    async def test_hud_bad_corner_metadata_cannot_disable_operator_controls(self):
        import numpy as np
        image=np.zeros((100,120,3),dtype=np.uint8)
        result=draw_follow_overlay(image,obs={'found':True,'center_x':60,'center_y':50,
            'corners':[['x','y']],'image_width':120,'image_height':100})
        self.assertEqual(result.shape,image.shape)
        self.assertFalse(np.shares_memory(result,image))

    async def test_hud_explains_start_pause_and_stop(self):
        self.assertIn('start following',follow_status_text(None)[0])
        self.assertIn('tts',follow_status_text({'event':'pause','reason':'tts'})[0])
        self.assertIn('say it again',follow_status_text({'event':'stop','reason':'lost'})[0])
