"""Real vendor command fixture with a substituted UDP transport."""
import importlib
import threading
from unittest.mock import patch

from SafetyFixture import NoNetwork, wait_for
from drone_sdk import _driver as drv, controller as ctl


class SDKWireFixture(NoNetwork):
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
