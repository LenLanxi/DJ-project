"""Physical labels require evidence; these tests never access hardware."""
from dataclasses import replace
import json
import math
from pathlib import Path
import tempfile
import unittest

from physical_calibration import audit_diary, import_speed_records
from r3_follow.core import Observation
from r3_follow.physical_metrics import (
    create_distance_calibration, estimate_distance, speed_measurement,
)


def observation(size=.2, timestamp=10., target=0):
    side = size * 640
    return Observation(1, timestamp, "camera", 640, 480, target, True,
                       320., 240., size,
                       ((320 - side / 2, 240 - side / 2),
                        (320 + side / 2, 240 - side / 2),
                        (320 + side / 2, 240 + side / 2),
                        (320 - side / 2, 240 + side / 2)))


def displacement_record():
    return dict(method="displacement", measured_object="aircraft", aircraft_key="unit-test-aircraft",
                axis="forward_back", rc=20, signed_displacement_m=.6, elapsed_s=2.,
                constant_rc_confirmed=True, other_axes_zero_confirmed=True,
                heading_held_confirmed=True, evidence="Synthetic test fixture, not a flight result")


class DistanceMetricTests(unittest.TestCase):
    def calibration(self, **kwargs):
        args = dict(now=10.1, camera_key="camera0:uncropped", marker_key="boardA:print1",
                    front_facing_confirmed=True)
        args.update(kwargs)
        return create_distance_calibration(observation(), 1.5, **args)

    def estimate(self, obs=None, calibration=None, **kwargs):
        args = dict(now=10.1, camera_key="camera0:uncropped", marker_key="boardA:print1",
                    reference_size_ratio=.2)
        args.update(kwargs)
        return estimate_distance(obs or observation(.1),
                                 calibration or self.calibration()["calibration"], **args)

    def test_known_reference_scales_depth_but_marks_estimate(self):
        value = self.estimate()
        self.assertEqual(value["status"], "estimated")
        self.assertAlmostEqual(value["distance_m"], 3.)
        self.assertTrue(value["is_estimate"])
        self.assertIn("camera-axis depth", value["assumptions"])

    def test_no_calibration_returns_no_metres(self):
        value = estimate_distance(observation(), None, now=10.1, camera_key="a",
                                  marker_key="b", reference_size_ratio=.2)
        self.assertEqual((value["distance_m"], value["reason"]), (None, "not_calibrated"))

    def test_reference_requires_explicit_front_facing_confirmation(self):
        result = create_distance_calibration(observation(), 1., now=10.1,
                                             camera_key="a", marker_key="b")
        self.assertEqual(result["reason"], "front_facing_confirmation_required")
        self.assertIsNone(result["calibration"])

    def test_known_distance_must_be_positive_finite_and_not_boolean(self):
        for depth in (0, -1, None, True, float("nan"), float("inf"), 10**1000):
            with self.subTest(depth=str(depth)[:20]):
                result = create_distance_calibration(observation(), depth, now=10.1,
                    camera_key="a", marker_key="b", front_facing_confirmed=True)
                self.assertIsNone(result["calibration"])

    def test_reprint_camera_dictionary_target_and_resolution_changes_reject(self):
        cases = [(dict(camera_key="camera0:zoomed"), None),
                 (dict(marker_key="boardA:print2"), None),
                 (dict(dictionary="DICT_5X5_50"), None),
                 ({}, observation(.1, target=1)),
                 ({}, replace(observation(.1), height=720))]
        for kwargs, obs in cases:
            with self.subTest(kwargs=kwargs, obs=obs):
                value = self.estimate(obs=obs, **kwargs)
                self.assertEqual(value["status"], "unavailable")
                self.assertIsNone(value["distance_m"])

    def test_new_controller_reference_invalidates_saved_metres(self):
        self.assertEqual(self.estimate(reference_size_ratio=.21)["reason"], "reference_size_changed")

    def test_stale_and_future_frames_never_show_distance(self):
        self.assertEqual(self.estimate(now=11.01)["reason"], "stale_frame")
        self.assertEqual(self.estimate(now=9.9)["reason"], "future_frame")

    def test_missing_or_ambiguous_target_never_shows_distance(self):
        for obs in (replace(observation(), found=False),
                    replace(observation(), reason="ambiguous_duplicate_id")):
            self.assertIsNone(self.estimate(obs=obs)["distance_m"])

    def test_side_turn_missing_and_corrupt_geometry_reject(self):
        for corners in ((), None, (("1", "2"),) * 4,
                        ((290, 176), (350, 176), (350, 304), (290, 304))):
            self.assertEqual(self.estimate(obs=replace(observation(), corners=corners))["status"],
                             "unavailable")

    def test_disagreement_between_ratio_and_corners_rejects(self):
        self.assertEqual(self.estimate(obs=replace(observation(), size_ratio=.1))["reason"],
                         "size_geometry_mismatch")

    def test_outside_image_and_nonfinite_input_reject(self):
        for changes in (dict(center_x=-1), dict(size_ratio=float("nan")),
                        dict(acquired_at=float("inf")), dict(width=True)):
            self.assertIsNone(self.estimate(obs=replace(observation(), **changes))["distance_m"])

    def test_invalid_calibration_does_not_infer_distance(self):
        self.assertEqual(self.estimate(calibration={"schema": "invented"})["reason"], "invalid_calibration")
        self.assertEqual(self.estimate(camera_key=" ")["reason"], "binding_required")


class SpeedRecordTests(unittest.TestCase):
    def test_independent_displacement_is_segment_average_not_rc_speed_law(self):
        result = speed_measurement(displacement_record())
        self.assertAlmostEqual(result["mean_velocity_m_s"], .3)
        self.assertAlmostEqual(result["mean_velocity_cm_s"], 30.)
        self.assertFalse(result["independently_verified"])
        self.assertIn("not maximum", result["interpretation"])

    def test_signed_backward_displacement_is_preserved(self):
        row = displacement_record()
        row.update(rc=-20, signed_displacement_m=-.4)
        self.assertAlmostEqual(speed_measurement(row)["mean_velocity_m_s"], -.2)

    def test_relative_target_motion_or_command_setting_cannot_supply_speed(self):
        row = displacement_record()
        row["measured_object"] = "marker_relative_to_camera"
        self.assertEqual(speed_measurement(row)["status"], "unavailable")
        self.assertEqual(speed_measurement({"speed": 50, "status": "ok"})["status"], "unavailable")

    def test_invalid_duration_rc_values_and_nonfinite_displacement_reject(self):
        for updates in (dict(elapsed_s=0), dict(elapsed_s=-1), dict(elapsed_s=True),
                        dict(rc=101), dict(rc=True), dict(rc=20.),
                        dict(signed_displacement_m=float("nan")), dict(signed_displacement_m=1e308)):
            row = displacement_record()
            row.update(updates)
            self.assertEqual(speed_measurement(row)["status"], "unavailable", updates)

    def test_no_evidence_or_unconfirmed_command_is_not_a_measurement(self):
        for updates in (dict(evidence=""), dict(aircraft_key=""),
                        dict(constant_rc_confirmed=False), dict(other_axes_zero_confirmed=False),
                        dict(heading_held_confirmed=False), dict(axis="yaw")):
            row = displacement_record()
            row.update(updates)
            self.assertIsNone(speed_measurement(row)["mean_velocity_m_s"])

    def telemetry(self):
        row = displacement_record()
        row.update(method="telemetry", telemetry_unit="m/s", telemetry_axis="forward_back",
                   common_clock_confirmed=True, axis_mapping_confirmed=True,
                   rc_start_s=1., rc_end_s=4., samples=[
                       dict(time_s=1., velocity_m_s=.1),
                       dict(time_s=2., velocity_m_s=.3),
                       dict(time_s=4., velocity_m_s=.5)])
        return row

    def test_telemetry_uses_time_weighted_average(self):
        result = speed_measurement(self.telemetry())
        self.assertAlmostEqual(result["mean_velocity_m_s"], 1 / 3)
        self.assertEqual(result["duration_s"], 3.)

    def test_telemetry_requires_declared_units_axes_and_clock(self):
        for updates in (dict(telemetry_unit="unknown"), dict(telemetry_axis="up_down"),
                        dict(common_clock_confirmed=False), dict(axis_mapping_confirmed=False)):
            row = self.telemetry()
            row.update(updates)
            self.assertEqual(speed_measurement(row)["status"], "unavailable")

    def test_telemetry_outside_command_interval_or_nonincreasing_time_rejects(self):
        row = self.telemetry()
        row["rc_end_s"] = 3.
        self.assertEqual(speed_measurement(row)["status"], "unavailable")
        row = self.telemetry()
        row["samples"][1]["time_s"] = 1.
        self.assertEqual(speed_measurement(row)["status"], "unavailable")

    def test_blank_real_measurement_template_remains_uncalibrated(self):
        path = Path(__file__).parents[1] / "physical_templates/speed_measurements_blank.json"
        result = import_speed_records(json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(result["mapping_status"], "not_calibrated")
        self.assertEqual(result["recorded_count"], 0)
        self.assertFalse(result["linear_mapping_assumed"])


class ExistingDiaryAuditTests(unittest.TestCase):
    def test_existing_rc_and_image_logs_do_not_create_metres_or_velocity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flight.jsonl"
            rows = [dict(event="start", time=1.),
                    dict(event="observation", time=1.05, size_ratio=.2),
                    dict(event="motion_request", time=1.05, channels=[0, 30, 0, 0]),
                    dict(event="motion_request", time=1.15, channels=[0, 30, 0, 0]),
                    dict(event="tracking_halted", time=2.)]
            source = "\n".join(json.dumps(x) for x in rows)
            path.write_text(source, encoding="utf-8")
            result = audit_diary(tmp, path)
            self.assertEqual(result["speed_mapping_status"], "not_calibrated")
            self.assertEqual(result["distance_mapping_status"], "not_calibrated")
            self.assertIn("velocity_telemetry_fields", result["absent_measurement_groups"])
            self.assertEqual(result["caller_selected_flight_log"]["segments"][0]["rc_requests_per_s"], 2.)
            self.assertEqual(path.read_text(encoding="utf-8"), source)

    def test_nonexistent_empty_directory_and_external_flight_log_reject(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                audit_diary(Path(tmp) / "missing")
            with self.assertRaises(ValueError):
                audit_diary(tmp)
            (Path(tmp) / "x.jsonl").write_text('{"event":"start","time":1}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                audit_diary(tmp, Path(tmp).parent / "other.jsonl")

    def test_import_records_do_not_interpolate_a_speed_table(self):
        records = [displacement_record(), dict(displacement_record(), rc=40, signed_displacement_m=.7)]
        result = import_speed_records(dict(records=records))
        self.assertEqual(result["recorded_count"], 2)
        self.assertEqual(result["mapping_status"], "individual_measurements_only")
        self.assertFalse(result["linear_mapping_assumed"])
        self.assertEqual([r["rc"] for r in result["records"]], [20, 40])


if __name__ == "__main__":
    unittest.main()
