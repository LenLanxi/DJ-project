"""The HUD consumes the current snapshot and scales its marker to the frame."""
import numpy as np
from app.Hud import draw_follow_overlay


def test_snapshot_marker_scales_without_mutating_camera_frame():
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    snapshot = dict(state='tracking', following=True, target_visible=True,
        requested_channels=(1, 2, 3, 4), observation=dict(
            found=True, width=1280, height=960, center_x=900, center_y=680,
            size_ratio=.2, corners=((800, 600), (1000, 600), (1000, 760), (800, 760))))
    output = draw_follow_overlay(frame, snapshot)
    assert (output[150, 200:251, 1] == 255).any()
    assert (output[165:176, 220:231, 2] == 255).any()
    assert not frame.any()
