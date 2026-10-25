"""目标丢失后保持跟随模式的窗口（实飞入口默认 30 秒）。"""
import importlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
FOLLOW = os.path.join(ROOT, 'modules', 'follow')
for path in (ROOT, FOLLOW):
    if path not in sys.path:
        sys.path.insert(0, path)

import drone_sdk.follow_integration as fi  # noqa: E402


class RecoveryWindowTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop('R3_RECOVERY_WINDOW_S', None)
        importlib.reload(fi)

    def test_default_window_is_30_seconds(self):
        os.environ.pop('R3_RECOVERY_WINDOW_S', None)
        importlib.reload(fi)
        self.assertEqual(fi.RECOVERY_WINDOW, 30.0)

    def test_env_override_for_field_tuning(self):
        os.environ['R3_RECOVERY_WINDOW_S'] = '12.5'
        importlib.reload(fi)
        self.assertEqual(fi.RECOVERY_WINDOW, 12.5)

    def test_window_reaches_the_bridge(self):
        seen = {}

        class FakePort:
            def __init__(self, *args, **kwargs):
                pass

        class FakeBridge:
            def __init__(self, port, recovery_window=None, **kwargs):
                seen['window'] = recovery_window
                self.service = type('S', (), {'start_background': lambda self: None})()

        original = None
        try:
            from r3_follow import team2_bridge, team2_adapter
            original = (team2_bridge.Team3FollowBridge, team2_adapter.Team2StubPort)
            team2_bridge.Team3FollowBridge = FakeBridge
            team2_adapter.Team2StubPort = FakePort
            integration = fi.FollowIntegration()
            integration._frame_provider = lambda: None
            bridge = integration._ensure_bridge()
            self.assertIsNotNone(bridge)
        finally:
            if original is not None:
                from r3_follow import team2_bridge, team2_adapter
                team2_bridge.Team3FollowBridge, team2_adapter.Team2StubPort = original
        self.assertEqual(seen.get('window'), fi.RECOVERY_WINDOW)
        self.assertGreaterEqual(seen.get('window'), 30.0)


if __name__ == '__main__':
    unittest.main()
