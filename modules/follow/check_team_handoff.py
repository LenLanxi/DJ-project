"""Current SDK + actual R3 workers + actual G4 Gateway, simulated device only.
No language model/container/audio/camera is substituted as verified functionality.
"""
import argparse
import asyncio
import json
import os
import socket
import sys
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch


async def scenario(root, output):
    sys.path[:0] = [str(root), str(root/'modules/follow'), str(root/'modules/ai')]
    from drone_sdk import controller as ctl, _driver
    from drone_sdk.contract import FrameResult
    from backends import SDKBackend
    from gateway import Gateway
    from r3_follow.vision import synthetic_frame
    from r3_follow.session_log import SessionLog
    import cv2
    cv2.setNumThreads(1)
    assert _driver.SIMULATED
    os.environ['R3_FOLLOW_CONFIG'] = str(root/'modules/follow/configs/offline_candidates/aircraft_A_ID0_hybrid.json')
    records = []
    def record(name, result):
        try: result = asdict(result)
        except TypeError: pass
        records.append({'step':name,'result':result,'device':asdict(ctl.get_status())})
    class Feed:
        x = .5
        visible = True
        cached = None
        def __call__(self):
            index = int(time.monotonic()*20)
            key = (index,self.x,self.visible)
            if self.cached is None or self.cached[0] != key:
                self.cached = (key,FrameResult(synthetic_frame(x=self.x, visible=self.visible),index,time.time(),False))
            return self.cached[1]
    feed = Feed()
    backend = SDKBackend(root)
    gateway = Gateway(backend)
    async def wait(predicate, seconds=2):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if predicate(): return True
            await asyncio.sleep(.02)
        return False
    def audit_log(directory, metadata): return SessionLog(output/'background_logs', metadata)
    with patch('r3_follow.service.SessionLog',audit_log):
        try:
            ctl.request_control('model')
            record('simulated_takeoff',ctl.takeoff('model'))
            ctl.release_control('model'); ctl.set_frame_provider(feed)
            record('follow_start',await asyncio.to_thread(ctl.start_follow))
            feed.x=.68
            moving=await wait(lambda:any(ctl.get_status().velocity))
            record('off_center_generates_request',{'nonzero':moving})
            feed.visible=False
            recovering=await wait(lambda:ctl._follow.status().get('state')=='recovering_target')
            record('temporarily_occluded',{'recovering':recovering,'is_following':ctl.is_following()})
            gateway.begin()
            try: result=await gateway.dispatch('move_forward',[30],{})
            except Exception as exc: result={'exception':type(exc).__name__,'message':str(exc)}
            record('model_action_during_recovery',result)
            await gateway.finish(False)
            feed.visible=True
            record('marker_returns',{'following':await wait(ctl.is_following)})
            gateway.begin()
            record('model_forward30',await gateway.dispatch('move_forward',[30],{}))
            await gateway.finish(True)
            record('model_completed_then_resume',{'following':ctl.is_following()})
            paused=await asyncio.to_thread(backend.pause_follow_for_action)
            record('pause_before_late_callback',{'ok':paused.get('ok')})
            record('explicit_stop',ctl.stop_follow())
            late=await asyncio.to_thread(backend.finish_follow_action,paused.get('ticket'),True)
            record('late_completion_after_stop',late)
            assert not ctl.is_following()
            record('restart',await asyncio.to_thread(ctl.start_follow))
            await gateway.interrupt(True)
            record('gateway_priority_land_sent',{'following':ctl.is_following()})
            landed=await wait(lambda:ctl.get_status().flight_state=='landed')
            record('simulated_landed_confirmed',{'landed':landed})
            assert landed
        finally:
            ctl.stop_follow(); ctl.set_frame_provider(None)
            owner=ctl.get_status().control_owner
            if owner not in ('none',None): ctl.release_control(owner)
            assert _driver.SIMULATED
    return {'scope':'actual workers/SDK/Gateway; synthetic frames and simulated flight only',
            'default_volc_entry_not_used':True,'records':records}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path);a=p.parse_args();a.output.mkdir(exist_ok=True)
    def deny(*a,**k):raise RuntimeError('audit_network_forbidden')
    with asyncio.Runner() as runner:
        runner.get_loop()
        with patch.object(socket.socket,'connect',deny),patch.object(socket.socket,'connect_ex',deny),patch.object(socket.socket,'sendto',deny):
            data=runner.run(scenario(a.root.resolve(),a.output.resolve()))
    (a.output/'joint_results.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(data,ensure_ascii=False,indent=2))
