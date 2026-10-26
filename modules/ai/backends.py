"""Host-side device backends; only public SDK calls are exposed to the sandbox."""
import importlib
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
import threading
import contextvars
from contextlib import nullcontext
GRANT = contextvars.ContextVar("sdk_action_grant", default=None)

class SDKBackend:
    """Explicit SDK selection. This adapter never imports DJITelloPy directly."""
    def __init__(self, root, simulated=None, *, allow_simulated=False):
        self.grant=None
        root=Path(root).resolve()
        if not (root/'drone_sdk'/'controller.py').is_file():
            raise ValueError('SDK根目录必须含drone_sdk/controller.py')
        sys.path.insert(0,str(root))
        self.sdk=importlib.import_module('drone_sdk.controller')
        actual=Path(self.sdk.__file__).resolve()
        if actual != root/'drone_sdk'/'controller.py':
            raise RuntimeError('当前进程已加载另一份SDK，请重新启动')
        if self.simulated and not allow_simulated:
            raise RuntimeError('real_device_not_connected; simulation is allowed only in explicit tests')
    @property
    def simulated(self):
        from drone_sdk import _driver
        return _driver.SIMULATED
    def pause_follow_for_action(self):
        r,ticket=self.sdk._follow.pause_for_action()
        return {'ok':r.ok,'ticket':ticket}
    def finish_follow_action(self,ticket,success):
        if success and ticket is not None:
            bridge, generation, inner=ticket
            deadline=time.monotonic()+.5
            while bridge.service.last_frame_id<=inner.frame_id and time.monotonic()<deadline:
                time.sleep(.01)
        return self.normalize(self.sdk._follow.finish_action(ticket,'completed' if success else 'failed'))
    def normalize(self,value): return asdict(value) if is_dataclass(value) else value
    def priority_stop(self, land=False):
        """Trusted physical/user interruption; a new lease invalidates old cleanup."""
        with self.sdk._velocity_lock:
            action = 'land' if land else 'stop_motion'
            result = self.normalize(getattr(self.sdk, action)('model', priority=True))
            state = self.sdk.get_status()
            self.grant = {'control_epoch': state.control_epoch, 'command_epoch': state.command_epoch}
            result['grant'] = dict(self.grant)
        return result
    def get_status(self): return self.normalize(self.sdk.get_status())
    def get_frame(self): return self.normalize(self.sdk.get_frame())
    def request_control(self,owner):
        r=self.sdk.request_control(owner)
        if r.ok: self.grant={'control_epoch':r.control_epoch,'command_epoch':r.command_epoch}
        return self.normalize(r)
    def _bound_grant(self):
        context = GRANT.get()
        grant = context if context is not None else self.grant
        if not isinstance(grant,dict): return {'control_epoch':-1,'command_epoch':-1}
        return {'control_epoch':grant.get('control_epoch',-1),
                'command_epoch':grant.get('command_epoch',-1)}
    def release_control(self,owner):
        return self.normalize(self.sdk.release_control(owner, **self._bound_grant()))
    def is_following(self): return self.sdk.is_following()
    def start_follow(self): return self.normalize(self.sdk.start_follow())
    def stop_follow(self): return self.normalize(self.sdk.stop_follow())
    def action(self,name,*args):
        grant=self._bound_grant()
        # Capture the post-stop command counter under the same SDK lock as its
        # validation. A subsequent new model lease must never be borrowed here.
        with self.sdk._velocity_lock if name in ('stop_motion','land') else nullcontext():
            result=self.normalize(getattr(self.sdk,name)('model',*args,**grant))
            result['simulated'] = self.simulated
            if result.get('ok') and name in ('stop_motion','land'):
                s=self.sdk.get_status()
                if s.control_epoch == grant['control_epoch']:
                    self.grant={'control_epoch':s.control_epoch,'command_epoch':s.command_epoch}
                    result['grant']=dict(self.grant)
        return result
