"""Contract and failure-path checks, plus one actual shared-stub integration flow."""
import unittest
from unittest.mock import patch

from drone_sdk.controller import CommandResult, ControlResult, FrameResult, StatusResult

from .ai_agent import Agent
from .demo_ai import ScriptedClient
from .SdkRunner import SdkRunner, snapshot
from .session import Session


TARGET = 'modules.AICompanion.SdkRunner.'


class SdkTests(unittest.TestCase):
    def state(self, owner='none', connected=True):
        return StatusResult(88, 0, connected, 'landed', owner, 100.0)

    def test_read_without_control(self):
        with patch(TARGET + 'get_status', return_value=self.state()), \
             patch(TARGET + 'request_control') as request:
            result = SdkRunner().run('drone.get_battery()')
        request.assert_not_called()
        self.assertIn('88%', result['result'])
        self.assertIsNone(result['simulated'])
        self.assertEqual(result['state']['flight_state'], 'landed')
        self.assertEqual(result['state']['control_owner'], 'none')

    def test_frame_keeps_payload_identity(self):
        image = object()
        with patch(TARGET + 'get_frame', return_value=FrameResult(image, 5, 100.0, False)):
            result = SdkRunner().get_frame()
        self.assertIs(result['frame'], image)
        self.assertEqual(result['frame_id'], 5)

    def test_wrong_contract_is_rejected(self):
        with self.assertRaises(TypeError):
            snapshot({'ok': True})

    def test_busy_never_executes_or_releases_another_owner(self):
        with patch(TARGET + 'get_status', return_value=self.state(owner='other')), \
             patch(TARGET + 'is_following', return_value=False), \
             patch(TARGET + 'request_control', return_value=ControlResult(False, 'other', 'busy')), \
             patch(TARGET + 'takeoff') as action, patch(TARGET + 'release_control') as release:
            result = SdkRunner().run('drone.takeoff()')
        self.assertTrue(result['accepted'])
        self.assertFalse(result['command']['ok'])
        self.assertEqual(result['command']['reason'], 'busy')
        action.assert_not_called()
        release.assert_not_called()

    def test_exception_and_timeout_release_control_without_retry(self):
        for error, status in ((RuntimeError('failed'), 'failed'), (TimeoutError(), 'timeout')):
            with self.subTest(status=status), \
                 patch(TARGET + 'get_status', return_value=self.state()), \
                 patch(TARGET + 'is_following', return_value=False), \
                 patch(TARGET + 'request_control', return_value=ControlResult(True, 'model')), \
                 patch(TARGET + 'takeoff', side_effect=error) as action, \
                 patch(TARGET + 'release_control', return_value=ControlResult(True, 'none')) as release:
                result = SdkRunner().run('drone.takeoff()')
            action.assert_called_once_with('model')
            release.assert_called_once_with('model')
            self.assertEqual(result['command']['status'], status)
            self.assertFalse(result['command']['ok'])

    def test_accepted_is_not_reported_as_completed(self):
        with patch(TARGET + 'get_status', return_value=self.state()), \
             patch(TARGET + 'is_following', return_value=False), \
             patch(TARGET + 'request_control', return_value=ControlResult(True, 'model')), \
             patch(TARGET + 'takeoff', return_value=CommandResult(True, 'accepted', is_blocking=False)), \
             patch(TARGET + 'release_control', return_value=ControlResult(True, 'none')):
            result = SdkRunner().run('drone.takeoff()')
        self.assertIn('未报告动作完成', result['result'])

    def test_failed_release_does_not_reexecute_action(self):
        with patch(TARGET + 'get_status', return_value=self.state()), \
             patch(TARGET + 'is_following', return_value=False), \
             patch(TARGET + 'request_control', return_value=ControlResult(True, 'model')), \
             patch(TARGET + 'takeoff', return_value=CommandResult(True, 'completed')) as action, \
             patch(TARGET + 'release_control', side_effect=RuntimeError('release failed')):
            result = SdkRunner().run('drone.takeoff()')
        action.assert_called_once()
        self.assertTrue(result['command']['ok'])
        self.assertFalse(result['release']['ok'])
        self.assertIn('控制权释放失败', result['result'])

    def test_disconnected_rejects_action(self):
        with patch(TARGET + 'get_status', return_value=self.state(connected=False)), \
             patch(TARGET + 'request_control') as request:
            result = SdkRunner().run('drone.takeoff()')
        self.assertEqual(result['command']['reason'], 'disconnected')
        request.assert_not_called()

    def test_follow_handoff_not_assumed(self):
        for owner, following in (('follow', False), ('none', True)):
            with self.subTest(owner=owner, following=following), \
                 patch(TARGET + 'get_status', return_value=self.state(owner)), \
                 patch(TARGET + 'is_following', return_value=following), \
                 patch(TARGET + 'request_control') as request:
                result = SdkRunner().run('drone.takeoff()')
            request.assert_not_called()
            self.assertEqual(result['command']['reason'], 'follow_handoff_unavailable')

    def test_follow_commands_are_explicitly_unavailable(self):
        with patch(TARGET + 'get_status', return_value=self.state()), \
             patch(TARGET + 'request_control') as request:
            for name in ('start_follow', 'stop_follow'):
                result = SdkRunner().run('drone.' + name + '()')
                self.assertEqual(result['command']['reason'], 'follow_not_integrated')
        request.assert_not_called()

    def test_invalid_program_has_no_sdk_side_effect(self):
        with patch(TARGET + 'get_status') as state, patch(TARGET + 'request_control') as request:
            for code in ('drone.takeoff(); drone.land()', 'drone.move_forward(100)', None):
                self.assertFalse(SdkRunner().run(code)['accepted'])
        state.assert_not_called()
        request.assert_not_called()

    def test_async_session_rejects_unverified_sdk_interrupts(self):
        with self.assertRaises(ValueError):
            Session(Agent(ScriptedClient(), runner=SdkRunner()))

    def test_actual_shared_stub_flow(self):
        # Only the current repository's fake SDK is exercised, with no driver/device.
        runner = SdkRunner()
        agent = Agent(ScriptedClient(), runner=runner)
        self.assertEqual(runner.get_status()['flight_state'], 'landed')
        try:
            takeoff_result = agent.handle('起飞')
            self.assertEqual(takeoff_result['execution']['command']['status'], 'completed')
            self.assertEqual(runner.get_status()['flight_state'], 'flying')
            self.assertEqual(runner.get_status()['control_owner'], 'none')
            move_result = agent.handle('向前走30厘米')
            self.assertTrue(move_result['execution']['command']['ok'])
        finally:
            result = runner.run('drone.land()')
        self.assertTrue(result['command']['ok'])
        self.assertEqual(runner.get_status()['flight_state'], 'landed')
        self.assertEqual(runner.get_status()['control_owner'], 'none')


if __name__ == '__main__':
    unittest.main()
