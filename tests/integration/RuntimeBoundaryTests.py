"""External device readiness and operator failure boundaries."""
import asyncio
import types
import unittest
from app import Application as main
from app.DeviceSession import validate_health

class RuntimeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_health_is_required_for_takeoff_authorization(self):
        args=types.SimpleNamespace(allow_real_takeoff=True,min_battery=30,temp_max=95,temp_warn=80)
        health=dict(simulated=False,connected=True,link_ok=True,battery=80,
                    temperature_c=40,telemetry_fresh=True,video=True)
        validate_health(health,args)
        for field,bad in [('simulated',True),('telemetry_fresh',False),('temperature_c',95),
                          ('temperature_c',float('nan')),('battery',10),('video',False)]:
            with self.subTest(field=field,value=bad), self.assertRaises(RuntimeError):
                validate_health(dict(health,**{field:bad}),args)

    async def test_failed_operator_task_cancels_main_operation(self):
        cancelled=asyncio.Event()
        async def operation():
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        async def controls(): raise RuntimeError('operator task failed')
        with self.assertRaisesRegex(RuntimeError,'operator task failed'):
            await main.supervise(operation(),asyncio.create_task(controls()))
        self.assertTrue(cancelled.is_set())
