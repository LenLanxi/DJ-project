"""Unknown/malformed requests fail closed through the current interfaces.

The old fixed apology and single-expression language are deliberately retired.
Real natural-language understanding and Python syntax require model/Docker tests.
"""
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from RuntimeFixture import container, reset, runtime, sdk
from main import plan_program


class FakeModel:
    def __init__(self, reply): self.reply = reply
    def complete(self, _messages): return self.reply


class UnknownCommandSafetyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset()
        self.app = runtime()
    def tearDown(self): reset()

    async def test_invalid_json_does_not_create_program(self):
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            plan_program(FakeModel('not JSON'), 'unknown')
        self.assertEqual(sdk.get_status().flight_state, 'landed')
        self.assertEqual(sdk.get_status().control_owner, 'none')

    async def test_invalid_program_type_is_rejected(self):
        for reply in ('[]','{}','{"code":123}'):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                plan_program(FakeModel(reply), 'unknown')

    async def test_unclear_request_can_return_no_action(self):
        result = plan_program(FakeModel('{"code":"","explanation":"请说明要做什么"}'), '原定')
        self.assertEqual(result['code'], '')
        self.assertFalse(self.app.running)
        self.assertEqual(sdk.get_status().flight_state, 'landed')

    async def test_unexposed_host_capability_is_blocked(self):
        with container([('emergency', [])]):
            result = await self.app.run('drone.emergency()')
        self.assertFalse(result['ok'], result)
        self.assertIn('host capability not exposed', str(result))
        self.assertEqual(sdk.get_status().control_owner, 'none')

    async def test_invalid_motion_parameters_do_not_take_control(self):
        for args in ([19],[501],[True]):
            with self.subTest(args=args), container([('move_forward', args)]):
                result = await self.app.run('drone.move_forward(invalid)')
                self.assertFalse(result['ok'])
                self.assertEqual(sdk.get_status().control_owner, 'none')

    async def test_multistatement_program_is_admitted_to_full_python_runtime(self):
        code = 'for _ in range(2):\n    print(drone.get_status())'
        result = plan_program(FakeModel(json.dumps({'code':code})), 'read status twice')
        with container([('get_status', []), ('get_status', [])]):
            record = await self.app.run(result['code'])
        self.assertTrue(record['ok'], record)
        self.assertEqual(len(record['journal']), 2)


if __name__ == '__main__': unittest.main(verbosity=2)
