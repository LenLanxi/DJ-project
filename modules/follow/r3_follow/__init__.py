"""R3 control-stability candidate: no hardware verification."""
from .core import FollowController, Observation, MotionRequest
from .vision import MarkerTracker

__all__ = ["FollowController", "Observation", "MotionRequest", "MarkerTracker"]
__version__ = "0.14-side-view-candidate"
