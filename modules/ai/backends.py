"""Host-side device backends; only public SDK calls are exposed to the sandbox."""
import importlib
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
import threading
import contextvars
GRANT = contextvars.ContextVar("sdk_action_grant", default=None)

class MockBackend:
    simulated=True
    def __init__(self):
        self.owner=None; self.flying=False; self.following=False
        self.calls=[]; self.observation_index=0
        self.lock=threading.RLock()
    def get_status(self):
        with self.lock:
            return {'battery':80,'height_cm':80 if self.flying else 0,'connected':True,
                    'flight_state':'flying' if self.flying else 'landed','control_owner':self.owner,
                    'timestamp':time.time(),'simulated':True}
    def request_control(self,owner):
        with self.lock:
            if self.owner not in (None,owner): return {'ok':False,'reason':'busy'}
            self.owner=owner
            return {'ok':True,'current_owner':owner}
    def release_control(self,owner):
        with self.lock:
            if self.owner==owner: self.owner=None
            return {'ok':True,'current_owner':self.owner}
    def is_following(self): return self.following
    def stop_follow(self):
        with self.lock:
            self.following=False
            if self.owner=='follow': self.owner=None
            return {'ok':True}
    def start_follow(self):
        with self.lock:
            if not self.flying: return {'ok':False,'reason':'landed'}
            r=self.request_control('follow')
            if r['ok']: self.following=True
            return r
    def action(self,name,*args):
        with self.lock:
            self.calls.append((name,args))
            if self.owner!='model': return {'ok':False,'status':'failed','reason':'no_control'}
            if name=='takeoff': self.flying=True
            elif name=='land': self.flying=False
            elif name!='stop_motion' and not self.flying:
                return {'ok':False,'status':'failed','reason':'landed'}
            return {'ok':True,'status':'accepted' if name=='update_velocity' else 'completed',
                    'is_blocking':name!='update_velocity','simulated':True}

    def emergency(self, land=False):
        """模拟层的急停：同样跳过队列/控制权检查（真实层走 priority 通道）。"""
        with self.lock:
            self.calls.append(('land' if land else 'stop_motion', ()))
            if land:
                self.flying=False
            return {'ok':True,'status':'accepted','simulated':True}
    def get_frame(self):
        raise RuntimeError('模拟器没有摄像头画面；不能伪造实时视觉')
    def observe_fixture(self,target):
        # Closed-loop demo data, not an image recognition result.
        i=self.observation_index; self.observation_index+=1
        return {'source':'scripted_fixture','simulated':True,'target':target,'visible':True,
                'center_x':0.7 if i==0 else 0.5,'width_fraction':min(0.15+0.10*i,0.45),
                'frame_id':i,'description':'离线红色帐篷场景测试数据'}

class SDKBackend:
    """Explicit SDK selection. This adapter never imports DJITelloPy directly."""
    def __init__(self, root, simulated=True):
        self.simulated=simulated
        self.grant=None
        root=Path(root).resolve()
        if not (root/'drone_sdk'/'controller.py').is_file():
            raise ValueError('SDK根目录必须含drone_sdk/controller.py')
        sys.path.insert(0,str(root))
        self.sdk=importlib.import_module('drone_sdk.controller')
        actual=Path(self.sdk.__file__).resolve()
        if actual != root/'drone_sdk'/'controller.py':
            raise RuntimeError('当前进程已加载另一份SDK，请重新启动')
    @property
    def simulated(self):
        from drone_sdk import _driver
        return _driver.SIMULATED
    @simulated.setter
    def simulated(self, value): pass
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
    def get_status(self): return self.normalize(self.sdk.get_status())
    def get_frame(self): return self.normalize(self.sdk.get_frame())
    def request_control(self,owner):
        r=self.sdk.request_control(owner)
        if r.ok: self.grant={'control_epoch':r.control_epoch,'command_epoch':r.command_epoch}
        return self.normalize(r)
    def release_control(self,owner): return self.normalize(self.sdk.release_control(owner, control_epoch=(self.grant or {}).get('control_epoch',-1)))
    def is_following(self): return self.sdk.is_following()
    def start_follow(self): return self.normalize(self.sdk.start_follow())
    def stop_follow(self): return self.normalize(self.sdk.stop_follow())
    def action(self,name,*args):
        grant=GRANT.get() or self.grant or {'control_epoch':-1,'command_epoch':-1}
        result=self.normalize(getattr(self.sdk,name)('model',*args,**grant))
        if result.get('ok') and name in ('stop_motion','land'):
            s=self.sdk.get_status()
            self.grant={'control_epoch':s.control_epoch,'command_epoch':s.command_epoch}
        return result

    def emergency(self, land=False):
        """急停走底层 priority 通道：不进动作队列、不受 action_result_pending 阻塞。"""
        call = self.sdk.land if land else self.sdk.stop_motion
        result = self.normalize(call('model', priority=True))
        s = self.sdk.get_status()
        self.grant = {'control_epoch': s.control_epoch, 'command_epoch': s.command_epoch}
        return result
