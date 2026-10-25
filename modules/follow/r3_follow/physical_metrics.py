"""Read-only physical estimates and measurement records; never a flight controller.

RC requests are dimensionless. Nothing here converts RC by assuming a speed law.
Distance is an apparent-size estimate anchored to a measured reference depth,
not a calibrated pose solution or a measured person-to-aircraft separation.
"""
from collections.abc import Mapping
import math

from .core import ApparentSizeGuard, marker_shape

DISTANCE_SCHEMA = "r3_reference_depth_v1"
DISTANCE_ASSUMPTIONS = (
    "Same physical marker, camera, lens/zoom/crop and image settings as calibration; "
    "approximately front-facing marker; estimate is camera-axis depth, not slant range. "
    "Passing image geometry cannot prove physical pose. No accuracy bound is calibrated."
)
AXES = ("left_right", "forward_back", "up_down")


def _number(value, *, positive=False):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and (not positive or value > 0))
    except (ValueError, OverflowError):
        return False


def _name(value):
    return isinstance(value, str) and bool(value.strip())


def _unavailable(reason):
    return {"status": "unavailable", "distance_m": None, "unit": "m",
            "reason": reason, "is_estimate": True,
            "assumptions": DISTANCE_ASSUMPTIONS}


def _observation_issue(obs, now, max_age):
    if not _number(now) or not _number(max_age, positive=True):
        return "invalid_clock_or_age_limit"
    if obs is None or getattr(obs, "found", None) is not True:
        return "target_unavailable"
    if getattr(obs, "reason", ""):
        return "target_result_not_clear"
    acquired = getattr(obs, "acquired_at", None)
    if not _number(acquired):
        return "invalid_timestamp"
    age = now - acquired
    if age < -1e-9:
        return "future_frame"
    if age > max_age:
        return "stale_frame"
    width, height = getattr(obs, "width", None), getattr(obs, "height", None)
    if not all(isinstance(x, int) and not isinstance(x, bool) and x > 0
               for x in (width, height)):
        return "invalid_image_size"
    target = getattr(obs, "target_id", None)
    if not isinstance(target, int) or isinstance(target, bool) or target < 0:
        return "invalid_target_id"
    ratio = getattr(obs, "size_ratio", None)
    if not _number(ratio, positive=True) or ratio > 1:
        return "invalid_size_ratio"
    if not _number(getattr(obs, "center_x", None)) or not _number(getattr(obs, "center_y", None)):
        return "invalid_target_center"
    if not 0 <= obs.center_x < width or not 0 <= obs.center_y < height:
        return "target_outside_image"
    corners = getattr(obs, "corners", ())
    try:
        if len(corners) != 4 or any(len(p) != 2 or not all(_number(v) for v in p) for p in corners):
            return "geometry_unsuitable"
    except TypeError:
        return "geometry_unsuitable"
    shape = marker_shape(corners)
    if not ApparentSizeGuard.suitable(shape, strict=True):
        return "geometry_unsuitable"
    if any(not 0 <= x < width or not 0 <= y < height for x, y in corners):
        return "target_outside_image"
    side = sum(math.hypot(corners[(i + 1) % 4][0] - corners[i][0],
                          corners[(i + 1) % 4][1] - corners[i][1])
               for i in range(4)) / 4
    if not math.isclose(side / width, ratio, rel_tol=.02, abs_tol=1e-6):
        return "size_geometry_mismatch"
    return None


def create_distance_calibration(observation, reference_distance_m, *, now,
                                camera_key, marker_key, dictionary="DICT_4X4_50",
                                max_age=.5, front_facing_confirmed=False):
    """Record a caller-measured camera-axis depth. Never infer this from pixels.

    camera_key identifies the camera AND its zoom/crop/lens/settings. marker_key
    identifies a physical board, not just a dictionary ID. A reprint/rescale or
    camera change requires another calibration. An explicit confirmation is
    required; it is caller supplied, not independently verified by this function.
    """
    issue = _observation_issue(observation, now, max_age)
    if issue:
        return {"status": "rejected", "reason": issue, "calibration": None}
    if not _number(reference_distance_m, positive=True):
        return {"status": "rejected", "reason": "known_distance_required", "calibration": None}
    if front_facing_confirmed is not True:
        return {"status": "rejected", "reason": "front_facing_confirmation_required", "calibration": None}
    if not all(_name(x) for x in (camera_key, marker_key, dictionary)):
        return {"status": "rejected", "reason": "binding_required", "calibration": None}
    calibration = {"schema": DISTANCE_SCHEMA, "camera_key": camera_key,
                   "marker_key": marker_key, "dictionary": dictionary,
                   "target_id": observation.target_id,
                   "image_width": observation.width, "image_height": observation.height,
                   "reference_size_ratio": observation.size_ratio,
                   "reference_distance_m": reference_distance_m,
                   "reference_frame_id": observation.frame_id,
                   "front_facing_confirmed": True,
                   "measurement_source": "caller_measured_reference_depth",
                   "assumptions": DISTANCE_ASSUMPTIONS}
    return {"status": "recorded", "reason": "caller_measurement_not_independently_verified",
            "calibration": calibration}


def estimate_distance(observation, calibration, *, now, camera_key, marker_key,
                      reference_size_ratio, dictionary="DICT_4X4_50", max_age=.5):
    """Return a bound estimate or an explicit unavailable result; no RC changes."""
    if calibration is None:
        return _unavailable("not_calibrated")
    if not isinstance(calibration, Mapping) or calibration.get("schema") != DISTANCE_SCHEMA:
        return _unavailable("invalid_calibration")
    if not all(_name(x) for x in (camera_key, marker_key, dictionary)):
        return _unavailable("binding_required")
    issue = _observation_issue(observation, now, max_age)
    if issue:
        return _unavailable(issue)
    for key, actual in (("camera_key", camera_key), ("marker_key", marker_key),
                        ("dictionary", dictionary), ("target_id", observation.target_id),
                        ("image_width", observation.width), ("image_height", observation.height)):
        if calibration.get(key) != actual:
            return _unavailable(key + "_mismatch")
    depth = calibration.get("reference_distance_m")
    ratio = calibration.get("reference_size_ratio")
    if (not _number(depth, positive=True) or not _number(ratio, positive=True)
            or ratio > 1 or calibration.get("front_facing_confirmed") is not True):
        return _unavailable("invalid_calibration")
    if (not _number(reference_size_ratio, positive=True)
            or not math.isclose(reference_size_ratio, ratio, rel_tol=1e-9, abs_tol=1e-12)):
        return _unavailable("reference_size_changed")
    distance = depth * ratio / observation.size_ratio
    if not _number(distance, positive=True):
        return _unavailable("estimate_out_of_range")
    return {"status": "estimated", "distance_m": distance, "unit": "m", "reason": "",
            "is_estimate": True, "method": "measured_reference_apparent_size_ratio",
            "frame_id": observation.frame_id, "assumptions": DISTANCE_ASSUMPTIONS}


def speed_measurement(record):
    """Validate one caller-supplied aircraft measurement; no curve fitting.

    Constant single-axis RC and stable heading are required for assigning a
    measurement to one RC channel. Displacement/time is a segment average (which
    can include acceleration), not an assumed steady-state or maximum velocity.
    Telemetry must be explicitly converted to m/s and synchronized with the RC
    interval by its producer. Validation here cannot verify supplied evidence.
    """
    def reject(reason):
        return {"status": "unavailable", "reason": reason, "mean_velocity_m_s": None}
    if not isinstance(record, Mapping):
        return reject("invalid_record")
    if record.get("measured_object") != "aircraft":
        return reject("aircraft_motion_required_not_target_relative_motion")
    if not all(_name(record.get(k)) for k in ("aircraft_key", "evidence")):
        return reject("aircraft_and_evidence_required")
    if record.get("axis") not in AXES:
        return reject("unsupported_axis")
    rc = record.get("rc")
    if not isinstance(rc, int) or isinstance(rc, bool) or not -100 <= rc <= 100:
        return reject("invalid_rc")
    if any(record.get(k) is not True for k in
           ("constant_rc_confirmed", "other_axes_zero_confirmed", "heading_held_confirmed")):
        return reject("isolated_constant_command_confirmation_required")
    if record.get("method") == "displacement":
        distance, duration = record.get("signed_displacement_m"), record.get("elapsed_s")
        if not _number(distance) or not _number(duration, positive=True):
            return reject("measured_displacement_and_duration_required")
        mean = distance / duration
        samples = None
    elif record.get("method") == "telemetry":
        if (record.get("telemetry_unit") != "m/s"
                or record.get("telemetry_axis") != record["axis"]
                or record.get("common_clock_confirmed") is not True
                or record.get("axis_mapping_confirmed") is not True):
            return reject("telemetry_units_axis_and_clock_required")
        samples = record.get("samples")
        if not isinstance(samples, list) or len(samples) < 2:
            return reject("at_least_two_telemetry_samples_required")
        if any(not isinstance(s, Mapping) or not _number(s.get("time_s"))
               or not _number(s.get("velocity_m_s")) for s in samples):
            return reject("invalid_telemetry_sample")
        times = [s["time_s"] for s in samples]
        if any(b <= a for a, b in zip(times, times[1:])):
            return reject("telemetry_times_must_increase")
        begin, end = record.get("rc_start_s"), record.get("rc_end_s")
        if (not _number(begin) or not _number(end) or begin > times[0]
                or end < times[-1] or end <= begin):
            return reject("telemetry_not_covered_by_constant_rc_interval")
        duration = times[-1] - times[0]
        mean = sum((b["time_s"] - a["time_s"]) *
                   (a["velocity_m_s"] + b["velocity_m_s"]) / 2
                   for a, b in zip(samples, samples[1:])) / duration
    else:
        return reject("unsupported_measurement_method")
    if not _number(mean) or not _number(mean * 100):
        return reject("nonfinite_derived_velocity")
    return {"status": "recorded", "method": record["method"],
            "aircraft_key": record["aircraft_key"], "axis": record["axis"], "rc": rc,
            "mean_velocity_m_s": mean, "mean_velocity_cm_s": mean * 100,
            "duration_s": duration, "sample_count": len(samples) if samples else None,
            "evidence": record["evidence"], "independently_verified": False,
            "interpretation": "One measured segment average, including any acceleration; "
                              "not maximum/steady speed and not a conversion law. "
                              "Do not interpolate to other RC values or aircraft."}
