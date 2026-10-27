"""Full Python in an isolated Docker container with bounded host RPC supervision.

Model code is never executed on the host. Device work runs through Gateway and
its captured grant even when a cancelled host worker finishes late.
"""
import asyncio
import json
import shutil
from types import MappingProxyType
import uuid


class SandboxUnavailable(RuntimeError): pass
class ProgramCancelled(RuntimeError): pass


class SandboxRunner:
    CLEANUP_SECONDS = 3
    SPAWN_SETTLE_SECONDS = 2
    def __init__(self, gateway, image='companion-sandbox:0.4', timeout=90, max_rpc=2000):
        if not 0 < timeout <= 3600 or type(max_rpc) is not int or not 1 <= max_rpc <= 10000:
            raise ValueError('invalid sandbox budget')
        self.gateway, self.image = gateway, image
        self.timeout, self.max_rpc = timeout, max_rpc
        self.cancelled = asyncio.Event()
        self.running = False
        self.last_cleanup = MappingProxyType({})

    @staticmethod
    def command(image, name):
        return ['docker', 'run', '--rm', '--init', '--pull=never', '--name', name, '-i',
                '--network=none', '--read-only', '--cap-drop=ALL',
                '--security-opt=no-new-privileges', '--pids-limit=64', '--memory=128m',
                '--memory-swap=128m', '--cpus=0.5', '--ulimit', 'nofile=128:128',
                '--user=65534:65534', '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m', image]

    def cancel(self):
        self.cancelled.set()
        self.gateway.cancelled.set()
        self.gateway.resume = False

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

    async def _race_cancel(self, operation):
        work = asyncio.ensure_future(operation)
        cancelled = asyncio.create_task(self.cancelled.wait())
        try:
            done, _ = await asyncio.wait((work, cancelled), return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done or self.cancelled.is_set():
                raise ProgramCancelled('program_cancelled')
            return work.result()
        finally:
            for task in (work, cancelled):
                if not task.done(): task.cancel()
            await asyncio.gather(work, cancelled, return_exceptions=True)

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
                if exit_code == 0:
                    return []
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
            # An empty listing alone cannot fence a create. For --rm's
            # removal-in-progress race, first require observed worker output.
            if known_created and await self._removed(name):
                return []
            return ['container_remove_exit: ' + str(exit_code)]

    async def run(self, code):
        if self.running:
            return {'ok':False, 'status':'busy', 'reason':'another_program_running'}
        if not isinstance(code, str) or not code.strip() or len(code.encode()) > 60000:
            return {'ok':False, 'status':'failed', 'reason':'invalid_program_size'}
        self.running = True
        self.cancelled.clear()
        journal, debug = [], bytearray()
        response_bytes = 0
        name = 'companion-' + uuid.uuid4().hex
        process = None
        spawn_task = None
        stderr_task = None
        result = {'ok':False, 'status':'failed'}
        begun = output_limit = container_attempted = known_created = False
        cleanup_errors = []
        external_cancel = None
        try:
            self.gateway.begin()
            begun = True
            async with asyncio.timeout(self.timeout):
                await self._race_cancel(self.check())
                if self.cancelled.is_set(): raise ProgramCancelled()
                container_attempted = True
                spawn_task = asyncio.create_task(asyncio.create_subprocess_exec(*self.command(self.image, name),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=16001))
                # Cancellation must not lose the Process when OS spawn and the
                # cancel event complete in the same loop turn.
                process = await self._race_cancel(asyncio.shield(spawn_task))
                process.stdin.write(json.dumps({'code':code}).encode() + b'\n')
                await self._race_cancel(process.stdin.drain())
                async def drain_errors():
                    nonlocal output_limit
                    count = 0
                    while chunk := await process.stderr.read(4096):
                        count += len(chunk)
                        debug.extend(chunk[:max(0, 16000-len(debug))])
                        if count > 64000:
                            output_limit = True
                            self.cancel()  # also abort an in-flight host RPC
                            return
                stderr_task = asyncio.create_task(drain_errors())
                expected_id = 1
                for _ in range(self.max_rpc + 1):
                    line = await self._race_cancel(process.stdout.readline())
                    if not line:
                        result = {'ok':False, 'status':'failed', 'reason':'sandbox_closed_without_result'}
                        break
                    if len(line) > 16000: raise ValueError('sandbox RPC request too large')
                    message = json.loads(line)
                    if not isinstance(message, dict): raise ValueError('invalid RPC message')
                    if message.get('type') == 'done':
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
                        value = await self._race_cancel(self.gateway.dispatch(message['method'], message.get('args', []), message.get('kwargs', {})))
                        # Full image/audio payloads may reach the isolated worker,
                        # but retaining one per RPC would grow host memory by GB.
                        entry['result'] = self.evidence(value)
                        reply = {'id':message['id'], 'result':value}
                    except ProgramCancelled:
                        raise
                    except Exception as exc:
                        entry['error'] = str(exc)[:1000]
                        reply = {'id':message['id'], 'error':entry['error']}
                    if self.cancelled.is_set(): raise ProgramCancelled()
                    encoded_reply = json.dumps(reply, ensure_ascii=False, allow_nan=False).encode() + b'\n'
                    response_bytes += len(encoded_reply)
                    if len(encoded_reply) > 8_000_000 or response_bytes > 16_000_000:
                        raise ValueError('host RPC response budget exhausted')
                    process.stdin.write(encoded_reply)
                    await self._race_cancel(process.stdin.drain())
                else:
                    result = {'ok':False, 'status':'failed', 'reason':'RPC budget exhausted'}
                if result.get('ok') and any('error' in entry or isinstance(entry.get('result'), dict) and entry['result'].get('ok') is False for entry in journal):
                    result = {'ok':False, 'status':'failed', 'reason':'one_or_more_interface_calls_failed'}
        except SandboxUnavailable as exc:
            result = {'ok':False, 'status':'unavailable', 'reason':str(exc)}
        except ProgramCancelled:
            result = {'ok':False, 'status':'cancelled', 'reason':'program_cancelled'}
        except TimeoutError:
            self.cancel()
            result = {'ok':False, 'status':'timeout', 'reason':'program_deadline_exceeded'}
        except asyncio.CancelledError as exc:
            external_cancel = exc
            self.cancel()
            result = {'ok':False, 'status':'cancelled', 'reason':'task_cancelled'}
            raise
        except Exception as exc:
            result = {'ok':False, 'status':'failed', 'reason':type(exc).__name__ + ': ' + str(exc)[:500]}
        finally:
            cleanup_finished = False
            try:
                if process is None and spawn_task is not None:
                    try:
                        process = await asyncio.wait_for(asyncio.shield(spawn_task), self.SPAWN_SETTLE_SECONDS)
                    except Exception as exc:
                        cleanup_errors.append('docker_spawn_unconfirmed: ' + type(exc).__name__)
                    finally:
                        if not spawn_task.done():
                            spawn_task.cancel()
                            await asyncio.gather(spawn_task, return_exceptions=True)
                if container_attempted:
                    try:
                        cleanup_errors.extend(await self._remove_container(name, known_created))
                    except (OSError, TimeoutError, ProcessLookupError) as exc:
                        cleanup_errors.append('container_remove: ' + str(exc))
                # The remove/create fence must precede killing run's CLI.
                if process is not None:
                    try: await self._kill_wait(process)
                    except (OSError, TimeoutError, ProcessLookupError) as exc:
                        cleanup_errors.append('docker_cli_wait: ' + str(exc))
                if stderr_task:
                    try: await asyncio.wait_for(asyncio.shield(stderr_task), .5)
                    except TimeoutError:
                        stderr_task.cancel()
                        await asyncio.gather(stderr_task, return_exceptions=True)
                        cleanup_errors.append('debug_stream_incomplete')
                    except Exception as exc:
                        cleanup_errors.append('debug_stream: ' + str(exc))
                if output_limit:
                    result = {'ok':False, 'status':'failed', 'reason':'sandbox_output_budget_exceeded'}
                if cleanup_errors:
                    result = {'ok':False, 'status':'failed', 'reason':'sandbox_cleanup_unconfirmed',
                              'cleanup_errors':cleanup_errors,
                              'pending_container':name if container_attempted else None}
                if begun:
                    try: await self.gateway.finish(result.get('ok') is True)
                    except Exception as exc:
                        if cleanup_errors:
                            result['follow_cleanup_error'] = str(exc)[:500]
                        else:
                            result = {'ok':False, 'status':'failed', 'reason':'cleanup_or_follow_resume_failed: ' + str(exc)[:500]}
                cleanup_finished = True
            except asyncio.CancelledError as exc:
                external_cancel = exc
                self.cancel()
                raise
            finally:
                if not cleanup_finished:
                    cleanup_errors.append('cleanup_interrupted')
                # Task.cancel() preserves its exception contract, so its caller
                # cannot receive the normal result. Retain cleanup evidence in
                # both the runner and that exception, including the name needed
                # to reconcile a create that may still finish after the deadline.
                pending = name if container_attempted and cleanup_errors else None
                self.last_cleanup = MappingProxyType({
                    'container_name':name if container_attempted else None,
                    'pending_container':pending,
                    'cleanup_errors':tuple(cleanup_errors),
                    'confirmed':not cleanup_errors,
                    'follow_cleanup_error':result.get('follow_cleanup_error'),
                })
                if external_cancel is not None:
                    external_cancel.pending_container = pending
                    external_cancel.cleanup_errors = tuple(cleanup_errors)
                self.running = False
        return {**result, 'simulated':self.gateway.simulated, 'journal':journal,
                'follow_cleanup':dict(self.gateway.cleanup),
                'debug':debug.decode(errors='replace')}

    @staticmethod
    def evidence(value):
        """Keep evidence and capture metadata, not repeated media buffers."""
        if isinstance(value, dict):
            return {str(k): ('[payload omitted]' if str(k).lower() in ('image_url','frame','audio','pcm')
                             else SandboxRunner.evidence(v)) for k,v in value.items()}
        if isinstance(value, list):
            return [SandboxRunner.evidence(v) for v in value]
        return value
