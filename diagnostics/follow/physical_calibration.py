"""Offline records only. Never opens a camera, controls a drone or installs packages."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'follow'
sys.path.insert(0, str(ROOT))

import argparse
from collections import Counter
import hashlib
import json
import statistics

from follow.r3_follow.physical_metrics import speed_measurement


def _keys(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            path = prefix + key
            yield path
            yield from _keys(item, path + ".")
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item, prefix + "[].")


def _read_log(path):
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if line.strip():
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}:{line_number}: invalid JSON") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path.name}:{line_number}: event must be an object")
            records.append(row)
    return records


def audit_diary(directory, flight_log=None):
    """Audit keys and timing, never promote requested RC or height into velocity.

    The caller must name the actual-flight log. Largest file is not proof of a
    flight; neither is a legacy simulation flag proof that no flight occurred.
    This report reports data presence, not physical truth of the attachment.
    """
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise ValueError("diary directory does not exist")
    files = sorted(directory.rglob("*.jsonl"))
    if not files:
        raise ValueError("no JSONL evidence found; no measurements inferred")
    fields, events = Counter(), Counter()
    for path in files:
        for row in _read_log(path):
            fields.update(_keys(row))
            events.update([row.get("event", "unknown")])
    leafs = {k.rsplit(".", 1)[-1] for k in fields}
    groups = {
        "velocity_telemetry_fields": ("vgx", "vgy", "vgz", "velocity_m_s", "velocity_cm_s"),
        "measured_reference_distance_fields": ("reference_distance_m", "physical_distance_m"),
        "physical_marker_size_fields": ("marker_side_m", "marker_side_cm", "marker_length_m"),
        "camera_calibration_fields": ("camera_matrix", "dist_coeffs", "intrinsics"),
        "independent_aircraft_motion_fields": ("aircraft_position_m", "signed_displacement_m"),
    }
    present = {k: [name for name in names if name in leafs] for k, names in groups.items()}
    report = {"scope": "read-only JSONL field inventory; text/notes and images are not parsed as measurements",
              "directory": str(directory), "log_files": len(files), "event_counts": dict(events),
              "fields": dict(sorted(fields.items())), "measurement_field_presence": present,
              "absent_measurement_groups": [k for k, names in present.items() if not names],
              "speed_mapping_status": "not_calibrated",
              "distance_mapping_status": "not_calibrated",
              "reason": "Field presence alone is not calibration. Explicit independent measurement, "
                        "units, camera/marker identity and synchronized RC evidence are required.",
              "limits": ["speed 50 is a command setting, not measured aircraft velocity.",
                         "Height is not forward range to the target.",
                         "Image motion combines aircraft, target motion and marker pose.",
                         "No RC-to-m/s conversion or physical distance is inferred."]}
    if flight_log is not None:
        path = Path(flight_log).resolve()
        if not path.is_relative_to(directory):
            raise ValueError("flight log must be inside the audited directory")
        records = _read_log(path)
        active = None
        segments = []
        for row in records:
            event = row.get("event")
            if event == "start":
                if active is not None:
                    raise ValueError("overlapping start events; inspect lifecycle manually")
                active = row.get("time")
            elif event in ("tracking_halted", "stop", "handoff_pause") and active is not None:
                end = row.get("time")
                if not isinstance(active, (int, float)) or not isinstance(end, (int, float)) or end <= active:
                    raise ValueError("invalid lifecycle timestamps")
                observations = [r for r in records if r.get("event") == "observation"
                                and active <= r.get("time", -1) < end]
                requests = [r for r in records if r.get("event") == "motion_request"
                            and active <= r.get("time", -1) < end]
                deltas = [b["time"] - a["time"] for a, b in zip(requests, requests[1:])]
                seconds = end - active
                segments.append({"start_monotonic": active, "end_monotonic": end,
                                 "active_s": seconds, "observations": len(observations),
                                 "observation_results_per_s": len(observations) / seconds,
                                 "rc_requests": len(requests),
                                 "rc_requests_per_s": len(requests) / seconds,
                                 "median_rc_interval_s": statistics.median(deltas) if deltas else None})
                active = None
        report["caller_selected_flight_log"] = {
            "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "selection_note": "Caller labels this as actual flight; hardware not independently verified.",
            "segments": segments, "unterminated_active_interval": active is not None,
            "rate_unit": "software events per second, not m/s"}
    return report


def import_speed_records(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise ValueError("input must be an object with a records list")
    results = [speed_measurement(item) for item in payload["records"]]
    recorded = sum(r["status"] == "recorded" for r in results)
    return {"records": results, "recorded_count": recorded,
            "rejected_count": len(results) - recorded,
            "mapping_status": "individual_measurements_only" if recorded else "not_calibrated",
            "linear_mapping_assumed": False, "flight_behavior_changed": False,
            "note": "No interpolation/extrapolation and no maximum-speed claim. "
                    "Evidence is caller supplied; input validation is not a flight verification."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit-diary", help="Inventory existing JSONL; leave source files unchanged")
    audit.add_argument("--input", type=Path, required=True)
    audit.add_argument("--flight-log", type=Path)
    audit.add_argument("--output", type=Path, required=True)
    speed = sub.add_parser("import-speed", help="Import independently measured aircraft speed records")
    speed.add_argument("--input", type=Path, required=True)
    speed.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "audit-diary":
            result = audit_diary(args.input, args.flight_log)
        else:
            result = import_speed_records(json.loads(args.input.read_text(encoding="utf-8-sig")))
        destination = args.output.resolve()
        if destination == args.input.resolve() or (args.command == "audit-diary"
                and destination.is_relative_to(args.input.resolve())):
            raise ValueError("write results outside source inputs; original measurements are read-only")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                               encoding="utf-8")
    except (ValueError, OSError, TypeError) as exc:
        parser.exit(2, f"Invalid input: {exc}\n")
    print(destination)


if __name__ == "__main__":
    main()
