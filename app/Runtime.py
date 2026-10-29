"""One owner for program execution, cancellation and resource reconciliation."""
import asyncio
from dataclasses import dataclass, field
from execution.Host import Host, interrupt_device
from execution.sandbox_runner import SandboxRunner, SandboxOutcome


@dataclass(frozen=True)
class StopReceipt:
    generation: int
    requested_land: bool


@dataclass
class ProgramRun:
    host: Host
    outcome: SandboxOutcome = field(default_factory=SandboxOutcome)
    task: asyncio.Task | None = None

    def cancel(self):
        self.host.cancel()
        if (self.task is not None and not self.task.done()
                and self.outcome.executing and not self.task.cancelling()):
            self.task.cancel()


async def _settle(task):
    """Repeated caller cancellation cannot abandon owned cleanup."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass
        except Exception:
            break


class UnifiedRuntime:
    def __init__(self, device, follow, vision=None, log=None, *, runner=None, methods=None):
        self.device, self.follow, self.vision = device, follow, vision
        self.methods = methods
        self.runner = runner if runner is not None else SandboxRunner()
        self.log = log or (lambda _event: None)
        self.running = self.closed = False
        self.generation = 0
        self.land_requested = False
        self.current = self.pending = self.pending_sandbox = None

    def cancel(self):
        self.generation += 1
        if self.current is not None:
            self.current.cancel()

    async def _recover(self):
        host = self.pending
        if host is None:
            return {'ok': True, 'status': 'no_pending_control'}
        try:
            await host.finish(False)
        except Exception as exc:
            return {'ok': False, 'reason': str(exc), 'follow_cleanup': host.cleanup}
        self.pending = None
        return {'ok': True, 'follow_cleanup': host.cleanup}

    async def _recover_sandbox(self):
        if self.pending_sandbox is None:
            return {'ok': True, 'status': 'no_pending_container'}
        result = await self.runner.retry_cleanup(self.pending_sandbox)
        if result['ok']:
            self.pending_sandbox = None
        return result

    async def run(self, code, expected_generation=None):
        if expected_generation is not None and expected_generation != self.generation:
            return {'ok': False, 'status': 'cancelled', 'reason': 'stale_plan'}
        if self.closed or self.running:
            return {'ok': False, 'status': 'cancelled' if self.closed else 'busy',
                    'reason': 'runtime_closed' if self.closed else 'another_program_running'}
        entry_generation = self.generation
        self.running = True
        run = None
        error = None
        try:
            await self._recover()
            sandbox = await self._recover_sandbox()
            if not sandbox['ok']:
                return {**sandbox, 'ok': False, 'status': 'failed', 'reason': 'sandbox_cleanup_pending'}
            if self.closed or self.generation != entry_generation:
                return {'ok': False, 'status': 'cancelled', 'reason': 'stale_plan'}
            recovery = self.pending is not None
            vision = None if recovery else self.vision
            if vision is not None:
                vision.reset()
            run = self.current = ProgramRun(Host(self.device, self.follow, vision, recovery=recovery, methods=self.methods))
            self.log({'event': 'program_start', 'code': code, 'recovery': recovery})
            run.task = asyncio.create_task(self.runner.run(code, run.host, run.outcome))
            try:
                await asyncio.shield(run.task)
            except asyncio.CancelledError as exc:
                if asyncio.current_task().cancelling():
                    error = exc
                    run.cancel()
                    await _settle(run.task)
                # Consume task cancellation; all evidence is already in outcome.
                try:
                    run.task.result()
                except asyncio.CancelledError:
                    pass
            except BaseException as exc:
                error = exc
        finally:
            if run is not None:
                run.host.close_admission()
                result = run.outcome.result
                if not result:
                    result.update(ok=False, status='cancelled', reason='program_cancelled')
                async def finish():
                    try:
                        await run.host.finish(error is None and result['ok'])
                    except Exception as exc:
                        result['follow_cleanup_error'] = str(exc)
                        if not (result.get('cleanup_errors') or result.get('pending_container')):
                            result.update(ok=False, status='failed', reason='cleanup_unconfirmed: ' + str(exc))
                    result['follow_cleanup'] = run.host.cleanup
                cleanup = asyncio.create_task(finish())
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError as exc:
                    run.host.cancel()
                    error = exc
                    await _settle(cleanup)
                if run.host.cleanup_pending:
                    self.pending = run.host
                if run.outcome.cleanup.get('pending_container'):
                    self.pending_sandbox = run.outcome
                # Record evidence even when cancellation must propagate.
            self.current = None
            self.running = False
            if run is not None:
                try:
                    self.log({'event': 'program_result', 'result': run.outcome.result})
                except Exception:
                    if error is None:
                        raise  # A log failure cannot replace an existing cancellation/error.
        if error is not None:
            raise error
        return run.outcome.result

    async def retry_cleanup(self):
        if self.running:
            return {'ok': False, 'reason': 'program_cleanup_in_progress'}
        self.running = True
        try:
            control = await self._recover()
            sandbox = await self._recover_sandbox()
            return {'ok': control['ok'] and sandbox['ok'], 'control': control, 'sandbox': sandbox}
        finally:
            self.running = False

    def begin_interrupt(self, land=False, expected_generation=None):
        if expected_generation is not None and expected_generation != self.generation:
            return None
        self.land_requested |= bool(land)
        self.cancel()
        return StopReceipt(self.generation, bool(land))

    async def finish_interrupt(self, receipt):
        if receipt.generation != self.generation:
            return {'ok': False, 'status': 'cancelled', 'reason': 'stale_interrupt'}
        result = await interrupt_device(self.device, self.follow, self.land_requested)
        self.log({'event': 'interrupt_result', 'requested_land': receipt.requested_land,
                  'effective_land': self.land_requested, 'result': result})
        return result

    async def interrupt(self, land=False):
        return await self.finish_interrupt(self.begin_interrupt(land))

    def close(self):
        self.closed = True
        self.cancel()


    async def close_sandbox(self):
        return await self.runner.close() if isinstance(self.runner, SandboxRunner) else {'ok': True}
