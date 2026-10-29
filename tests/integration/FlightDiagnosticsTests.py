"""Passive descent evidence, replayed without a cloud or aircraft connection."""
import unittest
from unittest.mock import patch
from app.FlightDiagnostics import FlightDiagnostics
from drone_sdk import _driver as driver


class FlightDiagnosticsTests(unittest.TestCase):
    def sample(self, t, height, **extra):
        return {'event':'telemetry_received', 'monotonic':t,
                'state':{'h':height, 'tof':height, 'bat':22, 'time':26, **extra}}

    def test_latest_descent_shape_is_marked_without_inventing_a_cause(self):
        rows = []
        recorder = FlightDiagnostics(rows.append)
        recorder.observe({'event':'wire_sent', 'monotonic':99, 'command':'rc 0 0 0 0'})
        recorder.observe(self.sample(100, 80, roll=0, agz=-1000))
        recorder.observe(self.sample(101.9, 10, roll=30, agz=-1400))
        recorder.observe(self.sample(102, 0))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['drop_cm'], 70)
        self.assertEqual(rows[0]['cause'], 'undetermined')
        self.assertEqual(rows[0]['after']['state']['roll'], 30)
        self.assertEqual(rows[0]['recent_wire_commands'][0]['command'], 'rc 0 0 0 0')

    def test_slow_change_and_stale_history_do_not_mark_sudden_descent(self):
        rows = []
        recorder = FlightDiagnostics(rows.append)
        for t, h in [(100,80), (101,75), (102,70), (110,0)]:
            recorder.observe(self.sample(t,h))
        self.assertEqual(rows, [])

    def test_flush_is_incremental_and_reports_overwritten_evidence(self):
        rows = []
        recorder = FlightDiagnostics(rows.append)
        event = dict(self.sample(100,80), sequence=recorder.cursor+4)
        with patch.object(driver, 'events_since', return_value=([event],event['sequence'],3)):
            recorder.flush()
        self.assertEqual(rows[0]['event'], 'flight_diagnostic_gap')
        self.assertEqual(rows[0]['missing_events'], 3)
        self.assertEqual(rows[1]['record'], event)
        with patch.object(driver, 'events_since', return_value=([],event['sequence'],0)):
            recorder.flush()
        self.assertEqual(len(rows), 2)

    def test_logging_failure_is_observable_but_does_not_escape_into_control(self):
        recorder = FlightDiagnostics(lambda _: (_ for _ in ()).throw(OSError('disk')))
        event = dict(self.sample(100,80), sequence=recorder.cursor+1)
        with patch.object(driver, 'events_since', return_value=([event],event['sequence'],0)):
            recorder.flush()
        self.assertIn('OSError', recorder.error)
