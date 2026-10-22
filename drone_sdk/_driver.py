"""Inspectable simulator ONLY; no camera, socket or DJITelloPy connection.

Records prove software ordering, never physical stopping or landing.
"""
from collections import deque
import threading
import time

SIMULATED = True
_lock = threading.RLock()
_delay = 0.0
_velocity = (0, 0, 0, 0)
_events = deque(maxlen=2000)

def set_simulated_delay(seconds):
    global _delay
    with _lock: _delay = max(0., float(seconds))

def simulated_delay():
    with _lock: return _delay

def execute(action, param=None):
    with _lock:
        _events.append(dict(event='action_sent', action=action, param=param,
                            monotonic=time.monotonic(), simulated=True))
        delay = _delay
    if delay: time.sleep(delay)
    return True

def set_velocity(channels, reason):
    global _velocity
    with _lock:
        _velocity = tuple(channels)
        _events.append(dict(event='velocity', channels=_velocity, reason=reason,
                            monotonic=time.monotonic(), simulated=True))

def interrupt(action):
    set_velocity((0, 0, 0, 0), action)
    with _lock:
        _events.append(dict(event='priority_sent', action=action,
                            monotonic=time.monotonic(), simulated=True))

def snapshot():
    with _lock: return dict(velocity=_velocity, events=list(_events), simulated=True)
