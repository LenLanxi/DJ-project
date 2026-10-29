"""Fixed adaptive relative-size following with per-session numeric settings."""
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
from typing import Mapping, ClassVar


@dataclass(frozen=True)
class FollowConfig:
    instance_id: str = "r3-default"
    target_id: int = 0
    dictionary: str = "DICT_4X4_50"
    control_profile: ClassVar[str] = "openpose_pid"
    steering_mode: ClassVar[str] = "adaptive"
    horizontal_limit_rc: float = 20.0
    forward_limit_rc: float = 30.0
    slew_rc_per_second: float | None = 80.0
    vertical_follow: bool = False
    distance_mode: ClassVar[str] = "relative_size"
    vertical_min_height_cm: float = 50.0
    vertical_max_height_cm: float = 200.0
    pid_tuning: ClassVar[str] = "responsive_v1"
    reference_size_limit: float | None = None

    def __post_init__(self):
        if self.reference_size_limit is not None:
            value = self.reference_size_limit
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not .05 <= value <= .30):
                raise ValueError('reference_size_limit must be in [.05, .30]')
        if (not isinstance(self.instance_id, str) or not self.instance_id.strip()
                or self.instance_id != self.instance_id.strip()
                or len(self.instance_id) > 80
                or any(ord(c) < 32 or c in "/\\" for c in self.instance_id)):
            raise ValueError("instance_id must be a nonempty label, at most 80 characters")
        if type(self.target_id) is not int or not 0 <= self.target_id <= 49:
            raise ValueError("target_id must be an integer from 0 to 49 (not bool)")
        if self.dictionary != "DICT_4X4_50":
            raise ValueError("dictionary must be DICT_4X4_50")
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
        for name in ("vertical_min_height_cm", "vertical_max_height_cm"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 250:
                raise ValueError(name + " must be in [0, 250] cm")
        if self.vertical_max_height_cm-self.vertical_min_height_cm < 40:
            raise ValueError("vertical height interval must span at least 40 cm")

    def to_dict(self):
        return asdict(self)


def _from_mapping(value):
    required = {"instance_id", "target_id", "dictionary"}
    allowed = set(FollowConfig.__dataclass_fields__) - {"control_profile", "steering_mode", "distance_mode", "pid_tuning"}
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
    """Load settings from a config, mapping or UTF-8 file selected by R3_FOLLOW_CONFIG."""
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
