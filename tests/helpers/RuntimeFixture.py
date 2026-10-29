"""Test-only current-runtime fixtures. No aircraft, API, camera or host exec.

Use the real SDK and follow service; inject a Runner for program orchestration.
"""
import asyncio
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[2]
from drone_sdk import controller as sdk, _driver as driver
from drone_sdk.contract import FrameResult
from app.Runtime import UnifiedRuntime
from follow.r3_follow.vision import synthetic_frame
from follow.r3_follow.service import FollowService
from follow.r3_follow.config import load_config

_follows = []

def make_follow(frame_provider=None):
    follow = FollowService(sdk, config=load_config(), frame_provider=frame_provider)
    _follows.append(follow)
    return follow




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
    for follow in _follows:
        follow.close(wait=True)
    _follows.clear()
    settle()
    sdk.priority_interrupt(land=True)
    settle()
    grant = sdk.request_control('model').grant
    if flying:
        assert sdk.takeoff(grant).ok
    assert sdk.release_control(grant).ok
    return make_follow()


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


def runtime(log=None, device=None, follow=None, runner=None):
    return UnifiedRuntime(device or sdk, follow if follow is not None else make_follow(),
                          log=log, runner=runner)


async def wait_until(predicate, timeout=2):
    deadline = time.monotonic()+timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('condition timed out')
        await asyncio.sleep(.005)
