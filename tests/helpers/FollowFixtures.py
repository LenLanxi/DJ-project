"""Shared deterministic follow test fixtures; no test cases are collected here."""


from follow.r3_follow.core import Observation

class Clock:
    def __init__(self):
        self.now = 10.0
    def mono(self):
        return self.now
    def wall(self):
        return 1800000000.0 + self.now


class Detector:
    def detect(self, frame, frame_id, acquired_at, source, *, processing_budget_ms=None):
        corners = ((frame[1]-80,160), (frame[1]+80,160),
                   (frame[1]+80,320), (frame[1]-80,320)) if frame[0] else ()
        return Observation(frame_id, acquired_at, source, 640, 480, 0,
                           frame[0], frame[1], 240, .25, corners=corners,
                           reason="not_found" if not frame[0] else "")


class GeometryDetector:
    def detect(self, frame, frame_id, acquired_at, source, *, processing_budget_ms=None):
        found, x, size = frame
        h = size / 2
        corners = ((x-h,240-h),(x+h,240-h),(x+h,240+h),(x-h,240+h)) if found else ()
        return Observation(frame_id, acquired_at, source, 640, 480, 0,
                           found, x, 240, size/640, corners=corners,
                           reason="" if found else "not_found")


def horizontal_observation(seq, now, *, x=.5, size=.2, target_id=0, found=True, narrow=False):
    cx, cy, side = x*640, 240, size*640
    half_x = side*(.25 if narrow else .5)
    corners = ((cx-half_x, cy-side/2), (cx+half_x, cy-side/2),
               (cx+half_x, cy+side/2), (cx-half_x, cy+side/2))
    return Observation(seq, now, f"camera-{target_id}", 640, 480, target_id, found,
        cx if found else None, cy if found else None, size if found else None,
        corners if found else (), "" if found else "not_found")


def control_observation(seq, time, *, x=.5, size=.2, width=640, found=True, narrow=False):
    cx, cy, side = x*width, width*.375, size*width
    horizontal = side*(.5 if narrow else 1)
    corners = ((cx-horizontal/2, cy-side/2), (cx+horizontal/2, cy-side/2),
               (cx+horizontal/2, cy+side/2), (cx-horizontal/2, cy+side/2))
    return Observation(seq, time, "openpose-test", width, width*3//4, 0, found,
                       cx if found else None, cy if found else None,
                       size if found else None, corners if found else (),
                       "" if found else "not_found")
