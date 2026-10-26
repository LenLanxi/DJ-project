"""A real --rm race exposed by the paid voice probe must not cause false failure.

Unconfirmed removal is still a failure, and must be known BEFORE follow resumes.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
from ContainerWireTests import Process
from Support import MockBackend
from gateway import Gateway
from sandbox_runner import SandboxRunner


def failed_remove():
    process=Process(stderr=b'Error: removal of container is already in progress')
    async def wait():process.returncode=1;return 1
    process.wait=wait
    return process


def listing(code=0,output=b''):
    return SimpleNamespace(returncode=code,communicate=AsyncMock(return_value=(output,b'')))


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_removal_race_confirmed_absent_is_success(self):
        runner=SandboxRunner(Gateway(MockBackend()))
        process=Process([{'type':'done','ok':True}])
        with patch.object(runner,'check',new=AsyncMock()),patch('sandbox_runner.asyncio.create_subprocess_exec',side_effect=[process,failed_remove(),listing()]):
            result=await runner.run('print(1)')
        self.assertTrue(result['ok'],result)
        self.assertFalse(runner.running)

    async def test_unconfirmed_container_cannot_restore_follow(self):
        backend=MockBackend();backend.flying=True;backend.start_follow()
        runner=SandboxRunner(Gateway(backend))
        process=Process([{'type':'rpc','id':1,'method':'move_forward','args':[20],'kwargs':{}},{'type':'done','ok':True}])
        with patch.object(runner,'check',new=AsyncMock()),patch('sandbox_runner.asyncio.create_subprocess_exec',side_effect=[process,failed_remove(),listing(1)]):
            result=await runner.run('drone.move_forward(20)')
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'],'sandbox_cleanup_unconfirmed')
        self.assertFalse(backend.following)
        self.assertIn(('stop_motion',()),backend.calls)

    async def test_listing_must_confirm_no_matching_container(self):
        with patch('sandbox_runner.asyncio.create_subprocess_exec',side_effect=[listing(0,b'containerId'),listing()]):
            self.assertTrue(await SandboxRunner._removed('companionTest'))

    async def test_daemon_error_is_not_proof_of_absence(self):
        with patch('sandbox_runner.asyncio.create_subprocess_exec',return_value=listing(1)):
            self.assertFalse(await SandboxRunner._removed('companionTest'))
