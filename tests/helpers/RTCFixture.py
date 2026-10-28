"""Generated camera frames and deterministic RTC runtime substitute."""
import asyncio
import time
import numpy as np
from drone_sdk.contract import FrameResult


class Camera:
    simulated = True
    def __init__(self):
        self.stale = False
        self.index = 0
    def get_frame(self):
        self.index += 1
        return FrameResult(np.zeros((16,16,3),dtype=np.uint8),self.index,
                           time.time()-2 if self.stale else time.time(),self.stale)


class Runtime:
    def __init__(self):
        self.generation = 0
        self.running = False
        self.calls = []
        self.release = asyncio.Event()
        self.release.set()
        self.interrupt_error = False
    def cancel(self):
        self.generation += 1
        self.calls.append(('cancel',))
        self.release.set()
    async def interrupt(self, land=False):
        self.calls.append(('interrupt', land))
        if self.interrupt_error:
            raise OSError('fake failed priority send')
        self.cancel()
        return {'ok':True,'status':'accepted'}
    async def run(self, code, expected_generation=None):
        if expected_generation != self.generation:
            return {'ok':False,'status':'cancelled','reason':'stale_plan'}
        self.running = True
        self.calls.append(('run', code))
        try:
            await self.release.wait()
            return {'ok':True,'status':'completed','simulated':True,
                    'journal':[{'method':'get_status','args':[],'result':{'flight_state':'landed'}}],
                    'debug':'actual result fixture'}
        finally: self.running = False
