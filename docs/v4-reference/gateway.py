import asyncio
import inspect
import threading

DISTANCE={'move_forward','move_back','move_left','move_right','move_up','move_down'}
MOTION=DISTANCE|{'takeoff','land','stop_motion','rotate','update_velocity'}

class Gateway:
    def __init__(self,backend,vision=None,fixture_vision=False):
        self.backend,self.vision=backend,vision
        self.fixture_vision=fixture_vision
        self.simulated=backend.simulated
        self.cancelled=threading.Event()
        self.acquired=False; self.was_following=False; self.resume=False
        self.observations=set()
        self.control_lock=asyncio.Lock()

    def begin(self):
        self.cancelled.clear(); self.acquired=False; self.was_following=False
        self.resume=True; self.observations.clear()

    async def acquire(self):
        async with self.control_lock:
            if self.cancelled.is_set(): raise RuntimeError('program_cancelled')
            if self.acquired: return
            self.was_following=await asyncio.to_thread(self.backend.is_following)
            if self.was_following:
                r=await asyncio.to_thread(self.backend.stop_follow)
                if not r.get('ok'): raise RuntimeError('cannot_pause_follow')
            r=await asyncio.to_thread(self.backend.request_control,'model')
            if not r.get('ok'): raise RuntimeError('control_busy: '+r.get('reason','unknown'))
            self.acquired=True
            if self.cancelled.is_set(): raise RuntimeError('program_cancelled')

    async def dispatch(self,method,args,kwargs):
        if self.cancelled.is_set(): raise RuntimeError('program_cancelled')
        if not isinstance(args,list) or not isinstance(kwargs,dict) or kwargs:
            raise ValueError('RPC仅接受位置参数；这是接口协议，不限制Python语法')
        if method=='get_status' and not args:
            return await asyncio.to_thread(self.backend.get_status)
        if method=='is_following' and not args:
            return await asyncio.to_thread(self.backend.is_following)
        if method in ('observe','get_frame'):
            if method=='observe':
                if len(args)!=1 or not isinstance(args[0],str) or not 0<len(args[0])<=2000:
                    raise ValueError('observe(target)需要目标文字')
                if self.fixture_vision:
                    result=self.backend.observe_fixture(args[0])
                elif self.vision:
                    result=await asyncio.to_thread(self.vision.observe,args[0])
                else: raise RuntimeError('没有配置视觉模型/画面源')
                identity=(result.get('source'),result.get('frame_id'))
                if identity in self.observations and result.get('source')=='sdk_camera':
                    raise RuntimeError('没有新帧，不能继续基于旧帧运动')
                self.observations.add(identity)
                if self.cancelled.is_set(): raise RuntimeError('program_cancelled')
                return result
            if args: raise ValueError('get_frame不接受参数')
            if not self.vision: raise RuntimeError('没有可用画面源')
            return await asyncio.to_thread(self.vision.frame)
        if method in ('start_follow','stop_follow'):
            if args: raise ValueError('跟随接口不接受参数')
            self.resume=False
            if method=='start_follow':
                if self.acquired:
                    await asyncio.to_thread(self.backend.release_control,'model'); self.acquired=False
                return await asyncio.to_thread(self.backend.start_follow)
            return await asyncio.to_thread(self.backend.stop_follow)
        if method not in MOTION: raise ValueError('未暴露的宿主机能力：'+str(method))
        if method in DISTANCE:
            if len(args)!=1 or type(args[0]) is not int or not 20<=args[0]<=500:
                raise ValueError('移动距离为SDK规定的20至500厘米整数')
        elif method=='rotate':
            if len(args)!=1 or type(args[0]) is not int or not -360<=args[0]<=360 or args[0]==0:
                raise ValueError('旋转需要非零-360至360度整数；SDK实机符号仍需确认')
        elif method=='update_velocity':
            if len(args)!=4 or any(type(v) is not int or not -100<=v<=100 for v in args):
                raise ValueError('速度需要四个-100至100整数百分比')
        elif args: raise ValueError('该动作不接受参数')
        await self.acquire()
        if self.cancelled.is_set(): raise RuntimeError('program_cancelled')
        if method in ('land','stop_motion'): self.resume=False
        result=await asyncio.to_thread(self.backend.action,method,*args)
        if not result.get('ok') or result.get('status') not in ('completed','accepted'):
            self.resume=False
            raise RuntimeError('SDK动作失败：'+str(result))
        return result

    async def interrupt(self,land=False):
        # Set cancellation before any await. Does not wait for the movement thread.
        self.cancelled.set(); self.resume=False
        try:
            await asyncio.to_thread(self.backend.stop_follow)
            control=await asyncio.to_thread(self.backend.request_control,'model')
            if not control.get('ok'): return {'ok':False,'reason':'control_busy','detail':control}
            self.acquired=True
            return await asyncio.to_thread(self.backend.action,'land' if land else 'stop_motion')
        finally:
            # finish() releases owner after old action has returned; no blind resume.
            pass

    async def finish(self,success):
        if not self.acquired: return
        if not success or self.cancelled.is_set():
            await asyncio.to_thread(self.backend.action,'stop_motion')
        await asyncio.to_thread(self.backend.release_control,'model')
        self.acquired=False
        if success and self.resume and self.was_following and not self.cancelled.is_set():
            # Actual target/frame checks belong to group's start_follow implementation.
            r=await asyncio.to_thread(self.backend.start_follow)
            if not r.get('ok'): raise RuntimeError('跟随未恢复：'+str(r))
