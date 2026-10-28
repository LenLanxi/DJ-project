"""Real SDK/driver final writes with substituted UDP only; no aircraft/network.

The tests establish local ordering and refusal, not physical braking or landing.
"""
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
sys.path.insert(0,str(ROOT/'modules'/'ai'))
import SDKProtocolTests as protocol
from drone_sdk import controller as ctl, _driver as drv
from SafetyTests import wait_for
from gateway import NavigationGuard


class DeadlineGuard:
    """The actual host guard, with deterministic wall/monotonic clocks."""
    def __init__(self):
        self.lock=threading.Lock()
        self.clock=0.
        self.cancelled=threading.Event()
        self.guard=NavigationGuard(self.cancelled,1000.,clock=lambda:self.clock,
                                   wall_clock=lambda:1000.+self.clock)
    def is_set(self):
        with self.lock: return self.guard.is_set()
    def advance(self,value):
        with self.lock: self.clock=value


class WireCase(unittest.TestCase):
    def setUp(self):
        # Reuse only the protocol fixture, without inheriting/re-running its tests.
        self.wire=protocol.SDKProtocolTests('test01_actual_sdk_methods_send_and_accept_exact_ok')
        self.wire.setUp()
        self.addCleanup(self.wire.doCleanups)
        self.addCleanup(self.wire.tearDown)
    def reasons(self):
        return [e.get('reason') for e in drv.snapshot()['events'] if e['event']=='commands_cancelled']


class LandingPriorityTests(WireCase):
    def delayed_priority(self,action):
        entered=threading.Event()
        result=[]
        def late():
            entered.set()
            result.append(action())
        drv._command_lock.acquire()  # Hold the genuine land worker before final send.
        try:
            with ctl._velocity_lock:
                worker=threading.Thread(target=late)
                worker.start()
                self.assertTrue(entered.wait(1))
                self.assertTrue(ctl.land('model',priority=True).ok)
                epoch=drv.capture_cancel_epoch()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertTrue(result[0].ok)
            self.assertEqual(result[0].reason,'landing_preserved')
            self.assertEqual(drv.capture_cancel_epoch(),epoch)
            self.assertEqual(ctl.get_status().flight_state,'landing')
            self.assertNotIn('stop_motion',self.reasons())
            self.assertNotIn('priority_takeover',self.reasons())
        finally:
            drv._command_lock.release()
        self.assertTrue(wait_for(lambda:'land' in self.wire.sent))
        self.assertTrue(wait_for(lambda:not drv.action_pending()))
        self.assertEqual(ctl.get_status().flight_state,'landed')

    def test01_late_priority_stop_does_not_cancel_or_replace_a_landing_dispatch(self):
        self.delayed_priority(lambda:ctl.stop_motion('model',priority=True))

    def test02_late_priority_control_grant_preserves_the_landing_worker(self):
        self.delayed_priority(lambda:ctl.request_control('follow',priority=True))

    def test03_failed_land_is_retried_once_while_parallel_takeovers_remain_pending(self):
        self.wire.reply=b'error'
        self.assertTrue(ctl.land('model',priority=True).ok)
        self.assertTrue(wait_for(lambda:not drv.action_pending()))
        self.assertEqual(ctl.get_status().flight_state,'landing')
        before=self.wire.sent.count('land')
        self.wire.reply=b'ok'
        drv._command_lock.acquire()
        results=[]
        try:
            workers=[threading.Thread(target=lambda:results.append(ctl.stop_motion('model',priority=True)))
                     for _ in range(4)]
            for worker in workers: worker.start()
            for worker in workers: worker.join(1); self.assertFalse(worker.is_alive())
            self.assertTrue(all(r.ok and r.reason=='landing_preserved' for r in results))
            self.assertEqual(self.wire.sent.count('land'),before)
            self.assertNotIn('stop_motion',self.reasons())
        finally: drv._command_lock.release()
        self.assertTrue(wait_for(lambda:not drv.action_pending()))
        self.assertEqual(self.wire.sent.count('land'),before+1)
        self.assertEqual(ctl.get_status().flight_state,'landed')

    def test04_stale_ground_telemetry_cannot_turn_preserved_landing_into_hover_or_completed(self):
        self.assertTrue(ctl.land('model',priority=True).ok)
        self.assertTrue(wait_for(lambda:not drv.action_pending()))
        self.wire.packet['_received_monotonic']-=2
        epoch=drv.capture_cancel_epoch()
        self.assertEqual(ctl.get_status().flight_state,'landing')
        for action in (lambda:ctl.stop_motion('model',priority=True),
                       lambda:ctl.request_control('model',priority=True)):
            result=action()
            self.assertTrue(result.ok)
            self.assertEqual(result.reason,'landing_preserved')
        self.assertEqual(drv.capture_cancel_epoch(),epoch)
        self.assertEqual(ctl.get_status().flight_state,'landing')

    def test05_unknown_takeoff_stays_unconfirmed_after_landing_and_priority_stop(self):
        drv._mark_response_unknown('takeoff','cancelled_after_send')
        self.assertTrue(ctl.land('model',priority=True).ok)
        self.assertTrue(wait_for(lambda:not drv.action_pending()))
        self.assertTrue(ctl.stop_motion('model',priority=True).ok)
        self.assertEqual(ctl.get_status().flight_state,'landing')
        self.assertNotIn('stop_motion',self.reasons())


class NavigationFinalSendTests(WireCase):
    def move(self,guard,result):
        with ctl.operation_guard(guard):
            result.append(ctl.move_forward('model',20,**self.wire.grant))

    def test06_move_queued_past_fixed_observation_deadline_never_reaches_udp(self):
        guard=DeadlineGuard()
        result=[]
        drv._command_lock.acquire()
        worker=threading.Thread(target=self.move,args=(guard,result))
        try:
            worker.start()
            self.assertTrue(wait_for(drv.action_pending))
            guard.advance(1.1)
        finally: drv._command_lock.release()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertFalse(result[0].ok)
        self.assertNotIn('forward 20',self.wire.sent)
        self.assertTrue(drv.health()['link_ok'])
        self.assertTrue(any(e.get('reason')=='operation_cancelled_before_send' for e in drv.snapshot()['events']))

    def test07_expiry_between_worker_admission_and_final_udp_is_rechecked(self):
        guard=DeadlineGuard()
        result=[]
        entered,release=threading.Event(),threading.Event()
        original=drv._wire_send
        def send(command,callback):
            if command=='forward 20':
                entered.set()
                if not release.wait(1): raise AssertionError('test barrier timeout')
            return original(command,callback)
        with patch.object(drv,'_wire_send',side_effect=send):
            worker=threading.Thread(target=self.move,args=(guard,result))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                guard.advance(1.1)
            finally: release.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertFalse(result[0].ok)
        self.assertNotIn('forward 20',self.wire.sent)

    def test08_nonzero_rc_checks_the_same_guard_at_final_udp(self):
        guard=DeadlineGuard()
        result=[]
        entered,release=threading.Event(),threading.Event()
        original=drv._wire_send
        def send(command,callback):
            if command=='rc 0 20 0 0':
                entered.set()
                if not release.wait(1): raise AssertionError('test barrier timeout')
            return original(command,callback)
        def velocity():
            with ctl.operation_guard(guard):
                result.append(ctl.update_velocity('model',0,20,0,0,**self.wire.grant))
        self.wire.tello.last_rc_control_timestamp=0.
        with patch.object(drv,'_wire_send',side_effect=send):
            worker=threading.Thread(target=velocity)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                guard.advance(1.1)
            finally: release.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertFalse(result[0].ok)
        self.assertNotIn('rc 0 20 0 0',self.wire.sent)
        self.assertTrue(drv.health()['link_ok'])

    def test09_zero_rc_and_land_keep_the_safe_channel_after_evidence_expiry(self):
        guard=DeadlineGuard(); guard.advance(1.1)
        self.wire.tello.last_rc_control_timestamp=0.
        self.assertTrue(drv.set_velocity((0,0,0,0),'expired-safe-stop',guard=guard))
        self.assertIn('rc 0 0 0 0',self.wire.sent)
        self.assertTrue(drv.execute('land',guard=guard))
        self.assertIn('land',self.wire.sent)

    def test10_fresh_guard_allows_a_legitimate_discrete_move(self):
        result=[]
        self.move(DeadlineGuard(),result)
        self.assertTrue(result[0].ok)
        self.assertIn('forward 20',self.wire.sent)
