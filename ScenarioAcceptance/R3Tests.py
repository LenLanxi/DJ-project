"""R3 residual safety regressions. Only the disconnected SDK simulator is used.

Assertions describe required safety behavior; current defects fail normally.
Run directly from any directory. No aircraft, camera, cloud or Docker is used.
"""
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'modules' / 'follow')]
from r3_follow.mock_port import MockPort
from r3_follow.service import FollowService
from r3_follow.team2_adapter import Team2StubPort, load_reviewed_stub


class AdapterTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket', 'cv2.VideoCapture', 'tkinter.Tk'):
            guard = patch(target, side_effect=AssertionError('Offline test boundary'))
            guard.start()
            self.addCleanup(guard.stop)
        self.sdk = load_reviewed_stub(ROOT)
        self.assertTrue(self.sdk.SIMULATED)
        self.assertTrue(self.sdk._driver.snapshot()['simulated'])
        self.assertFalse(self.sdk._driver.snapshot()['connected'])
        self.sdk.stop_follow()
        self.reset_owner()
        self.addCleanup(self.reset_owner)
        self.port = Team2StubPort(ROOT)

    def reset_owner(self):
        owner = self.sdk.get_status().control_owner
        if owner != 'none':
            self.sdk.stop_motion(owner)
            self.sdk.release_control(owner)

    def airborne(self):
        self.assertTrue(self.sdk.request_control('model').ok)
        self.sdk.land('model')
        deadline = time.monotonic() + 1
        while self.sdk.get_status().flight_state != 'landed':
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        self.assertTrue(self.sdk.takeoff('model').ok)
        self.assertTrue(self.sdk.release_control('model').ok)

    def testR301_cancel_between_check_and_send_rejects_old_velocity(self):
        """team2_adapter.update_velocity omits SDK command_epoch."""
        self.airborne()
        lease = self.port.request_control('follow').lease
        send = self.sdk.update_velocity

        def interrupt_before_send(*args, **kwargs):
            self.assertTrue(self.sdk.stop_motion('follow').ok)
            return send(*args, **kwargs)

        with patch.object(self.sdk, 'update_velocity', side_effect=interrupt_before_send):
            result = self.port.update_velocity(lease, left_right=0, forward_back=20,
                                               up_down=0, yaw=0)
        self.assertFalse(result.ok, 'Old velocity accepted after stop; driver velocity=' +
                         repr(self.sdk._driver.snapshot()['velocity']))

    def testR302_confirmation_cannot_borrow_replacement_grant(self):
        """request_control must bind confirmation to its own returned grant."""
        request = self.sdk.request_control
        original_epochs = []

        def replace_grant(owner):
            original = request(owner)
            original_epochs.append(original.control_epoch)
            self.assertTrue(self.sdk.release_control(owner).ok)
            self.assertTrue(request(owner).ok)
            return original

        with patch.object(self.sdk, 'request_control', side_effect=replace_grant):
            result = self.port.request_control('follow')
        self.assertFalse(result.ok,
                         f'Original epochs={original_epochs}; borrowed lease={result.lease}')

    def test_grant_confirmation_without_race_succeeds(self):
        result = self.port.request_control('follow')
        self.assertTrue(result.ok)
        self.assertEqual(result.lease.epoch, self.sdk.get_status().control_epoch)

    def test_old_control_epoch_is_rejected(self):
        self.airborne()
        old = self.port.request_control('follow').lease
        self.sdk.release_control('follow')
        self.sdk.request_control('follow')
        result = self.port.update_velocity(old, left_right=0, forward_back=20,
                                           up_down=0, yaw=0)
        self.assertFalse(result.ok)
        self.assertEqual(self.sdk._driver.snapshot()['velocity'], (0, 0, 0, 0))


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.port = MockPort()
        self.service = FollowService(self.port, log_directory=False)
        self.service.lease = self.port.request_control('follow').lease
        self.port.update_velocity(self.service.lease, left_right=0, forward_back=20,
                                  up_down=0, yaw=20)
        self.addCleanup(self.service.close)

    def testR303_status_error_cannot_skip_lease_checked_stop_attempt(self):
        """A status exception must not suppress all token-checked stop attempts."""
        with patch.object(self.port, 'get_status', side_effect=RuntimeError('telemetry lost')), \
             patch.object(self.port, 'stop_motion', wraps=self.port.stop_motion) as stop:
            result = self.service.stop_follow()
            self.assertFalse(result.ok)
            self.assertGreater(stop.call_count, 0,
                               'No stop attempted; remaining velocity=' + repr(self.port.velocity))

    def test_normal_stop_zeros_and_releases(self):
        self.assertTrue(self.service.stop_follow().ok)
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.assertIsNone(self.port.lease)

    def test_replacement_owner_is_not_stopped(self):
        new = self.port.revoke_for_test('model')
        self.port.update_velocity(new, left_right=0, forward_back=11, up_down=0, yaw=12)
        self.service.stop_follow()
        self.assertEqual(self.port.velocity, (0, 11, 0, 12))
        self.assertEqual(self.port.lease, new)


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.records.append(dict(test=test.id(), status='pass'))

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.records.append(dict(test=test.id(), status='fail',
                                 evidence=''.join(traceback.format_exception(*err))))

    def addError(self, test, err):
        super().addError(test, err)
        self.records.append(dict(test=test.id(), status='error',
                                 evidence=''.join(traceback.format_exception(*err))))


def git_value(*args, default='unknown'):
    """A missing ``git`` on PATH must not fail an acceptance run."""
    try:
        return subprocess.check_output(['git', '-C', str(ROOT), *args], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.SubprocessError):
        return default


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2, resultclass=EvidenceResult).run(suite)
    output = Path(__file__).resolve().parent / 'Results' / 'R3Results.json'
    output.parent.mkdir(exist_ok=True)
    revision = git_value('rev-parse', 'HEAD')
    dirty = bool(git_value('status', '--porcelain', default=''))
    output.write_text(json.dumps(dict(revision=revision, dirty=dirty, tests=result.testsRun,
        passed=result.testsRun-len(result.failures)-len(result.errors),
        failed=len(result.failures), errors=len(result.errors), records=result.records,
        scope='Disconnected simulated SDK and explicit mock port; no physical flight'),
        ensure_ascii=False, indent=2), encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
