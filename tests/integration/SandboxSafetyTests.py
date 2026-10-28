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
    async def create(*args, **kwargs):
        calls.append(args)
        if args[:2] == ('docker', 'run'): return process
        if args[:3] == ('docker', 'rm', '-f'): return cleanup
        raise AssertionError('unexpected subprocess: ' + repr(args))
    with patch.object(runner, 'check', new=AsyncMock()), patch('execution.sandbox_runner.asyncio.create_subprocess_exec', side_effect=create):
        yield calls


class SandboxSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        NoNetwork.setUp(self)

    def runner(self, **kwargs):
        backend = MockBackend()
        backend.flying = True
        return runner_for(backend, **kwargs), backend

    async def test01_begin_failure_does_not_lock_runner_running(self):
        runner, _backend = self.runner()
        app = UnifiedRuntime(runner.test_device, runner.test_follow, runner=runner)
        with patch('app.Runtime.Host', side_effect=RuntimeError('old cleanup pending')):
            with self.assertRaisesRegex(RuntimeError, 'old cleanup pending'):
                await app.run('pass')
        self.assertFalse(app.running)
        with fake_process(runner, Process([{'type':'done','ok':True}])):
            result = await app.run('pass')
        self.assertTrue(result['ok'], result)

    async def test02_cancel_aborts_blocking_host_observation_promptly(self):
        entered, release = threading.Event(), threading.Event()
        runner, _backend = self.runner()
        class SlowVision(FixtureVision):
            def observe(self, _target):
                entered.set(); release.wait(2)
                return {'visible':True,'source':'sdk_camera','frame_id':1,'timestamp':time.time()}
        runner.test_vision = SlowVision()
        try:
            with fake_process(runner, Process([rpc(1, 'observe', ['tent'])])):
                task = asyncio.create_task(run_program(runner, "drone.observe('tent')"))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                runner.test_runtime.cancel()
                result = await asyncio.wait_for(task, .5)
            self.assertEqual(result['status'], 'cancelled', result)
            self.assertFalse(runner.test_runtime.running)
            self.assertTrue(runner.test_host.cancelled.is_set())
            self.assertIsNone(runner.test_host.observation)
        finally: release.set()

    async def test03_cancelled_old_host_action_cannot_use_new_model_grant(self):
        from RuntimeFixture import reset
        follow = reset(flying=True)
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        class Delayed:
            def __getattr__(self, name): return getattr(ctl, name)
            def action_with_grant(self, name, grant, *args, **kwargs):
                if name == 'move_forward':
                    entered.set(); release.wait(2)
                    try: return ctl.action_with_grant(name, grant, *args, **kwargs)
                    finally: returned.set()
                return ctl.action_with_grant(name, grant, *args, **kwargs)
        backend = Delayed()
        runner = runner_for(backend, follow)
        try:
            with fake_process(runner, Process([rpc(1, 'move_forward', [20])])):
                task = asyncio.create_task(run_program(runner, 'drone.move_forward(20)'))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                runner.test_runtime.cancel()
                result = await asyncio.wait_for(task, .5)
                self.assertEqual(result['status'], 'cancelled', result)
            replacement = Host(backend, follow)
            await replacement.acquire()
            drv._events.clear()
            release.set()
            self.assertTrue(await asyncio.to_thread(returned.wait, 1))
            self.assertFalse(any(e.get('action')=='forward' for e in drv.snapshot()['events']))
            await replacement.finish(False)
        finally:
            release.set()
            await asyncio.to_thread(returned.wait, 1)
            reset()

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

    async def test06_stderr_budget_overflow_cannot_report_done_success(self):
        runner, _backend = self.runner()
        process = Process([{'type':'done','ok':True}], stderr=b'x' * 65000)
        with fake_process(runner, process): result = await run_program(runner, 'pass')
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['reason'], 'sandbox_output_budget_exceeded')
        self.assertLessEqual(len(result['debug'].encode()), 16000)

    async def test26_budget_failure_survives_unconfirmed_container_cleanup(self):
        runner, _backend = self.runner()
        process = Process([{'type':'done','ok':True}], stderr=b'x' * 65000)
        with fake_process(runner, process), patch.object(runner, '_remove_container',
                new=AsyncMock(return_value=['container_creation_unconfirmed'])):
            result = await run_program(runner, 'pass')
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'sandbox_cleanup_unconfirmed')
        self.assertEqual(result['failure_reason'], 'sandbox_output_budget_exceeded')
        self.assertEqual(result['cleanup_errors'], ['container_creation_unconfirmed'])
        self.assertEqual(result['pending_container'], runner.last_cleanup['pending_container'])
        self.assertFalse(runner.last_cleanup['confirmed'])
        self.assertLessEqual(len(result['debug'].encode()), 16000)

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

    async def test09_cli_wait_failure_still_removes_named_container_and_unlocks(self):
        runner, _backend = self.runner()
        process = Process([{'type':'done','ok':True}])
        process.wait = AsyncMock(side_effect=OSError('wait failed'))
        with fake_process(runner, process) as calls:
            result = await run_program(runner, 'pass')
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'sandbox_cleanup_unconfirmed')
        self.assertTrue(any(call[:3]==('docker','rm','-f') for call in calls))
        self.assertFalse(runner.test_runtime.running)

    async def test10_external_task_cancellation_releases_running_flag(self):
        runner, _backend = self.runner()
        process = Process()
        process.stdout = asyncio.StreamReader()
        with fake_process(runner, process):
            task = asyncio.create_task(run_program(runner, 'while True: pass'))
            await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(runner.test_runtime.running)
        self.assertTrue(runner.test_host.cancelled.is_set())

    async def test15_cancel_during_spawn_still_removes_named_container(self):
        runner, _backend = self.runner()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        process = Process()
        process.stdout = asyncio.StreamReader()
        original_kill = process.kill
        def kill():
            calls.append(('kill',))
            original_kill()
        process.kill = kill
        async def create(*args, **kwargs):
            calls.append(args)
            if args[:2] == ('docker', 'run'):
                entered.set()
                await release.wait()
                return process
            if args[:3] == ('docker', 'rm', '-f'):
                return Process()
            raise AssertionError(args)
        with patch.object(runner, 'check', new=AsyncMock()), patch('execution.sandbox_runner.asyncio.create_subprocess_exec', side_effect=create):
            task = asyncio.create_task(run_program(runner, 'pass'))
            await asyncio.wait_for(entered.wait(), .5)
            runner.test_runtime.cancel()
            release.set()
            result = await asyncio.wait_for(task, .5)
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(any(call[:3] == ('docker','rm','-f') for call in calls))
        self.assertIn(('kill',), calls)
        self.assertLess(next(i for i,call in enumerate(calls) if call[:3]==('docker','rm','-f')), calls.index(('kill',)))
        self.assertFalse(runner.test_runtime.running)

    async def test16_cancel_during_stdin_backpressure_is_prompt(self):
        runner, _backend = self.runner()
        process = Process([{'type':'done','ok':True}])
        entered = asyncio.Event()
        async def drain():
            entered.set()
            await asyncio.Event().wait()
        process.stdin.drain = drain
        with fake_process(runner, process):
            task = asyncio.create_task(run_program(runner, 'pass'))
            await asyncio.wait_for(entered.wait(), .5)
            runner.test_runtime.cancel()
            result = await asyncio.wait_for(task, .5)
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse(runner.test_runtime.running)

    async def test17_output_flood_aborts_blocking_host_rpc(self):
        runner, _backend = self.runner()
        entered, release = threading.Event(), threading.Event()
        class SlowVision(FixtureVision):
            def observe(self, _target):
                entered.set(); release.wait(2)
                return {'visible':False}
        runner.test_vision = SlowVision()
        process = Process([rpc(1, 'observe', ['tent'])])
        process.stderr = asyncio.StreamReader()
        try:
            with fake_process(runner, process):
                task = asyncio.create_task(run_program(runner, "drone.observe('tent')"))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                process.stderr.feed_data(b'x' * 65000)
                process.stderr.feed_eof()
                result = await asyncio.wait_for(task, .5)
            self.assertEqual(result['reason'], 'sandbox_output_budget_exceeded')
            self.assertFalse(runner.test_runtime.running)
        finally: release.set()

    async def test18_container_remove_failure_is_not_silent_success(self):
        runner, _backend = self.runner()
        process = Process([{'type':'done','ok':True}])
        cleanup = Process(stderr=b'permission denied')
        async def fail_wait(): cleanup.returncode = 1; return 1
        cleanup.wait = fail_wait
        with patch.object(runner, 'check', new=AsyncMock()), patch.object(runner, '_removed', new=AsyncMock(return_value=False)), patch('execution.sandbox_runner.asyncio.create_subprocess_exec', side_effect=[process, cleanup]):
            result = await run_program(runner, 'pass')
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'sandbox_cleanup_unconfirmed')

    async def test19_late_daemon_create_is_removed_before_cli_is_killed(self):
        """A request already sent to Docker can finish after CLI cancellation.

        The first rm deterministically sees 404. The daemon then creates the
        container independently of the CLI's lifetime. Killing run before this
        barrier leaves an orphan in the former implementation.
        """
        runner, _backend = self.runner()
        entered, first_remove, created = asyncio.Event(), asyncio.Event(), asyncio.Event()
        state = {'exists':False, 'removed':False, 'killed':False, 'early_kill':False}
        process = Process()
        process.stdout = asyncio.StreamReader()
        original_kill = process.kill
        def kill():
            state['killed'] = True
            state['early_kill'] = not state['removed']
            original_kill()
        process.kill = kill
        async def daemon_create():
            await first_remove.wait()
            state['exists'] = True
            created.set()
        daemon = asyncio.create_task(daemon_create())
        async def create(*args, **kwargs):
            if args[:2] == ('docker','run'):
                entered.set()
                return process
            if args[:3] == ('docker','rm','-f'):
                if state['exists']:
                    state['exists'] = False
                    state['removed'] = True
                    return Process()
                missing = Process(stderr=b'Error response from daemon: No such container')
                async def absent():
                    missing.returncode = 1
                    first_remove.set()
                    return 1
                missing.wait = absent
                return missing
            raise AssertionError(args)
        try:
            with patch.object(runner, 'check', new=AsyncMock()), patch('execution.sandbox_runner.asyncio.create_subprocess_exec', side_effect=create):
                task = asyncio.create_task(run_program(runner, 'while True: pass'))
                await asyncio.wait_for(entered.wait(), .5)
                runner.test_runtime.cancel()
                result = await asyncio.wait_for(task, 1)
                await asyncio.wait_for(created.wait(), .5)
            self.assertFalse(state['exists'], 'cancelled run returned before delayed daemon creation was removed')
            self.assertFalse(state['early_kill'], 'run CLI was killed before create/remove barrier')
            self.assertTrue(state['removed'])
            self.assertTrue(state['killed'])
            self.assertEqual(result['status'], 'cancelled', result)
        finally:
            daemon.cancel()
            await asyncio.gather(daemon, return_exceptions=True)

    async def test_delayed_create_past_former_deadline_is_removed_before_cli_kill(self):
        runner, _backend = self.runner()
        entered, first_remove = asyncio.Event(), asyncio.Event()
        state = {'exists': False, 'removed': False, 'early_kill': False}
        process = Process()
        process.stdout = asyncio.StreamReader()
        original_kill = process.kill
        def kill():
            state['early_kill'] = not state['removed']
            original_kill()
        process.kill = kill
        async def daemon_create():
            await first_remove.wait()
            # Docker Desktop may finish a request after the old 3s cleanup
            # budget. Killing its CLI cannot revoke that daemon-side request.
            await asyncio.sleep(3.2)
            state['exists'] = True
        daemon = asyncio.create_task(daemon_create())
        async def create(*args, **kwargs):
            if args[:2] == ('docker','run'):
                entered.set()
                return process
            if state['exists']:
                state['exists'] = False
                state['removed'] = True
                return Process()
            missing = Process(stderr=b'No such container')
            async def absent():
                missing.returncode = 1
                first_remove.set()
                return 1
            missing.wait = absent
            return missing
        try:
            with patch.object(runner, 'check', new=AsyncMock()), patch('execution.sandbox_runner.asyncio.create_subprocess_exec', side_effect=create):
                task = asyncio.create_task(run_program(runner, 'while True: pass'))
                await entered.wait()
                runner.test_runtime.cancel()
                result = await asyncio.wait_for(task, 6)
            await daemon
            self.assertEqual(result['status'], 'cancelled', result)
            self.assertTrue(state['removed'])
            self.assertFalse(state['exists'])
            self.assertFalse(state['early_kill'])
            self.assertTrue(runner.last_cleanup['confirmed'])
        finally:
            daemon.cancel()
            await asyncio.gather(daemon, return_exceptions=True)

    async def test20_zero_exit_missing_before_late_create_requires_retry(self):
        runner, _backend = self.runner()
        runner.CLEANUP_SECONDS = .01
        entered = asyncio.Event()
        process = Process()
        process.returncode = 0
        process.stdout = asyncio.StreamReader()
        calls = []
        state = {'exists': False}
        async def create(*args, **kwargs):
            calls.append(args)
            if args[:2] == ('docker','run'):
                entered.set()
                return process
            if args[:3] == ('docker','rm','-f'):
                if state['exists']:
                    state['exists'] = False
                    return Process()
                missing = Process(stderr=b'No such container')
                # Docker Desktop reports this absence with a successful exit.
                async def absent(): missing.returncode = 0; return 0
                missing.wait = absent
                return missing
            raise AssertionError(args)
        with patch.object(runner, 'check', new=AsyncMock()), patch('execution.sandbox_runner.asyncio.create_subprocess_exec', side_effect=create):
            task = asyncio.create_task(run_program(runner, 'pass'))
            await asyncio.wait_for(entered.wait(), .5)
            runner.test_runtime.cancel()
            result = await asyncio.wait_for(task, .5)
            pending = result['pending_container']
            # A second program cannot replace unresolved creation evidence.
            blocked = await run_program(runner, 'pass')
            self.assertEqual(blocked['reason'], 'sandbox_cleanup_pending', blocked)
            self.assertEqual(runner.last_cleanup['pending_container'], pending)
            self.assertEqual(sum(call[:2] == ('docker','run') for call in calls), 1)
            retry = await runner.test_runtime.retry_cleanup()
            self.assertFalse(retry['ok'])
            self.assertEqual(retry['sandbox']['pending_container'], pending)
            # The daemon finishes the original create after the failed return.
            state['exists'] = True
            recovered = await runner.test_runtime.retry_cleanup()
            self.assertTrue(recovered['ok'], recovered)
            self.assertEqual(recovered['sandbox']['container_name'], pending)
            self.assertIsNone(runner.last_cleanup['pending_container'])
            self.assertTrue(runner.last_cleanup['confirmed'])
            self.assertFalse(state['exists'])
        self.assertEqual(result['reason'], 'sandbox_cleanup_unconfirmed', result)
        self.assertIn('container_creation_unconfirmed', result['cleanup_errors'])
        self.assertTrue(result['pending_container'].startswith('companion-'))
        self.assertFalse(any(call[:2] == ('docker','ps') for call in calls), 'an empty listing cannot fence an unobserved create')
        self.assertFalse(runner.test_runtime.running)

    async def test21_unresolved_spawn_retains_cleanup_identity(self):
        runner, _backend = self.runner()
        runner.SPAWN_SETTLE_SECONDS = .01
        entered, spawn_cancelled = asyncio.Event(), asyncio.Event()
        async def create(*args, **kwargs):
            if args[:2] == ('docker','run'):
                entered.set()
                try: await asyncio.Event().wait()
                finally: spawn_cancelled.set()
            if args[:3] == ('docker','rm','-f'):
                return Process()
            raise AssertionError(args)
        with patch.object(runner, 'check', new=AsyncMock()), patch('execution.sandbox_runner.asyncio.create_subprocess_exec', side_effect=create):
            task = asyncio.create_task(run_program(runner, 'pass'))
            # Admission is not the cancellation-latency assertion below.
            # Windows asyncio debug scheduling can exceed 500ms under load.
            await asyncio.wait_for(entered.wait(), 5)
            runner.test_runtime.cancel()
            result = await asyncio.wait_for(task, .5)
        self.assertEqual(result['reason'], 'sandbox_cleanup_unconfirmed', result)
        self.assertTrue(any(e.startswith('docker_spawn_unconfirmed:') for e in result['cleanup_errors']))
        self.assertTrue(result['pending_container'].startswith('companion-'))
        self.assertTrue(spawn_cancelled.is_set())
        self.assertFalse(runner.test_runtime.running)

    async def test22_follow_cleanup_error_preserves_pending_container_identity(self):
        runner, _backend = self.runner()
        runner.CLEANUP_SECONDS = .001
        process = Process([{'type':'done','ok':True}])
        missing = Process(stderr=b'No such container')
        async def absent(): missing.returncode = 1; return 1
        missing.wait = absent
        with patch.object(runner, 'check', new=AsyncMock()), patch.object(runner, '_removed', new=AsyncMock(return_value=False)), patch('execution.Host.Host.finish', new=AsyncMock(side_effect=RuntimeError('follow cleanup failed'))), patch('execution.sandbox_runner.asyncio.create_subprocess_exec', side_effect=[process, missing]):
            result = await run_program(runner, 'pass')
        self.assertEqual(result['reason'], 'sandbox_cleanup_unconfirmed', result)
        self.assertEqual(result['cleanup_errors'], ['container_absence_unconfirmed'])
        self.assertTrue(result['pending_container'].startswith('companion-'))
        self.assertEqual(result['follow_cleanup_error'], 'follow cleanup failed')
        self.assertFalse(runner.test_runtime.running)

    async def test23_invalid_stdout_does_not_fence_container_creation(self):
        runner, _backend = self.runner()
        runner.CLEANUP_SECONDS = .001
        process = Process([{'type':'untrusted-output'}])
        async def create(*args, **kwargs):
            if args[:2] == ('docker','run'): return process
            missing = Process(stderr=b'No such container')
            async def absent(): missing.returncode = 1; return 1
            missing.wait = absent
            return missing
        with patch.object(runner, 'check', new=AsyncMock()), patch.object(runner, '_removed', new=AsyncMock(return_value=True)) as removed, patch('execution.sandbox_runner.asyncio.create_subprocess_exec', side_effect=create):
            result = await run_program(runner, 'pass')
        self.assertEqual(result['reason'], 'sandbox_cleanup_unconfirmed', result)
        self.assertIn('container_creation_unconfirmed', result['cleanup_errors'])
        removed.assert_not_awaited()

    async def test24_external_task_cancel_retains_unconfirmed_cleanup_evidence(self):
        runner, _backend = self.runner()
        runner.CLEANUP_SECONDS = .001
        entered = asyncio.Event()
        process = Process()
        process.stdout = asyncio.StreamReader()
        async def create(*args, **kwargs):
            if args[:2] == ('docker','run'):
                entered.set()
                return process
            missing = Process(stderr=b'No such container')
            async def absent(): missing.returncode = 1; return 1
            missing.wait = absent
            return missing
        with patch.object(runner, 'check', new=AsyncMock()), patch('execution.sandbox_runner.asyncio.create_subprocess_exec', side_effect=create):
            task = asyncio.create_task(run_program(runner, 'while True: pass'))
            await asyncio.wait_for(entered.wait(), .5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(task, .5)
        self.assertFalse(runner.last_cleanup['confirmed'])
        self.assertTrue(runner.last_cleanup['pending_container'].startswith('companion-'))
        self.assertEqual(caught.exception.pending_container, runner.last_cleanup['pending_container'])
        self.assertIn('container_creation_unconfirmed', caught.exception.cleanup_errors)
        with self.assertRaises(TypeError):
            runner.last_cleanup['pending_container'] = None
        self.assertIsInstance(runner.last_cleanup['cleanup_errors'], tuple)
        self.assertFalse(runner.test_runtime.running)

    async def test25_second_task_cancel_during_cleanup_cannot_abandon_container(self):
        runner, _backend = self.runner()
        spawned, removing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        process = Process()
        process.stdout = asyncio.StreamReader()
        cleanup = Process()
        async def blocked_remove():
            removing.set()
            await release.wait()
            cleanup.returncode = 0
            return 0
        cleanup.wait = blocked_remove
        async def create(*args, **kwargs):
            if args[:2] == ('docker','run'):
                spawned.set()
                return process
            if args[:3] == ('docker','rm','-f'):
                return cleanup
            raise AssertionError(args)
        with patch.object(runner, 'check', new=AsyncMock()), patch('execution.sandbox_runner.asyncio.create_subprocess_exec', side_effect=create):
            task = asyncio.create_task(run_program(runner, 'while True: pass'))
            await asyncio.wait_for(spawned.wait(), .5)
            task.cancel()
            await asyncio.wait_for(removing.wait(), .5)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, .5)
        self.assertTrue(runner.last_cleanup['confirmed'])
        self.assertIsNone(runner.last_cleanup['pending_container'])
        self.assertFalse(runner.test_runtime.running)

    async def test26_late_output_flood_cannot_cancel_container_removal(self):
        runner, _backend = self.runner()
        removing, release = asyncio.Event(), asyncio.Event()
        process = Process([{'type':'done','ok':True}])
        process.stderr = asyncio.StreamReader()
        cleanup = Process()
        async def remove():
            removing.set()
            await release.wait()
            cleanup.returncode = 0
            return 0
        cleanup.wait = remove
        with patch.object(runner, 'check', new=AsyncMock()), patch('execution.sandbox_runner.asyncio.create_subprocess_exec', side_effect=[process, cleanup]):
            task = asyncio.create_task(run_program(runner, 'print many bytes'))
            await asyncio.wait_for(removing.wait(), 1)
            process.stderr.feed_data(b'x' * 65000)
            process.stderr.feed_eof()
            await asyncio.sleep(.01)
            self.assertFalse(task.done())
            release.set()
            result = await asyncio.wait_for(task, 1)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'sandbox_output_budget_exceeded')
        self.assertTrue(runner.last_cleanup['confirmed'])


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
                    {'initial':result, 'cleanup':dict(runner.last_cleanup)})
        self.assertTrue(names)
        for name in names:
            process = await asyncio.create_subprocess_exec('docker','inspect',name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            details = {key: result.get(key) for key in
                ('ok','status','reason','failure_reason','cleanup_errors','pending_container')}
            self.assertNotEqual(await process.wait(), 0,
                'container was not removed: '+name+'; result='+repr(details)+'; cleanup='+repr(dict(runner.last_cleanup)))
        return result

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

    async def test13_real_stdout_flood_is_bounded_and_failed(self):
        runner = runner_for(timeout=5)
        result = await self.run_recording_container(runner, "print('x'*65000)")
        self.assertFalse(result['ok'], result)
        # On a fast engine --rm may finish before an RPC/done message was
        # consumed. The conservative create fence then remains unconfirmed.
        # Preserve the budget failure without overriding cleanup uncertainty.
        if result['reason'] == 'sandbox_cleanup_unconfirmed':
            self.assertEqual(result['failure_reason'], 'sandbox_output_budget_exceeded')
            self.assertTrue(result['cleanup_errors'])
            self.assertTrue(result['pending_container'])
            self.assertFalse(runner.last_cleanup['confirmed'])
            self.assertEqual(runner.last_cleanup['pending_container'], result['pending_container'])
        else:
            self.assertEqual(result['reason'], 'sandbox_output_budget_exceeded')
            self.assertTrue(runner.last_cleanup['confirmed'])
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
