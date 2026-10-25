"""全局物理快捷键（Ctrl+Shift+Alt+A/L）——两个入口共用同一实现。"""
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
AI = os.path.join(ROOT, 'modules', 'ai')
for path in (ROOT, AI):
    if path not in sys.path:
        sys.path.insert(0, path)

from drone_sdk.emergency_hotkeys import (HOTKEY_COOLDOWN_S, HOTKEY_HELP, HOTKEYS,
                                         EmergencyHotkeys, ensure_pynput)


class HotkeyDefinitionTests(unittest.TestCase):
    def test_combos_are_the_documented_ones(self):
        mapping = {combo: (kind, human) for combo, kind, human in HOTKEYS}
        self.assertEqual(len(mapping), 2)
        self.assertEqual(mapping['<ctrl>+<shift>+<alt>+a'], ('stop', 'Ctrl+Shift+Alt+A'))
        self.assertEqual(mapping['<ctrl>+<shift>+<alt>+l'], ('land', 'Ctrl+Shift+Alt+L'))
        self.assertIn('Ctrl+Shift+Alt+A', HOTKEY_HELP)
        self.assertIn('Ctrl+Shift+Alt+L', HOTKEY_HELP)

    def test_pynput_is_available_and_parses_both_combos(self):
        keyboard = ensure_pynput()
        self.assertIsNotNone(keyboard, 'pynput 不可用')
        for combo, _kind, _human in HOTKEYS:
            self.assertTrue(keyboard.HotKey.parse(combo))

    def test_listener_is_a_daemon_thread_and_does_not_hook_on_import(self):
        guard = EmergencyHotkeys(lambda kind, combo: None)
        self.assertTrue(guard.daemon)
        self.assertIsNone(guard.listener)      # 未 start() 前不得挂钩子
        self.assertFalse(guard.ready)


class CooldownAndDispatchTests(unittest.TestCase):
    def test_cooldown_blocks_only_the_same_action(self):
        guard = EmergencyHotkeys(lambda kind, combo: None, cooldown=1.0)
        self.assertTrue(guard.allow('stop', 100.0))
        self.assertFalse(guard.allow('stop', 100.5))
        self.assertTrue(guard.allow('land', 100.5))
        self.assertTrue(guard.allow('stop', 101.5))

    def test_fire_dispatches_once_and_emits_the_loud_line(self):
        calls, lines = [], []
        guard = EmergencyHotkeys(lambda kind, combo: calls.append((kind, combo)),
                                 cooldown=1.0,
                                 emit=lambda tag, text: lines.append((tag, text)))
        self.assertTrue(guard.fire('stop', 'Ctrl+Shift+Alt+A'))
        self.assertFalse(guard.fire('stop', 'Ctrl+Shift+Alt+A'))   # 1s 内连发被吃掉
        self.assertEqual(calls, [('stop', 'Ctrl+Shift+Alt+A')])
        self.assertEqual(lines[0][0], '物理快捷键')
        self.assertIn('触发急停', lines[0][1])
        self.assertTrue(guard.fire('land', 'Ctrl+Shift+Alt+L'))
        self.assertEqual([kind for kind, _ in calls], ['stop', 'land'])
        self.assertIn('触发降落', lines[1][1])

    def test_callback_exception_is_reported_not_raised(self):
        lines = []
        def boom(kind, combo):
            raise RuntimeError('boom')
        guard = EmergencyHotkeys(boom, emit=lambda tag, text: lines.append((tag, text)))
        self.assertTrue(guard.fire('stop', 'Ctrl+Shift+Alt+A'))
        self.assertEqual(lines[-1][0], '错误')
        self.assertIn('boom', lines[-1][1])


class FlightEntryWiringTests(unittest.TestCase):
    """实飞入口（modules/ai/main.py）必须注册同一组组合键。"""

    def test_main_exposes_start_and_stop(self):
        import main
        self.assertTrue(callable(main.start_emergency_hotkeys))
        self.assertTrue(callable(main.stop_emergency_hotkeys))

    def test_start_registers_a_real_hook_and_stop_releases_it(self):
        import main
        if ensure_pynput() is None:
            self.skipTest('pynput 不可用')
        main.stop_emergency_hotkeys()
        events = []
        listener = main.start_emergency_hotkeys(
            lambda: events.append('stop'), lambda: events.append('land'),
            printer=lambda text: None)
        if listener is None:
            self.skipTest('本会话无法注册全局钩子')
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not listener.ready and not listener.error:
                time.sleep(0.05)
            if listener.error:
                self.skipTest('本会话注册失败：%s' % listener.error)
            self.assertTrue(listener.ready)
            self.assertIsNotNone(listener.listener)
            listener.fire('stop', 'Ctrl+Shift+Alt+A')       # 直接走回调路径
            self.assertEqual(events, ['stop'])
        finally:
            main.stop_emergency_hotkeys()
        self.assertIsNone(main._EMERGENCY_HOTKEYS['listener'])
        self.assertFalse(listener.listener.is_alive())


if __name__ == '__main__':
    unittest.main()
