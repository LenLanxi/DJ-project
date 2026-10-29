"""全局物理快捷键（Ctrl+Shift+Alt+A/L）——两个入口共用同一实现。"""
import os
import sys
import time
import unittest
import asyncio
import types
import threading
from unittest.mock import AsyncMock, patch


from drone_sdk.emergency_hotkeys import (HOTKEY_COOLDOWN_S, HOTKEY_HELP, HOTKEYS,
                                         EmergencyHotkeys, ensure_pynput)


class HotkeyDefinitionTests(unittest.TestCase):
    def test_combos_are_the_documented_ones(self):
        mapping = {combo: (kind, human) for combo, kind, human in HOTKEYS}
        self.assertEqual(len(mapping), 4)
        self.assertEqual(mapping['<f12>'], ('stop', 'F12'))
        self.assertEqual(mapping['<f11>'], ('land', 'F11'))
        self.assertEqual(mapping['<ctrl>+<shift>+<alt>+a'], ('stop', 'Ctrl+Shift+Alt+A'))
        self.assertEqual(mapping['<ctrl>+<shift>+<alt>+l'], ('land', 'Ctrl+Shift+Alt+L'))
        self.assertIn('Ctrl+Shift+Alt+A', HOTKEY_HELP)
        self.assertIn('Ctrl+Shift+Alt+L', HOTKEY_HELP)

    def test_pynput_is_available_and_parses_both_combos(self):
        keyboard = ensure_pynput()
        self.assertIsNotNone(keyboard, 'pynput 不可用')
        for combo, _kind, _human in HOTKEYS:
            self.assertTrue(keyboard.HotKey.parse(combo))

    def test_creating_the_owner_does_not_register_os_hooks(self):
        guard = EmergencyHotkeys(lambda kind, combo: None)
        self.assertIsNone(guard.listener)      # 未 start() 前不得挂钩子


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


class HookThread(threading.Thread):
    """Listener contract without installing an operating-system hook."""
    mode = 'ready'

    def __init__(self, mapping):
        super().__init__()
        self._ready = False
        self.ended = threading.Event()

    def run(self):
        if self.mode == 'dead':
            return
        self._ready = self.mode == 'ready'
        self.ended.wait(2)

    def stop(self):
        self.ended.set()


class ListenerHealthTests(unittest.TestCase):
    def guard(self, mode='ready'):
        listener = type('Listener', (HookThread,), {'mode': mode})
        return patch('drone_sdk.emergency_hotkeys.ensure_pynput',
                     return_value=types.SimpleNamespace(GlobalHotKeys=listener))

    def test_ready_listener_can_stop_normally(self):
        with self.guard():
            guard = EmergencyHotkeys(lambda *args: None, emit=lambda *args: None)
            guard.start()
            guard.check()
            guard.stop()
            self.assertFalse(guard.listener.is_alive())

    def test_startup_death_does_not_claim_hotkeys_are_enabled(self):
        lines = []
        with self.guard('dead'):
            guard = EmergencyHotkeys(lambda *args: None, emit=lambda *args: lines.append(args))
            try:
                with self.assertRaisesRegex(RuntimeError, 'hotkey_listener_stopped'):
                    guard.start()
            finally:
                guard.stop()
        self.assertFalse(lines)

    def test_unready_live_thread_has_a_bounded_startup(self):
        with self.guard('stalled'), patch('drone_sdk.emergency_hotkeys.HOTKEY_START_TIMEOUT_S', .03):
            guard = EmergencyHotkeys(lambda *args: None)
            try:
                with self.assertRaisesRegex(RuntimeError, 'hotkey_listener_start_timeout'):
                    guard.start()
            finally:
                guard.stop()
        self.assertFalse(guard.listener.is_alive())

    def test_dead_listener_reports_queued_backend_error(self):
        with self.guard():
            guard = EmergencyHotkeys(lambda *args: None, emit=lambda *args: None)
            guard.start()
            guard.stop()
            with patch.object(guard.listener, 'join', side_effect=ValueError('backend hook failed')):
                with self.assertRaisesRegex(RuntimeError, 'hotkey_listener_failed: backend hook failed'):
                    guard.check()


class FlightEntryWiringTests(unittest.IsolatedAsyncioTestCase):
    """实飞入口（app/Hud.py）必须注册同一组组合键。"""

    async def exercise(self, dispatch=False):
        from app import Hud
        created=[]
        class Listener:
            def __init__(self,trigger,emit=None):
                self.trigger=trigger; self.started=False; self.stopped=False
                created.append(self)
            def start(self): self.started=True
            def stop(self): self.stopped=True
            def check(self): pass
        runtime=types.SimpleNamespace(interrupt=AsyncMock(return_value={'ok':True}))
        with patch('drone_sdk.emergency_hotkeys.EmergencyHotkeys',Listener):
            worker=asyncio.create_task(Hud.operator_controls(runtime,types.SimpleNamespace(show_video=False),lambda e:None,
                types.SimpleNamespace(diagnostics=lambda: {})))
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

    async def test_listener_death_reaches_supervisor_and_cancels_active_operation(self):
        from app.Hud import operator_controls
        from app.Application import supervise
        operation_cancelled = asyncio.Event()
        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                operation_cancelled.set()

        with ListenerHealthTests().guard():
            # Use the real EmergencyHotkeys owner and health check, replacing
            # only the thread that would otherwise register the OS hook.
            owner = EmergencyHotkeys(lambda *args: None, emit=lambda *args: None)
            runtime = types.SimpleNamespace(interrupt=AsyncMock())
            with patch('drone_sdk.emergency_hotkeys.EmergencyHotkeys', return_value=owner):
                controls = asyncio.create_task(operator_controls(runtime,
                    types.SimpleNamespace(show_video=False), lambda row: None,
                    types.SimpleNamespace(diagnostics=lambda: {})))
                supervising = asyncio.create_task(supervise(operation(), controls))
                try:
                    await asyncio.sleep(.02)
                    owner.listener.stop()
                    await asyncio.to_thread(owner.listener.join, .5)
                    with self.assertRaisesRegex(RuntimeError, 'hotkey_listener_stopped'):
                        await asyncio.wait_for(supervising, 1)
                    self.assertTrue(operation_cancelled.is_set())
                finally:
                    controls.cancel()
                    supervising.cancel()
                    await asyncio.gather(controls, supervising, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
