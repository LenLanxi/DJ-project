"""Behavioral tests migrated from the retired docs/v4-reference runtime."""
import asyncio
import base64
import json
import os
import shutil
import threading
import time
import unittest
import pytest
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock
from Support import MockBackend
from Fixtures import FixtureVision
from execution.Host import Host
from execution.sandbox_runner import SandboxRunner
from ExecutionFixture import run_program, runner_for
from navigation.vision import Vision
class ConfigTests(unittest.TestCase):
    def test_container_boundary(self):
        c=SandboxRunner.command('image','name')
        for flag in ('--network=none','--read-only','--cap-drop=ALL','--pull=never','--memory=128m','--pids-limit=64'):
            self.assertIn(flag,c)
        self.assertNotIn('-v',c); self.assertNotIn('--privileged',c)
        self.assertNotIn('-e',c)


@pytest.mark.docker
@unittest.skipUnless(os.getenv('RUN_DOCKER_TESTS')=='1' and shutil.which('docker'),'requires explicit RUN_DOCKER_TESTS=1 and built Docker image')
class ContainerIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_host_secrets_and_network_unavailable(self):
        code="""import os,socket
assert os.getenv('AI_API_KEY') is None
assert not os.path.exists('/var/run/docker.sock')
assert not os.path.exists('/workspace')
s=socket.socket()
s.settimeout(0.5)
try:
 s.connect(('1.1.1.1',443))
except OSError:
 pass
else:
 raise RuntimeError('unexpected network access')
"""
        result=await run_program(runner_for(MockBackend(), MockBackend(), vision=None, timeout=15), code)
        self.assertTrue(result['ok'],result)
