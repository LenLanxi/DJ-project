'Current behavioral acceptance migrated from Scenario/Docs/review matrix.\n\nMappings: old T11-19 and T20-24 -> DeviceAcceptance; old FlightSafety and\nFlightBringUp -> DeviceAcceptance; old Vision25-29 -> VisualAcceptance.\nOld source-string assertions and removed Agent/Session admission-only tests are\nnot presented as runtime evidence. SafetyTests covers cancellation G1-G4;\nRuntimeTests covers new entry lifecycle and real follow-service handoffs.\n'
import asyncio
import threading
import time
import unittest
from unittest.mock import patch
import numpy as np
from SafetyFixture import DeviceCase, FakeTello, NoNetwork, wait_for
from drone_sdk import controller as ctl, _driver as drv

class DeviceAcceptance(DeviceCase):

    def grant(self):
        result = ctl.request_control('model')
        self.assertTrue(result.ok, result)
        return result.grant

    def test01_shared_status_uses_real_battery_height_and_mode(self):
        self.real()
        self.fake.packet['bat'] = 57
        state = ctl.get_status()
        self.assertEqual(state.battery, 57)
        self.assertEqual(state.height_cm, 80)
        self.assertFalse(state.simulated)
        self.assertTrue(state.connected)

    def test02_unknown_battery_blocks_takeoff_through_backend(self):
        self.real(flying=False)
        self.fake.packet['bat'] = None
        grant = self.grant()
        self.assertFalse(ctl.takeoff(grant).ok)
        self.assertNotIn(('takeoff',), self.fake.calls)

    def test03_low_battery_blocks_takeoff_through_backend(self):
        self.real(flying=False)
        self.fake.packet['bat'] = 10
        grant = self.grant()
        self.assertFalse(ctl.takeoff(grant).ok)
        self.assertNotIn(('takeoff',), self.fake.calls)

    def test04_takeoff_requires_explicit_authorization(self):
        grant = self.real(flying=False)
        ctl.configure_takeoff(authorized=False)
        self.assertFalse(ctl.takeoff(grant).ok)
        self.assertNotIn(('takeoff',), self.fake.calls)

    def test_zero_threshold_allows_known_low_battery_but_not_missing_battery(self):
        grant = self.real(flying=False)
        ctl.configure_takeoff(authorized=True, min_battery=0)
        self.fake.packet['bat'] = None
        self.assertFalse(ctl.takeoff(grant).ok)
        self.fake.packet['bat'] = 10
        self.assertTrue(ctl.takeoff(grant).ok)
        self.assertIn(('takeoff',), self.fake.calls)

    def test_zero_battery_threshold_does_not_disable_temperature_gate(self):
        grant = self.real(flying=False)
        ctl.configure_takeoff(authorized=True, min_battery=0)
        self.fake.packet.update(bat=10, templ=96, temph=97)
        self.assertFalse(ctl.takeoff(grant).ok)
        self.assertNotIn(('takeoff',), self.fake.calls)

    def test05_landing_stays_pending_until_ack_and_ground_packet(self):
        self.real()
        entered, release = (threading.Event(), threading.Event())
        original = self.fake.land

        def delayed():
            entered.set()
            release.wait(1)
            original()
        self.fake.land = delayed
        self.assertTrue(ctl.priority_interrupt(land=True).ok)
        self.assertTrue(entered.wait(1))
        ctl._landing_deadline = time.monotonic() - 10
        self.assertEqual(ctl.get_status().flight_state, 'landing')
        self.assertTrue(self.fake.is_flying)
        release.set()
        self.assertTrue(wait_for(lambda: not drv.action_pending()))
        self.assertEqual(ctl.get_status().flight_state, 'landed')

    def test06_timed_out_move_keeps_control_barrier(self):
        grant = self.real()
        entered, release = (threading.Event(), threading.Event())

        def delayed(_cm):
            entered.set()
            release.wait(1)
        self.fake.move_forward = delayed
        try:
            with patch.object(drv, '_move_deadline', return_value=0.02):
                result = ctl.move_forward(grant, 20)
            self.assertTrue(entered.is_set())
            self.assertFalse(result.ok)
            self.assertEqual(ctl.request_control('model').reason, 'action_result_pending')
            self.assertFalse(ctl.release_control(grant).ok)
        finally:
            release.set()
        self.assertTrue(wait_for(lambda: not drv.action_pending()))

    def test07_landing_retries_busy_dispatch_lock(self):
        self.real()
        drv._command_lock.acquire()
        try:
            with patch.object(drv, 'LAND_DISPATCH_TIMEOUT_S', 0.01):
                drv._dispatch_land(self.fake)
                self.assertTrue(wait_for(lambda: any((e['event'] == 'land_deferred' for e in drv.snapshot()['events']))))
        finally:
            drv._command_lock.release()
        self.assertTrue(wait_for(lambda: ('land',) in self.fake.calls))

    def decoder(self, age=0, number=7):
        decoder = drv._VideoDecoder('offline-only')
        decoder._frame = np.ones((2, 3, 3), dtype=np.uint8)
        decoder.count = number
        decoder.last_frame_at = time.monotonic() - age
        decoder.last_frame_timestamp = time.time() - age
        drv._video = decoder
        return decoder

    def test08_repeated_frame_publication_preserves_capture_identity(self):
        decoder = self.decoder()
        first = ctl.get_frame()
        second = ctl.get_frame()
        self.assertEqual((first.frame_id, first.timestamp), (second.frame_id, second.timestamp))
        self.assertEqual(first.frame_id, decoder.count)

    def test09_expired_decoder_frame_cannot_be_current(self):
        self.decoder(age=10)
        self.assertIsNone(drv.get_frame().frame)
        self.assertTrue(ctl.get_frame().stale)

    def test10_no_decoder_has_no_synthetic_capture_metadata(self):
        drv._video = None
        packet = ctl.get_frame()
        self.assertTrue(packet.stale)
        self.assertIsNone(packet.frame)
        self.assertEqual((packet.frame_id, packet.timestamp), (0, 0.0))

    def test11_capture_pixels_and_metadata_come_from_one_source(self):
        decoder = self.decoder()
        packet = ctl.get_frame()
        self.assertEqual((packet.frame_id, packet.timestamp), (7, decoder.last_frame_timestamp))
        packet.frame[:] = 255
        self.assertTrue((decoder._frame == 1).all())

    def test12_rc_send_failure_is_reported_and_logged(self):
        grant = self.real()
        self.fake.fail_rc = True
        result = ctl.update_velocity(grant, 0, 20, 0, 0)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'velocity_send_failed')
        self.assertTrue(any((e['event'] == 'link_fault' for e in drv.snapshot()['events'])))

    def test14_keepalive_uses_no_reply_rc_zero(self):
        self.real()
        self.fake.calls.clear()
        stop = threading.Event()
        with patch.object(drv, 'KEEPALIVE_INTERVAL_S', 0.01):
            worker = threading.Thread(target=drv._keepalive_loop, args=(self.fake, stop))
            worker.start()
            self.assertTrue(wait_for(lambda: bool(self.fake.calls)))
            stop.set()
            worker.join(1)
        self.assertTrue(all((call == ('rc', (0, 0, 0, 0)) for call in self.fake.calls)))

    def test15_keepalive_skips_pending_action(self):
        self.real()
        self.fake.calls.clear()
        pending = threading.Event()
        drv._pending_commands.add(pending)
        stop = threading.Event()
        try:
            with patch.object(drv, 'KEEPALIVE_INTERVAL_S', 0.01):
                worker = threading.Thread(target=drv._keepalive_loop, args=(self.fake, stop))
                worker.start()
                time.sleep(0.03)
                stop.set()
                worker.join(1)
            self.assertEqual(self.fake.calls, [])
        finally:
            drv._pending_commands.discard(pending)

    def test16_tof_cross_checks_zero_baro_and_limits_height(self):
        self.real()
        self.fake.packet.update(h=0, tof=120)
        self.assertEqual(drv._height_probe(), (120, 'tof'))
        self.assertEqual(drv._plan_ascent(200), 130)
        self.fake.packet.update(tof=250)
        self.assertEqual(drv._sanitise_rc((0, 0, 60, 0))[2], 0)

    def test17_baro_is_preferred_when_positive(self):
        self.real()
        self.fake.packet.update(h=95, tof=100)
        self.assertEqual(drv._height_probe(), (95, 'baro'))

    def test18_ground_requires_actual_ground_measurement(self):
        self.real(flying=False)
        self.assertEqual(drv._height_probe(), (0, 'ground_telemetry'))
        self.fake.packet.update(h=None, tof=None)
        self.assertEqual(drv._height_probe(), (None, 'unknown'))

    def test19_connect_retries_transient_decode_failure(self):
        attempts = []

        class Flaky(FakeTello):
            address = ('fake', 8889)

            def __init__(self, host=None):
                super().__init__(False)

            def connect(self):
                attempts.append(1)
                if len(attempts) < 3:
                    raise UnicodeDecodeError('utf-8', b'\xcc', 0, 1, 'test')

            def set_speed(self, _value):
                pass

            def end(self):
                pass
        with patch.object(drv, '_tello_class', return_value=Flaky), patch.object(drv, 'CONNECT_RETRY_DELAY_S', 0):
            try:
                info = drv.connect()
            finally:
                if drv._keepalive_stop:
                    drv._keepalive_stop.set()
                drv.disconnect(safe=False)
        self.assertEqual(len(attempts), 3)
        self.assertTrue(info['connected'])
        self.assertTrue(info['telemetry_fresh'])
        self.assertEqual(len([e for e in drv.snapshot()['events'] if e['event'] == 'connect_retry']), 2)

    def test20_connect_failure_reports_not_ready(self):

        class Dead:

            def __init__(self, host=None):
                pass

            def connect(self):
                raise UnicodeDecodeError('utf-8', b'\xcc', 0, 1, 'test')

            def end(self):
                pass
        with patch.object(drv, '_tello_class', return_value=Dead), patch.object(drv, 'CONNECT_RETRY_DELAY_S', 0):
            with self.assertRaisesRegex(RuntimeError, 'tello_not_ready'):
                drv.connect()
        self.assertIsNone(drv._tello)

    def test_failed_connect_destructor_cannot_remove_replacement_host(self):
        hosts = {}
        attempts = []

        class SharedHost(FakeTello):
            address = ('fake', 8889)

            def __init__(self, host=None):
                super().__init__(False)
                hosts['fake'] = True

            def connect(self):
                attempts.append(1)
                if len(attempts) == 1:
                    raise UnicodeDecodeError('utf-8', b'\xcc', 0, 1, 'test')
                assert hosts['fake']

            def set_speed(self, value):
                pass

            def end(self):
                hosts.pop('fake', None)

            def __del__(self):
                self.end()
        with patch.object(drv, '_tello_class', return_value=SharedHost), patch.object(drv, 'CONNECT_RETRY_DELAY_S', 0):
            try:
                self.assertTrue(drv.connect()['connected'])
            finally:
                if drv._keepalive_stop:
                    drv._keepalive_stop.set()
                drv.disconnect(safe=False)
        self.assertEqual(len(attempts), 2)

    def test21_explicit_takeoff_rejection_has_diagnostic(self):
        self.real(flying=False)
        self.fake.takeoff = lambda: (_ for _ in ()).throw(RuntimeError('flight controller error'))
        self.assertFalse(drv.execute('takeoff'))
        events = [e for e in drv.snapshot()['events'] if e['event'] == 'takeoff_rejected']
        self.assertTrue(events)
        self.assertEqual(events[-1]['battery'], 80)
        self.assertEqual(events[-1]['advice'], 'flight_controller_rejected')
        self.assertIn('takeoff_rejected', drv.health()['last_error'])
