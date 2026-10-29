"""Public execution permissions and recovery behavior with real Host scopes."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from app.Runtime import UnifiedRuntime
from app.Results import tool_result
from drone_sdk.contract import CommandResult
from Support import MockBackend


class RuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, run=None):
        self.device = MockBackend()
        self.device.flying = True
        async def execute(code, host, outcome):
            try:
                value = await run(code, host) if run else {'ok': True, 'status': 'completed'}
                outcome.result = value
            finally:
                outcome.executing = False
        self.runner = SimpleNamespace(retry_cleanup=AsyncMock(return_value={'ok': True}), run=AsyncMock(side_effect=execute))
        return UnifiedRuntime(self.device, self.device, runner=self.runner)

    async def test_stale_stop_permission_does_not_dispatch_or_revoke_again(self):
        runtime = self.runtime()
        old = runtime.generation
        runtime.cancel()
        self.assertIsNone(runtime.begin_interrupt(True, expected_generation=old))
        self.assertFalse(runtime.land_requested)
        self.assertEqual(self.device.calls, [])

    async def test_stop_returns_permission_before_async_dispatch(self):
        runtime = self.runtime()
        receipt = runtime.begin_interrupt(True)
        self.assertEqual(self.device.calls, [])
        self.assertTrue(runtime.land_requested)
        self.assertTrue((await runtime.finish_interrupt(receipt))['ok'])
        self.assertIn(('priority_stop', (True,)), self.device.calls)

    async def test_older_hover_permission_cannot_downgrade_later_land(self):
        runtime = self.runtime()
        hover = runtime.begin_interrupt(False)
        runtime.begin_interrupt(True)
        self.assertEqual((await runtime.finish_interrupt(hover))['reason'], 'stale_interrupt')
        await runtime.interrupt(False)
        self.assertIn(('priority_stop', (True,)), self.device.calls)

    async def test_runtime_finishes_the_exact_context_passed_to_runner(self):
        runtime = self.runtime()
        self.assertTrue((await runtime.run('program'))['ok'])
        host = self.runner.run.call_args.args[1]
        with self.assertRaisesRegex(RuntimeError, 'execution_closed'):
            await host.dispatch('move_forward', [20], {})
        self.assertFalse(runtime.running)

    async def test_pending_cleanup_allows_status_but_blocks_motion(self):
        async def program(code, host):
            method, args = ('get_status', []) if code == 'status' else ('move_forward', [20])
            try:
                return {'ok': True, 'value': await host.dispatch(method, args, {})}
            except RuntimeError as exc:
                return {'ok': False, 'reason': str(exc)}
        runtime = self.runtime(program)
        finish = AsyncMock(side_effect=RuntimeError('release unconfirmed'))
        runtime.pending = SimpleNamespace(finish=finish, cleanup={'status': 'release_unconfirmed'})
        self.assertTrue((await runtime.run('status'))['ok'])
        self.assertIn('readonly_or_priority', (await runtime.run('motion'))['reason'])
        finish.side_effect = None
        self.assertTrue((await runtime.retry_cleanup())['ok'])
        self.assertIsNone(runtime.pending)

    async def test_cancel_during_recovery_never_starts_the_old_program(self):
        runtime = self.runtime()
        entered, release = asyncio.Event(), asyncio.Event()
        async def finish(_success):
            entered.set()
            await release.wait()
        runtime.pending = SimpleNamespace(finish=finish, cleanup={})
        task = asyncio.create_task(runtime.run('old program'))
        await entered.wait()
        runtime.cancel()
        release.set()
        self.assertEqual((await task)['status'], 'cancelled')
        self.runner.run.assert_not_awaited()

    async def test_container_recovery_failure_does_not_skip_control_recovery(self):
        runtime = self.runtime()
        finish = AsyncMock()
        runtime.pending = SimpleNamespace(finish=finish, cleanup={})
        runtime.pending_sandbox = object()
        self.runner.retry_cleanup.return_value = {'ok':False, 'reason':'engine_unavailable'}
        result = await runtime.retry_cleanup()
        self.assertFalse(result['ok'])
        finish.assert_awaited_once_with(False)
        self.assertIsNone(runtime.pending)
        self.assertTrue(result['control']['ok'])
        self.runner.retry_cleanup.assert_awaited_once()

    async def test_repeated_stop_does_not_interrupt_container_cleanup(self):
        entered, cleaning, release, completed = (asyncio.Event() for _ in range(4))
        async def run(_code, _host):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                runtime.current.outcome.executing = False
                cleaning.set()
                await release.wait()
                completed.set()
        runtime = self.runtime(run)
        task = asyncio.create_task(runtime.run('program'))
        await entered.wait()
        runtime.cancel()
        await cleaning.wait()
        runtime.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        self.assertEqual((await task)['status'], 'cancelled')
        self.assertTrue(completed.is_set())

    async def test_external_task_cancel_waits_for_owned_cleanup(self):
        entered, cleaning, release = (asyncio.Event() for _ in range(3))
        async def run(_code, _host):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                runtime.current.outcome.executing = False
                cleaning.set()
                await release.wait()
        runtime = self.runtime(run)
        task = asyncio.create_task(runtime.run('program'))
        await entered.wait()
        task.cancel()
        await cleaning.wait()
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(runtime.running)

    async def test_result_log_failure_does_not_leave_runtime_busy(self):
        runtime = self.runtime()
        def log(event):
            if event['event'] == 'program_result':
                raise OSError('disk unavailable')
        runtime.log = log
        with self.assertRaisesRegex(OSError, 'disk unavailable'):
            await runtime.run('program')
        self.assertFalse(runtime.running)
        self.assertIsNone(runtime.current)
        runtime.log = lambda event: None
        self.assertTrue((await runtime.run('next'))['ok'])

    async def test_cancelled_stop_keeps_its_late_updated_grant_for_cleanup(self):
        import threading
        from RuntimeFixture import reset, sdk
        follow = reset(flying=True)
        entered, release = threading.Event(), threading.Event()
        original = sdk.stop_motion
        def stop(grant, **options):
            result = original(grant, **options)
            if not entered.is_set():
                entered.set()
                release.wait(2)
            return result
        async def execute(code, host, outcome):
            try:
                await host.dispatch('stop_motion', [], {})
                outcome.result = {'ok': True}
            finally:
                outcome.executing = False
        runtime = UnifiedRuntime(sdk, MockBackend(), runner=SimpleNamespace(run=execute))
        try:
            with patch.object(sdk, 'stop_motion', side_effect=stop):
                task = asyncio.create_task(runtime.run('stop'))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                runtime.cancel()
                await task
                self.assertIsNotNone(runtime.pending)
                release.set()
                await asyncio.wait_for(runtime.pending._action_request, 1)
                self.assertTrue((await runtime.retry_cleanup())['ok'])
                self.assertEqual(sdk.get_status().control_owner, 'none')
        finally:
            release.set()
            reset()

    async def test_cleanup_failed_zero_retains_its_updated_grant_until_retry(self):
        from RuntimeFixture import reset, sdk, driver
        from execution.Host import Host
        reset(flying=True)
        host = Host(sdk, MockBackend())
        try:
            await host.acquire()
            original = host.grant
            with patch.object(driver, 'set_velocity', return_value=False):
                with self.assertRaisesRegex(RuntimeError, 'cleanup_stop_failed'):
                    await host.finish(False)
            self.assertTrue(host.acquired)
            self.assertTrue(host.cleanup_pending)
            self.assertEqual(host.cleanup['status'], 'stop_unconfirmed')
            self.assertGreater(host.grant.command_epoch, original.command_epoch)
            self.assertEqual(host.grant.control_epoch, original.control_epoch)
            await host.finish(False)
            self.assertFalse(host.cleanup_pending)
            self.assertEqual(sdk.get_status().control_owner, 'none')
        finally:
            reset()

    def test_summary_keeps_the_actual_last_journal_entries(self):
        result = tool_result({'ok': True, 'journal': [
            {'method': 'get_status', 'result': {'index': i}} for i in range(150)]})
        self.assertEqual([entry['result']['index'] for entry in result['journal']], list(range(138, 150)))
