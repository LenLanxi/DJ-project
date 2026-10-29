"""Compose the real runtime with scripted container I/O in tests."""
from unittest.mock import patch
from execution.sandbox_runner import SandboxRunner
from execution.Host import Host
from app.Runtime import UnifiedRuntime
from Support import MockBackend


def runner_for(device=None, follow=None, vision=None, **options):
    device = device if device is not None else MockBackend()
    runner = SandboxRunner(**options)
    runner.test_device = device
    runner.test_follow = follow if follow is not None else device
    runner.test_vision = vision
    return runner


async def run_program(runner, code):
    if not hasattr(runner, 'test_runtime'):
        runner.test_runtime = UnifiedRuntime(runner.test_device, runner.test_follow, runner.test_vision, runner=runner)
        original = runner.run
        async def run(code, host, outcome):
            runner.test_outcome = outcome
            return await original(code, host, outcome)
        runner.run = run
    runtime = runner.test_runtime
    def capture(*args, **kwargs):
        runner.test_host = Host(*args, **kwargs)
        return runner.test_host
    with patch('app.Runtime.Host', side_effect=capture):
        return await runtime.run(code)
