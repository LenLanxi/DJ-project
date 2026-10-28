"""Generated images -> actual follow -> current sandbox RPC/gateway -> SDK.

No real model, Docker, microphone, camera or aircraft is exercised here. Container
RPC is scripted; detector, controller, backend and coordination are actual code.
"""
import asyncio
from pathlib import Path
import sys
import threading
import unittest
from RuntimeFixture import SyntheticFeed, container, reset, runtime, sdk, wait_until


class FullIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.follow = reset(flying=True)
        self.feed = SyntheticFeed()
        self.follow.set_frame_provider(self.feed)
        self.app = runtime(follow=self.follow)
    def tearDown(self):
        reset()

    async def test_full_follow_ai_handover_without_deadlock(self):
        with container([('start_follow', [])]):
            started = await self.app.run('drone.start_follow()')
        self.assertTrue(started['ok'], started)
        self.assertTrue(self.follow.is_following())
        self.assertEqual(sdk.get_status().control_owner, 'follow')
        self.feed.position = .8
        await wait_until(lambda: any(e.get('event') == 'motion_request' and any(e['channels'])
                                    for e in self.follow.events()))
        before = self.follow.controller.reference_size
        with container([('move_forward', [50])]):
            moved = await asyncio.wait_for(self.app.run('drone.move_forward(50)'), 5)
        self.assertTrue(moved['ok'], moved)
        self.assertEqual(moved['follow_cleanup']['status'], 'follow_resumed')
        self.assertTrue(self.follow.is_following())
        self.assertEqual(sdk.get_status().control_owner, 'follow')
        self.assertAlmostEqual(before, self.follow.controller.reference_size)
        with container([('stop_follow', [])]):
            stopped = await self.app.run('drone.stop_follow()')
        self.assertTrue(stopped['ok'], stopped)
        self.assertFalse(self.follow.is_following())
        self.follow.close(wait=True)
        await wait_until(lambda: not self.follow.status()['background_running'])

    async def test_follow_requires_frame_source(self):
        self.follow.set_frame_provider(None)
        with container([('start_follow', [])]):
            result = await self.app.run('drone.start_follow()')
        self.assertFalse(result['ok'], result)
        self.assertFalse(self.follow.is_following())
        self.assertEqual(sdk.get_status().control_owner, 'none')


if __name__ == '__main__':
    unittest.main(verbosity=2)
