"""Exercise djitellopy 2.5 command methods without creating sockets or devices.

Only its socket.sendto is substituted. This verifies final send cancellation,
reply ordering and receipt timestamps, not a physical aircraft response.
"""
import importlib
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT, ROOT / 'tests'):
    sys.path.insert(0, str(folder))
from SafetyTests import NoNetwork, wait_for
from drone_sdk import _driver as drv, controller as ctl


class SDKProtocolTests(NoNetwork):
    def setUp(self):
        super().setUp()
        import djitellopy
        self.vendor = importlib.import_module('djitellopy.tello')
        self.sdk_type = drv._tello_class(1)
        self.tello = object.__new__(self.sdk_type)
        self.tello.address = ('not-a-device', 8889)
        self.tello.last_received_command_timestamp = 0.
        self.tello.last_rc_control_timestamp = 0.
        self.tello.retry_count = 1
        self.tello.is_flying = True
        self.tello.stream_on = False
        self.tello.background_frame_read = None
        self.tello.end = lambda: None
        self.responses = []
        self.packet = djitellopy.Tello.parse_state('h:80;tof:80;bat:80;templ:35;temph:36;time:1;')
        self.tello.get_own_udp_object = lambda: {'responses': self.responses, 'state': self.packet}
        self.sent = []
        self.auto_reply = True
        self.reply = b'ok'
        self.on_send = None
        self.forward_sent = threading.Event()
        def sendto(data, address):
            self.sent.append(data.decode())
            if self.on_send is not None:
                self.on_send(data)
            if data.startswith(b'forward'):
                self.forward_sent.set()
            if data == b'land':
                self.packet = djitellopy.Tello.parse_state('h:0;tof:0;bat:80;templ:35;temph:36;time:1;')
            if self.auto_reply and not data.startswith(b'rc '):
                self.responses.append(self.reply)
            return len(data)
        self.enterContext(patch.object(self.vendor, 'client_socket', type('Wire', (), {'sendto': staticmethod(sendto)})(), create=True))
        ctl._current_owner = None
        ctl._flight_state = 'flying'
        ctl._active_action = ctl._landing_deadline = None
        ctl._last_velocity_time = 0
        drv.SIMULATED, drv._tello = False, self.tello
        drv._flying = True
        drv._link_ok = True
        drv._faults = 0
        drv._unknown_actions.clear()
        drv._unknown_since.clear()
        drv._response_quarantine = None
        drv._last_wire_send = 0.
        drv._pending_commands.clear()
        drv._events.clear()
        grant = ctl.request_control('model')
        self.grant = {'control_epoch': grant.control_epoch, 'command_epoch': grant.command_epoch}

    def tearDown(self):
        self.auto_reply = True
        drv.cancel_pending('test teardown')
        wait_for(lambda: not drv.action_pending())
        drv.SIMULATED, drv._tello = True, None
        drv._response_quarantine = None
        drv._unknown_actions.clear()
        drv._unknown_since.clear()
        ctl._current_owner = None
        ctl._flight_state = 'landed'
        ctl._active_action = None
        ctl._last_velocity_time = 0
        super().tearDown()

    def test01_actual_sdk_methods_send_and_accept_exact_ok(self):
        result = ctl.move_forward('model', 20, **self.grant)
        self.assertTrue(result.ok, result)
        self.assertIn('forward 20', self.sent)

    def test02_not_ok_reply_is_not_success(self):
        self.reply = b'not ok'
        result = ctl.move_forward('model', 20, **self.grant)
        self.assertFalse(result.ok)

    def test03_old_queued_reply_is_drained_before_send(self):
        self.responses.append(b'error from previous command')
        result = ctl.move_forward('model', 20, **self.grant)
        self.assertTrue(result.ok, result)
        self.assertEqual(self.responses, [])

    def test04_cancellation_interrupts_real_sdk_reply_wait(self):
        self.auto_reply = False
        results = []
        worker = threading.Thread(target=lambda: results.append(ctl.move_forward('model', 20, **self.grant)))
        worker.start()
        self.assertTrue(self.forward_sent.wait(1))
        started = time.monotonic()
        self.assertTrue(ctl.stop_motion('model', **self.grant).ok)
        worker.join(.5)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, .5)
        self.assertFalse(results[0].ok)

    def test05_priority_land_preempts_vendor_response_wait(self):
        self.auto_reply = False
        results = []
        worker = threading.Thread(target=lambda: results.append(ctl.move_forward('model', 20, **self.grant)))
        worker.start()
        self.assertTrue(self.forward_sent.wait(1))
        self.auto_reply = True
        self.assertTrue(ctl.land('model', priority=True).ok)
        self.assertTrue(wait_for(lambda: 'land' in self.sent, .5))
        worker.join(.5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(wait_for(lambda: not drv.action_pending()))
        self.assertEqual(ctl.get_status().flight_state, 'landed')

    def test06_polling_vendor_state_does_not_stamp_new_receipt(self):
        stamp = self.packet['_received_at']
        for _ in range(5):
            self.assertEqual(self.tello.get_current_state()['_received_at'], stamp)
        self.assertEqual(drv.health()['telemetry_timestamp'], stamp)
        self.assertTrue(drv.health()['telemetry_fresh'])

    def test07_rc_uses_final_send_gate(self):
        before = len(self.sent)
        drv._command_context.epoch = drv.capture_cancel_epoch()
        drv.cancel_pending('stop')
        try:
            with self.assertRaisesRegex(RuntimeError, 'cancelled_before_send'):
                self.tello.send_command_without_return('rc 0 20 0 0')
        finally:
            drv._command_context.epoch = None
        self.assertEqual(len(self.sent), before)

    def test08_stale_receipt_not_refreshed_by_health(self):
        self.packet['_received_monotonic'] -= 2
        first = drv.health()
        second = drv.health()
        self.assertFalse(first['telemetry_fresh'])
        self.assertFalse(second['telemetry_fresh'])
        self.assertIsNone(first['battery'])
        self.assertIsNone(first['temperature_c'])

    def test09_late_reply_cannot_approve_a_new_move(self):
        # A's reply is absent while A is cancelled. If B is sent, the injected
        # late A 'ok' arrives during B's wait, with no B acknowledgement at all.
        self.auto_reply = False
        old_result = []
        worker = threading.Thread(target=lambda: old_result.append(ctl.move_forward('model',20,**self.grant)))
        worker.start()
        self.assertTrue(self.forward_sent.wait(1))
        self.assertTrue(ctl.stop_motion('model',**self.grant).ok)
        worker.join(.5)
        self.assertFalse(worker.is_alive())
        grant = ctl.request_control('model')
        self.on_send = lambda data: self.responses.append(b'ok') if data == b'forward 30' else None
        result = ctl.move_forward('model',30,control_epoch=grant.control_epoch,command_epoch=grant.command_epoch)
        self.assertFalse(result.ok,'Late acknowledgement of A was mistaken for successful B')
        self.assertNotIn('forward 30',self.sent)

    def test10_response_timeout_keeps_barrier_despite_ground_telemetry(self):
        self.auto_reply = False
        with self.assertRaisesRegex(RuntimeError,'response_timeout_after_send'):
            self.tello.send_command_with_return('forward 20', timeout=.02)
        self.assertTrue(drv.health()['command_channel_ambiguous'])
        self.packet.update(h=0,tof=0)
        drv.reconcile_state()
        self.assertFalse(drv.command_channel_clear())
        self.assertEqual(ctl.request_control('model').reason,'command_channel_ambiguous')
        self.assertFalse(drv.set_velocity((0,20,0,0),'test'))
        self.assertTrue(drv.set_velocity((0,0,0,0),'safe stop'))
        self.assertTrue(ctl.get_status().command_channel_ambiguous)

    def test11_priority_land_remains_available_without_clearing_barrier(self):
        drv._mark_response_unknown('forward','test uncertain reply')
        self.assertTrue(ctl.land('model',priority=True).ok)
        self.assertTrue(wait_for(lambda:'land' in self.sent))
        self.assertTrue(wait_for(lambda:not drv.action_pending()))
        self.assertEqual(ctl.get_status().flight_state,'landed')
        self.assertFalse(drv.command_channel_clear())
        self.assertTrue(drv.disconnect(safe=False))
        self.assertTrue(drv.command_channel_clear())
        self.assertFalse(drv.is_connected())

    def test12_keepalive_does_not_insert_zero_into_continuous_rc(self):
        self.sent.clear()
        stop = threading.Event()
        with patch.object(drv,'KEEPALIVE_INTERVAL_S',.02):
            worker = threading.Thread(target=drv._keepalive_loop,args=(self.tello,stop))
            worker.start()
            try:
                for _ in range(10):
                    self.assertTrue(drv.set_velocity((0,10,0,0),'follow update'))
                    time.sleep(.008)
            finally:
                stop.set(); worker.join(1)
        self.assertIn('rc 0 10 0 0',self.sent)
        self.assertNotIn('rc 0 0 0 0',self.sent)

    def test13_keepalive_sends_zero_after_real_wire_idle(self):
        self.sent.clear()
        drv._last_wire_send = time.monotonic()-1
        stop = threading.Event()
        with patch.object(drv,'KEEPALIVE_INTERVAL_S',.01):
            worker = threading.Thread(target=drv._keepalive_loop,args=(self.tello,stop))
            worker.start()
            try:
                self.assertTrue(wait_for(lambda:'rc 0 0 0 0' in self.sent))
            finally:
                stop.set(); worker.join(1)
        self.assertLess(time.monotonic()-drv._last_wire_send,.2)

    def test14_ambiguous_takeoff_reply_cannot_confirm_ground_after_land(self):
        self.packet.update(h=0,tof=0)
        drv._flying = self.tello.is_flying = False
        ctl._flight_state = 'landed'
        ctl.configure_takeoff(authorized=True,min_battery=30)
        takeoff_sent = threading.Event()
        self.on_send = lambda data: takeoff_sent.set() if data==b'takeoff' else None
        self.auto_reply = False
        result = []
        worker = threading.Thread(target=lambda:result.append(ctl.takeoff('model',**self.grant)))
        worker.start()
        self.assertTrue(takeoff_sent.wait(1))
        self.auto_reply = True
        self.assertTrue(ctl.land('model',priority=True).ok)
        worker.join(.5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(wait_for(lambda:not drv.action_pending()))
        self.assertTrue(drv.action_unknown('takeoff'))
        self.assertEqual(ctl.get_status().flight_state,'landing')
        self.assertTrue(ctl.get_status().command_channel_ambiguous)


if __name__ == '__main__':
    unittest.main(verbosity=2)
