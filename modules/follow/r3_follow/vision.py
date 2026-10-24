"""ArUco adapter. No camera ownership and no control logic."""
import cv2
import numpy as np
from time import perf_counter
from .core import Observation


class MarkerTracker:
    # The extra large-image pass only examines existing rejected quads. The
    # bounds cap additional decoder calls/pixels, not total detector latency.
    RESCUE_MAX_INPUT_PIXELS = 3840 * 2160
    RESCUE_MAX_CANDIDATES = 64
    RESCUE_MAX_ROIS = 8
    RESCUE_MAX_ROI_SIDE = 512
    RESCUE_MAX_PIXELS = 512 * 1024

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
        # An independent edge/line quad extractor for foreshortened markers.
        # OpenCV's AprilTag quad extraction can decode our SAME ArUco dictionary;
        # this does not change the printed marker or add an AprilTag dependency.
        # The last-resort CURRENT-frame decode requires exact dictionary bits.
        side_parameters = cv2.aruco.DetectorParameters()
        side_parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_APRILTAG
        side_parameters.perspectiveRemovePixelPerCell = 8
        side_parameters.perspectiveRemoveIgnoredMarginPerCell = .25
        side_parameters.errorCorrectionRate = 0.
        self.side_detector = cv2.aruco.ArucoDetector(dictionary, side_parameters)
        self.last_diagnostics = {}

    def detect(self, frame, frame_id, acquired_at, source="camera"):
        self.last_diagnostics = {}
        started = perf_counter()
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
        candidate_quads = []
        candidate_overflow = False
        def detect_with(detector, image, name, scale=1, offset=(0, 0), collect=True):
            nonlocal candidate_overflow
            attempt_started = perf_counter()
            corners, ids, rejected = detector.detectMarkers(image)
            attempts.append({"method": name, "image_scale": scale,
                             "image_offset": list(offset),
                             "input_pixels": int(image.size),
                             "elapsed_ms": (perf_counter()-attempt_started)*1000,
                             "ids": [] if ids is None else [int(v) for v in ids.flatten()],
                             "rejected_candidates": len(rejected)})
            if scale != 1 or offset != (0, 0):
                shift = np.asarray(offset, dtype=np.float32)
                corners = [points / scale + shift for points in corners]
                rejected = [points / scale + shift for points in rejected]
            if collect and max(height, width) > 960:
                for quad in rejected:
                    points = np.asarray(quad[0], dtype=np.float32)
                    # Duplicate threshold passes should not consume the ROI
                    # budget. Retain different nearby quads as separate inputs.
                    if abs(cv2.contourArea(points)) < 64:
                        continue
                    if any(np.linalg.norm(points.mean(0)-q.mean(0)) < 3
                           and abs(cv2.contourArea(points)-cv2.contourArea(q))
                           < max(32, abs(cv2.contourArea(q))*.15)
                           for q in candidate_quads):
                        continue
                    if len(candidate_quads) >= self.RESCUE_MAX_CANDIDATES:
                        candidate_overflow = True
                        break
                    candidate_quads.append(points)
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
        if ids is None and max(height, width) <= 960:
            # Mild spatial edge emphasis only. No temporal tracking, prediction,
            # identity relabelling or reuse of an earlier frame's coordinates.
            sharpened = cv2.addWeighted(gray, 1.8, cv2.GaussianBlur(gray, (0, 0), 1), -.8, 0)
            corners, ids, rejected = detect_with(self.side_detector, sharpened, "edge_quad_exact")
            method = "edge_quad_exact"
        rescue = {"roi_count": 0, "input_pixels": 0, "budget_exceeded": False}
        if ids is None and max(height, width) > 960:
            # Preserve small marker pixels instead of shrinking the entire
            # camera image. Crop only CURRENT-frame rejected candidates; no
            # bit repair, predicted position, previous-frame ROI, or ID remap.
            rois = []
            rescue["candidate_count"] = len(candidate_quads)
            if gray.size > self.RESCUE_MAX_INPUT_PIXELS or candidate_overflow:
                rescue["budget_exceeded"] = True
            else:
                for points in candidate_quads:
                    span = points.max(0)-points.min(0)
                    margin = max(16, int(np.ceil(span.max()*.4)))
                    x1, y1 = np.maximum(np.floor(points.min(0)).astype(int)-margin, 0)
                    x2, y2 = np.minimum(np.ceil(points.max(0)).astype(int)+margin+1,
                                        (width, height))
                    if x2 <= x1 or y2 <= y1:
                        continue
                    scale = min(1., self.RESCUE_MAX_ROI_SIDE/max(x2-x1, y2-y1))
                    pixels = round((x2-x1)*scale)*round((y2-y1)*scale)
                    if (len(rois) >= self.RESCUE_MAX_ROIS
                            or sum(r[5] for r in rois)+pixels > self.RESCUE_MAX_PIXELS):
                        rescue["budget_exceeded"] = True
                        break
                    rois.append((x1, y1, x2, y2, scale, pixels))
            rescued = []
            if not rescue["budget_exceeded"]:
                for x1, y1, x2, y2, scale, pixels in rois:
                    crop = gray[y1:y2, x1:x2]
                    if scale != 1:
                        crop = cv2.resize(crop, None, fx=scale, fy=scale,
                                          interpolation=cv2.INTER_AREA)
                    sharp = cv2.addWeighted(crop, 1.8,
                        cv2.GaussianBlur(crop, (0, 0), 1), -.8, 0)
                    new_corners, new_ids, rejected = detect_with(self.side_detector,
                        sharp, "edge_quad_roi_exact", scale, (int(x1), int(y1)), False)
                    rescue["roi_count"] += 1
                    rescue["input_pixels"] += int(crop.size)
                    # Search EVERY planned ROI, even after the first success.
                    # One target seen in overlapping crops counts once; two
                    # distinct targets remain ambiguous and stop following.
                    if new_ids is not None:
                        for quad in new_corners:
                            if not any(np.max(np.linalg.norm(quad[0]-old[0], axis=1))
                                       < 3 for old in rescued):
                                rescued.append(quad)
                if rois:
                    method = "edge_quad_roi_exact"
                corners = rescued
                ids = (np.full((len(rescued), 1), self.target_id, dtype=np.int32)
                       if rescued else None)
            # Do not report a single rescued target if another candidate could
            # not be searched within the bounded budget.
            if rescue["budget_exceeded"]:
                corners, ids = [], None
        self.last_diagnostics = {"detector_method": method,
            "recognition_profile": "aruco_edge_quad_bounded_roi_v015_candidate",
            "large_image_rescue": rescue,
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
            "image_sharpness": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
            "detect_ms": (perf_counter()-started)*1000}
        matches = [] if ids is None else [i for i, v in enumerate(ids.flatten()) if int(v) == self.target_id]
        base = dict(frame_id=frame_id, acquired_at=acquired_at, source=source,
                    width=width, height=height, target_id=self.target_id)
        if len(matches) != 1:
            return Observation(**base, found=False,
                               reason=("ambiguous_duplicate_id" if matches else
                                       "rescue_budget_exceeded" if rescue["budget_exceeded"]
                                       else "not_found"))
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
                "min_side_px": float(np.linalg.norm(points-np.roll(points, -1, axis=0), axis=1).min()),
                "max_side_px": float(np.linalg.norm(points-np.roll(points, -1, axis=0), axis=1).max()),
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
