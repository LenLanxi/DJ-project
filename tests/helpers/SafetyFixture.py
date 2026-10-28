"""Offline device and network guards shared by regression suites."""
import socket
import threading
import time
import unittest
from unittest.mock import patch

from drone_sdk import controller as ctl, _driver as drv


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
