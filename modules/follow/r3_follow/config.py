"""Per-follow-instance configuration; no SDK mutation or hardware connection.

R3_FOLLOW_CONFIG is read on bridge construction, never cached globally. An
explicit file must name the target and controller; a typo cannot silently bind
all three aircraft to ID 0. physical_distance_m is a manually measured note,
not a camera distance estimate or a conversion of RC values to physical speed.

yaw_limit / forward_limit are the dimensionless RC budget of this instance
(0.20 / 0.30 = the reviewed default = 20 / 30 RC). The service derives its RC
clamp from the same two numbers, so raising them here raises the command
ceiling; it does NOT make 100 mean 1 m/s (RC 100 is the aircraft's maximum
stick, not a metric speed).
"""
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class FollowConfig:
    instance_id: str = "r3-default"
    target_id: int = 0
    dictionary: str = "DICT_4X4_50"
    control_profile: str = "legacy"
    physical_distance_m: float | None = None
    steering_mode: str = "yaw"
    yaw_limit: float = 0.20
    forward_limit: float = 0.30
    max_request_rate: float | None = 0.8

    def __post_init__(self):
        if (not isinstance(self.instance_id, str) or not self.instance_id.strip()
                or self.instance_id != self.instance_id.strip()
                or len(self.instance_id) > 80
                or any(ord(c) < 32 or c in "/\\" for c in self.instance_id)):
            raise ValueError("instance_id must be a nonempty label, at most 80 characters")
        if type(self.target_id) is not int or not 0 <= self.target_id <= 49:
            raise ValueError("target_id must be an integer from 0 to 49 (not bool)")
        if self.dictionary != "DICT_4X4_50":
            raise ValueError("dictionary must be DICT_4X4_50")
        if self.control_profile not in ("legacy", "openpose_pid"):
            raise ValueError("control_profile must be legacy or openpose_pid")
        if self.steering_mode not in ("yaw", "lateral", "hybrid"):
            raise ValueError("steering_mode must be yaw, lateral or hybrid")
        if self.steering_mode == "hybrid" and self.control_profile != "openpose_pid":
            raise ValueError("hybrid steering requires openpose_pid")
        for name in ("yaw_limit", "forward_limit"):
            value = getattr(self, name)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not 0 < value <= 1):
                raise ValueError(name + " must be a finite number in (0, 1]")
        if self.max_request_rate is not None and (
                type(self.max_request_rate) not in (int, float)
                or not math.isfinite(self.max_request_rate) or self.max_request_rate <= 0):
            raise ValueError("max_request_rate must be a finite positive number or null")
        if self.physical_distance_m is not None and (
                type(self.physical_distance_m) not in (int, float)
                or not math.isfinite(self.physical_distance_m)
                or self.physical_distance_m <= 0):
            raise ValueError("physical_distance_m must be a finite positive measured value or null")

    def to_dict(self):
        return asdict(self)


def _from_mapping(value):
    required = {"instance_id", "target_id", "dictionary", "control_profile"}
    allowed = required | {"physical_distance_m", "steering_mode",
                          "yaw_limit", "forward_limit", "max_request_rate"}
    keys = set(value)
    missing, unknown = required - keys, keys - allowed
    if missing:
        raise ValueError("explicit configuration missing: " + ", ".join(sorted(missing)))
    if unknown:
        raise ValueError("unknown configuration fields: " + ", ".join(sorted(map(str, unknown))))
    return FollowConfig(**value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration field: " + key)
        result[key] = value
    return result


def load_config(config=None):
    """Load a frozen config, complete mapping or explicit UTF-8 JSON file.

    With no argument, R3_FOLLOW_CONFIG selects the file; if the environment
    variable is absent the existing ID-0 legacy behavior remains unchanged.
    Each aircraft process needs its own file AND independently bound device
    port. Three configs do not turn the upstream singleton SDK into three SDKs.
    """
    if config is None:
        config = os.environ.get("R3_FOLLOW_CONFIG")
        if config is None:
            return FollowConfig()
    if isinstance(config, FollowConfig):
        return config
    if isinstance(config, Mapping):
        return _from_mapping(config)
    if isinstance(config, (str, os.PathLike)):
        if isinstance(config, str) and not config.strip():
            raise ValueError("R3_FOLLOW_CONFIG/config path must not be empty")
        path = Path(config).expanduser()
        value = json.loads(path.read_text(encoding="utf-8-sig"),
                           object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise ValueError("configuration JSON must be an object")
        return _from_mapping(value)
    raise TypeError("config must be FollowConfig, a complete mapping or JSON file path")
