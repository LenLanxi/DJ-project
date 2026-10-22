"""Host-side device backends; only public SDK calls are exposed to the sandbox."""
import importlib
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
import threading

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
        root=Path(root).resolve()
        if not (root/'drone_sdk'/'controller.py').is_file():
            raise ValueError('SDK根目录必须含drone_sdk/controller.py')
        sys.path.insert(0,str(root))
        self.sdk=importlib.import_module('drone_sdk.controller')
        actual=Path(self.sdk.__file__).resolve()
        if actual != root/'drone_sdk'/'controller.py':
            raise RuntimeError('当前进程已加载另一份SDK，请重新启动')
    def normalize(self,value): return asdict(value) if is_dataclass(value) else value
    def get_status(self): return self.normalize(self.sdk.get_status())
    def get_frame(self): return self.normalize(self.sdk.get_frame())
    def request_control(self,owner): return self.normalize(self.sdk.request_control(owner))
    def release_control(self,owner): return self.normalize(self.sdk.release_control(owner))
    def is_following(self): return self.sdk.is_following()
    def start_follow(self): return self.normalize(self.sdk.start_follow())
    def stop_follow(self): return self.normalize(self.sdk.stop_follow())
    def action(self,name,*args): return self.normalize(getattr(self.sdk,name)('model',*args))
