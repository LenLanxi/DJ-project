# drone_sdk/contract.py
from dataclasses import dataclass
from typing import Optional
import numpy as np

@dataclass
class CommandResult:
    ok: bool
    status: str = "failed" # "accepted", "completed", "failed", "timeout"
    reason: str = ""
    is_blocking: bool = True

@dataclass
class ControlResult:
    ok: bool
    current_owner: Optional[str] = None
    reason: str = ""
    control_epoch: int = 0
    command_epoch: int = 0

@dataclass
class StatusResult:
    battery: int
    height_cm: int
    connected: bool
    flight_state: str # "landed", "flying", "unknown"
    control_owner: str
    timestamp: float
    control_epoch: int = 0
    command_epoch: int = 0
    velocity: tuple = (0, 0, 0, 0)
    simulated: bool = True
    command_channel_ambiguous: bool = False
    command_uncertainty: Optional[dict] = None
    height_timestamp: Optional[float] = None  # actual state-packet receipt, never query time

@dataclass
class FrameResult:
    frame: Optional[np.ndarray] # BGR 格式，无画面时 None
    frame_id: int
    timestamp: float
    stale: bool
