"""Generated navigation frames and a backend recording motion authority."""
import time

from SceneFixture import scene
from Support import MockBackend
from execution.Host import NavigationGuard
from drone_sdk.contract import FrameResult, StatusResult


BOX = [170/640,120/360,320/640,240/360]  # public/cloud xyxy, not tracker xywh


class CameraBackend(MockBackend):
    def __init__(self):
        super().__init__()
        self.image = scene()
        self.origin = time.time()
        self.motion_observations = []
        self.freeze = False
    def get_frame(self):
        import numpy as np
        tick = 1 if self.freeze else int((time.time()-self.origin)*30)+1
        # Model sensor noise without adding a counter, marker, text or geometry.
        noise = np.random.default_rng(tick).integers(-1,2,self.image.shape[:2]+(1,),dtype=np.int16)
        image = np.clip(self.image.astype(np.int16)+noise,0,255).astype(np.uint8)
        return FrameResult(image,tick,self.origin+(tick-1)/30,False)
    def get_status(self):
        return super().get_status()
    def action(self, name, *args, **kwargs):
        if name == 'move_forward':
            guard = kwargs.get('cancelled')
            self.motion_observations.append({'is_navigation_guard':isinstance(guard,NavigationGuard),'expired':guard.is_set()})
        return super().action(name,*args,**kwargs)
