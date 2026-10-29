"""One owner for structured actions, cancellation and control reconciliation."""
import asyncio
from dataclasses import dataclass, field
from execution.Host import Host, SDKActionError, interrupt_device
from execution.ActionRunner import ActionRunner, ActionOutcome
from app.Tools import validate_action


@dataclass(frozen=True)
class StopReceipt:
    generation: int
    requested_land: bool


@dataclass
class ActionRun:
    host: Host
    outcome: ActionOutcome = field(default_factory=ActionOutcome)
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
    def __init__(self, device, follow, vision=None, log=None, *, runner=None, methods=None, timeout=90):
        self.device, self.follow, self.vision = device, follow, vision
        self.methods = methods
        self.runner = runner if runner is not None else ActionRunner(timeout=timeout)
        self.log = log or (lambda _event: None)
        self.running = self.closed = False
        self.generation = 0
        self.land_requested = False
        self.current = self.pending = None
        self.motion_host = self.motion_task = self.motion_owner = None
        self.motion_channels = None
        self.motion_details = None
        self.motion_startup = None
        self.motion_stop_lock = asyncio.Lock()
        self.motion_reason = 'not_started'

    def cancel(self):
        self.generation += 1
        if self.motion_host is not None:
            self.motion_host.cancel()
        if self.current is not None:
            self.current.cancel()

    def velocity_status(self):
        return {'running': self.motion_host is not None and not self.motion_host.cancelled.is_set(),
                'channels': self.motion_channels, 'last_command': self.motion_details,
                'last_reason': self.motion_reason, 'units': 'RC [-100,100], not cm/s'}

    def _velocity_result(self, result):
        previous = self.motion_details or {}
        self.motion_details = result.get('details')
        details = self.motion_details or {}
        if (previous.get('clamped', False), previous.get('clamp_reason')) != (details.get('clamped', False), details.get('clamp_reason')):
            self.log({'event': 'continuous_motion_clamp_changed', 'details': details})

    async def start_velocity(self, owner, channels):
        if len(channels) != 4 or any(type(value) is not int or not -100 <= value <= 100 for value in channels):
            raise ValueError('start_velocity requires four RC integers in [-100,100]')
        owner._current()
        await self.stop_velocity()
        owner._current()
        if not any(channels):
            return {'ok': True, 'status': 'stopped', 'continuous_motion': self.velocity_status()}
        if owner.acquired:
            owner.resume = False
            await owner._finish_control(True)
        host = Host(self.device, self.follow)
        host.resume = False
        self.motion_host, self.motion_owner = host, owner
        self.motion_channels = tuple(channels)
        self.motion_reason = 'starting'
        startup = self.motion_startup = asyncio.create_task(host.acquire())
        try:
            await asyncio.shield(startup)
            owner._current()
            result = await host._action('update_velocity', *channels)
            if not result.get('ok'):
                raise SDKActionError(result)
            owner._current()
            self._velocity_result(result)
            self.motion_task = asyncio.create_task(self._renew_velocity(host, tuple(channels)))
        except BaseException:
            host.cancel()
            await self.stop_velocity()
            raise
        finally:
            if self.motion_startup is startup:
                self.motion_startup = None
        self.motion_reason = 'running'
        self.log({'event': 'continuous_motion_started', 'channels': channels, 'units': 'RC'})
        return {'ok': True, 'status': 'running', 'command_sent': bool((result.get('details') or {}).get('wire_sent')),
                'motion_verified': False,
                'details': result.get('details'), 'continuous_motion': self.velocity_status(),
                'meaning': 'Host renews RC until stopped, replaced, failed or session ends; not a measured physical speed.'}

    async def _renew_velocity(self, host, channels):
        reason = 'stopped'
        try:
            while not host.cancelled.is_set():
                await asyncio.sleep(.1)
                if host.cancelled.is_set():
                    break
                result = await host._action('update_velocity', *channels)
                if not result.get('ok'):
                    raise RuntimeError(str(result))
                self._velocity_result(result)
        except asyncio.CancelledError:
            reason = 'task_cancelled'
        except Exception as exc:
            reason = str(exc)
        finally:
            self.motion_reason = reason
            host.cancel()
            try:
                await host.finish(False)
            except Exception as exc:
                reason += '; cleanup_unconfirmed: ' + str(exc)
            else:
                if self.motion_host is host:
                    self.motion_host = self.motion_owner = None
                    self.motion_channels = None
                    self.motion_details = None
            self.motion_reason = reason
            self.log({'event': 'continuous_motion_stopped', 'reason': reason, 'cleanup': host.cleanup})

    async def stop_velocity(self):
        async with self.motion_stop_lock:
            host, task = self.motion_host, self.motion_task
            if host is None:
                self.motion_task = None
                return
            host.cancel()
            if self.motion_startup is not None:
                await _settle(self.motion_startup)
            if task is not None:
                await asyncio.shield(task)
            if self.motion_host is host:
                await host.finish(False)
                self.motion_host = self.motion_owner = None
                self.motion_channels = None
                self.motion_details = None
                self.motion_reason = 'stopped'
            if self.motion_task is task:
                self.motion_task = None

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

    async def run(self, request, expected_generation=None):
        validate_action(request)
        if expected_generation is not None and expected_generation != self.generation:
            return {'ok': False, 'status': 'cancelled', 'reason': 'stale_plan'}
        if self.closed or self.running:
            return {'ok': False, 'status': 'cancelled' if self.closed else 'busy',
                    'reason': 'runtime_closed' if self.closed else 'another_action_running'}
        entry_generation = self.generation
        self.running = True
        run = None
        error = None
        try:
            await self._recover()
            if self.closed or self.generation != entry_generation:
                return {'ok': False, 'status': 'cancelled', 'reason': 'stale_plan'}
            recovery = self.pending is not None
            vision = None if recovery else self.vision
            if vision is not None:
                vision.reset()
            run = self.current = ActionRun(Host(self.device, self.follow, vision, recovery=recovery, methods=self.methods, motion=self))
            self.log({'event': 'action_start', 'request': request, 'recovery': recovery})
            run.task = asyncio.create_task(self.runner.run(request, run.host, run.outcome))
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
                    result.update(ok=False, status='cancelled', reason='action_cancelled')
                async def finish():
                    try:
                        if self.motion_owner is run.host and (error is not None or not result['ok']):
                            await self.stop_velocity()
                        await run.host.finish(error is None and result['ok'])
                    except Exception as exc:
                        result['follow_cleanup_error'] = str(exc)
                        result.update(ok=False, status='failed', reason='cleanup_unconfirmed: ' + str(exc))
                    result['follow_cleanup'] = run.host.cleanup
                cleanup = asyncio.create_task(finish())
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError as exc:
                    run.host.cancel()
                    error = exc
                    result.update(ok=False, status='cancelled', reason='action_cancelled')
                    await _settle(cleanup)
                    if self.motion_owner is run.host:
                        await self.stop_velocity()
                if run.host.cleanup_pending:
                    self.pending = run.host
                # Record evidence even when cancellation must propagate.
            self.current = None
            self.running = False
            if run is not None:
                try:
                    self.log({'event': 'action_result', 'result': run.outcome.result})
                except Exception:
                    if error is None:
                        raise  # A log failure cannot replace an existing cancellation/error.
        if error is not None:
            raise error
        return run.outcome.result

    async def retry_cleanup(self):
        if self.running:
            return {'ok': False, 'reason': 'action_cleanup_in_progress'}
        self.running = True
        try:
            await self.stop_velocity()
            control = await self._recover()
            return {'ok': control['ok'], 'control': control}
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
        await self.stop_velocity()
        self.log({'event': 'interrupt_result', 'requested_land': receipt.requested_land,
                  'effective_land': self.land_requested, 'result': result})
        return result

    async def interrupt(self, land=False):
        return await self.finish_interrupt(self.begin_interrupt(land))

    def close(self):
        self.closed = True
        self.cancel()
