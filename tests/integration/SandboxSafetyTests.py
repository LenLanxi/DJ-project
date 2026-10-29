"""Adversarial sandbox supervision checks; explicit Docker tests never use aircraft.

Set RUN_DOCKER_TESTS=1 after building companion-sandbox:0.5 to run real container
checks. Fake wire tests exercise the supervisor without executing model code.
"""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import unittest
from Fixtures import FixtureVision
import pytest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
from SafetyFixture import NoNetwork
from ContainerFixture import Process
from Support import MockBackend
from execution.Host import Host
from execution.sandbox_runner import SandboxRunner
from ExecutionFixture import run_program, runner_for
from app.Runtime import UnifiedRuntime
from drone_sdk import controller as ctl, _driver as drv


def rpc(number, method='get_status', args=None):
    return {'type':'rpc','id':number,'method':method,'args':args or [],'kwargs':{}}


@contextmanager
def fake_process(runner, process):
    cleanup = Process()
    calls = []
    runner.container, runner.created = 'companion-fixture', True
    async def create(*args, **kwargs):
        calls.append(args)
        if args[:2] == ('docker', 'exec'): return process
        if args[:3] == ('docker', 'rm', '-f'): return cleanup
        raise AssertionError('unexpected subprocess: ' + repr(args))
    with patch.object(runner, 'check', new=AsyncMock()), patch.object(runner, '_reset', new=AsyncMock()), patch('execution.sandbox_runner.asyncio.create_subprocess_exec', side_effect=create):
        yield calls


class SandboxSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        NoNetwork.setUp(self)

    def runner(self, **kwargs):
        backend = MockBackend()
        backend.flying = True
        return runner_for(backend, **kwargs), backend


    async def test04_repeated_rpc_id_is_rejected_before_second_action(self):
        runner, backend = self.runner()
        with fake_process(runner, Process([rpc(1), rpc(1, 'move_forward', [20])])):
            result = await run_program(runner, 'pass')
        self.assertFalse(result['ok'])
        self.assertIn('RPC sequence', result['reason'])
        self.assertEqual(backend.calls, [])

    async def test05_out_of_order_and_boolean_rpc_ids_are_rejected(self):
        for number in (0, 2, True):
            runner, backend = self.runner()
            with fake_process(runner, Process([rpc(number, 'move_forward', [20])])):
                result = await run_program(runner, 'pass')
            self.assertFalse(result['ok'])
            self.assertEqual(backend.calls, [])

    async def test06_large_debug_output_is_truncated_without_failing_program(self):
        runner, _backend = self.runner()
        process = Process([{'type':'done','ok':True}], stderr=b'x' * 65000)
        with fake_process(runner, process): result = await run_program(runner, 'pass')
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['debug_truncated'])
        self.assertLessEqual(len(result['debug'].encode()), 16000)

    async def test_debug_stream_failure_does_not_leave_container_cleanup_pending(self):
        runner, _backend = self.runner()
        process = Process([{'type':'done','ok':True}])
        process.stderr.read = AsyncMock(side_effect=OSError('diagnostic pipe closed'))
        with fake_process(runner, process):
            result = await run_program(runner, 'pass')
        self.assertTrue(result['ok'], result)
        self.assertIn('diagnostic pipe closed', result['debug_warnings'][0])
        self.assertTrue(runner.test_outcome.cleanup['confirmed'])
        self.assertIsNone(runner.test_outcome.cleanup['pending_container'])


    async def test07_rpc_budget_exhaustion_stops_before_extra_action(self):
        runner, backend = self.runner(max_rpc=1)
        with fake_process(runner, Process([rpc(1), rpc(2, 'move_forward', [20])])):
            result = await run_program(runner, 'pass')
        self.assertFalse(result['ok'])
        self.assertIn('RPC budget exhausted', result['reason'])
        self.assertEqual(len(result['journal']), 1)
        self.assertEqual(backend.calls, [])

    async def test08_oversized_rpc_has_no_device_effect(self):
        runner, backend = self.runner()
        message = rpc(1, 'move_forward', ['x'*17000])
        with fake_process(runner, Process([message])):
            result = await run_program(runner, 'pass')
        self.assertFalse(result['ok'])
        self.assertEqual(backend.calls, [])


    async def test_failed_removal_retains_container_for_retry(self):
        runner, _ = self.runner()
        runner.container, runner.created = 'companion-fixture', True
        with patch.object(runner, '_remove_container', new=AsyncMock(side_effect=[['busy'], []])):
            self.assertFalse((await runner.close())['ok'])
            self.assertIsNotNone(runner.container)
            self.assertTrue((await runner.close())['ok'])
            self.assertIsNone(runner.container)


@pytest.mark.docker
@unittest.skipUnless(os.getenv('RUN_DOCKER_TESTS')=='1' and shutil.which('docker'), 'requires explicit RUN_DOCKER_TESTS=1 and built sandbox image')
class RealSandboxSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def run_recording_container(self, runner, code, cancel=False):
        names = []
        spawned = asyncio.Event()
        original = SandboxRunner.command
        original_create = asyncio.create_subprocess_exec
        def command(image, name):
            names.append(name)
            return original(image, name)
        async def create(*args, **kwargs):
            process = await original_create(*args, **kwargs)
            if args[:2] == ('docker','run'):
                spawned.set()
            return process
        with patch.object(SandboxRunner, 'command', side_effect=command), patch('execution.sandbox_runner.asyncio.create_subprocess_exec', side_effect=create):
            task = asyncio.create_task(run_program(runner, code))
            if cancel:
                # Cancel at the first real OS-spawn acknowledgement, before
                # requiring container visibility. This exercises early create.
                await asyncio.wait_for(spawned.wait(), 10)
                self.assertTrue(names)
                runner.test_runtime.cancel()
            result = await asyncio.wait_for(task, 15)
            if cancel and result.get('pending_container'):
                # A failed first cleanup stays failed. Public reconciliation
                # must remove the late creation before absence is asserted.
                self.assertFalse(result['ok'])
                self.assertEqual(result['reason'], 'sandbox_cleanup_unconfirmed')
                self.assertTrue((await runner.test_runtime.retry_cleanup())['ok'],
                    {'initial':result, 'cleanup':dict(runner.test_outcome.cleanup)})
        await runner.close()
        self.assertTrue(names)
        for name in names:
            process = await asyncio.create_subprocess_exec('docker','inspect',name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            details = {key: result.get(key) for key in
                ('ok','status','reason','failure_reason','cleanup_errors','pending_container')}
            self.assertNotEqual(await process.wait(), 0,
                'container was not removed: '+name+'; result='+repr(details)+'; cleanup='+repr(dict(runner.test_outcome.cleanup)))
        return result

    async def test_resident_reuse_has_fresh_scope_and_no_scratch_or_children(self):
        runner = runner_for(timeout=15)
        try:
            first = await run_program(runner, "import subprocess\nfrom pathlib import Path\nx=42\nPath('/tmp/proof').write_text('old')\nsubprocess.Popen(['python','-c','import time;time.sleep(60)'])\nimport atexit,threading,time\natexit.register(lambda: subprocess.Popen(['python','-c','import time;time.sleep(60)']))\nthreading.Thread(target=lambda: (time.sleep(.1), subprocess.Popen(['python','-c','import time;time.sleep(60)']))).start()")
            self.assertTrue(first['ok'], first)
            name = runner.container
            second = await run_program(runner, "from pathlib import Path\nassert 'x' not in globals()\nassert not Path('/tmp/proof').exists()\nimport os\nassert sorted(int(p.name) for p in Path('/proc').iterdir() if p.name.isdigit()) == sorted([1,os.getpid()])\nprint(drone.get_status())")
            self.assertTrue(second['ok'], second)
            self.assertEqual(name, runner.container)
            timed = runner.timeout
            runner.timeout = .2
            failed = await run_program(runner, 'while True: pass')
            self.assertEqual(failed['status'], 'timeout', failed)
            self.assertIsNone(runner.container)
            runner.timeout = timed
            self.assertTrue((await run_program(runner, 'print(1)'))['ok'])
            self.assertNotEqual(name, runner.container)
        finally:
            self.assertTrue((await runner.close())['ok'])

    async def test11_real_infinite_loop_cancelled_without_leftover_container(self):
        runner = runner_for(timeout=10)
        result = await self.run_recording_container(runner, 'while True: pass', cancel=True)
        if result.get('pending_container'):
            self.assertEqual(result['failure_reason'], 'task_cancelled', result)
            self.assertFalse(result['ok'])
        else:
            self.assertEqual(result['status'], 'cancelled', result)
        self.assertFalse(runner.test_runtime.running)

    async def test12_real_infinite_loop_deadline_removes_container(self):
        runner = runner_for(timeout=2)
        # Validate the real engine/image first so the test deadline measures
        # container work, rather than a slow Windows docker-inspect startup.
        await runner.check()
        with patch.object(runner, 'check', new=AsyncMock()):
            result = await self.run_recording_container(runner, 'while True: pass')
        self.assertEqual(result['status'], 'timeout', result)

    async def test13_real_large_output_is_bounded_and_program_continues(self):
        runner = runner_for(timeout=10)
        result = await self.run_recording_container(runner,
            "try:\n drone.unknown_method()\nexcept RuntimeError:\n pass\nprint('x'*65000)")
        self.assertTrue(result['ok'], result)
        self.assertIn('error', result['journal'][0])
        self.assertTrue(result['debug_truncated'])
        self.assertTrue(runner.test_outcome.cleanup['confirmed'])
        self.assertLessEqual(len(result['debug'].encode()), 16000)

    async def test14_real_cancel_during_host_vision_wait_is_prompt(self):
        entered, release = threading.Event(), threading.Event()
        class SlowVision(FixtureVision):
            def observe(self, _target):
                entered.set(); release.wait(2)
                return {'visible':False,'source':'sdk_camera','frame_id':1,'timestamp':time.time()}
        gateway = Host(MockBackend(), MockBackend(), SlowVision())
        runner = runner_for(gateway.device, gateway.follow, gateway.vision, timeout=10)
        names = []
        original = SandboxRunner.command
        def command(image, name):
            names.append(name)
            return original(image,name)
        try:
            with patch.object(SandboxRunner, 'command', side_effect=command):
                task = asyncio.create_task(run_program(runner, "drone.observe('tent')"))
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                started = time.monotonic()
                runner.test_runtime.cancel()
                result = await asyncio.wait_for(task, 2)
                self.assertLess(time.monotonic()-started, 2)
            self.assertEqual(result['status'], 'cancelled', result)
            self.assertIsNone(runner.test_host.observation)
            for name in names:
                process = await asyncio.create_subprocess_exec('docker','inspect',name,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                self.assertNotEqual(await process.wait(),0)
        finally: release.set()


if __name__ == '__main__':
    unittest.main(verbosity=2)
