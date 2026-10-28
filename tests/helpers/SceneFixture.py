"""Deterministic camera arrays shared by vision and navigation tests."""
import cv2
import numpy as np


def scene():
    rng=np.random.default_rng(4925)
    # Smooth irregular fabric/grain, not a fiducial marker or a binary grid.
    fabric=rng.normal(0,24,(120,150,3)).astype(np.float32)
    fabric=cv2.GaussianBlur(fabric,(3,3),.5)
    fabric+=np.array([55,90,175],dtype=np.float32)
    fabric=np.clip(fabric,0,255).astype(np.uint8)
    for _ in range(100):
        p=tuple(int(v) for v in rng.integers([4,4],[145,115]))
        color=tuple(int(v) for v in rng.integers(20,200,3))
        cv2.circle(fabric,p,int(rng.integers(1,4)),color,-1)
    background=np.full((360,640,3),170,dtype=np.uint8)
    background[120:240,170:320]=fabric
    return background
