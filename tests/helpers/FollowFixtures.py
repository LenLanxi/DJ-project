"""Shared deterministic follow test fixtures; no test cases are collected here."""
import json
import math

import cv2
import numpy as np

from follow.r3_follow.core import Observation

class Clock:
    def __init__(self):
        self.now = 10.0
    def mono(self):
        return self.now
    def wall(self):
        return 1800000000.0 + self.now


class Detector:
    def detect(self, frame, frame_id, acquired_at, source):
        corners = ((frame[1]-80,160), (frame[1]+80,160),
                   (frame[1]+80,320), (frame[1]-80,320)) if frame[0] else ()
        return Observation(frame_id, acquired_at, source, 640, 480, 0,
                           frame[0], frame[1], 240, .25, corners=corners,
                           reason="not_found" if not frame[0] else "")


class GeometryDetector:
    def detect(self, frame, frame_id, acquired_at, source):
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


def calibration(path):
    value = dict(schema='r3_camera_calibration_v1', camera_id='synthetic-camera',
                 image_size=[640,480], camera_matrix=[[500,0,320],[0,500,240],[0,0,1]],
                 dist_coeffs=[0,0,0,0,0], rms_px=0., source='synthetic_not_hardware')
    path.write_text(json.dumps(value),encoding='utf8')
    return value


def projected(seq, t, depth=2., angle=0., x=0., y=0.):
    side=.24; h=side/2
    points=np.array([[-h,h,0],[h,h,0],[h,-h,0],[-h,-h,0]])
    a=math.radians(angle)
    R=np.array([[math.cos(a),0,math.sin(a)],[0,1,0],[-math.sin(a),0,math.cos(a)]])@np.diag([1.,-1.,-1.])
    corners=cv2.projectPoints(points,cv2.Rodrigues(R)[0],np.array([x,y,depth]),
        np.array([[500.,0,320],[0,500.,240],[0,0,1]]),np.zeros(5))[0].reshape(4,2)
    size=float(np.linalg.norm(np.roll(corners,-1,axis=0)-corners,axis=1).mean()/640)
    center=corners.mean(axis=0)
    return Observation(seq,t,'synthetic',640,480,0,True,float(center[0]),float(center[1]),size,
                       tuple(map(tuple,corners.tolist())))
