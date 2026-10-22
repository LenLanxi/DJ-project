"""Adapter for the reviewed 6d39131 stub only. Not authorized for real flight.

Lease epochs protect this adapter's callers, NOT external ABA owner changes.
The upstream SDK only exposes an owner string; its missing concurrency guarantees
cannot be invented by this translation layer.
"""
import hashlib
import importlib
import math
from pathlib import Path
import sys
import threading
import time

from .service_ports import DeviceStatus, FrameSnapshot, Lease, Result

COMMIT = '7f2468c0ab395afcd2f6071ebf21fa96a0c24359'
# 2组接入说明（2026-10-22）：controller.py 已改为把 start_follow/stop_follow 委托给
# 本包 FollowService，故重新钉 controller.py 哈希；contract.py / __init__.py / _driver.py 未变。
# 差异仅限跟随接口委托与 set_frame_provider 注入点，请 3组 复核后确认。
HASHES = {'controller.py': '4801a92f38d28ae5044fc878bab595e7aa2fc8b4de0e07768fc45ba0125211c8', 'contract.py': 'dcd9c098a0b50e51920f76625b0e3e7dae131a92e664b2c718f67bc26d08ed6b', '__init__.py': '13d3e6725b134ac4d9a3e683ac8322a6514978ae0a89425516803d999c3ababd', '_driver.py': '02b6a4bbb00d9b9d21b1f2d08866385da6296273e0f8131876788e6a67020429'}


def load_reviewed_stub(repository):
    repository = Path(repository).resolve()
    for name, expected in HASHES.items():
        path = repository/'drone_sdk'/name
        # Ignore Git line endings only; any code change requires review.
        raw = path.read_bytes().replace(b'\r\n', b'\n')
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f'Unreviewed SDK source: {name}; review before updating pin')
    for name in ('drone_sdk','drone_sdk.controller','drone_sdk.contract'):
        existing = sys.modules.get(name)
        if existing and not Path(existing.__file__).resolve().is_relative_to(repository/'drone_sdk'):
            raise ValueError('Another drone_sdk is already loaded; use a clean process')
    sys.path.insert(0, str(repository))
    try:
        sdk=importlib.import_module('drone_sdk.controller')
    finally:
        sys.path.pop(0)
    return sdk


class Team2StubPort:
    simulation_only = True

    def __init__(self, repository, *, frame_provider=None):
        self.sdk=load_reviewed_stub(repository)
        # A test-only generated feed is explicit and never disguised as SDK video.
        self.frame_provider=frame_provider
        self.frame_origin='injected_generated_images' if frame_provider else 'upstream_get_frame'
        self.lock=threading.RLock()
        self.lease=None
        self.epoch=0
        self.events=[]

    def _record(self, event, **values):
        self.events.append(dict(time=time.monotonic(), event=event, simulation_only=True, **values))

    @staticmethod
    def _command(result):
        status=getattr(result,'status',None)
        ok=getattr(result,'ok',None)
        reason=getattr(result,'reason','')
        if ok is True and status in ('accepted','completed'):
            return Result(status,str(reason))
        if ok is False and status in ('failed','timeout'):
            return Result(status,str(reason))
        return Result('failed',str(reason) or 'inconsistent_command_result')

    def get_frame(self):
        result=self.frame_provider() if self.frame_provider else self.sdk.get_frame()
        # Preserve upstream timestamp/id exactly; service rejects stale/empty data.
        return FrameSnapshot(result.frame,result.frame_id,result.timestamp,result.stale)

    def get_status(self):
        with self.lock:
            status=self.sdk.get_status()
            owner=status.control_owner
            if owner in (None,'none',''):
                owner=None
            elif owner not in ('follow','model'):
                raise ValueError('unknown_control_owner')
            if owner is None:
                self.lease=None
            elif self.lease is None or self.lease.owner != owner:
                self.epoch+=1
                self.lease=Lease(owner,self.epoch)
            connected=status.connected if type(status.connected) is bool else None
            airborne={'flying':True,'landed':False}.get(status.flight_state)
            if (type(status.timestamp) not in (int,float) or not math.isfinite(status.timestamp)
                    or not 0 <= time.time()-status.timestamp <= .5):
                connected=airborne=None
            return DeviceStatus(connected,airborne,self.lease)

    def request_control(self, owner):
        with self.lock:
            if owner not in ('follow','model'):
                return Result('failed','invalid_owner')
            if self.get_status().lease is not None:
                return Result('failed','busy')
            result=self.sdk.request_control(owner)
            if result.ok is not True:
                return Result('failed',result.reason or 'control_denied')
            state=self.get_status()
            if state.lease is None or state.lease.owner != owner:
                return Result('failed','control_grant_unconfirmed')
            return Result('completed',lease=state.lease)

    def _owns(self, lease):
        return lease is not None and self.get_status().lease==lease

    def release_control(self, lease):
        with self.lock:
            if not self._owns(lease):
                return Result('failed','not_owner')
            result=self.sdk.release_control(lease.owner)
            if result.ok is True and self.get_status().lease is None:
                self._record('release_acknowledged',owner=lease.owner)
                return Result('completed')
            return Result('failed',result.reason or 'release_unconfirmed')

    def stop_motion(self, lease):
        with self.lock:
            if not self._owns(lease):
                return Result('failed','not_owner')
            # Send an explicit zero channel request, not just the upstream print.
            zero=self.update_velocity(lease,left_right=0,forward_back=0,up_down=0,yaw=0)
            result=self._command(self.sdk.stop_motion(lease.owner))
            self._record('stop_acknowledged',status=result.status)
            return result if zero.ok else zero

    def update_velocity(self, lease, *, left_right, forward_back, up_down, yaw):
        with self.lock:
            state=self.get_status()
            if lease is None or state.lease!=lease:
                return Result('failed','not_owner')
            channels=(left_right,forward_back,up_down,yaw)
            if any(type(v) is not int or not -100<=v<=100 for v in channels):
                return Result('failed','invalid_channels')
            if any(channels) and (state.connected is not True or state.airborne is not True):
                return Result('failed','connected_airborne_state_required')
            result=self._command(self.sdk.update_velocity(lease.owner,*channels))
            self._record('velocity_call',channels=channels,status=result.status,owner=lease.owner)
            return result
