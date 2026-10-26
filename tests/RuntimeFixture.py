"""Test-only current-runtime fixtures. No aircraft, API, camera or host exec.

ContainerProcess supplies scripted RPC frames to the real SandboxRunner. This
checks host protocol/coordination; it does not claim to execute Python in Docker.
"""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT/'modules'/'ai', ROOT/'modules'/'follow'):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
from drone_sdk import controller as sdk, _driver as driver
from drone_sdk.contract import FrameResult
from backends import SDKBackend
from gateway import Gateway
from main import UnifiedRuntime
from sandbox_runner import SandboxRunner
from r3_follow.vision import synthetic_frame


def settle(timeout=3):
    deadline = time.monotonic()+timeout
    while sdk.get_status().flight_state == 'landing' or driver.action_pending():
        if time.monotonic() >= deadline:
            raise AssertionError('simulated landing/action did not settle')
        time.sleep(.005)


def reset(flying=False):
    if not driver.SIMULATED:
        raise AssertionError('Test requires a fresh simulated SDK process; refusing connected aircraft')
    driver.set_simulated_delay(0)
    sdk.stop_follow()
    sdk.set_frame_provider(None)
    settle()
    sdk.request_control('model', priority=True)
    sdk.land('model', priority=True)
    settle()
    if flying:
        assert sdk.takeoff('model').ok
    state = sdk.get_status()
    if state.control_owner != 'none':
        assert sdk.release_control(state.control_owner).ok


class SyntheticFeed:
    def __init__(self, position=.5, size=.25, visible=True):
        self.position, self.size, self.visible = position, size, visible
        self.frame_id = 0
        self.cache = {}
        self.frozen = None
    def __call__(self):
        if self.frozen is not None:
            return self.frozen
        self.frame_id += 1
        key = (self.position, self.size, self.visible)
        if key not in self.cache:
            self.cache[key] = synthetic_frame(x=self.position, size=self.size, visible=self.visible)
        return FrameResult(self.cache[key], self.frame_id, time.time(), False)


class ContainerProcess:
    def __init__(self, calls):
        self.calls = iter(calls)
        self.queue = asyncio.Queue()
        self.returncode = None
        self.stdout = self
        self.stderr = self.Errors()
        self.stdin = self.Input(self)
        self.index = 0
    class Errors:
        async def read(self, _n): return b''
    class Input:
        def __init__(self, parent): self.parent = parent
        def write(self, raw):
            message = json.loads(raw)
            if 'error' in message:
                self.parent.queue.put_nowait(json.dumps({'type':'done','ok':False,'error':message['error']}).encode()+b'\n')
                return
            self.parent.next()
        async def drain(self): pass
    def next(self):
        item = next(self.calls, None)
        if item is None:
            message = {'type':'done','ok':True}
        else:
            method, args = item
            self.index += 1
            message = {'type':'rpc','id':self.index,'method':method,'args':list(args),'kwargs':{}}
        self.queue.put_nowait(json.dumps(message).encode()+b'\n')
    async def readline(self): return await self.queue.get()
    def kill(self): self.returncode = -9
    async def wait(self): return 0 if self.returncode is None else self.returncode


class CompletedProcess:
    returncode = 0
    async def wait(self): return 0


@contextmanager
def container(calls):
    process = ContainerProcess(calls)
    async def create(*args, **kwargs):
        if args[:2] == ('docker','run'):
            return process
        if args[:3] == ('docker','rm','-f'):
            return CompletedProcess()
        raise AssertionError('Unexpected subprocess: '+repr(args))
    with patch.object(SandboxRunner, 'check', new=AsyncMock()), \
         patch('sandbox_runner.asyncio.create_subprocess_exec', side_effect=create):
        yield process


def runtime(log=None, backend=None):
    backend = backend or SDKBackend(ROOT, allow_simulated=True)
    gateway = Gateway(backend)
    runner = SandboxRunner(gateway)
    return UnifiedRuntime(runner, log or (lambda event: None))


async def wait_until(predicate, timeout=2):
    deadline = time.monotonic()+timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('condition timed out')
        await asyncio.sleep(.005)
