"""One program task, one host capability scope, and an independent stop channel."""
import asyncio
from dataclasses import dataclass
from execution.Host import Host, interrupt_device
from execution.sandbox_runner import SandboxRunner


@dataclass(frozen=True)
class StopReceipt:
    generation: int
    requested_land: bool


async def _settle(task):
    """Caller cancellation requests shutdown; it cannot abandon owned cleanup."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass
        except Exception:
            break


class UnifiedRuntime:
    def __init__(self, device, follow, vision=None, log=None, *, runner=None):
        self.device, self.follow, self.vision = device, follow, vision
        self.runner = runner if runner is not None else SandboxRunner()
        self.log = log or (lambda _event: None)
        self.running = self.closed = False
        self.generation = 0
        self.land_requested = False
        self.execution = self.pending = self.program = None

    def cancel(self):
        self.generation += 1
        if self.execution is not None:
            self.execution.cancel()
        if (self.program is not None and not self.program.done()
                and not self.execution.closed and not self.program.cancelling()):
            self.program.cancel()

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

    async def run(self, code, expected_generation=None):
        if expected_generation is not None and expected_generation != self.generation:
            return {'ok': False, 'status': 'cancelled', 'reason': 'stale_plan'}
        if self.closed or self.running:
            return {'ok': False, 'status': 'cancelled' if self.closed else 'busy',
                    'reason': 'runtime_closed' if self.closed else 'another_program_running'}
        entry_generation = self.generation
        self.running = True
        result = error = None
        host = None
        try:
            await self._recover()
            if self.closed or self.generation != entry_generation:
                return {'ok': False, 'status': 'cancelled', 'reason': 'stale_plan'}
            recovery = self.pending is not None
            vision = None if recovery else self.vision
            if vision is not None:
                vision.reset()
            host = Host(self.device, self.follow, vision, recovery=recovery)
            self.execution = host
            self.log({'event': 'program_start', 'code': code, 'recovery': recovery})
            self.program = asyncio.create_task(self.runner.run(code, host))
            try:
                result = await asyncio.shield(self.program)
            except asyncio.CancelledError as exc:
                if asyncio.current_task().cancelling():
                    error = exc
                    host.cancel()
                    if not host.closed and not self.program.cancelling():
                        self.program.cancel()
                    await _settle(self.program)
                try:
                    result = self.program.result()
                except asyncio.CancelledError as cancelled:
                    if error is not None:
                        error.pending_container = getattr(cancelled, 'pending_container', None)
                        error.cleanup_errors = getattr(cancelled, 'cleanup_errors', ())
                    result = getattr(cancelled, 'program_result',
                        {'ok': False, 'status': 'cancelled', 'reason': 'program_cancelled'})
            except BaseException as exc:
                error = exc
        finally:
            if host is not None:
                async def finish():
                    nonlocal result
                    try:
                        await host.finish(error is None and result is not None and result['ok'])
                    except Exception as exc:
                        if result is not None:
                            result['follow_cleanup_error'] = str(exc)
                            if not (result.get('cleanup_errors') or result.get('pending_container')):
                                result.update(ok=False, status='failed', reason='cleanup_unconfirmed: ' + str(exc))
                        if error is not None:
                            error.follow_cleanup_error = str(exc)
                    if result is not None:
                        result['follow_cleanup'] = host.cleanup
                cleanup = asyncio.create_task(finish())
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError as exc:
                    host.cancel()
                    error = exc
                    await _settle(cleanup)
                if host.cleanup_pending:
                    self.pending = host
            self.execution = self.program = None
            self.running = False
        if error is not None:
            raise error
        self.log({'event': 'program_result', 'result': result})
        return result

    async def retry_cleanup(self):
        if self.running:
            return {'ok': False, 'reason': 'program_cleanup_in_progress'}
        self.running = True
        try:
            control = await self._recover()
            sandbox = await self.runner.retry_cleanup()
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
