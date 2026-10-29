"""Full Python in an isolated Docker container with bounded host RPC supervision.

Model code is never executed on the host. Device work runs through Host and
its captured grant even when a cancelled host worker finishes late.
"""
import asyncio
import json
import shutil
from dataclasses import dataclass, field
import uuid


class SandboxUnavailable(RuntimeError): pass
class ProgramCancelled(RuntimeError): pass


@dataclass
class SandboxOutcome:
    """Evidence owned by one ProgramRun, including cancellation cleanup."""
    result: dict = field(default_factory=dict)
    cleanup: dict = field(default_factory=dict)
    executing: bool = True


class SandboxRunner:
    CLEANUP_SECONDS = 10
    SPAWN_SETTLE_SECONDS = 2
    def __init__(self, image='companion-sandbox:0.5', timeout=90, max_rpc=2000):
        if not 0 < timeout <= 3600 or type(max_rpc) is not int or not 1 <= max_rpc <= 10000:
            raise ValueError('invalid sandbox budget')
        self.image = image
        self.timeout, self.max_rpc = timeout, max_rpc
        self.container = None
        self.created = False

    @staticmethod
    def command(image, name):
        return ['docker', 'run', '--rm', '--pull=never', '--name', name, '-d',
                '--network=none', '--read-only', '--cap-drop=ALL',
                '--security-opt=no-new-privileges', '--pids-limit=64', '--memory=128m',
                '--memory-swap=128m', '--cpus=0.5', '--ulimit', 'nofile=128:128',
                '--user=65534:65534', '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
                '--entrypoint=python', image, '-I', '-u', '-c',
                "import signal;signal.signal(signal.SIGCHLD,signal.SIG_IGN);signal.pause()"]

    @staticmethod
    async def _removed(name):
        """Confirm absence through a successful engine listing, not an error code.

        docker --rm can race an explicit rm with 'removal already in progress'.
        A failed inspect cannot distinguish absence from an unavailable daemon.
        """
        until = asyncio.get_running_loop().time() + 2
        while True:
            query = await asyncio.create_subprocess_exec('docker','ps','-a',
                '--filter','name=^/' + name + '$','--format','{{.ID}}',
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
            try:
                output,_ = await asyncio.wait_for(query.communicate(), 2)
            except TimeoutError:
                await SandboxRunner._kill_wait(query)
                return False
            if query.returncode != 0:
                return False
            if not output.strip():
                return True
            if asyncio.get_running_loop().time() >= until:
                return False
            await asyncio.sleep(.05)

    @staticmethod
    async def _kill_wait(process, timeout=2):
        if process.returncode is None:
            try: process.kill()
            except ProcessLookupError: pass
        await asyncio.wait_for(process.wait(), timeout)

    async def check(self):
        if not shutil.which('docker'):
            raise SandboxUnavailable('Docker is required; host exec fallback is forbidden')
        process = await asyncio.create_subprocess_exec('docker', 'image', 'inspect', self.image,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            if await asyncio.wait_for(process.wait(), 10) != 0:
                raise SandboxUnavailable('Docker engine unavailable or sandbox image not built')
        except BaseException:
            try: await self._kill_wait(process)
            except (OSError, TimeoutError, ProcessLookupError): pass
            raise

    async def _remove_container(self, name, known_created):
        """Remove before killing run's CLI, which may still be creating it.

        docker run performs one synchronous create followed by start. A
        successful rm proves that creation finished and that the container was
        deleted; killing the CLI after that cannot create it again. In contrast,
        an early 404 while run is alive is not evidence of permanent absence.
        """
        until = asyncio.get_running_loop().time() + self.CLEANUP_SECONDS
        while True:
            cleanup = await asyncio.create_subprocess_exec('docker', 'rm', '-f', name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            try:
                remaining = max(.01, until-asyncio.get_running_loop().time())
                exit_code = await asyncio.wait_for(cleanup.wait(), remaining)
                stream = getattr(cleanup, 'stderr', None)
                error = await asyncio.wait_for(stream.read(4096), 1) if stream is not None else b''
            except TimeoutError:
                await self._kill_wait(cleanup)
                return ['container_remove_timeout']
            if b'No such container' in error:
                if known_created:
                    # Worker output fences create, but still require a healthy
                    # engine's empty listing to confirm --rm has finished.
                    return [] if await self._removed(name) else ['container_absence_unconfirmed']
                if asyncio.get_running_loop().time() >= until:
                    return ['container_creation_unconfirmed']
                await asyncio.sleep(.05)
                continue
            # Docker Desktop can return zero together with "No such container".
            # Read that evidence before treating exit zero as a create fence.
            if exit_code == 0:
                return []
            # An empty listing alone cannot fence a create. For --rm's
            # removal-in-progress race, first require observed worker output.
            if known_created and await self._removed(name):
                return []
            return ['container_remove_exit: ' + str(exit_code)]

    async def retry_cleanup(self, outcome):
        """Reconcile only this runner's previously unconfirmed container."""
        name = outcome.cleanup.get('pending_container')
        if name is None:
            return {'ok': True, 'status': 'no_pending_container'}
        try:
            errors = await self._remove_container(name, outcome.cleanup['known_created'])
        except (OSError, TimeoutError, ProcessLookupError) as exc:
            errors = ['container_remove: ' + str(exc)]
        if not errors and self.container == name:
            self.container, self.created = None, False
        outcome.cleanup = {**outcome.cleanup,
            'pending_container': name if errors else None,
            'cleanup_errors': tuple(errors), 'confirmed': not errors}
        return {'ok': not errors, 'status': 'cleanup_pending' if errors else 'removed',
                'container_name': name, 'pending_container': name if errors else None,
                'cleanup_errors': errors}

    async def _reset(self, name, worker_pid):
        # Run trusted cleanup only AFTER the model interpreter has exited,
        # including its threads and atexit handlers. No model state is reused.
        process = await asyncio.create_subprocess_exec('docker', 'exec', name,
            'python', '-I', '-u', '/app/worker.py', '--cleanup', str(worker_pid),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, error = await asyncio.wait_for(process.communicate(), 4)
            if process.returncode != 0:
                raise RuntimeError('resident_reset_failed: ' + error.decode(errors='replace')[:300])
        finally:
            if process.returncode is None: await self._kill_wait(process)

    async def run(self, code, execution, outcome):
        if not isinstance(code, str) or not code.strip() or len(code.encode()) > 60000:
            outcome.result = {'ok':False, 'status':'failed', 'reason':'invalid_program_size'}
            outcome.executing = False
            return
        journal, debug = [], bytearray()
        response_bytes = 0
        name = self.container or 'companion-' + uuid.uuid4().hex
        process = None
        spawn_task = None
        stderr_task = None
        worker_pid = None
        result = {'ok':False, 'status':'failed'}
        container_attempted = known_created = False
        cleanup_errors = []
        startup = None
        debug_warnings = []
        debug_truncated = False
        try:
            async with asyncio.timeout(self.timeout):
                if execution.cancelled.is_set(): raise ProgramCancelled()
                if self.container is None:
                    await self.check()
                    self.container = name
                    container_attempted = True
                    async def start():
                        proc = await asyncio.create_subprocess_exec(*self.command(self.image, name),
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        try:
                            code = await proc.wait()
                            if code != 0:
                                raise SandboxUnavailable('resident sandbox could not start')
                            self.created = True
                        finally:
                            if proc.returncode is None: await self._kill_wait(proc)
                    startup = asyncio.create_task(start())
                    await asyncio.shield(startup)
                container_attempted = known_created = self.created
                spawn_task = asyncio.create_task(asyncio.create_subprocess_exec(
                    'docker', 'exec', '-i', name, 'python', '-I', '-u', '/app/worker.py',
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=16001))
                process = await asyncio.shield(spawn_task)
                process.stdin.write(json.dumps({'code':code}).encode() + b'\n')
                await process.stdin.drain()
                async def drain_errors():
                    nonlocal debug_truncated
                    while chunk := await process.stderr.read(4096):
                        if len(debug) + len(chunk) > 16000:
                            debug_truncated = True
                        debug.extend(chunk[:max(0, 16000-len(debug))])
                stderr_task = asyncio.create_task(drain_errors())
                expected_id = 1
                for _ in range(self.max_rpc + 1):
                    line = await process.stdout.readline()
                    if not line:
                        result = {'ok':False, 'status':'failed', 'reason':'sandbox_closed_without_result'}
                        break
                    if len(line) > 16000: raise ValueError('sandbox RPC request too large')
                    message = json.loads(line)
                    if not isinstance(message, dict): raise ValueError('invalid RPC message')
                    if message.get('type') == 'done':
                        worker_pid = message.get('worker_pid')
                        known_created = True
                        result = {'ok':message.get('ok') is True,
                                  'status':'completed' if message.get('ok') is True else 'failed',
                                  'reason':message.get('error', '')}
                        break
                    if message.get('type') != 'rpc': raise ValueError('invalid sandbox protocol')
                    known_created = True
                    if type(message.get('id')) is not int or message['id'] != expected_id:
                        raise ValueError('invalid or repeated RPC sequence')
                    expected_id += 1
                    if not isinstance(message.get('method'), str): raise ValueError('invalid RPC method')
                    if len(journal) >= self.max_rpc: raise ValueError('RPC budget exhausted')
                    entry = {'method':message['method'], 'args':message.get('args'), 'kwargs':message.get('kwargs')}
                    journal.append(entry)
                    try:
                        value = await execution.dispatch(message['method'], message.get('args', []), message.get('kwargs', {}))
                        # Full image/audio payloads may reach the isolated worker,
                        # but retaining one per RPC would grow host memory by GB.
                        entry['result'] = self.evidence(value)
                        reply = {'id':message['id'], 'result':value}
                    except ProgramCancelled:
                        raise
                    except Exception as exc:
                        entry['error'] = str(exc)[:1000]
                        if hasattr(exc, 'result'):
                            entry['result'] = self.evidence(exc.result)
                        reply = {'id':message['id'], 'error':entry['error']}
                    if execution.cancelled.is_set(): raise ProgramCancelled()
                    encoded_reply = json.dumps(reply, ensure_ascii=False, allow_nan=False).encode() + b'\n'
                    response_bytes += len(encoded_reply)
                    if len(encoded_reply) > 8_000_000 or response_bytes > 16_000_000:
                        raise ValueError('host RPC response budget exhausted')
                    process.stdin.write(encoded_reply)
                    await process.stdin.drain()
        except SandboxUnavailable as exc:
            result = {'ok':False, 'status':'unavailable', 'reason':str(exc)}
        except ProgramCancelled:
            execution.cancel()
            result = {'ok':False, 'status':'cancelled', 'reason':'program_cancelled'}
        except TimeoutError:
            execution.cancel()
            result = {'ok':False, 'status':'timeout', 'reason':'program_deadline_exceeded'}
        except asyncio.CancelledError:
            execution.cancel()
            result = {'ok':False, 'status':'cancelled', 'reason':'task_cancelled'}
            raise
        except Exception as exc:
            result = {'ok':False, 'status':'failed', 'reason':type(exc).__name__ + ': ' + str(exc)[:500]}
        finally:
            outcome.executing = False
            cleanup_finished = False
            try:
                if startup is not None and not startup.done():
                    try: await asyncio.wait_for(asyncio.shield(startup), self.SPAWN_SETTLE_SECONDS)
                    except Exception: pass
                    finally:
                        if not startup.done(): startup.cancel()
                        await asyncio.gather(startup, return_exceptions=True)
                known_created = self.created
                if process is None and spawn_task is not None:
                    try:
                        process = await asyncio.wait_for(asyncio.shield(spawn_task), self.SPAWN_SETTLE_SECONDS)
                    except Exception as exc:
                        cleanup_errors.append('docker_spawn_unconfirmed: ' + type(exc).__name__)
                    finally:
                        if not spawn_task.done():
                            spawn_task.cancel()
                            await asyncio.gather(spawn_task, return_exceptions=True)
                if process is not None and result.get('ok'):
                    try:
                        await self._reset(name, worker_pid)
                        await asyncio.wait_for(process.wait(), 2)
                        if process.returncode != 0:
                            raise RuntimeError('sandbox_worker_exit_failed')
                    except Exception as exc:
                        result = {'ok': False, 'status': 'failed', 'reason': str(exc) or type(exc).__name__}
                if container_attempted and not result.get('ok'):
                    try:
                        cleanup_errors.extend(await self._remove_container(name, known_created))
                        if not cleanup_errors:
                            self.container, self.created = None, False
                    except (OSError, TimeoutError, ProcessLookupError) as exc:
                        cleanup_errors.append('container_remove: ' + str(exc))
                # The remove/create fence must precede killing run's CLI.
                if process is not None:
                    try:
                        if not result.get('ok'):
                            await self._kill_wait(process)
                    except (OSError, TimeoutError, ProcessLookupError) as exc:
                        cleanup_errors.append('docker_cli_wait: ' + str(exc))
                if stderr_task:
                    try: await asyncio.wait_for(asyncio.shield(stderr_task), .5)
                    except TimeoutError:
                        stderr_task.cancel()
                        await asyncio.gather(stderr_task, return_exceptions=True)
                        debug_warnings.append('debug_stream_incomplete')
                    except Exception as exc:
                        debug_warnings.append('debug_stream: ' + str(exc))
                if cleanup_errors:
                    result = {'ok':False, 'status':'failed', 'reason':'sandbox_cleanup_unconfirmed',
                              'failure_reason':result.get('reason'),
                              'cleanup_errors':cleanup_errors,
                              'pending_container':name if container_attempted else None}
                cleanup_finished = True
            except asyncio.CancelledError:
                execution.cancel()
                raise
            finally:
                if not cleanup_finished:
                    cleanup_errors.append('cleanup_interrupted')
                pending = name if container_attempted and cleanup_errors else None
                outcome.cleanup = {
                    'container_name': name if container_attempted else None,
                    'known_created': known_created,
                    'resident_container': self.container if not cleanup_errors else None,
                    'pending_container': pending,
                    'cleanup_errors': tuple(cleanup_errors),
                    'confirmed': not cleanup_errors,
                }
                action_error = next((entry['result'] for entry in reversed(journal)
                                     if 'error' in entry and 'result' in entry), None)
                if action_error is not None: result['action_error'] = action_error
                outcome.result = {**result, 'simulated': execution.simulated,
                    'journal': journal, 'debug': debug.decode(errors='replace'),
                    'debug_truncated': debug_truncated, 'debug_warnings': debug_warnings}

    async def close(self):
        if self.container is None:
            return {'ok': True, 'status': 'no_resident_container'}
        name = self.container
        errors = await self._remove_container(name, self.created)
        if not errors:
            self.container, self.created = None, False
        return {'ok': not errors, 'status': 'cleanup_pending' if errors else 'removed',
                'container_name': name, 'cleanup_errors': errors}

    @staticmethod
    def evidence(value):
        """Keep evidence and capture metadata, not repeated media buffers."""
        if isinstance(value, dict):
            return {str(k): ('[payload omitted]' if str(k).lower() in ('image_url','frame','audio','pcm')
                             else SandboxRunner.evidence(v)) for k,v in value.items()}
        if isinstance(value, list):
            return [SandboxRunner.evidence(v) for v in value]
        return value
