"""ArUco adapter. No camera ownership and no control logic."""
import cv2
import numpy as np
from time import perf_counter
from numbers import Integral, Real
from math import isfinite
from .core import Observation


class _ProcessingBudgetExceeded(Exception):
    """Internal cooperative cancellation between non-interruptible calls."""


class MarkerTracker:
    # The extra large-image pass only examines existing rejected quads. The
    # bounds cap additional decoder calls/pixels, not total detector latency.
    RESCUE_MAX_INPUT_PIXELS = 3840 * 2160
    RESCUE_MAX_CANDIDATES = 64
    RESCUE_MAX_ROIS = 8
    # Identity validation must not reject nine tiny background contours just
    # because eight LARGE rescue crops were the original work allowance.
    # Keep every unresolved candidate, the existing pixel/deadline limits,
    # and the original eight-crop limit for locating an undetected target.
    DUPLICATE_MAX_ROIS = RESCUE_MAX_CANDIDATES
    RESCUE_MAX_ROI_SIDE = 512
    RESCUE_MAX_PIXELS = 512 * 1024

    def __init__(self, target_id=0, *, processing_budget_ms=None, clock=perf_counter):
        if (isinstance(target_id, (bool, np.bool_))
                or not isinstance(target_id, Integral) or not 0 <= target_id < 50):
            raise ValueError("target_id must be an integer from 0 to 49 for DICT_4X4_50")
        self.target_id = int(target_id)
        self.processing_budget_ms = self._validate_budget(processing_budget_ms)
        if not callable(clock):
            raise ValueError("clock must be a monotonic callable")
        self._clock = clock
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

    @staticmethod
    def _validate_budget(value):
        if value is not None and (isinstance(value, (bool, np.bool_))
                or not isinstance(value, Real) or not isfinite(value) or value <= 0):
            raise ValueError("processing_budget_ms must be finite and positive, or None")
        return None if value is None else float(value)

    def detect(self, frame, frame_id, acquired_at, source="camera", *,
               processing_budget_ms=None):
        """Decode one current frame, optionally limiting additional work.

        The budget is COOPERATIVE: an OpenCV call already in progress cannot
        be interrupted. After it returns no further rescue is started if the
        budget has expired. An incomplete identity/duplicate scan never
        supplies a control position. None uses the constructor's budget;
        constructing with None preserves exhaustive offline fixture checks.
        """
        self.last_diagnostics = {}
        if (not isinstance(frame, np.ndarray) or frame.dtype != np.uint8
                or frame.size == 0 or frame.ndim not in (2, 3)
                or (frame.ndim == 3 and frame.shape[2] not in (3, 4))):
            raise ValueError("Expected a non-empty uint8 gray/BGR/BGRA image")
        limit = self._validate_budget(processing_budget_ms)
        if limit is None:
            limit = self.processing_budget_ms
        started = self._clock()
        budget = {"limit_ms": limit, "elapsed_ms": 0., "exceeded": False,
                  "exhausted_at": None, "policy": "cooperative_between_native_calls"}
        height, width = frame.shape[:2]
        self.last_diagnostics = {
            "recognition_profile": "v020_identity_current_frame_reuse",
            "target_id": self.target_id, "image_width": width, "image_height": height,
            "processing_budget": budget, "detector_method": "not_started",
            "attempts": [], "ignored_other_markers": [],
            "detected_ids": [], "detected_markers": []}

        def check_budget(stage):
            budget["elapsed_ms"] = max(0., (self._clock()-started)*1000)
            if limit is not None and budget["elapsed_ms"] >= limit:
                budget.update(exceeded=True, exhausted_at=stage)
                raise _ProcessingBudgetExceeded

        try:
            result = self._detect(frame, frame_id, acquired_at, source, check_budget)
            check_budget("observation_complete")
        except _ProcessingBudgetExceeded:
            # Even an easy decoded target is unsafe to return when another
            # candidate could not be checked. Never keep a previous position.
            self.last_diagnostics.pop("target_quality", None)
            self.last_diagnostics["detected_ids"] = []
            self.last_diagnostics["detected_markers"] = []
            result = Observation(frame_id=frame_id, acquired_at=acquired_at,
                                 source=source, width=width, height=height,
                                 target_id=self.target_id, found=False,
                                 reason="detection_budget_exceeded")
        self.last_diagnostics["processing_budget"] = budget
        self.last_diagnostics["detect_ms"] = max(0., (self._clock()-started)*1000)
        return result

    def _detect(self, frame, frame_id, acquired_at, source, check_budget):
        started = perf_counter()
        check_budget("grayscale:before")
        height, width = frame.shape[:2]
        if frame.ndim == 2:
            gray = frame
        else:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY if frame.shape[2] == 4 else cv2.COLOR_BGR2GRAY)
        check_budget("grayscale:after")
        attempts = []
        ignored_other_markers = []
        self.last_diagnostics.update(attempts=attempts,
                                     ignored_other_markers=ignored_other_markers)
        candidate_quads = []
        candidate_geometry = []
        decoded_quads = []
        candidate_overflow = False
        half_contrast_checked = False
        identity_check = {"performed": False, "reused_current_frame": False,
                          "method": None, "saved_decoder_calls": 0}
        self.last_diagnostics["identity_check"] = identity_check
        def detect_with(detector, image, name, scale=1, offset=(0, 0), collect=True):
            nonlocal candidate_overflow
            check_budget(name+":before")
            attempt_started = perf_counter()
            corners, ids, rejected = detector.detectMarkers(image)
            attempts.append({"method": name, "image_scale": scale,
                             "image_offset": list(offset),
                             "input_pixels": int(image.size),
                             "elapsed_ms": (perf_counter()-attempt_started)*1000,
                             "ids": [] if ids is None else [int(v) for v in ids.flatten()],
                             "rejected_candidates": len(rejected)})
            self.last_diagnostics["detector_method"] = name
            check_budget(name+":after")
            if scale != 1 or offset != (0, 0):
                shift = np.asarray(offset, dtype=np.float32)
                corners = [points / scale + shift for points in corners]
                rejected = [points / scale + shift for points in rejected]
            if ids is not None:
                decoded_quads.extend(np.asarray(quad[0], dtype=np.float32)
                                     for quad in corners)
            # Keep threshold-based rejected quads even before a success, so
            # a later side-view success cannot conceal a contrast-only copy.
            # The edge extractor's rejected shapes include unrelated line
            # fragments; reprocessing those would add noise rather than the
            # bounded marker candidates used by our other rescue paths.
            if collect and not name.startswith("edge_quad") and not candidate_overflow:
                for quad in rejected:
                    check_budget(name+":candidate_collection")
                    points = np.asarray(quad[0], dtype=np.float32)
                    # These immutable current-frame geometry values were
                    # previously recomputed for every pair of candidates.
                    # Cache exactly the same mean/area, changing no decision.
                    area = abs(cv2.contourArea(points))
                    # Duplicate threshold passes should not consume the ROI
                    # budget. Retain different nearby quads as separate inputs.
                    if area < 64:
                        continue
                    # The cells inside ANY decoded marker are not a second
                    # marker. Do not spend decoder calls on those cells.
                    if any(all(cv2.pointPolygonTest(known, tuple(map(float, pt)), True)
                                   >= -2 for pt in points) for known in decoded_quads):
                        continue
                    center = points.mean(0)
                    if any(np.linalg.norm(center-known_center) < 3
                           and abs(area-known_area) < max(32, known_area*.15)
                           for known_center, known_area in candidate_geometry):
                        continue
                    if len(candidate_quads) >= self.RESCUE_MAX_CANDIDATES:
                        candidate_overflow = True
                        break
                    candidate_quads.append(points)
                    candidate_geometry.append((center, area))
            # Other IDs are not the selected target. Retain their geometry in
            # diagnostics, then keep searching. Never relabel them as ID0
            # or suppress duplicate target IDs.
            if ids is not None:
                keep = []
                for i, marker_id in enumerate(ids.flatten()):
                    check_budget(name+":identity_selection")
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
        if ids is None:
            corners, ids, rejected = detect_with(self.motion_detector, gray, "cell_interior")
            method = "cell_interior"
        if ids is None and min(height, width) >= 240:
            # Extra current-frame scale only; no tracking/predicted coordinates.
            # INTER_AREA can make blurred/noisy cell boundaries easier to decode.
            check_budget("downscaled_cell_interior:resize")
            reduced = cv2.resize(gray, None, fx=.5, fy=.5, interpolation=cv2.INTER_AREA)
            corners, ids, rejected = detect_with(
                self.motion_detector, reduced, "downscaled_cell_interior", .5)
            method = "downscaled_cell_interior"
        if ids is None and min(height, width) >= 240 and max(height, width) <= 960:
            # Work at half size: full-resolution enhancement amplifies grain
            # and made failed-frame processing substantially slower in testing.
            check_budget("downscaled_local_contrast:enhance")
            enhanced = self.contrast_enhancer.apply(reduced)
            corners, ids, rejected = detect_with(
                self.motion_detector, enhanced, "downscaled_local_contrast", .5)
            half_contrast_checked = True
            identity_check.update(performed=True, method="downscaled_local_contrast")
            method = "downscaled_local_contrast"
        if ids is None and max(height, width) <= 960:
            # Mild spatial edge emphasis only. No temporal tracking, prediction,
            # identity relabelling or reuse of an earlier frame's coordinates.
            check_budget("edge_quad_exact:sharpen")
            sharpened = cv2.addWeighted(gray, 1.8, cv2.GaussianBlur(gray, (0, 0), 1), -.8, 0)
            corners, ids, rejected = detect_with(self.side_detector, sharpened, "edge_quad_exact")
            method = "edge_quad_exact"
        if ids is None and max(height, width) <= 960:
            # Upscaling processes FOUR times as many pixels. Try original and
            # reduced current-frame decodes first so contrast/side rescue need
            # not wait behind this expensive full-frame pass. Successful side
            # decodes still undergo the same unknown-quad duplicate scan.
            check_budget("upscaled_wide_threshold:resize")
            enlarged = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            corners, ids, rejected = detect_with(self.fallback_detector, enlarged,
                                                "upscaled_wide_threshold", 2)
            method = "upscaled_wide_threshold"
        rescue = {"roi_count": 0, "input_pixels": 0, "decoder_calls": 0,
                  "decoder_pixels": 0, "budget_exceeded": False}
        self.last_diagnostics["large_image_rescue"] = rescue
        if ids is None and max(height, width) > 960:
            # Preserve small marker pixels instead of shrinking the entire
            # camera image. Crop only CURRENT-frame rejected candidates; no
            # bit repair, predicted position, previous-frame ROI, or ID remap.
            rois = []
            roi_keys = set()
            rescue["candidate_count"] = len(candidate_quads)
            if gray.size > self.RESCUE_MAX_INPUT_PIXELS or candidate_overflow:
                rescue["budget_exceeded"] = True
            else:
                for points in candidate_quads:
                    check_budget("large_image_rescue:plan_roi")
                    span = points.max(0)-points.min(0)
                    margin = max(16, int(np.ceil(span.max()*.4)))
                    x1, y1 = np.maximum(np.floor(points.min(0)).astype(int)-margin, 0)
                    x2, y2 = np.minimum(np.ceil(points.max(0)).astype(int)+margin+1,
                                        (width, height))
                    if x2 <= x1 or y2 <= y1:
                        continue
                    scale = min(1., self.RESCUE_MAX_ROI_SIDE/max(x2-x1, y2-y1))
                    pixels = round((x2-x1)*scale)*round((y2-y1)*scale)
                    roi_key = (int(x1), int(y1), int(x2), int(y2), scale)
                    if roi_key in roi_keys:
                        continue
                    if (len(rois) >= self.RESCUE_MAX_ROIS
                            or sum(r[5] for r in rois)+pixels > self.RESCUE_MAX_PIXELS):
                        rescue["budget_exceeded"] = True
                        break
                    rois.append((x1, y1, x2, y2, scale, pixels))
                    roi_keys.add(roi_key)
            rescued = []
            if not rescue["budget_exceeded"]:
                for x1, y1, x2, y2, scale, pixels in rois:
                    check_budget("large_image_rescue:prepare_roi")
                    crop = gray[y1:y2, x1:x2]
                    if scale != 1:
                        crop = cv2.resize(crop, None, fx=scale, fy=scale,
                                          interpolation=cv2.INTER_AREA)
                    sharp = cv2.addWeighted(crop, 1.8,
                        cv2.GaussianBlur(crop, (0, 0), 1), -.8, 0)
                    check_budget("large_image_rescue:enhance_roi")
                    enhanced = self.contrast_enhancer.apply(crop)
                    # Different targets may need DIFFERENT rescue decoders.
                    # A side-view target does not establish uniqueness while
                    # another candidate only decodes after local contrast.
                    for decoder, pixels_image, name in (
                            (self.side_detector, sharp, "edge_quad_roi_exact"),
                            (self.motion_detector, crop, "cell_roi"),
                            (self.motion_detector, enhanced, "contrast_roi")):
                        new_corners, new_ids, rejected = detect_with(decoder,
                            pixels_image, name, scale, (int(x1), int(y1)), False)
                        rescue["decoder_calls"] += 1
                        rescue["decoder_pixels"] += int(pixels_image.size)
                        if new_ids is not None:
                            for quad in new_corners:
                                if not any(self._same_quad(quad[0], old[0]) for old in rescued):
                                    rescued.append(quad)
                        if len(rescued) > 1:
                            break
                    rescue["roi_count"] += 1
                    rescue["input_pixels"] += int(crop.size)
                    # Search EVERY planned ROI, even after the first success.
                    # One target seen in overlapping crops counts once; two
                    # distinct targets remain ambiguous and stop following.
                    if len(rescued) > 1:
                        break
                if rois:
                    method = "edge_quad_roi_exact"
                corners = rescued
                ids = (np.full((len(rescued), 1), self.target_id, dtype=np.int32)
                       if rescued else None)
            # Do not report a single rescued target if another candidate could
            # not be searched within the bounded budget.
            if rescue["budget_exceeded"]:
                corners, ids = [], None
        if ids is not None and len(ids) == 1 and half_contrast_checked:
            # The earlier pass already decoded these exact current-frame
            # half-size CLAHE pixels with this same detector and settings.
            # It also recorded all decoded IDs/geometry and collected its
            # rejected candidates. A later side/upscale rescue does not make
            # repeating that completed pass add identity evidence. This is
            # per-call reuse only, never a result/position from an older frame.
            identity_check.update(reused_current_frame=True,
                saved_decoder_calls=int(method != "downscaled_local_contrast"))
        if (ids is not None and len(ids) == 1 and min(height, width) >= 240
                and not half_contrast_checked):
            # A low-contrast copy may produce no rejected quad at all in the
            # successful pass. Check current-frame identity at the same half
            # scale/CLAHE settings used by the fallback before declaring the
            # decoded target unique. If that exact full-frame pass already
            # succeeded, repeating its identical pixels cannot add identity.
            # This supplements the ROI scan; unresolved candidates remain.
            check_budget("identity_half_contrast:resize")
            identity_reduced = cv2.resize(gray, None, fx=.5, fy=.5,
                                          interpolation=cv2.INTER_AREA)
            check_budget("identity_half_contrast:enhance")
            identity_enhanced = self.contrast_enhancer.apply(identity_reduced)
            extra, extra_ids, _ = detect_with(self.motion_detector,
                identity_enhanced, "identity_half_contrast", .5, collect=False)
            identity_check.update(performed=True, method="identity_half_contrast")
            # detect_with records every decoded ID's geometry, so known
            # non-target marker cells can also leave the pending ROI scan.
            # Do not add enhanced-image rejection noise to that scan.
            if extra_ids is not None:
                for quad in extra:
                    if not any(self._same_quad(quad[0], old[0]) for old in corners):
                        corners.append(quad)
            ids = np.full((len(corners), 1), self.target_id, dtype=np.int32)
        ambiguity = {"roi_count": 0, "input_pixels": 0, "decoder_calls": 0,
                     "decoder_pixels": 0, "budget_exceeded": False,
                      "budget_reason": None, "planned_roi_count": 0,
                      "planned_input_pixels": 0, "scan_complete": False,
                      "candidate_overflow": candidate_overflow,
                      "limits": {"candidates": self.RESCUE_MAX_CANDIDATES,
                                 "rois": self.DUPLICATE_MAX_ROIS,
                                 "pixels": self.RESCUE_MAX_PIXELS,
                                 "input_pixels": self.RESCUE_MAX_INPUT_PIXELS},
                     "scope": "current_frame_rejected_quads_only"}
        self.last_diagnostics["duplicate_check"] = ambiguity
        if ids is not None and len(ids) == 1 and method != "edge_quad_roi_exact":
            # The full-frame identity supplement cannot resolve every harder
            # copy. Continue checking unknown CURRENT-frame rejected quads;
            # an incomplete bounded scan still cannot supply a position.
            unknown = [q for q in candidate_quads if not any(
                self._same_quad(q, known) or all(
                    cv2.pointPolygonTest(known, tuple(map(float, pt)), True) >= -2
                    for pt in q) for known in decoded_quads)]
            check_budget("duplicate_check:unknown_candidates")
            unknown.sort(key=lambda q: abs(cv2.contourArea(q)), reverse=True)
            rois = []
            roi_keys = set()
            ambiguity["candidate_count"] = len(unknown)
            if candidate_overflow or (unknown and gray.size > self.RESCUE_MAX_INPUT_PIXELS):
                ambiguity["budget_exceeded"] = True
                ambiguity["budget_reason"] = ("candidate_count" if candidate_overflow else "input_pixels")
            else:
                for points in unknown:
                    check_budget("duplicate_check:plan_roi")
                    span = points.max(0)-points.min(0)
                    margin = max(16, int(np.ceil(span.max()*.4)))
                    x1, y1 = np.maximum(np.floor(points.min(0)).astype(int)-margin, 0)
                    x2, y2 = np.minimum(np.ceil(points.max(0)).astype(int)+margin+1,
                                        (width, height))
                    scale = min(1., self.RESCUE_MAX_ROI_SIDE/max(x2-x1, y2-y1))
                    pixels = round((x2-x1)*scale)*round((y2-y1)*scale)
                    # Only identical crops at identical scale are equivalent.
                    # Contained/overlapping crops can decode differently.
                    roi_key = (int(x1), int(y1), int(x2), int(y2), scale)
                    if roi_key in roi_keys:
                        continue
                    if (len(rois) >= self.DUPLICATE_MAX_ROIS or
                            sum(r[5] for r in rois)+pixels > self.RESCUE_MAX_PIXELS):
                        ambiguity["budget_exceeded"] = True
                        ambiguity["budget_reason"] = ("roi_count" if len(rois) >= self.DUPLICATE_MAX_ROIS
                                                      else "roi_pixels")
                        break
                    rois.append((x1, y1, x2, y2, scale, pixels))
                    roi_keys.add(roi_key)
            ambiguity["planned_roi_count"] = len(rois)
            ambiguity["planned_input_pixels"] = sum(r[5] for r in rois)
            if not ambiguity["budget_exceeded"]:
                for x1, y1, x2, y2, scale, pixels in rois:
                    check_budget("duplicate_check:prepare_roi")
                    crop = gray[y1:y2, x1:x2]
                    if scale != 1:
                        crop = cv2.resize(crop, None, fx=scale, fy=scale,
                                          interpolation=cv2.INTER_AREA)
                    sharp = cv2.addWeighted(crop, 1.8,
                        cv2.GaussianBlur(crop, (0, 0), 1), -.8, 0)
                    check_budget("duplicate_check:enhance_roi")
                    enhanced = self.contrast_enhancer.apply(crop)
                    # At most three decoder calls per ROI. A repeat decode of
                    # the known marker must not hide another copy, but two
                    # distinct copies already prove ambiguity: stop early.
                    for decoder, pixels_image, suffix in (
                            (self.motion_detector, crop, "cell"),
                            (self.motion_detector, enhanced, "contrast"),
                            (self.side_detector, sharp, "edge_exact")):
                        extra, extra_ids, _ = detect_with(decoder, pixels_image,
                            "duplicate_roi_"+suffix, scale, (int(x1), int(y1)), False)
                        ambiguity["decoder_calls"] += 1
                        ambiguity["decoder_pixels"] += int(pixels_image.size)
                        if extra_ids is not None:
                            for quad in extra:
                                if not any(self._same_quad(quad[0], old[0]) for old in corners):
                                    corners.append(quad)
                        if len(corners) > 1:
                            break
                    ambiguity["roi_count"] += 1
                    ambiguity["input_pixels"] += int(crop.size)
                    if len(corners) > 1:
                        break
                ambiguity["scan_complete"] = ambiguity["roi_count"] == len(rois)
                ids = np.full((len(corners), 1), self.target_id, dtype=np.int32)
            else:
                corners, ids = [], None
        check_budget("image_diagnostics:before")
        self.last_diagnostics = {"detector_method": method,
            "recognition_profile": "v020_identity_current_frame_reuse",
            "target_id": self.target_id,
            "identity_check": identity_check,
            "large_image_rescue": rescue,
            "duplicate_check": ambiguity,
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
            # Diagnostic only; avoid a full-resolution float64 convolution on
            # every control frame. Detector pixels/identity rules stay intact.
            "image_sharpness": float(cv2.Laplacian(cv2.resize(gray,
                (min(320,width), max(1, round(height*min(320,width)/width)))), cv2.CV_16S).var()),
            "sharpness_diagnostic_width": min(320,width),
            "detect_ms": (perf_counter()-started)*1000}
        check_budget("image_diagnostics:after")
        matches = [] if ids is None else [i for i, v in enumerate(ids.flatten()) if int(v) == self.target_id]
        base = dict(frame_id=frame_id, acquired_at=acquired_at, source=source,
                    width=width, height=height, target_id=self.target_id)
        if len(matches) != 1:
            return Observation(**base, found=False,
                               reason=("ambiguous_duplicate_id" if matches else
                                       "ambiguous_scan_budget_exceeded" if ambiguity["budget_exceeded"] else
                                       "rescue_budget_exceeded" if rescue["budget_exceeded"]
                                       else "not_found"))
        points = corners[matches[0]][0]
        center = points.mean(axis=0)
        side = np.linalg.norm(points - np.roll(points, -1, axis=0), axis=1).mean()
        x1, y1 = np.maximum(np.floor(points.min(axis=0)).astype(int), 0)
        x2, y2 = np.minimum(np.ceil(points.max(axis=0)).astype(int) + 1, (width, height))
        roi = gray[y1:y2, x1:x2]
        if roi.size:
            check_budget("target_quality:before")
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

    @staticmethod
    def _same_quad(a, b):
        """Deduplicate the same physical quad across crops/order/refinement."""
        tolerance = max(3., .04*min(np.linalg.norm(a-np.roll(a, -1, axis=0), axis=1)))
        # Disjoint bounding boxes cannot pass either corner-distance or IoU
        # matching. Avoid eight corner permutations and polygon intersection
        # for unrelated background contours; no candidate/decoder is skipped.
        if (np.any(a.min(axis=0) - b.max(axis=0) > tolerance)
                or np.any(b.min(axis=0) - a.max(axis=0) > tolerance)):
            return False
        if any(np.max(np.linalg.norm(a-np.roll(order, n, axis=0), axis=1)) < tolerance
               for order in (b, b[::-1]) for n in range(4)):
            return True
        # Crop scale/refinement can shift a narrow quad's corners by a few
        # pixels. Require strong polygon overlap AND a close center, rather
        # than declaring that single physical marker to be a second target.
        area_a, area_b = abs(cv2.contourArea(a)), abs(cv2.contourArea(b))
        intersection, _ = cv2.intersectConvexConvex(a.astype(np.float32), b.astype(np.float32))
        union = area_a+area_b-intersection
        return (union > 0 and intersection/union > .75
                and np.linalg.norm(a.mean(0)-b.mean(0)) < .15*np.sqrt(min(area_a, area_b)))


def synthetic_frame(x=0.5, size=0.25, visible=True, marker_id=0, *, y=0.5):
    """Synthetic camera input only; does not simulate aircraft physics."""
    frame = np.full((480, 640, 3), 245, dtype=np.uint8)
    if visible:
        side = max(40, min(250, int(size * 640)))
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        marker = cv2.aruco.generateImageMarker(dictionary, marker_id, side)
        left = max(20, min(620 - side, int(x * 640 - side / 2)))
        top = max(20, min(460 - side, int(y * 480) - side // 2))
        frame[top:top+side, left:left+side] = cv2.cvtColor(marker, cv2.COLOR_GRAY2BGR)
    return frame
