"""Offline coverage of retained benchmark queries and explicit cloud opt-in."""
import asyncio
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from QueryRunner import QueryOnlyRunner, status_query_only


class QueryFilterTests(unittest.TestCase):
    def test_status_queries_and_print_are_retained(self):
        for code in ('drone.get_status()', 'print(drone.get_status())',
                     'state = drone.get_status()\nprint(state)'):
            with self.subTest(code=code):
                self.assertTrue(status_query_only(code))

    def test_actions_and_unrelated_calls_are_rejected(self):
        for code in (None, '', 'print(80)', 'print(', 'drone.takeoff()',
                     'drone.get_status(); drone.land()',
                     'drone.get_status(unexpected=True)',
                     'import os\nprint(drone.get_status())',
                     'open("output.txt", "w"); drone.get_status()'):
            with self.subTest(code=code):
                self.assertFalse(status_query_only(code))


class QueryRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_returns_labelled_fixture_without_executing_code(self):
        with patch('execution.sandbox_runner.SandboxRunner') as sandbox, patch('builtins.print') as printing:
            runner = QueryOnlyRunner()
            result = await runner.run('print(drone.get_status())')
            rejected = await runner.run('drone.get_status(); drone.takeoff()')
        sandbox.assert_not_called()
        printing.assert_not_called()
        self.assertTrue(result['ok'])
        self.assertTrue(result['simulated'])
        state = result['journal'][0]['result']
        self.assertEqual(state['battery'], 73)
        self.assertEqual(state['source'], 'test_fixture_not_a_real_drone')
        self.assertFalse(rejected['ok'])
        self.assertFalse(runner.container_rpc)
        self.assertEqual([call['allowed_status_query'] for call in runner.calls], [True, False])

    async def test_explicit_sandbox_delegates_queries_and_cancellation_only(self):
        sandbox = SimpleNamespace()
        runtime = SimpleNamespace(cancel=Mock(), interrupt=AsyncMock(return_value={'ok': True}),
            run=AsyncMock(return_value={'ok': True, 'journal': [
                {'method': 'get_status', 'result': {'battery': 80}}]}))
        with patch('execution.sandbox_runner.SandboxRunner', return_value=sandbox) as constructor, \
             patch('app.Runtime.UnifiedRuntime', return_value=runtime) as owner:
            runner = QueryOnlyRunner(real_sandbox=True, timeout=8)
        self.assertEqual(constructor.call_args.kwargs['timeout'], 8)
        self.assertTrue(owner.call_args.args[0].get_status().simulated)
        self.assertIs(owner.call_args.kwargs['runner'], sandbox)
        await runner.run('drone.takeoff()')
        runtime.run.assert_not_awaited()
        result = await runner.run('print(drone.get_status())')
        runtime.run.assert_awaited_once_with('print(drone.get_status())')
        self.assertTrue(result['ok'])
        self.assertTrue(runner.container_rpc)
        self.assertEqual(await runner.interrupt(False), {'ok': True})
        runtime.cancel.assert_called_once_with()
        runtime.interrupt.assert_awaited_once_with(False)
        self.assertEqual(runner.cancel_count, 1)
        self.assertEqual(runner.interrupts, [False])



class BenchmarkSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_retained_benchmarks_import_and_require_explicit_cloud_flag(self):
        for name in ('AlibabaBenchmark', 'DoubaoBenchmark'):
            with self.subTest(benchmark=name):
                with patch('asyncio.create_subprocess_exec') as spawn:
                    benchmark = importlib.import_module(name)
                    with patch('pathlib.Path.read_text') as read:
                        with self.assertRaises(ValueError):
                            await benchmark.main(SimpleNamespace(real=False, credentials='Unused.json'))
                spawn.assert_not_called()
                read.assert_not_called()
