'Exercise djitellopy 2.5 command methods without creating sockets or devices.\n\nOnly its socket.sendto is substituted. This verifies final send cancellation,\nreply ordering and receipt timestamps, not a physical aircraft response.\n'
import importlib
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[2]
from SafetyFixture import wait_for
from SDKWireFixture import SDKWireFixture
from drone_sdk import _driver as drv, controller as ctl

class SDKProtocolTests(SDKWireFixture):

    def test01_actual_sdk_methods_send_and_accept_exact_ok(self):
        result = ctl.move_forward(self.grant, 20)
        self.assertTrue(result.ok, result)
        self.assertIn('forward 20', self.sent)

    def test02_not_ok_reply_is_not_success(self):
        self.reply = b'not ok'
        result = ctl.move_forward(self.grant, 20)
        self.assertFalse(result.ok)

    def test03_old_queued_reply_is_drained_before_send(self):
        self.responses.append(b'error from previous command')
        result = ctl.move_forward(self.grant, 20)
        self.assertTrue(result.ok, result)
        self.assertEqual(self.responses, [])

    def test04_cancellation_interrupts_real_sdk_reply_wait(self):
        self.auto_reply = False
        results = []
        worker = threading.Thread(target=lambda: results.append(ctl.move_forward(self.grant, 20)))
        worker.start()
        self.assertTrue(self.forward_sent.wait(1))
        started = time.monotonic()
        self.assertTrue(ctl.stop_motion(self.grant).ok)
        worker.join(0.5)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(results[0].ok)

    def test05_priority_land_preempts_vendor_response_wait(self):
        self.auto_reply = False
        results = []
        worker = threading.Thread(target=lambda: results.append(ctl.move_forward(self.grant, 20)))
        worker.start()
        self.assertTrue(self.forward_sent.wait(1))
        self.auto_reply = True
        self.assertTrue(ctl.priority_interrupt(land=True).ok)
        self.assertTrue(wait_for(lambda: 'land' in self.sent, 0.5))
        worker.join(0.5)
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
        self.auto_reply = False
        old_result = []
        worker = threading.Thread(target=lambda: old_result.append(ctl.move_forward(self.grant, 20)))
        worker.start()
        self.assertTrue(self.forward_sent.wait(1))
        stopped = ctl.stop_motion(self.grant)
        self.assertTrue(stopped.ok)
        worker.join(0.5)
        self.assertFalse(worker.is_alive())
        grant = ctl.request_control('model')
        self.assertFalse(grant.ok)
        self.assertIsNone(grant.grant)
        self.on_send = lambda data: self.responses.append(b'ok') if data == b'forward 30' else None
        result = ctl.move_forward(stopped.grant, 30)
        self.assertFalse(result.ok, 'Late acknowledgement of A was mistaken for successful B')
        self.assertNotIn('forward 30', self.sent)

    def test10_response_timeout_keeps_barrier_despite_ground_telemetry(self):
        self.auto_reply = False
        with self.assertRaisesRegex(RuntimeError, 'response_timeout_after_send'):
            self.tello.send_command_with_return('forward 20', timeout=0.02)
        self.assertTrue(drv.health()['command_channel_ambiguous'])
        self.packet.update(h=0, tof=0)
        drv.reconcile_state()
        self.assertFalse(drv.command_channel_clear())
        self.assertEqual(ctl.request_control('model').reason, 'command_channel_ambiguous')
        self.assertFalse(drv.set_velocity((0, 20, 0, 0), 'test'))
        self.assertTrue(drv.set_velocity((0, 0, 0, 0), 'safe stop'))
        self.assertTrue(ctl.get_status().command_channel_ambiguous)

    def test11_priority_land_remains_available_without_clearing_barrier(self):
        drv._mark_response_unknown('forward', 'test uncertain reply')
        self.assertTrue(ctl.priority_interrupt(land=True).ok)
        self.assertTrue(wait_for(lambda: 'land' in self.sent))
        self.assertTrue(wait_for(lambda: not drv.action_pending()))
        self.assertEqual(ctl.get_status().flight_state, 'landed')
        self.assertFalse(drv.command_channel_clear())
        self.assertTrue(drv.disconnect(safe=False))
        self.assertTrue(drv.command_channel_clear())
        self.assertFalse(drv.is_connected())

    def test12_keepalive_does_not_insert_zero_into_continuous_rc(self):
        self.sent.clear()
        stop = threading.Event()
        with patch.object(drv, 'KEEPALIVE_INTERVAL_S', 0.02):
            worker = threading.Thread(target=drv._keepalive_loop, args=(self.tello, stop))
            worker.start()
            try:
                for _ in range(10):
                    self.assertTrue(drv.set_velocity((0, 10, 0, 0), 'follow update'))
                    time.sleep(0.008)
            finally:
                stop.set()
                worker.join(1)
        self.assertIn('rc 0 10 0 0', self.sent)
        self.assertNotIn('rc 0 0 0 0', self.sent)

    def test13_keepalive_sends_zero_after_real_wire_idle(self):
        self.sent.clear()
        drv._last_wire_send = time.monotonic() - 1
        stop = threading.Event()
        with patch.object(drv, 'KEEPALIVE_INTERVAL_S', 0.01):
            worker = threading.Thread(target=drv._keepalive_loop, args=(self.tello, stop))
            worker.start()
            try:
                self.assertTrue(wait_for(lambda: 'rc 0 0 0 0' in self.sent))
            finally:
                stop.set()
                worker.join(1)
        self.assertLess(time.monotonic() - drv._last_wire_send, 0.2)

    def test14_ambiguous_takeoff_reply_cannot_confirm_ground_after_land(self):
        self.packet.update(h=0, tof=0)
        drv._flying = self.tello.is_flying = False
        ctl._flight_state = 'landed'
        ctl.configure_takeoff(authorized=True, min_battery=30)
        takeoff_sent = threading.Event()
        self.on_send = lambda data: takeoff_sent.set() if data == b'takeoff' else None
        self.auto_reply = False
        result = []
        worker = threading.Thread(target=lambda: result.append(ctl.takeoff(self.grant)))
        worker.start()
        self.assertTrue(takeoff_sent.wait(1))
        self.auto_reply = True
        self.assertTrue(ctl.priority_interrupt(land=True).ok)
        worker.join(0.5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(wait_for(lambda: not drv.action_pending()))
        self.assertTrue(drv.action_unknown('takeoff'))
        self.assertEqual(ctl.get_status().flight_state, 'landing')
        self.assertTrue(ctl.get_status().command_channel_ambiguous)

    def test_priority_zero_is_sent_even_inside_vendor_rc_rate_limit(self):
        self.sent.clear()
        self.tello.last_rc_control_timestamp = time.time() + 1
        self.assertTrue(drv.set_velocity((0, 0, 0, 0), 'priority_stop'))
        self.assertEqual(self.sent, ['rc 0 0 0 0'])

    def test_transient_send_faults_do_not_permanently_lock_out_the_next_action(self):

        def failed_send(data):
            if data.startswith(b'rc '):
                raise OSError('temporary send failure')
        self.on_send = failed_send
        for _ in range(3):
            self.assertFalse(drv.set_velocity((0, 0, 0, 0), 'temporary_fault'))
        self.assertFalse(drv.health()['link_ok'])
        self.assertTrue(drv.command_channel_clear())
        self.on_send = None
        result = ctl.move_forward(self.grant, 20)
        self.assertTrue(result.ok, result)
        self.assertTrue(drv.health()['link_ok'])
        self.assertIn('forward 20', self.sent)

    def test_bootstrap_timeout_does_not_poison_the_next_handshake_attempt(self):
        fixture = self
        attempts = []

        class Candidate(self.sdk_type):

            def __init__(self, host=None):
                self.address = ('not-a-device', 8889)
                self.last_received_command_timestamp = 0.0
                self.last_rc_control_timestamp = 0.0
                self.is_flying = self.stream_on = False
                self.background_frame_read = None

            def get_own_udp_object(self):
                return {'responses': fixture.responses, 'state': fixture.packet}

            def connect(self):
                attempts.append(self)
                self.send_control_command('command', timeout=0.02)

            def end(self):
                pass
        self.auto_reply = False

        def handshake(data):
            if data == b'command' and len(attempts) == 2:
                self.responses.append(b'ok')
        self.on_send = handshake
        with patch.object(drv, '_tello', None), patch.object(drv, '_tello_class', return_value=Candidate), patch.object(drv, 'CONNECT_RETRY_DELAY_S', 0), patch.object(drv, 'set_speed'), patch.object(drv, '_keepalive_loop'):
            health = drv.connect()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.sent.count('command'), 2)
        self.assertTrue(health['connected'])
        self.assertFalse(health['command_channel_ambiguous'])


    def test_rotation_timeout_preserves_sent_unknown_result_through_cloud_projection(self):
        import asyncio
        from unittest.mock import AsyncMock
        from ContainerFixture import Process
        from ExecutionFixture import runner_for, run_program
        from Support import MockBackend
        from app.Results import tool_result
        import drone_sdk as sdk
        self.auto_reply = False
        runner = runner_for(sdk, MockBackend(), timeout=8)
        runner.container, runner.created = 'companion-fixture', True
        async def run():
            process = Process([
                {'type':'rpc','id':1,'method':'rotate','args':[360],'kwargs':{}},
                {'type':'done','ok':False,'error':'RPC failed'}])
            with patch.object(runner, '_remove_container', new=AsyncMock(return_value=[])), patch(
                    'execution.sandbox_runner.asyncio.create_subprocess_exec', return_value=process):
                return await run_program(runner, 'drone.rotate(360)')
        result = tool_result(asyncio.run(run()))
        detail = result['action_error']
        self.assertEqual(detail['status'], 'unknown')
        self.assertEqual(detail['reason'], 'response_timeout_after_send')
        self.assertEqual(detail['details']['action'], 'rotate')
        self.assertEqual(detail['details']['param'], 360)
        self.assertTrue(detail['details']['wire_sent'])
        self.assertTrue(detail['details']['outcome_unknown'])
        self.assertEqual(detail['details']['stage'], 'response_wait')
        self.assertGreaterEqual(detail['details']['elapsed_seconds'], 1)
        self.assertEqual(detail['details']['response_timeout_seconds'], 1)
        self.assertGreater(detail['details']['caller_deadline_seconds'], 1)
        self.assertEqual(result['journal'][0]['result'], detail)
        self.assertEqual(self.sent.count('cw 360'), 1)
        self.assertTrue(any(e['event']=='command_exception' and 'traceback' in e for e in drv.snapshot()['events']))

    def test_firmware_rejection_has_reply_and_is_definitive_failure(self):
        self.reply = b'error motor stop'
        result = ctl.rotate(self.grant, 45)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 'failed')
        self.assertIn('error motor stop', result.reason)
        self.assertEqual(result.details['reply'], 'error motor stop')
        self.assertTrue(result.details['wire_sent'])
        self.assertFalse(result.details['outcome_unknown'])

    def test_unsent_transport_error_is_distinct_from_reply_timeout(self):
        def fail(data):
            if data.startswith(b'cw '): raise OSError('local transport unavailable')
        self.on_send = fail
        result = ctl.rotate(self.grant, 45)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.details['error_type'], 'OSError')
        self.assertFalse(result.details['wire_sent'])
        self.assertFalse(result.details['outcome_unknown'])
        self.assertIn('local transport unavailable', result.reason)

if __name__ == '__main__':
    unittest.main(verbosity=2)
