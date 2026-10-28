"""R3 control-stability candidate: no hardware verification."""
from .core import FollowController, Observation, MotionRequest
from .vision import MarkerTracker

__all__ = ["FollowController", "Observation", "MotionRequest", "MarkerTracker", "FollowService"]
__version__ = "r3-motion-follow1"

from .service import FollowService
