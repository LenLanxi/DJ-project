"""R3 v0.9: local marker detection and simulated follow requests."""
from .core import FollowController, Observation, MotionRequest
from .vision import MarkerTracker

__all__ = ["FollowController", "Observation", "MotionRequest", "MarkerTracker"]
__version__ = "0.9"
