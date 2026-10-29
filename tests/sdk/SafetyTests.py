'Behavioral safety regression tests; no aircraft, cameras or external network.\n\nRun: uv run pytest tests/sdk/SafetyTests.py\nA fake device exercises real controller/driver code. Wire checks inject a send\ncallback instead of UDP; they do not prove physical braking or flight dynamics.\n'
from SafetyFixture import FakeTello, NoNetwork, DeviceCase, wait_for
import asyncio
import builtins
import socket
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[2]
from drone_sdk import controller as ctl, _driver as drv
from drone_sdk.contract import FrameResult
from drone_sdk.emergency_hotkeys import ensure_pynput

class ControllerSafety(DeviceCase):

    def test01_normal_stop_drops_queued_move(self):
        self.queued()

    def test02_priority_stop_drops_queued_move(self):
        self.queued(priority=True)

    def test03_priority_land_drops_queued_move(self):
        self.queued(land=True)

    def test04_stop_before_driver_binding_drops_move(self):
        grant = self.real()
        entered, release = (threading.Event(), threading.Event())
        original = drv.execute

        def delayed(*args, **kwargs):
            entered.set()
            release.wait(1)
            return original(*args, **kwargs)
        result = []
        with patch.object(drv, 'execute', delayed):
            worker = threading.Thread(target=lambda: result.append(ctl.move_forward(grant, 20)))
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertTrue(ctl.stop_motion(grant).ok)
            release.set()
            worker.join(1)
        self.assertNotIn(('forward', 20), self.fake.calls)
        self.assertFalse(result[0].ok)

    def test05_wire_rechecks_after_vendor_delay(self):
        bound = drv.capture_cancel_epoch()
        sent = []
        drv.cancel_pending('test stop')
        drv._command_context.epoch = bound
        try:
            with self.assertRaisesRegex(RuntimeError, 'cancelled_before_send'):
                drv._wire_send('forward 20', sent.append)
        finally:
            drv._command_context.epoch = None
        self.assertEqual(sent, [])

    def test06_cancel_return_orders_after_in_progress_wire_write(self):
        entered, release, cancelled = (threading.Event(), threading.Event(), threading.Event())
        writes = []
        bound = drv.capture_cancel_epoch()

        def send(_value):
            entered.set()
            release.wait(1)
            writes.append('sent')

        def work():
            drv._command_context.epoch = bound
            try:
                drv._wire_send('forward 20', send)
            finally:
                drv._command_context.epoch = None
        worker = threading.Thread(target=work)
        worker.start()
        self.assertTrue(entered.wait(1))
        stop = threading.Thread(target=lambda: (drv.cancel_pending('stop'), cancelled.set()))
        stop.start()
        self.assertFalse(cancelled.wait(0.02))
        release.set()
        worker.join(1)
        stop.join(1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(writes, ['sent'])

    def test07_new_same_owner_lease_rejects_old_cleanup(self):
        old = self.real()
        new = ctl.request_control('model')
        before = len(self.fake.calls)
        self.assertGreater(new.grant.control_epoch, old.control_epoch)
        self.assertFalse(ctl.stop_motion(old).ok)
        self.assertFalse(ctl.release_control(old).ok)
        self.assertEqual(len(self.fake.calls), before)

    def test08_backend_context_does_not_borrow_priority_grant(self):
        old = self.real()
        self.assertTrue(ctl.priority_interrupt().ok)
        before = len(self.fake.calls)
        self.assertFalse(ctl.move_forward(old, 20).ok)
        self.assertFalse(ctl.release_control(old).ok)
        self.assertEqual(len(self.fake.calls), before)

    def test09_telemetry_poll_does_not_refresh_receipt(self):
        self.real()
        self.fake.refresh(age=2)
        stamp = self.fake.packet['_received_at']
        a, b = (drv.health(), drv.health())
        self.assertFalse(a['telemetry_fresh'])
        self.assertFalse(b['telemetry_fresh'])
        self.assertEqual(a['telemetry_timestamp'], stamp)
        self.assertIsNone(a['height_cm'])
        self.assertFalse(ctl.get_status().connected)

    def test10_zero_baro_uses_tof_even_during_unknown_takeoff(self):
        self.real(flying=False)
        self.fake.packet.update(h=0, tof=80)
        height, source = drv._height_probe()
        self.assertEqual((height, source), (80, 'tof'))
        drv._unknown_actions.add('takeoff')
        drv._unknown_since['takeoff'] = (time.monotonic() - 10, object())
        self.assertFalse(drv.reconcile_state())
        self.assertTrue(drv.action_unknown('takeoff'))

    def test11_late_takeoff_ack_corrects_unknown_state(self):
        grant = self.real(flying=False)
        self.fake.takeoff_release.clear()
        with patch.dict(drv.COMMAND_DEADLINE_S, takeoff=0.02):
            result = ctl.takeoff(grant)
        self.assertEqual(result.status, 'unknown')
        self.fake.takeoff_release.set()
        self.assertTrue(wait_for(lambda: not drv.action_pending()))
        self.assertTrue(self.fake.is_flying)
        self.assertEqual(ctl.get_status().flight_state, 'flying')

    def test12_missing_telemetry_cannot_confirm_unknown_takeoff_landed(self):
        self.real(flying=False)
        self.fake.packet.update(h=None, tof=None)
        drv._unknown_actions.add('takeoff')
        drv._unknown_since['takeoff'] = (time.monotonic() - 10, object())
        ctl._flight_state = 'unknown'
        self.assertFalse(drv.reconcile_state())
        self.assertEqual(ctl.get_status().flight_state, 'unknown')

    def test13_takeoff_cancelled_before_send_is_not_unknown(self):
        grant = self.real(flying=False)
        drv._command_lock.acquire()
        results = []
        worker = threading.Thread(target=lambda: results.append(ctl.takeoff(grant)))
        worker.start()
        try:
            self.assertTrue(wait_for(lambda: drv.action_pending()))
            ctl.stop_motion(grant)
        finally:
            drv._command_lock.release()
        worker.join(1)
        self.assertNotIn(('takeoff',), self.fake.calls)
        self.assertFalse(drv.action_unknown('takeoff'))
        self.assertEqual(ctl.get_status().flight_state, 'landed')

    def test14_failed_land_is_not_confirmed_landed(self):
        self.real()
        self.fake.fail_land = True
        self.assertTrue(ctl.priority_interrupt(land=True).ok)
        self.assertTrue(wait_for(lambda: not drv.action_pending()))
        self.assertEqual(ctl.get_status().flight_state, 'landing')
        self.assertFalse(drv.health()['landing_acknowledged'])

    def test15_failed_zero_keeps_watchdog_retry(self):
        grant = self.real()
        self.fake.fail_rc = True
        self.assertFalse(ctl.stop_motion(grant).ok)
        self.assertGreater(ctl._last_velocity_time, 0)
        before = len(self.fake.calls)
        ctl._last_velocity_time -= 1
        self.fake.fail_rc = False
        ctl._watchdog_tick()
        self.assertGreater(len(self.fake.calls), before)
        self.assertEqual(ctl._last_velocity_time, 0)

    def test_failed_zero_can_retry_and_release_its_updated_grant(self):
        grant = self.real()
        self.fake.fail_rc = True
        failed = ctl.stop_motion(grant)
        self.assertFalse(failed.ok)
        self.fake.fail_rc = False
        self.assertFalse(ctl.release_control(grant).ok)
        retried = ctl.stop_motion(failed.grant)
        self.assertTrue(retried.ok)
        self.assertTrue(ctl.release_control(retried.grant).ok)
        self.assertEqual(ctl.get_status().control_owner, 'none')

    def test16_stale_telemetry_rejects_nonzero_velocity(self):
        grant = self.real()
        self.fake.refresh(age=2)
        result = ctl.update_velocity(grant, 0, 20, 0, 0)
        self.assertEqual(result.reason, 'telemetry_stale')
        self.assertNotIn(('rc', (0, 20, 0, 0)), self.fake.calls)

    def test17_unknown_height_blocks_ascent(self):
        grant = self.real()
        self.fake.packet.update(h=0, tof=None)
        self.assertFalse(ctl.move_up(grant, 20).ok)
        self.assertNotIn(('up', 20), self.fake.calls)

    def test18_height_ceiling_clamps_ascent(self):
        grant = self.real()
        self.fake.refresh(height=220)
        self.assertTrue(ctl.move_up(grant, 80).ok)
        self.assertIn(('up', 30), self.fake.calls)

    def test19_temperature_gate_rejects_hot_takeoff(self):
        grant = self.real(flying=False)
        self.fake.refresh(temperature=95)
        self.assertFalse(ctl.takeoff(grant).ok)
        self.assertNotIn(('takeoff',), self.fake.calls)

    def test40_land_on_confirmed_ground_is_idempotent(self):
        self.real(flying=False)
        self.fake.fail_land = True
        result = ctl.priority_interrupt(land=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, 'completed')
        self.assertNotIn(('land',), self.fake.calls)
        self.assertEqual(ctl.get_status().flight_state, 'landed')
        self.assertTrue(ctl.request_control('model').ok)

    def test41_ground_land_shortcut_never_skips_pending_takeoff(self):
        self.real(flying=False)
        pending = threading.Event()
        drv._pending_commands.add(pending)
        try:
            result = ctl.priority_interrupt(land=True)
            self.assertTrue(result.ok)
            self.assertNotEqual(result.reason, 'already_landed')
            self.assertTrue(wait_for(lambda: ('land',) in self.fake.calls))
        finally:
            drv._pending_commands.discard(pending)
