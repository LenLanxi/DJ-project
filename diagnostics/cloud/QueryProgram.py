"""Real sandbox programs with status-only RPC and an unconnected synthetic device."""
import time
from app.Runtime import UnifiedRuntime
from drone_sdk.contract import StatusResult, FrameResult, CommandResult
from execution.sandbox_runner import SandboxRunner

QUERY_METHODS = frozenset({'get_status', 'is_following'})

class QueryDevice:
    def __init__(self, picture=None):
        self.picture = None
        if picture is not None:
            import cv2
            self.picture = cv2.imread(str(picture))
            if self.picture is None: raise ValueError('fixture_image_unreadable')
        self.frame_id = 0
        self.interrupts = []

    def get_status(self):
        return StatusResult(80, 0, True, 'landed', 'none', time.time(), simulated=True)

    def get_frame(self):
        self.frame_id += 1
        return FrameResult(None if self.picture is None else self.picture.copy(), self.frame_id, time.time(), self.picture is None)

    def is_following(self): return False
    def stop(self, **kwargs): return CommandResult(True, 'completed')
    def stop_cancelled_operation(self, operation):
        # This device has no motion source or control lease to retire.
        return CommandResult(True, 'completed', 'synthetic_fixture_has_no_motion_device')
    def priority_interrupt(self, land=False):
        self.interrupts.append(bool(land))
        return CommandResult(True, 'completed', 'synthetic_fixture_has_no_motion_device')

class QueryProgram:
    def __init__(self, timeout=15):
        self.sandbox = SandboxRunner(timeout=timeout)
        device = QueryDevice()
        self.runtime = UnifiedRuntime(device, device, runner=self.sandbox, methods=QUERY_METHODS)
        self.container_rpc = False

    async def run(self, code):
        result = await self.runtime.run(code)
        self.container_rpc = any(item.get('method') == 'get_status' and 'result' in item for item in result.get('journal', []))
        return result

    def cancel(self): self.runtime.cancel()
    async def interrupt(self, land=False): return await self.runtime.interrupt(land)
