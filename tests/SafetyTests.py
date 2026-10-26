"""Behavioral safety regression tests; no aircraft, cameras or external network.

Run: uv run --no-project --python 3.11 --with numpy==1.26.4 --with opencv-python==4.8.0.76 python -B tests/SafetyTests.py
A fake device exercises real controller/driver code. Wire checks inject a send
callback instead of UDP; they do not prove physical braking or flight dynamics.
"""
import asyncio
import builtins
import socket
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT, ROOT / 'modules' / 'ai', ROOT / 'tests'):
    sys.path.insert(0, str(folder))
from drone_sdk import controller as ctl, _driver as drv
from drone_sdk.contract import FrameResult
from drone_sdk.follow_integration import FollowIntegration
from drone_sdk.emergency_hotkeys import ensure_pynput, command_failed
from backends import SDKBackend, GRANT
from gateway import Gateway
from Support import MockBackend


def wait_for(predicate, timeout=1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(.002)
    return bool(predicate())


class FakeTello:
    def __init__(self, flying=True):
        self.calls = []
        self.is_flying = flying
        self.packet = {}
        self.refresh(80 if flying else 0)
        self.takeoff_release = threading.Event()
        self.takeoff_release.set()
        self.takeoff_entered = threading.Event()
        self.fail_rc = False
        self.fail_land = False

    def refresh(self, height=None, *, age=0, temperature=35):
        h = (80 if self.is_flying else 0) if height is None else height
        self.packet = {'h': h, 'tof': h, 'bat': 80, 'templ': temperature,
                       'temph': temperature, 'time': 0,
                       '_received_at': time.time() - age,
                       '_received_monotonic': time.monotonic() - age}

    def get_current_state(self): return self.packet
    def get_height(self): return self.packet.get('h')
    def get_distance_tof(self): return self.packet.get('tof')
    def get_battery(self): return self.packet.get('bat')
    def get_flight_time(self): return self.packet.get('time')
    def send_rc_control(self, *channels):
        self.calls.append(('rc', channels))
        if self.fail_rc:
            raise RuntimeError('send failed')
    def takeoff(self):
        self.takeoff_entered.set()
        self.takeoff_release.wait(1)
        self.is_flying = True
        self.refresh()
        self.calls.append(('takeoff',))
    def land(self):
        self.calls.append(('land',))
        if self.fail_land:
            raise RuntimeError('land failed')
        self.is_flying = False
        self.refresh()
    def move_forward(self, cm): self.calls.append(('forward', cm))
    def move_up(self, cm): self.calls.append(('up', cm))
    def rotate_clockwise(self, degrees): self.calls.append(('rotate', degrees))
    def rotate_counter_clockwise(self, degrees): self.calls.append(('rotate', -degrees))


class NoNetwork(unittest.TestCase):
    def setUp(self):
        # Windows asyncio uses a loopback socketpair. Everything else is banned.
        for name in ('connect', 'connect_ex', 'sendto'):
            original = getattr(socket.socket, name)
            def guarded(instance, address, *args, _original=original, **kwargs):
                host = address[0] if isinstance(address, tuple) else address
                if host in ('localhost', '::1') or isinstance(host, str) and host.startswith('127.'):
                    return _original(instance, address, *args, **kwargs)
                raise AssertionError('external network forbidden')
            self.enterContext(patch.object(socket.socket, name, guarded))


class DeviceCase(NoNetwork):
    def setUp(self):
        super().setUp()
        if not drv.SIMULATED and drv._tello is not None:
            raise AssertionError('offline tests refuse an existing real driver connection')
        ctl._current_owner = None
        ctl._flight_state = 'landed'
        ctl._landing_deadline = ctl._active_action = None
        ctl._last_velocity_time = 0
        ctl._frame = FrameResult(None, 0, 0., True)
        drv.SIMULATED = True
        drv._tello = drv._video = None
        drv._flying = False
        drv._height_cm = 0
        drv._velocity = (0, 0, 0, 0)
        drv._link_ok = True
        drv._faults = 0
        drv._unknown_actions.clear()
        drv._unknown_since.clear()
        drv._pending_commands.clear()
        drv._landing_acknowledged = False
        drv._response_quarantine = None
        drv._last_wire_send = 0.
        drv._events.clear()
        self.fake = None

    def tearDown(self):
        if self.fake:
            self.fake.takeoff_release.set()
        wait_for(lambda: not drv._pending_commands)
        drv.SIMULATED = True
        drv._tello = drv._video = None
        drv._unknown_actions.clear()
        drv._unknown_since.clear()
        ctl._current_owner = None
        ctl._flight_state = 'landed'
        ctl._active_action = None
        ctl._last_velocity_time = 0
        super().tearDown()

    def real(self, flying=True):
        self.fake = FakeTello(flying)
        drv.SIMULATED, drv._tello = False, self.fake
        drv._flying = flying
        ctl._flight_state = 'flying' if flying else 'landed'
        ctl.configure_takeoff(authorized=True, min_battery=30)
        grant = ctl.request_control('model')
        self.assertTrue(grant.ok)
        return {'control_epoch': grant.control_epoch, 'command_epoch': grant.command_epoch}

    def queued(self, *, priority=False, land=False):
        grant = self.real()
        drv._command_lock.acquire()
        result = []
        worker = threading.Thread(target=lambda: result.append(ctl.move_forward('model', 20, **grant)))
        worker.start()
        try:
            self.assertTrue(wait_for(lambda: bool(drv._pending_commands)))
            stopped = ctl.land('model', priority=True) if land else ctl.stop_motion('model', priority=priority, **({} if priority else grant))
            self.assertTrue(stopped.ok, stopped)
        finally:
            drv._command_lock.release()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertFalse(result[0].ok)
        self.assertNotIn(('forward', 20), self.fake.calls)
        if land:
            self.assertTrue(wait_for(lambda: ('land',) in self.fake.calls))
        return result

class ControllerSafety(DeviceCase):
    def test01_normal_stop_drops_queued_move(self): self.queued()
    def test02_priority_stop_drops_queued_move(self): self.queued(priority=True)
    def test03_priority_land_drops_queued_move(self): self.queued(land=True)

    def test04_stop_before_driver_binding_drops_move(self):
        grant = self.real()
        entered, release = threading.Event(), threading.Event()
        original = drv.execute
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(1)
            return original(*args, **kwargs)
        result = []
        with patch.object(drv, 'execute', delayed):
            worker = threading.Thread(target=lambda: result.append(ctl.move_forward('model', 20, **grant)))
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertTrue(ctl.stop_motion('model', **grant).ok)
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
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        writes = []
        bound = drv.capture_cancel_epoch()
        def send(_value):
            entered.set()
            release.wait(1)
            writes.append('sent')
        def work():
            drv._command_context.epoch = bound
            try: drv._wire_send('forward 20', send)
            finally: drv._command_context.epoch = None
        worker = threading.Thread(target=work)
        worker.start()
        self.assertTrue(entered.wait(1))
        stop = threading.Thread(target=lambda: (drv.cancel_pending('stop'), cancelled.set()))
        stop.start()
        self.assertFalse(cancelled.wait(.02))
        release.set()
        worker.join(1); stop.join(1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(writes, ['sent'])

    def test07_new_same_owner_lease_rejects_old_cleanup(self):
        old = self.real()
        new = ctl.request_control('model')
        before = len(self.fake.calls)
        self.assertGreater(new.control_epoch, old['control_epoch'])
        self.assertFalse(ctl.stop_motion('model', **old).ok)
        self.assertFalse(ctl.release_control('model', **old).ok)
        self.assertEqual(len(self.fake.calls), before)

    def test08_backend_context_does_not_borrow_priority_grant(self):
        old = self.real()
        backend = SDKBackend(ROOT)
        backend.grant = old
        self.assertTrue(backend.priority_stop()['ok'])
        before = len(self.fake.calls)
        token = GRANT.set(old)
        try:
            self.assertFalse(backend.action('move_forward', 20)['ok'])
            self.assertFalse(backend.release_control('model')['ok'])
        finally: GRANT.reset(token)
        self.assertEqual(len(self.fake.calls), before)

    def test09_telemetry_poll_does_not_refresh_receipt(self):
        self.real()
        self.fake.refresh(age=2)
        stamp = self.fake.packet['_received_at']
        a, b = drv.health(), drv.health()
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
        with patch.dict(drv.COMMAND_DEADLINE_S, takeoff=.02):
            result = ctl.takeoff('model', **grant)
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
        worker = threading.Thread(target=lambda: results.append(ctl.takeoff('model', **grant)))
        worker.start()
        try:
            self.assertTrue(wait_for(lambda: drv.action_pending()))
            ctl.stop_motion('model', **grant)
        finally: drv._command_lock.release()
        worker.join(1)
        self.assertNotIn(('takeoff',), self.fake.calls)
        self.assertFalse(drv.action_unknown('takeoff'))
        self.assertEqual(ctl.get_status().flight_state, 'landed')

    def test14_failed_land_is_not_confirmed_landed(self):
        self.real()
        self.fake.fail_land = True
        self.assertTrue(ctl.land('model', priority=True).ok)
        self.assertTrue(wait_for(lambda: not drv.action_pending()))
        self.assertEqual(ctl.get_status().flight_state, 'landing')
        self.assertFalse(drv.health()['landing_acknowledged'])

    def test15_failed_zero_keeps_watchdog_retry(self):
        grant = self.real()
        self.fake.fail_rc = True
        self.assertFalse(ctl.stop_motion('model', **grant).ok)
        self.assertGreater(ctl._last_velocity_time, 0)
        before = len(self.fake.calls)
        ctl._last_velocity_time -= 1
        self.fake.fail_rc = False
        ctl._watchdog_tick()
        self.assertGreater(len(self.fake.calls), before)
        self.assertEqual(ctl._last_velocity_time, 0)

    def test16_stale_telemetry_rejects_nonzero_velocity(self):
        grant = self.real()
        self.fake.refresh(age=2)
        result = ctl.update_velocity('model', 0, 20, 0, 0, **grant)
        self.assertEqual(result.reason, 'telemetry_stale')
        self.assertNotIn(('rc', (0, 20, 0, 0)), self.fake.calls)

    def test17_unknown_height_blocks_ascent(self):
        grant = self.real()
        self.fake.packet.update(h=0, tof=None)
        self.assertFalse(ctl.move_up('model', 20, **grant).ok)
        self.assertNotIn(('up', 20), self.fake.calls)

    def test18_height_ceiling_clamps_ascent(self):
        grant = self.real()
        self.fake.refresh(height=220)
        self.assertTrue(ctl.move_up('model', 80, **grant).ok)
        self.assertIn(('up', 30), self.fake.calls)

    def test19_temperature_gate_rejects_hot_takeoff(self):
        grant = self.real(flying=False)
        self.fake.refresh(temperature=95)
        self.assertFalse(ctl.takeoff('model', **grant).ok)
        self.assertNotIn(('takeoff',), self.fake.calls)

    def test20_production_backend_rejects_disconnected_simulator(self):
        with self.assertRaisesRegex(RuntimeError, 'real_device_not_connected'):
            SDKBackend(ROOT)
        self.assertTrue(SDKBackend(ROOT, allow_simulated=True).simulated)

    def test21_priority_rejects_invalid_owner(self):
        self.assertFalse(ctl.stop_motion('attacker', priority=True).ok)
        self.assertFalse(ctl.land('attacker', priority=True).ok)

    def test22_pending_cleanup_visible_when_bridge_retired(self):
        follow = FollowIntegration()
        follow._pending_cleanup.append(object())
        status = follow.status()
        self.assertTrue(status['cleanup_pending'])
        self.assertFalse(status['cleanup_confirmed'])


class GatewaySafety(NoNetwork):
    def run_async(self, body):
        return asyncio.run(body())

    def fake(self):
        backend = MockBackend()
        backend.flying = True
        gateway = Gateway(backend)
        gateway.begin()
        return backend, gateway

    def test23_invisible_target_blocks_translation(self):
        async def run():
            backend, gateway = self.fake()
            gateway.observation = {'visible': False, 'source': 'sdk_camera', 'timestamp': time.time()}
            with self.assertRaisesRegex(RuntimeError, 'target_not_visible'):
                await gateway.dispatch('move_forward', [20], {})
            self.assertEqual(backend.calls, [])
        self.run_async(run)

    def test24_explicit_motion_without_observation_is_supported(self):
        async def run():
            backend, gateway = self.fake()
            self.assertTrue((await gateway.dispatch('move_forward', [20], {}))['ok'])
            await gateway.finish(True)
            self.assertIsNone(backend.owner)
        self.run_async(run)

    def test25_rotation_can_inspect_an_invisible_target(self):
        async def run():
            _backend, gateway = self.fake()
            gateway.observation = {'visible': False}
            self.assertTrue((await gateway.dispatch('rotate', [10], {}))['ok'])
            await gateway.finish(True)
        self.run_async(run)

    def test26_one_observation_cannot_authorize_two_moves(self):
        async def run():
            _backend, gateway = self.fake()
            gateway.observation = {'visible': True, 'source': 'sdk_camera', 'timestamp': time.time()}
            await gateway.dispatch('move_forward', [20], {})
            with self.assertRaisesRegex(RuntimeError, 'new_observation'):
                await gateway.dispatch('move_forward', [20], {})
            await gateway.finish(False)
        self.run_async(run)

    def test27_describe_is_read_only_and_not_navigation_evidence(self):
        async def run():
            backend, gateway = self.fake()
            backend.start_follow()
            gateway.vision = types.SimpleNamespace(describe=lambda _: {'text': 'tent', 'timestamp': 1})
            result = await gateway.dispatch('describe', ['what is ahead?'], {})
            self.assertEqual(result['text'], 'tent')
            self.assertTrue(backend.following)
            self.assertFalse(gateway.acquired)
            self.assertIsNone(gateway.observation)
        self.run_async(run)

    def test28_follow_does_not_start_after_release_failure(self):
        async def run():
            backend, gateway = self.fake()
            backend.release_control = lambda _: {'ok': False, 'reason': 'send failed'}
            with self.assertRaisesRegex(RuntimeError, 'handoff_release_failed'):
                await gateway.dispatch('start_follow', [], {})
            self.assertFalse(backend.following)
            self.assertTrue(gateway.acquired)
        self.run_async(run)

    def test29_explicit_stop_does_not_resume_follow(self):
        async def run():
            backend, gateway = self.fake()
            backend.start_follow()
            await gateway.dispatch('stop_motion', [], {})
            await gateway.finish(True)
            self.assertFalse(backend.following)
        self.run_async(run)

    def test30_successful_completed_action_resumes_prior_follow(self):
        async def run():
            backend, gateway = self.fake()
            backend.start_follow()
            await gateway.dispatch('move_forward', [20], {})
            self.assertFalse(backend.following)
            await gateway.finish(True)
            self.assertTrue(backend.following)
        self.run_async(run)

    def test31_priority_action_runs_before_follow_cleanup(self):
        async def run():
            backend, gateway = self.fake()
            order = []
            original = backend.priority_stop
            def priority(land=False):
                order.append('priority')
                return original(land)
            backend.priority_stop = priority
            backend.stop_follow = lambda: (order.append('follow'), {'ok': True})[1]
            await gateway.interrupt()
            self.assertEqual(order, ['priority', 'follow'])
        self.run_async(run)

    def test32_old_cleanup_cannot_stop_new_priority_controller(self):
        async def run():
            backend, gateway = self.fake()
            await gateway.dispatch('move_forward', [20], {})
            old = dict(gateway.grant)
            await gateway.interrupt()
            before = len(backend.calls)
            await gateway.finish(False)
            self.assertEqual(len(backend.calls), before)
            self.assertEqual(gateway.cleanup['status'], 'superseded')
            self.assertGreater(backend.control_epoch, old['control_epoch'])
        self.run_async(run)

    def test33_late_observation_cannot_overwrite_new_generation(self):
        async def run():
            _backend, gateway = self.fake()
            entered, release = threading.Event(), threading.Event()
            def observe(_):
                entered.set(); release.wait(1)
                return {'visible': True, 'source': 'sdk_camera', 'frame_id': 1, 'timestamp': time.time()}
            gateway.vision = types.SimpleNamespace(observe=observe)
            task = asyncio.create_task(gateway.dispatch('observe', ['tent'], {}))
            await asyncio.to_thread(entered.wait, 1)
            gateway.begin()
            release.set()
            with self.assertRaisesRegex(RuntimeError, 'program_cancelled'):
                await task
            self.assertIsNone(gateway.observation)
        self.run_async(run)

    def test34_unconfirmed_cleanup_blocks_new_program(self):
        async def run():
            backend, gateway = self.fake()
            await gateway.dispatch('move_forward', [20], {})
            backend.release_control = lambda _: {'ok': False, 'reason': 'send failed'}
            with self.assertRaisesRegex(RuntimeError, 'cleanup_release_failed'):
                await gateway.finish(True)
            self.assertTrue(gateway.acquired)
            with self.assertRaisesRegex(RuntimeError, 'cleanup_unconfirmed'):
                gateway.begin()
        self.run_async(run)

    def test35_accepted_blocking_move_cannot_resume_follow(self):
        async def run():
            backend, gateway = self.fake()
            backend.start_follow()
            backend.action = lambda *_: {'ok': True, 'status': 'accepted'}
            with self.assertRaisesRegex(RuntimeError, 'SDK_action_failed'):
                await gateway.dispatch('move_forward', [20], {})
            self.assertFalse(gateway.resume)
            self.assertFalse(backend.following)
        self.run_async(run)

    def test36_missing_hotkey_dependency_never_installs(self):
        original = builtins.__import__
        def blocked(name, *args, **kwargs):
            if name == 'pynput':
                raise ImportError('test missing dependency')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', blocked), patch('subprocess.run', side_effect=AssertionError('must not install')):
            self.assertIsNone(ensure_pynput(lambda *_: None))
        self.assertTrue(command_failed({'ok': False}))
        self.assertFalse(command_failed({'ok': True}))

    def test37_land_waits_for_fresh_confirmed_ground_state(self):
        async def run():
            backend, gateway = self.fake()
            original = backend.get_status
            reads = [0]
            def status():
                state = original()
                reads[0] += 1
                if reads[0] < 3:
                    state.update(flight_state='landing', height_cm=80)
                return state
            backend.get_status = status
            result = await gateway.dispatch('land', [], {})
            self.assertGreaterEqual(reads[0], 3)
            self.assertEqual(result['status'], 'completed')
            self.assertTrue(result['landed_confirmed'])
            await gateway.finish(True)
        self.run_async(run)

    def test38_stale_landed_state_cannot_confirm_landing(self):
        async def run():
            backend, gateway = self.fake()
            original = backend.get_status
            backend.get_status = lambda: dict(original(), connected=False, timestamp=time.time()-2)
            with patch('gateway.LAND_CONFIRM_TIMEOUT_S', .02):
                with self.assertRaisesRegex(RuntimeError, 'landing_confirmation_timeout'):
                    await gateway.dispatch('land', [], {})
            self.assertFalse(gateway.resume)
        self.run_async(run)

    def test39_landing_confirmation_wait_is_cancellable(self):
        async def run():
            backend, gateway = self.fake()
            original = backend.get_status
            backend.get_status = lambda: dict(original(), flight_state='landing', height_cm=80)
            task = asyncio.create_task(gateway.dispatch('land', [], {}))
            await asyncio.sleep(.02)
            await gateway.interrupt(True)
            with self.assertRaisesRegex(RuntimeError, 'program_cancelled'):
                await asyncio.wait_for(task, .2)
            await gateway.finish(False)
        self.run_async(run)


if __name__ == '__main__':
    unittest.main(verbosity=2)
