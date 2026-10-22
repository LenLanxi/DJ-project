"""ArUco adapter. No camera ownership and no control logic."""
import cv2
import numpy as np
from .core import Observation


class MarkerTracker:
    def __init__(self, target_id=0):
        self.target_id = target_id
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        self.detector = cv2.aruco.ArucoDetector(dictionary)
        parameters = cv2.aruco.DetectorParameters()
        parameters.adaptiveThreshWinSizeMax = 63
        parameters.adaptiveThreshWinSizeStep = 4
        self.fallback_detector = cv2.aruco.ArucoDetector(dictionary, parameters)
        # Sample marker cells more densely and exclude uncertain cell edges.
        # Preserve dictionary/error-correction settings. Only used after all
        # previous paths fail to decode the selected target.
        motion_parameters = cv2.aruco.DetectorParameters()
        motion_parameters.adaptiveThreshWinSizeMax = 63
        motion_parameters.adaptiveThreshWinSizeStep = 4
        motion_parameters.perspectiveRemovePixelPerCell = 8
        motion_parameters.perspectiveRemoveIgnoredMarginPerCell = .25
        self.motion_detector = cv2.aruco.ArucoDetector(dictionary, motion_parameters)
        # Last-resort current-frame contrast normalization. It uses the SAME
        # dictionary and decoding tolerances; it never repairs/predicts bits.
        self.contrast_enhancer = cv2.createCLAHE(clipLimit=2., tileGridSize=(8, 8))
        self.last_diagnostics = {}

    def detect(self, frame, frame_id, acquired_at, source="camera"):
        self.last_diagnostics = {}
        if (not isinstance(frame, np.ndarray) or frame.dtype != np.uint8
                or frame.size == 0 or frame.ndim not in (2, 3)
                or (frame.ndim == 3 and frame.shape[2] not in (3, 4))):
            raise ValueError("Expected a non-empty uint8 gray/BGR/BGRA image")
        height, width = frame.shape[:2]
        if frame.ndim == 2:
            gray = frame
        else:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY if frame.shape[2] == 4 else cv2.COLOR_BGR2GRAY)
        attempts = []
        ignored_other_markers = []
        def detect_with(detector, image, name, scale=1):
            corners, ids, rejected = detector.detectMarkers(image)
            attempts.append({"method": name, "image_scale": scale,
                             "ids": [] if ids is None else [int(v) for v in ids.flatten()],
                             "rejected_candidates": len(rejected)})
            if scale != 1:
                corners = [points / scale for points in corners]
                rejected = [points / scale for points in rejected]
            # Other IDs are not the selected target. Retain their geometry in
            # diagnostics, then keep searching. Never relabel them as ID0
            # or suppress duplicate target IDs.
            if ids is not None:
                keep = []
                for i, marker_id in enumerate(ids.flatten()):
                    points = np.asarray(corners[i][0], dtype=np.float32)
                    edges = np.linalg.norm(points-np.roll(points, -1, axis=0), axis=1)
                    credible = (edges.min() >= 6
                                and edges.min() >= .25 * edges.max()
                                and abs(cv2.contourArea(points)) >= 64)
                    if int(marker_id) != self.target_id:
                        ignored_other_markers.append({
                            "id": int(marker_id), "corners": points.tolist(),
                            "method": name, "reason": (
                                "non_target_id" if credible else "small_or_degenerate_quad")})
                    else:
                        keep.append(i)
                corners = [corners[i] for i in keep]
                ids = ids[keep] if keep else None
            return corners, ids, rejected

        corners, ids, rejected = detect_with(self.detector, gray, "default")
        method = "default"
        if ids is None:
            corners, ids, rejected = detect_with(self.fallback_detector, gray, "wide_threshold")
            method = "wide_threshold"
        if ids is None and max(height, width) <= 960:
            enlarged = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            corners, ids, rejected = detect_with(self.fallback_detector, enlarged,
                                                "upscaled_wide_threshold", 2)
            method = "upscaled_wide_threshold"
        if ids is None:
            corners, ids, rejected = detect_with(self.motion_detector, gray, "cell_interior")
            method = "cell_interior"
        if ids is None and min(height, width) >= 240:
            # Extra current-frame scale only; no tracking/predicted coordinates.
            # INTER_AREA can make blurred/noisy cell boundaries easier to decode.
            reduced = cv2.resize(gray, None, fx=.5, fy=.5, interpolation=cv2.INTER_AREA)
            corners, ids, rejected = detect_with(
                self.motion_detector, reduced, "downscaled_cell_interior", .5)
            method = "downscaled_cell_interior"
        if ids is None and min(height, width) >= 240 and max(height, width) <= 960:
            # Work at half size: full-resolution enhancement amplifies grain
            # and made failed-frame processing substantially slower in testing.
            enhanced = self.contrast_enhancer.apply(reduced)
            corners, ids, rejected = detect_with(
                self.motion_detector, enhanced, "downscaled_local_contrast", .5)
            method = "downscaled_local_contrast"
        self.last_diagnostics = {"detector_method": method,
            "recognition_profile": "aruco_multiscale_contrast_v012_candidate",
            "detected_ids": [] if ids is None else [int(v) for v in ids.flatten()],
            "rejected_candidates": len(rejected),
            "attempts": attempts,
            "ignored_other_markers": ignored_other_markers,
            "detected_markers": [] if ids is None else [
                {"id": int(marker_id), "corners": np.asarray(quad[0]).tolist()}
                for marker_id, quad in list(zip(ids.flatten(), corners))[:16]],
            # A sample of un-decoded shapes, never treated as target positions.
            "rejected_quads_sample": [np.asarray(quad[0]).tolist() for quad in
                sorted(rejected, key=lambda q: abs(cv2.contourArea(q)), reverse=True)[:6]],
            "image_width": width, "image_height": height,
            "image_sharpness": float(cv2.Laplacian(gray, cv2.CV_64F).var())}
        matches = [] if ids is None else [i for i, v in enumerate(ids.flatten()) if int(v) == self.target_id]
        base = dict(frame_id=frame_id, acquired_at=acquired_at, source=source,
                    width=width, height=height, target_id=self.target_id)
        if len(matches) != 1:
            return Observation(**base, found=False,
                               reason=("ambiguous_duplicate_id" if matches else
                                       "not_found"))
        points = corners[matches[0]][0]
        center = points.mean(axis=0)
        side = np.linalg.norm(points - np.roll(points, -1, axis=0), axis=1).mean()
        x1, y1 = np.maximum(np.floor(points.min(axis=0)).astype(int), 0)
        x2, y2 = np.minimum(np.ceil(points.max(axis=0)).astype(int) + 1, (width, height))
        roi = gray[y1:y2, x1:x2]
        if roi.size:
            edge_margin = float(min(points[:, 0].min(), points[:, 1].min(),
                                    width - 1 - points[:, 0].max(),
                                    height - 1 - points[:, 1].max()))
            # An advisory framing margin, not a detection/flight threshold.
            suggested_margin = max(8., float(side) * .15)
            self.last_diagnostics["target_quality"] = {
                "side_px": float(side),
                "edge_margin_px": edge_margin,
                "suggested_edge_margin_px": suggested_margin,
                "near_image_edge": edge_margin < suggested_margin,
                "roi_contrast_std": float(roi.std()),
                "roi_sharpness": float(cv2.Laplacian(roi, cv2.CV_64F).var())}
        return Observation(**base, found=True, center_x=float(center[0]),
                           center_y=float(center[1]), size_ratio=float(side / width),
                           corners=tuple(tuple(float(v) for v in pt) for pt in points))


def synthetic_frame(x=0.5, size=0.25, visible=True, marker_id=0):
    """Synthetic camera input only; does not simulate aircraft physics."""
    frame = np.full((480, 640, 3), 245, dtype=np.uint8)
    if visible:
        side = max(40, min(250, int(size * 640)))
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        marker = cv2.aruco.generateImageMarker(dictionary, marker_id, side)
        left = max(20, min(620 - side, int(x * 640 - side / 2)))
        top = 240 - side // 2
        frame[top:top+side, left:left+side] = cv2.cvtColor(marker, cv2.COLOR_GRAY2BGR)
    return frame
