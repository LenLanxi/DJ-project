"""全局物理快捷键（Ctrl+Shift+Alt+A/L）——两个入口共用同一实现。"""
import os
import sys
import time
import unittest
import asyncio
import types
from unittest.mock import AsyncMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


class FlightEntryWiringTests(unittest.IsolatedAsyncioTestCase):
    """实飞入口（modules/ai/main.py）必须注册同一组组合键。"""

    async def exercise(self, dispatch=False):
        import ctypes
        import main
        created=[]
        class Listener:
            def __init__(self,trigger,emit=None):
                self.trigger=trigger; self.started=False; self.stopped=False
                created.append(self)
            def start(self): self.started=True
            def stop(self): self.stopped=True
        runtime=types.SimpleNamespace(interrupt=AsyncMock(return_value={'ok':True}))
        with patch('drone_sdk.emergency_hotkeys.EmergencyHotkeys',Listener), \
             patch.object(ctypes,'windll',types.SimpleNamespace(user32=types.SimpleNamespace(GetAsyncKeyState=lambda key:0)),create=True):
            worker=asyncio.create_task(main.operator_controls(runtime,types.SimpleNamespace(show_video=False),lambda e:None))
            try:
                await asyncio.sleep(.02)
                self.assertTrue(created[0].started)
                if dispatch:
                    created[0].trigger('stop','Ctrl+Shift+Alt+A')
                    created[0].trigger('land','Ctrl+Shift+Alt+L')
                    await asyncio.sleep(.02)
                    self.assertEqual([call.args for call in runtime.interrupt.await_args_list],[(False,),(True,)])
            finally:
                worker.cancel()
                await asyncio.gather(worker,return_exceptions=True)
        self.assertTrue(created[0].stopped)

    async def test_main_starts_and_stops_the_operator_listener(self):
        await self.exercise()

    async def test_operator_callbacks_use_the_unified_interrupt_channel(self):
        # The low-level OS hook is deliberately replaced, not registered during
        # unattended tests. Definition/parsing is checked independently above.
        await self.exercise(dispatch=True)


if __name__ == '__main__':
    unittest.main()
