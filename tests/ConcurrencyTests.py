from simulation_test_support import settle_landing, settle_workers
"""并发安全回归测试：阻塞动作进行中，急停接口必须毫秒级返回。

背景：真机上 move_forward / takeoff 会阻塞数秒。若急停路径和动作路径共用
一把锁，停止与降落就要等动作结束才发得出去。

本测试用 _driver 的模拟延时把动作变成真阻塞，验证 2组 SDK 不会复现该问题。

运行：
    uv run python -m pytest tests/ConcurrencyTests.py -v
"""

import threading
import time
import unittest

from drone_sdk import controller, _driver

STOP_LATENCY_LIMIT = 0.05     # 急停接口必须在 50ms 内返回
ACTION_BLOCK_SECONDS = 2.0    # 模拟真机阻塞动作的耗时


class BlockingActionConcurrencyTest(unittest.TestCase):
    def setUp(self):
        controller.release_control("model")
        controller.stop_follow()
        _driver.set_simulated_delay(0)
        self.assertTrue(controller.request_control("model").ok)
        controller.land('model')
        settle_landing(controller)
        self.assertTrue(controller.takeoff("model").ok)

    def tearDown(self):
        _driver.set_simulated_delay(0)
        controller.land("model")
        settle_landing(controller)
        controller.release_control("model")

    def _start_blocking_move(self):
        """在独立线程里发起一个会阻塞 ACTION_BLOCK_SECONDS 秒的前进动作。"""
        _driver.set_simulated_delay(ACTION_BLOCK_SECONDS)
        started = threading.Event()
        outcome = {}

        def worker():
            started.set()
            begin = time.perf_counter()
            outcome["result"] = controller.move_forward("model", 30)
            outcome["wall"] = time.perf_counter() - begin

        thread = threading.Thread(target=worker, name="blocking-move", daemon=True)
        thread.start()
        self.assertTrue(started.wait(1.0), "动作线程未能启动")
        time.sleep(0.1)  # 确认动作已经进入驱动层阻塞
        return thread, outcome

    def _assert_emergency_is_fast(self, name, call):
        begin = time.perf_counter()
        result = call()
        elapsed = time.perf_counter() - begin
        print("      %s 返回耗时 %.1f ms" % (name, elapsed * 1000))
        self.assertTrue(result.ok, name + " 应返回成功")
        self.assertLess(
            elapsed, STOP_LATENCY_LIMIT,
            "%s 被动作阻塞了 %.1f ms（上限 %.0f ms）"
            % (name, elapsed * 1000, STOP_LATENCY_LIMIT * 1000))
        return result

    def test_stop_motion_during_blocking_move(self):
        """2 秒动作进行中调用 stop_motion，必须 50ms 内返回成功。"""
        thread, outcome = self._start_blocking_move()
        try:
            self.assertTrue(thread.is_alive(), "前提不成立：动作应仍在阻塞中")
            self._assert_emergency_is_fast(
                "stop_motion", lambda: controller.stop_motion("model"))
            self.assertTrue(thread.is_alive(), "急停不应等待动作结束")
        finally:
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "动作线程未在 5 秒内结束")
        self.assertFalse(outcome["result"].ok)
        self.assertEqual(outcome["result"].reason,"action_cancelled")
        self.assertGreaterEqual(
            outcome["wall"], ACTION_BLOCK_SECONDS, "动作应确实阻塞过，否则测试无意义")

    def test_land_during_blocking_move(self):
        """2 秒动作进行中调用 land，必须 50ms 内返回成功且状态落地。"""
        thread, outcome = self._start_blocking_move()
        try:
            self.assertTrue(thread.is_alive(), "前提不成立：动作应仍在阻塞中")
            self._assert_emergency_is_fast("land", lambda: controller.land("model"))
            self.assertTrue(thread.is_alive(), "降落指令不应等待动作结束")
        finally:
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "动作线程未在 5 秒内结束")
        self.assertGreaterEqual(outcome["wall"], ACTION_BLOCK_SECONDS)
        self.assertEqual(controller.get_status().flight_state, "landed")

    def test_emergency_still_works_after_blocking_action(self):
        """阻塞动作结束后急停仍可用，防止改成死锁。"""
        thread, outcome = self._start_blocking_move()
        thread.join(timeout=5)
        self.assertTrue(outcome["result"].ok)
        self.assertEqual(controller.get_status().flight_state, "flying")
        self.assertTrue(controller.stop_motion("model").ok)
        self.assertEqual(controller.get_status().flight_state, "flying")
        self.assertTrue(controller.land("model").ok)
        settle_landing(controller)
        self.assertEqual(controller.get_status().flight_state, "landed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
