"""Calibrated square-marker camera-axis depth, never aircraft I/O.

OpenCV's maintained planar IPPE solver supplies candidate poses. Ambiguous
orientation is not used to steer: depth is accepted only when plausible
low-error candidates agree on it. Intrinsics must match this exact image size.
Thresholds are provisional measurement guards, not flight accuracy guarantees.
"""
from dataclasses import dataclass, asdict
import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np


@dataclass(frozen=True)
class DepthEstimate:
    valid: bool
    reason: str
    depth_m: float | None = None
    reprojection_px: float | None = None
    candidate_spread_m: float | None = None
    tilt_deg: float | None = None

    def status(self):
        return asdict(self)


def _numeric(value):
    if isinstance(value, (list, tuple)):
        return bool(value) and all(_numeric(v) for v in value)
    return type(value) in (int, float) and math.isfinite(value)


class PoseDepth:
    def __init__(self, calibration, marker_side_m, *, camera_id):
        if not _numeric(marker_side_m) or not .02 <= marker_side_m <= 1:
            raise ValueError("marker_side_m must be measured black-square side in [0.02, 1] m")
        if not isinstance(camera_id, str) or not camera_id.strip():
            raise ValueError("pose mode requires an explicit camera_id")
        path = Path(calibration)
        raw = path.read_bytes()
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate calibration field: " + key)
                result[key] = value
            return result
        data = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique)
        if not isinstance(data, dict) or data.get('schema') != 'r3_camera_calibration_v1':
            raise ValueError("unsupported camera calibration schema")
        if data.get('camera_id') != camera_id:
            raise ValueError("camera calibration identity mismatch")
        conditioning = data.get('view_conditioning')
        if conditioning is not None:
            ratio = conditioning.get('intrinsic_constraint_rank5_ratio') if isinstance(conditioning,dict) else None
            if type(ratio) not in (int,float) or not math.isfinite(ratio) or ratio < .005:
                raise ValueError('calibration view conditioning failed; collect distinct tilted views')
        size = data.get('image_size')
        if not isinstance(size, list) or len(size) != 2 or any(type(v) is not int or v < 2 for v in size):
            raise ValueError("calibration image_size must be [width, height]")
        matrix, distortion, rms = data.get('camera_matrix'), data.get('dist_coeffs'), data.get('rms_px')
        if not all(_numeric(v) for v in (matrix, distortion, rms)) or not 0 <= rms <= 2:
            raise ValueError("calibration must be finite with RMS in [0, 2] pixels")
        self.K, self.D = np.asarray(matrix, dtype=float), np.asarray(distortion, dtype=float)
        if (self.K.shape != (3, 3) or self.D.ndim != 1 or self.D.size not in (4, 5, 8)
                or self.K[0, 0] <= 0 or self.K[1, 1] <= 0
                or not np.allclose(self.K[2], [0, 0, 1])
                or self.K[0, 1] != 0 or self.K[1, 0] != 0
                or not 0 <= self.K[0, 2] < size[0] or not 0 <= self.K[1, 2] < size[1]):
            raise ValueError("invalid intrinsic matrix or distortion vector")
        self.size = tuple(size)
        self.camera_id = camera_id
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.marker_side_m = float(marker_side_m)
        h = self.marker_side_m / 2
        # Image-aligned object axes avoid OpenCV 4.8 IPPE_SQUARE's numerical
        # degeneracy converting an exactly pi rotation into a rotation vector.
        # General IPPE supports this planar point order and still returns both
        # candidates; no single guessed pose or historical target is substituted.
        self.points = np.array([[-h, -h, 0], [h, -h, 0], [h, h, 0], [-h, h, 0]])

    def estimate(self, observation):
        if not observation.found:
            return DepthEstimate(False, "target_unavailable")
        if (observation.width, observation.height) != self.size:
            return DepthEstimate(False, "calibration_image_size_mismatch")
        try:
            if not _numeric(observation.corners):
                return DepthEstimate(False, "invalid_corners")
            pixels = np.asarray(observation.corners, dtype=float)
            if pixels.shape != (4, 2) or not np.isfinite(pixels).all():
                return DepthEstimate(False, "invalid_corners")
            if (np.any(pixels < 2) or np.any(pixels[:, 0] >= self.size[0]-2)
                    or np.any(pixels[:, 1] >= self.size[1]-2)):
                return DepthEstimate(False, "marker_near_image_edge")
            edges = np.roll(pixels, -1, axis=0) - pixels
            lengths = np.linalg.norm(edges, axis=1)
            if min(lengths) < 12 or lengths.mean() < 24:
                return DepthEstimate(False, "marker_too_small_for_depth")
            cross = np.cross(edges, np.roll(edges, -1, axis=0))
            if not (np.all(cross > 0) or np.all(cross < 0)):
                return DepthEstimate(False, "invalid_corner_order")
            count, rotations, translations, _ = cv2.solvePnPGeneric(
                self.points, pixels, self.K, self.D, flags=cv2.SOLVEPNP_IPPE)
            candidates = []
            for rotation, translation in zip(rotations, translations):
                R = cv2.Rodrigues(rotation)[0]
                camera_points = (R @ self.points.T + translation).T
                if not np.isfinite(camera_points).all() or np.any(camera_points[:, 2] <= 0):
                    continue
                depth = float(translation[2, 0])
                if not .15 <= depth <= 10:
                    continue
                projected = cv2.projectPoints(self.points, rotation, translation, self.K, self.D)[0].reshape(4, 2)
                error = float(np.sqrt(np.mean(np.sum((projected-pixels)**2, axis=1))))
                tilt = math.degrees(math.acos(min(1., abs(float(R[2, 2])))))
                if error <= 1.5 and tilt <= 65:
                    candidates.append((error, depth, tilt))
            if not candidates:
                return DepthEstimate(False, "pose_unreliable")
            candidates.sort()
            error, depth, tilt = candidates[0]
            plausible = [c for c in candidates if c[0] <= error + .5]
            spread = max(c[1] for c in plausible)-min(c[1] for c in plausible)
            if spread > max(.03, depth*.03):
                return DepthEstimate(False, "depth_candidates_disagree", reprojection_px=error,
                                     candidate_spread_m=spread, tilt_deg=tilt)
            return DepthEstimate(True, "calibrated_pose", depth, error, spread, tilt)
        except (ValueError, TypeError, OverflowError, cv2.error):
            return DepthEstimate(False, "pose_solver_failed")
