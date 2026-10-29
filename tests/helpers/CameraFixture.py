"""Deterministic frame-reader fixture for service clock and motion regressions."""
from dataclasses import replace
import time

from follow.r3_follow.config import FollowConfig, load_config
from tests.helpers.FollowFixture import FakeDevice
from follow.r3_follow import FollowService
from drone_sdk.contract import FrameResult


class WebcamFakeDevice(FakeDevice):
    """Fresh camera pixels, simulated motion only; capture time stays immutable."""
    def __init__(self, reader, monotonic=time.monotonic, wall=time.time):
        super().__init__(monotonic)
        self.reader=reader
        self.wall=wall
        self.epoch_offset=wall()-monotonic()
        self.frames={}

    def epoch_time(self):
        # Use the frame producer's clock mapping for age checks. Independent
        # Windows wall/monotonic tick phases can otherwise make fresh frames
        # appear a few milliseconds in the future.
        return self.epoch_offset+self.clock()

    def get_frame(self):
        latest,_=self.reader.snapshot()
        with self.lock:
            if latest is None:return self.snapshot
            frame_id,acquired,frame=latest
            if frame_id>self.snapshot.frame_id:
                self.snapshot=FrameResult(frame,frame_id,self.epoch_offset+acquired,False)
                self.frames[frame_id]=frame
                while len(self.frames)>8:self.frames.pop(next(iter(self.frames)))
            packet=self.snapshot
            if self.clock()-(packet.timestamp-self.epoch_offset)>.5:
                return replace(packet,frame=None,stale=True)
            return packet

    def display_frame(self, observation):
        # A processed box belongs only to that processed image, never a newer one.
        with self.lock:
            matched=self.frames.get(observation.frame_id) if observation else None
        if matched is not None:
            return matched.copy(),True
        latest,_=self.reader.snapshot()
        return (latest[2].copy(),False) if latest else (None,False)

def make_service(reader, target_id=0, log_directory=False,
                 monotonic=time.monotonic, wall=time.time, *, vertical_follow=False, config=None):
    port = WebcamFakeDevice(reader, monotonic, wall)
    config = load_config(config) if config is not None else FollowConfig(
        f'cameraFixtureID{target_id}', target_id, 'DICT_4X4_50',
        vertical_follow=vertical_follow,
        )
    service = FollowService(port, config=config,
        log_directory=log_directory, monotonic=monotonic, wall_clock=port.epoch_time)
    return port, service
