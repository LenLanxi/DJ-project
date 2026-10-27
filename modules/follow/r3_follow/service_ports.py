"""Internal simulation ports, NOT team 2's agreed Python signatures.

A future adapter must translate the actual API to these types. Nothing here
connects to a device. Lease epochs make stale-owner races testable locally.
"""
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class FrameSnapshot:
    frame: Any
    frame_id: int
    timestamp: float  # Unix seconds, assigned to the image once by its producer
    stale: bool = False


@dataclass(frozen=True)
class Lease:
    owner: str
    epoch: int


@dataclass(frozen=True)
class Result:
    status: str
    reason: str = ""
    lease: Lease | None = None
    simulation_only: bool = True

    @property
    def ok(self):
        return self.status in ("accepted", "completed")


@dataclass(frozen=True)
class DeviceStatus:
    connected: bool | None
    airborne: bool | None
    lease: Lease | None
    height_cm: float | None = None
    height_fresh: bool = False


class SimulationPort(Protocol):
    # All methods must return promptly, including stop/release/status.
    # Source/detection can be slow; the service isolates these on another worker.
    simulation_only: bool

    def get_frame(self) -> FrameSnapshot: ...
    def get_status(self) -> DeviceStatus: ...
    def request_control(self, owner: str) -> Result: ...
    def release_control(self, lease: Lease) -> Result: ...
    def stop_motion(self, lease: Lease) -> Result: ...
    def update_velocity(self, lease: Lease, *, left_right: int,
                        forward_back: int, up_down: int, yaw: int) -> Result: ...
