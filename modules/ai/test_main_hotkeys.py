"""物理急停热键：两条语音链路都必须注册，且豆包/asyncio 路径要把协程投回事件循环。

真机踩过：豆包模式下按 Ctrl+Shift+Alt+L 毫无反应 —— start_emergency_hotkeys 只在
经典入口 _dispatch 注册，run_modern 压根没注册；即使注册了，pynput 线程直接调用
async 的 emergency() 也只会得到一个没被 await 的协程。
"""
import asyncio
import unittest
from pathlib import Path

import main


class HotkeyWiringTests(unittest.TestCase):
    def setUp(self):
        self.text = Path(main.__file__).read_text(encoding='utf-8')

    def _block(self, start_marker, end_marker):
        start = self.text.index(start_marker)
        end = self.text.index(end_marker, start)
        return self.text[start:end]

    def test_modern_path_registers_hotkeys(self):
        block = self._block('async def run_modern', 'def main()')
        self.assertIn('start_emergency_hotkeys', block)
        self.assertIn('stop_emergency_hotkeys', block)

    def test_modern_path_bridges_into_event_loop(self):
        block = self._block('async def run_modern', 'def main()')
        self.assertIn('run_coroutine_threadsafe', block)
        self.assertIn('dialog.emergency', block)

    def test_classic_path_registers_hotkeys(self):
        block = self._block('def _dispatch', "if __name__")
        self.assertIn('start_emergency_hotkeys', block)

    def test_hotkey_modules_available(self):
        from drone_sdk import emergency_hotkeys
        actions = [entry[1] for entry in emergency_hotkeys.HOTKEYS]
        self.assertEqual(actions, ['stop', 'land'])
        labels = ' '.join(entry[2] for entry in emergency_hotkeys.HOTKEYS)
        self.assertIn('Ctrl+Shift+Alt+A', labels)
        self.assertIn('Ctrl+Shift+Alt+L', labels)


class EmergencyDispatchTests(unittest.TestCase):
    def test_emergency_routes_to_priority_stop(self):
        import types
        from volc_dialog import VolcDuplex

        calls = []

        class Drone:
            def stop_motion(self):
                calls.append('stop_motion')
                return {'ok': True, 'message': '已停止'}

            def land(self):
                calls.append('land')
                return {'ok': True, 'message': '已降落'}

        runner = types.SimpleNamespace(demo=types.SimpleNamespace(drone=Drone()))
        dialog = VolcDuplex(runner, None, instructions='', api_key='k')
        asyncio.run(dialog.emergency(False))
        asyncio.run(dialog.emergency(True))
        self.assertEqual(calls, ['stop_motion', 'land'])


if __name__ == '__main__':
    unittest.main()
