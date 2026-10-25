"""R3 control-stability candidate: no hardware verification."""
from .core import FollowController, Observation, MotionRequest
from .vision import MarkerTracker

__all__ = ["FollowController", "Observation", "MotionRequest", "MarkerTracker"]
__version__ = "r3-9889b47-closeout1"
