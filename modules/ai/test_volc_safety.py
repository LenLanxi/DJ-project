"""豆包全双工的安全与工具行为：不 await 协程、无 Docker 时的状态查询、回音抑制。

实测踩到的两个坑：
1. SandboxRunner.run 是 async，旧代码 `await asyncio.to_thread(self.runner.run, code)`
   只拿到未 await 的协程 → 工具结果永远为空 + RuntimeWarning: coroutine was never awaited
2. 用外放时扬声器的声音被麦克风收回去，ASR 变成"你现在你现在你现在..."（严重回音）
"""
import asyncio
import time
import unittest

from volc_dialog import VolcDuplex


class FakeBackend:
    simulated = True

    def __init__(self):
        self.emergency_calls = []
        self.status_reads = 0

    def get_status(self):
        self.status_reads += 1
        return {'battery': 77, 'height_cm': 0, 'flying': False, 'connected': True}

    def emergency(self, land):
        self.emergency_calls.append(land)
        return {'ok': True, 'message': '零速度悬停' if not land else '降落已下发'}


class FakeGateway:
    def __init__(self):
        self.backend = FakeBackend()


class AsyncRunner:
    """模仿 SandboxRunner：run 是协程函数，且带 gateway.backend。"""

    def __init__(self):
        self.gateway = FakeGateway()
        self.codes = []
        self.cancelled = 0

    async def run(self, code):
        self.codes.append(code)
        return {'ok': True, 'status': 'completed', 'result': 'done'}

    def cancel(self):
        self.cancelled += 1


def make_dialog(runner=None):
    return VolcDuplex(runner or AsyncRunner(), audio=None, instructions='', api_key='test-key')


class ToolDispatchTests(unittest.TestCase):
    def test_run_program_awaits_async_runner(self):
        runner = AsyncRunner()
        dialog = make_dialog(runner)
        result = asyncio.run(dialog._dispatch('run_program', {'code': 'drone.get_status()'}))
        self.assertEqual(runner.codes, ['drone.get_status()'])
        self.assertEqual(result['status'], 'completed')
        self.assertNotIn('coroutine', str(type(result)))

    def test_get_status_reads_backend_without_docker(self):
        runner = AsyncRunner()
        dialog = make_dialog(runner)
        result = asyncio.run(dialog._dispatch('get_status', {}))
        self.assertTrue(result['ok'])
        self.assertEqual(result['state']['battery'], 77)
        self.assertEqual(runner.gateway.backend.status_reads, 1)
        self.assertEqual(runner.codes, [])          # 没有走沙箱

    def test_interrupt_uses_priority_emergency(self):
        runner = AsyncRunner()
        dialog = make_dialog(runner)
        result = asyncio.run(dialog._dispatch('interrupt', {'land': True}))
        self.assertTrue(result['ok'])
        self.assertEqual(runner.gateway.backend.emergency_calls, [True])
        self.assertEqual(runner.cancelled, 1)

    def test_unknown_tool_is_rejected(self):
        dialog = make_dialog()
        with self.assertRaises(ValueError):
            asyncio.run(dialog._dispatch('takeoff_everything', {}))

    def test_empty_code_is_rejected(self):
        dialog = make_dialog()
        with self.assertRaises(ValueError):
            asyncio.run(dialog._dispatch('run_program', {'code': '   '}))


class EchoGateTests(unittest.TestCase):
    def test_mic_is_gated_while_speaking(self):
        dialog = make_dialog()
        dialog.speaking = True
        dialog.speaking_since = time.monotonic()
        self.assertTrue(dialog._input_gated())

    def test_mic_resumes_after_audio_done(self):
        dialog = make_dialog()
        dialog.speaking = False
        self.assertFalse(dialog._input_gated())

    def test_barge_in_flag_disables_gating(self):
        dialog = make_dialog()
        dialog.speaking = True
        dialog.speaking_since = time.monotonic()
        dialog.barge_in = True
        self.assertFalse(dialog._input_gated())

    def test_gate_times_out_so_mic_never_stays_muted(self):
        dialog = make_dialog()
        dialog.speaking = True
        dialog.speaking_since = time.monotonic() - 31
        self.assertFalse(dialog._input_gated())
        self.assertFalse(dialog.speaking)


if __name__ == '__main__':
    unittest.main()
