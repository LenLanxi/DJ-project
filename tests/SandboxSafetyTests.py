"""Adversarial sandbox supervision checks; explicit Docker tests never use aircraft.

Set RUN_DOCKER_TESTS=1 after building companion-sandbox:0.4 to run real container
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
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT/'modules'/'ai', ROOT/'tests'):
    sys.path.insert(0, str(path))
from SafetyTests import NoNetwork
from ContainerWireTests import Process
from Support import MockBackend
from gateway import Gateway
from backends import SDKBackend
from sandbox_runner import SandboxRunner
from drone_sdk import controller as ctl, _driver as drv


def rpc(number, method='get_status', args=None):
    return {'type':'rpc','id':number,'method':method,'args':args or [],'kwargs':{}}


@contextmanager
def fake_process(runner, process):
    cleanup = Process()
    calls = []
    async def create(*args, **kwargs):
        calls.append(args)
        if args[:2] == ('docker', 'run'): return process
        if args[:3] == ('docker', 'rm', '-f'): return cleanup
        raise AssertionError('unexpected subprocess: ' + repr(args))
    with patch.object(runner, 'check', new=AsyncMock()), patch('sandbox_runner.asyncio.create_subprocess_exec', side_effect=create):
        yield calls


class SandboxSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        NoNetwork.setUp(self)

    def runner(self, **kwargs):
        backend = MockBackend()
        backend.flying = True
        return SandboxRunner(Gateway(backend), **kwargs), backend

    async def test01_begin_failure_does_not_lock_runner_running(self):
        runner, _backend = self.runner()
        with patch.object(runner.gateway, 'begin', side_effect=RuntimeError('old cleanup pending')):
            failed = await runner.run('pass')
        self.assertFalse(failed['ok'])
        self.assertFalse(runner.running)
        with fake_process(runner, Process([{'type':'done','ok':True}])):
            result = await runner.run('pass')
        self.assertTrue(result['ok'], result)

    async def test02_cancel_aborts_blocking_host_observation_promptly(self):
        entered, release = threading.Event(), threading.Event()
        runner, _backend = self.runner()
        class SlowVision:
            def observe(self, _target):
                entered.set(); release.wait(2)
                return {'visible':True,'source':'sdk_camera','frame_id':1,'timestamp':time.time()}
        runner.gateway.vision = SlowVision()
        try:
            with fake_process(runner, Process([rpc(1, 'observe', ['tent'])])):
                task = asyncio.create_task(runner.run("drone.observe('tent')"))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                runner.cancel()
                result = await asyncio.wait_for(task, .5)
            self.assertEqual(result['status'], 'cancelled', result)
            self.assertFalse(runner.running)
            self.assertTrue(runner.gateway.cancelled.is_set())
            self.assertIsNone(runner.gateway.observation)
        finally: release.set()

    async def test03_cancelled_old_host_action_cannot_use_new_model_grant(self):
        from RuntimeFixture import reset
        reset(flying=True)
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        class Delayed(SDKBackend):
            def action(self, name, *args):
                if name == 'move_forward':
                    entered.set(); release.wait(2)
                    try: return super().action(name, *args)
                    finally: returned.set()
                return super().action(name, *args)
        backend = Delayed(ROOT, allow_simulated=True)
        gateway = Gateway(backend)
        runner = SandboxRunner(gateway)
        try:
            with fake_process(runner, Process([rpc(1, 'move_forward', [20])])):
                task = asyncio.create_task(runner.run('drone.move_forward(20)'))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                runner.cancel()
                result = await asyncio.wait_for(task, .5)
                self.assertEqual(result['status'], 'cancelled', result)
            gateway.begin()
            await gateway.acquire()
            drv._events.clear()
            release.set()
            self.assertTrue(await asyncio.to_thread(returned.wait, 1))
            self.assertFalse(any(e.get('action')=='forward' for e in drv.snapshot()['events']))
            await gateway.finish(False)
        finally:
            release.set()
            await asyncio.to_thread(returned.wait, 1)
            reset()

    async def test04_repeated_rpc_id_is_rejected_before_second_action(self):
        runner, backend = self.runner()
        with fake_process(runner, Process([rpc(1), rpc(1, 'move_forward', [20])])):
            result = await runner.run('pass')
        self.assertFalse(result['ok'])
        self.assertIn('RPC sequence', result['reason'])
        self.assertEqual(backend.calls, [])

    async def test05_out_of_order_and_boolean_rpc_ids_are_rejected(self):
        for number in (0, 2, True):
            runner, backend = self.runner()
            with fake_process(runner, Process([rpc(number, 'move_forward', [20])])):
                result = await runner.run('pass')
            self.assertFalse(result['ok'])
            self.assertEqual(backend.calls, [])

    async def test06_stderr_budget_overflow_cannot_report_done_success(self):
        runner, _backend = self.runner()
        process = Process([{'type':'done','ok':True}], stderr=b'x' * 65000)
        with fake_process(runner, process): result = await runner.run('pass')
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['reason'], 'sandbox_output_budget_exceeded')
        self.assertLessEqual(len(result['debug'].encode()), 16000)

    async def test07_rpc_budget_exhaustion_stops_before_extra_action(self):
        runner, backend = self.runner(max_rpc=1)
        with fake_process(runner, Process([rpc(1), rpc(2, 'move_forward', [20])])):
            result = await runner.run('pass')
        self.assertFalse(result['ok'])
        self.assertIn('RPC budget exhausted', result['reason'])
        self.assertEqual(len(result['journal']), 1)
        self.assertEqual(backend.calls, [])

    async def test08_oversized_rpc_has_no_device_effect(self):
        runner, backend = self.runner()
        message = rpc(1, 'move_forward', ['x'*17000])
        with fake_process(runner, Process([message])):
            result = await runner.run('pass')
        self.assertFalse(result['ok'])
        self.assertEqual(backend.calls, [])

    async def test09_cli_wait_failure_still_removes_named_container_and_unlocks(self):
        runner, _backend = self.runner()
        process = Process([{'type':'done','ok':True}])
        process.wait = AsyncMock(side_effect=OSError('wait failed'))
        with fake_process(runner, process) as calls:
            result = await runner.run('pass')
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'sandbox_cleanup_unconfirmed')
        self.assertTrue(any(call[:3]==('docker','rm','-f') for call in calls))
        self.assertFalse(runner.running)

    async def test10_external_task_cancellation_releases_running_flag(self):
        runner, _backend = self.runner()
        process = Process()
        process.stdout = asyncio.StreamReader()
        with fake_process(runner, process):
            task = asyncio.create_task(runner.run('while True: pass'))
            await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(runner.running)
        self.assertTrue(runner.gateway.cancelled.is_set())

    async def test15_cancel_during_spawn_still_removes_named_container(self):
        runner, _backend = self.runner()
        entered = asyncio.Event()
        calls = []
        async def create(*args, **kwargs):
            calls.append(args)
            if args[:2] == ('docker', 'run'):
                entered.set()
                await asyncio.Event().wait()
            if args[:3] == ('docker', 'rm', '-f'):
                return Process()
            raise AssertionError(args)
        with patch.object(runner, 'check', new=AsyncMock()), patch('sandbox_runner.asyncio.create_subprocess_exec', side_effect=create):
            task = asyncio.create_task(runner.run('pass'))
            await asyncio.wait_for(entered.wait(), .5)
            runner.cancel()
            result = await asyncio.wait_for(task, .5)
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(any(call[:3] == ('docker','rm','-f') for call in calls))
        self.assertFalse(runner.running)

    async def test16_cancel_during_stdin_backpressure_is_prompt(self):
        runner, _backend = self.runner()
        process = Process([{'type':'done','ok':True}])
        entered = asyncio.Event()
        async def drain():
            entered.set()
            await asyncio.Event().wait()
        process.stdin.drain = drain
        with fake_process(runner, process):
            task = asyncio.create_task(runner.run('pass'))
            await asyncio.wait_for(entered.wait(), .5)
            runner.cancel()
            result = await asyncio.wait_for(task, .5)
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse(runner.running)

    async def test17_output_flood_aborts_blocking_host_rpc(self):
        runner, _backend = self.runner()
        entered, release = threading.Event(), threading.Event()
        class SlowVision:
            def observe(self, _target):
                entered.set(); release.wait(2)
                return {'visible':False}
        runner.gateway.vision = SlowVision()
        process = Process([rpc(1, 'observe', ['tent'])])
        process.stderr = asyncio.StreamReader()
        try:
            with fake_process(runner, process):
                task = asyncio.create_task(runner.run("drone.observe('tent')"))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                process.stderr.feed_data(b'x' * 65000)
                process.stderr.feed_eof()
                result = await asyncio.wait_for(task, .5)
            self.assertEqual(result['reason'], 'sandbox_output_budget_exceeded')
            self.assertFalse(runner.running)
        finally: release.set()

    async def test18_container_remove_failure_is_not_silent_success(self):
        runner, _backend = self.runner()
        process = Process([{'type':'done','ok':True}])
        cleanup = Process(stderr=b'permission denied')
        async def fail_wait(): cleanup.returncode = 1; return 1
        cleanup.wait = fail_wait
        with patch.object(runner, 'check', new=AsyncMock()), patch.object(runner, '_removed', new=AsyncMock(return_value=False)), patch('sandbox_runner.asyncio.create_subprocess_exec', side_effect=[process, cleanup]):
            result = await runner.run('pass')
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'sandbox_cleanup_unconfirmed')


@unittest.skipUnless(os.getenv('RUN_DOCKER_TESTS')=='1' and shutil.which('docker'), 'requires explicit RUN_DOCKER_TESTS=1 and built sandbox image')
class RealSandboxSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def run_recording_container(self, runner, code, cancel=False):
        names = []
        original = SandboxRunner.command
        def command(image, name):
            names.append(name)
            return original(image, name)
        with patch.object(SandboxRunner, 'command', side_effect=command):
            task = asyncio.create_task(runner.run(code))
            if cancel:
                deadline = time.monotonic() + 3
                while not names and time.monotonic() < deadline: await asyncio.sleep(.01)
                self.assertTrue(names)
                await asyncio.sleep(.15)
                runner.cancel()
            result = await asyncio.wait_for(task, 15)
        self.assertTrue(names)
        for name in names:
            process = await asyncio.create_subprocess_exec('docker','inspect',name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            self.assertNotEqual(await process.wait(), 0, 'container was not removed: '+name)
        return result

    async def test11_real_infinite_loop_cancelled_without_leftover_container(self):
        runner = SandboxRunner(Gateway(MockBackend()), timeout=10)
        result = await self.run_recording_container(runner, 'while True: pass', cancel=True)
        self.assertEqual(result['status'], 'cancelled', result)
        self.assertFalse(runner.running)

    async def test12_real_infinite_loop_deadline_removes_container(self):
        runner = SandboxRunner(Gateway(MockBackend()), timeout=2)
        # Validate the real engine/image first so the test deadline measures
        # container work, rather than a slow Windows docker-inspect startup.
        await runner.check()
        with patch.object(runner, 'check', new=AsyncMock()):
            result = await self.run_recording_container(runner, 'while True: pass')
        self.assertEqual(result['status'], 'timeout', result)

    async def test13_real_stdout_flood_is_bounded_and_failed(self):
        runner = SandboxRunner(Gateway(MockBackend()), timeout=5)
        result = await self.run_recording_container(runner, "print('x'*65000)")
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['reason'], 'sandbox_output_budget_exceeded')
        self.assertLessEqual(len(result['debug'].encode()), 16000)

    async def test14_real_cancel_during_host_vision_wait_is_prompt(self):
        entered, release = threading.Event(), threading.Event()
        class SlowVision:
            def observe(self, _target):
                entered.set(); release.wait(2)
                return {'visible':False,'source':'sdk_camera','frame_id':1,'timestamp':time.time()}
        gateway = Gateway(MockBackend(), SlowVision())
        runner = SandboxRunner(gateway, timeout=10)
        names = []
        original = SandboxRunner.command
        def command(image, name):
            names.append(name)
            return original(image,name)
        try:
            with patch.object(SandboxRunner, 'command', side_effect=command):
                task = asyncio.create_task(runner.run("drone.observe('tent')"))
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                started = time.monotonic()
                runner.cancel()
                result = await asyncio.wait_for(task, 2)
                self.assertLess(time.monotonic()-started, 2)
            self.assertEqual(result['status'], 'cancelled', result)
            self.assertIsNone(gateway.observation)
            for name in names:
                process = await asyncio.create_subprocess_exec('docker','inspect',name,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                self.assertNotEqual(await process.wait(),0)
        finally: release.set()


if __name__ == '__main__':
    unittest.main(verbosity=2)
