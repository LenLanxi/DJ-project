from simulation_test_support import settle_landing, settle_workers
"""4组 session.py 的急停延迟测试（在 2组 真阻塞驱动下）。

test_concurrency.py 验证的是 2组 SDK 自身不会被动作卡住；
本文件验证的是 4组 的 Session.interrupt() 在真实阻塞动作进行中能否毫秒级返回。
两者缺一不可：任何一层持锁，急停都会晚于动作结束。

运行：
    .venv\\Scripts\\python.exe test_session_interrupt.py
"""

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AI_DIR = ROOT / "modules" / "ai"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(AI_DIR))

from drone_sdk import controller, _driver            # noqa: E402
from drone_sdk.ai_bridge import UnifiedDrone         # noqa: E402
import program_runner                                # noqa: E402
from demo_text import Demo                           # noqa: E402
from ai_agent import Agent                           # noqa: E402
from session import Session                          # noqa: E402

STOP_LATENCY_LIMIT = 0.05
ACTION_BLOCK_SECONDS = 2.0


class MoveClient:
    """替代模型：固定返回一个前进程序。"""

    last_usage = {}

    def complete(self, messages):
        return '{"kind":"program","code":"drone.move_forward(30)"}'


class SessionInterruptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_mock = program_runner.MockDrone
        program_runner.MockDrone = UnifiedDrone   # 让 4组 Runner 接受统一 SDK

    @classmethod
    def tearDownClass(cls):
        program_runner.MockDrone = cls._original_mock

    def setUp(self):
        _driver.set_simulated_delay(0)
        controller.stop_follow()
        owner = controller.get_status().control_owner
        if owner not in ("none", None):
            controller.release_control(owner)
        self.assertTrue(controller.request_control("model").ok)
        controller.land("model")
        settle_landing(controller)
        deadline = time.monotonic() + 1
        while UnifiedDrone().get_status().get('landing_pending', False):
            self.assertLess(time.monotonic(), deadline, 'Prior landing not confirmed')
            time.sleep(.002)
        self.assertTrue(controller.request_control('model').ok)
        self.assertTrue(controller.takeoff("model").ok)
        controller.release_control("model")
        self.sessions = []

    def tearDown(self):
        _driver.set_simulated_delay(0)
        for session in self.sessions:
            try:
                session.close()
            except Exception:
                pass
        owner = controller.get_status().control_owner
        if owner not in ("none", None):
            controller.land(owner)
            settle_landing(controller)
            controller.release_control(owner)

    def _make_session(self):
        drone = UnifiedDrone()
        agent = Agent(MoveClient(), runner=program_runner.Runner(Demo(drone=drone)))
        session = Session(agent)
        self.sessions.append(session)
        return session

    def test_interrupt_does_not_wait_for_blocking_action(self):
        session = self._make_session()
        _driver.set_simulated_delay(ACTION_BLOCK_SECONDS)

        turn = session.submit("向前走30厘米")
        time.sleep(0.15)          # 让动作真正进入驱动层阻塞
        self.assertTrue(session.busy, "前提不成立：动作应仍在执行")

        begin = time.perf_counter()
        stopped = session.interrupt(False)
        elapsed = time.perf_counter() - begin
        print("      interrupt() 返回耗时 %.1f ms" % (elapsed * 1000))

        self.assertLess(elapsed, STOP_LATENCY_LIMIT,
                        "interrupt() 被阻塞动作卡住 %.1f ms" % (elapsed * 1000))
        done = stopped.result(timeout=2.0)
        self.assertTrue(done.get("accepted") is True, done)
        self.assertIn("请求已发送", done.get("result", ""), done)
        self.assertIn("尚未确认", done.get("result", ""), done)

        turn.result(timeout=ACTION_BLOCK_SECONDS + 5.0)   # 动作最终返回，不泄漏线程
        self.assertFalse(session.busy)

    def test_late_program_cancelled_after_interrupt(self):
        """模型还在返回途中就被急停：迟到的程序不得执行任何动作。"""
        class SlowClient:
            last_usage = {}

            def complete(self, messages):
                time.sleep(0.5)
                return '{"kind":"program","code":"drone.move_forward(30)"}'

        agent = Agent(SlowClient(), runner=program_runner.Runner(Demo(drone=UnifiedDrone())))
        session = Session(agent)
        self.sessions.append(session)

        turn = session.submit("向前走30厘米")
        time.sleep(0.1)                      # 此时模型尚未返回
        session.interrupt(False).result(timeout=2.0)
        record = turn.result(timeout=5.0)
        self.assertTrue(record.get("cancelled") is True, record)
        self.assertFalse(record.get("execution", {}).get("accepted", False), record)

    def test_emergency_path_releases_follow_control(self):
        """跟随时急停：必须先停跟随释放控制权，否则抢不到 model。"""
        session = self._make_session()

        controller.set_frame_provider(None)
        started = controller.start_follow()
        self.assertFalse(started.ok)          # 无画面来源时不会假跟随
        self.assertEqual(controller.get_status().control_owner, "none")

        result = session.interrupt(True).result(timeout=2.0)
        self.assertTrue(result["accepted"], result)

    def test_no_deadlock_when_interrupt_and_turn_race(self):
        session = self._make_session()
        _driver.set_simulated_delay(1.0)
        for _ in range(5):
            turn = session.submit("向前走30厘米")
            time.sleep(0.02)
            session.interrupt(False).result(timeout=2.0)
            turn.result(timeout=5.0)
        self.assertFalse(session.busy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
