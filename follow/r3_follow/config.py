"""Per-follow-instance configuration; no SDK mutation or hardware connection.

R3_FOLLOW_CONFIG is read on application construction, never cached globally. An
explicit file must name the target and controller; a typo cannot silently bind
all three aircraft to ID 0. physical_distance_m is a manually measured note,
not a camera distance estimate or a conversion of RC values to physical speed.

Motion limits and slew rates are RC channel numbers, not physical velocity.
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
    control_profile: str = "openpose_pid"
    physical_distance_m: float | None = None
    steering_mode: str = "yaw"
    horizontal_limit_rc: float = 20.0
    forward_limit_rc: float = 30.0
    slew_rc_per_second: float | None = 80.0
    vertical_follow: bool = False
    distance_mode: str = "apparent_pixels"
    camera_calibration: str | None = None
    camera_id: str | None = None
    marker_side_m: float | None = None
    vertical_min_height_cm: float = 50.0
    vertical_max_height_cm: float = 200.0
    pid_tuning: str = "marker_v2"
    reference_size_limit: float | None = None

    def __post_init__(self):
        if self.reference_size_limit is not None:
            value = self.reference_size_limit
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not .05 <= value <= .30 or self.distance_mode != 'relative_size'):
                raise ValueError('reference_size_limit requires relative_size and a ratio in [.05, .30]')
        if (not isinstance(self.instance_id, str) or not self.instance_id.strip()
                or self.instance_id != self.instance_id.strip()
                or len(self.instance_id) > 80
                or any(ord(c) < 32 or c in "/\\" for c in self.instance_id)):
            raise ValueError("instance_id must be a nonempty label, at most 80 characters")
        if type(self.target_id) is not int or not 0 <= self.target_id <= 49:
            raise ValueError("target_id must be an integer from 0 to 49 (not bool)")
        if self.dictionary != "DICT_4X4_50":
            raise ValueError("dictionary must be DICT_4X4_50")
        if self.control_profile != "openpose_pid":
            raise ValueError("control_profile must be openpose_pid")
        if self.steering_mode not in ("yaw", "lateral", "hybrid", "adaptive"):
            raise ValueError("steering_mode must be yaw, lateral, hybrid or adaptive")
        if self.pid_tuning not in ("marker_v2", "responsive_v1"):
            raise ValueError("pid_tuning must be marker_v2 or responsive_v1")
        for name in ("horizontal_limit_rc", "forward_limit_rc"):
            value = getattr(self, name)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not 0 < value <= 100):
                raise ValueError(name + " must be a finite number in (0, 100]")
        if self.slew_rc_per_second is not None and (
                type(self.slew_rc_per_second) not in (int, float)
                or not math.isfinite(self.slew_rc_per_second) or self.slew_rc_per_second <= 0):
            raise ValueError("slew_rc_per_second must be a finite positive number or null")
        if type(self.vertical_follow) is not bool:
            raise ValueError("vertical_follow must be a boolean")
        if self.distance_mode not in ("apparent_pixels", "relative_size", "pose"):
            raise ValueError("distance_mode must be apparent_pixels, relative_size or pose")
        if self.distance_mode == "pose":
            if (not isinstance(self.camera_calibration, str) or not self.camera_calibration.strip()
                    or not Path(self.camera_calibration).is_absolute()
                    or not isinstance(self.camera_id, str) or not self.camera_id.strip()
                    or type(self.marker_side_m) not in (int, float)
                    or not math.isfinite(self.marker_side_m) or not .02 <= self.marker_side_m <= 1):
                raise ValueError("pose requires absolute camera_calibration, camera_id and measured marker_side_m")
        elif any(v is not None for v in (self.camera_calibration, self.camera_id, self.marker_side_m)):
            raise ValueError("camera calibration fields require pose distance_mode")
        for name in ("vertical_min_height_cm", "vertical_max_height_cm"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 250:
                raise ValueError(name + " must be in [0, 250] cm")
        if self.vertical_max_height_cm-self.vertical_min_height_cm < 40:
            raise ValueError("vertical height interval must span at least 40 cm")
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
                          "horizontal_limit_rc", "forward_limit_rc", "slew_rc_per_second", "vertical_follow",
                          "distance_mode", "camera_calibration", "camera_id", "marker_side_m",
                          "vertical_min_height_cm", "vertical_max_height_cm", "pid_tuning", "reference_size_limit"}
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
    variable is absent the default marker PID binds target ID 0.
    Each aircraft process needs its own file AND independently bound device
    connection. Three configs do not turn the upstream singleton SDK into three SDKs.
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
