from simulation_test_support import settle_landing, settle_workers
"""终极跨组集成测试：2组统一 SDK + 3组跟随 + 4组 AI 交互。

真实整机场景（全部为模拟，不连硬件）：
  1. 3组 开启跟随，合成画面持续产生标记偏移 → 飞机持续运动
  2. 4组 收到"停止跟随"，调用 stop_follow() 暂停跟随
  3. 4组 request_control('model') 后执行 move_forward(50)
  4. 4组 执行完毕释放控制权，3组 重新申请控制权并恢复跟随
  5. 断言全程控制权交接顺滑、无死锁、无后台线程泄漏

运行：
    .venv\\Scripts\\python.exe test_full_integration.py
"""

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "modules" / "follow"))

from drone_sdk import controller                       # noqa: E402
from drone_sdk.ai_bridge import UnifiedDrone           # noqa: E402
from drone_sdk.contract import FrameResult             # noqa: E402
from r3_follow.vision import synthetic_frame           # noqa: E402

SCENARIO_TIMEOUT = 20.0


def wait_until(predicate, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class SyntheticFeed:
    """固定编号 0 的标记图；每调用一次产生一帧新画面。"""

    def __init__(self, position=0.5, visible=True):
        self.position = position
        self.visible = visible
        self._frame_id = 0
        self._cache = {}

    def __call__(self):
        self._frame_id += 1
        key = (round(self.position, 3), self.visible)
        frame = self._cache.get(key)
        if frame is None:
            frame = synthetic_frame(x=self.position, visible=self.visible)
            self._cache[key] = frame
        return FrameResult(frame=frame, frame_id=self._frame_id,
                           timestamp=time.time(), stale=False)


def follow_threads():
    return [t.name for t in threading.enumerate() if t.name.startswith("follow-")]


class FullIntegrationTest(unittest.TestCase):
    def setUp(self):
        controller.stop_follow()
        controller.set_frame_provider(None)
        owner = controller.get_status().control_owner
        if owner not in ("none", None):
            controller.release_control(owner)
        self.assertTrue(controller.request_control("model").ok)
        controller.land("model")
        settle_landing(controller)
        self.assertTrue(controller.takeoff("model").ok)
        controller.release_control("model")
        self.feed = SyntheticFeed()
        controller.set_frame_provider(self.feed)

    def tearDown(self):
        controller.stop_follow()
        controller.set_frame_provider(None)
        owner = controller.get_status().control_owner
        if owner not in ("none", None):
            controller.land(owner)
            settle_landing(controller)
            controller.release_control(owner)

    # ---------- 辅助 ----------
    def _velocity_events(self):
        return [e for e in controller._follow.events() if e["event"] == "velocity_call"]

    def _run_with_deadlock_guard(self, scenario):
        outcome = {}

        def runner():
            try:
                scenario()
            except BaseException as exc:   # noqa: BLE001 - 保留原始断言失败
                outcome["error"] = exc

        thread = threading.Thread(target=runner, name="integration-scenario", daemon=True)
        start = time.monotonic()
        thread.start()
        thread.join(SCENARIO_TIMEOUT)
        elapsed = time.monotonic() - start
        self.assertFalse(thread.is_alive(),
                         "场景超过 %.0f 秒仍未结束，疑似死锁" % SCENARIO_TIMEOUT)
        if "error" in outcome:
            raise outcome["error"]
        return elapsed

    # ---------- 场景 ----------
    def _scenario(self):
        # 1. 3组 开启跟随
        started = controller.start_follow()
        self.assertTrue(started.ok, "start_follow 失败: %s" % started.reason)
        self.assertEqual(controller.get_status().control_owner, "follow")
        self.assertTrue(controller.is_following())
        self.assertTrue(wait_until(lambda: bool(follow_threads()), 1.0),
                        "3组 后台线程未启动")

        # 标记偏离中心 → 飞机持续运动（非零速度指令）
        self.feed.position = 0.80
        self.assertTrue(wait_until(
            lambda: any(e["channels"] != (0, 0, 0, 0) for e in self._velocity_events()), 3.0),
            "跟随未产生任何非零速度指令")

        # 2. 4组 收到"停止跟随"
        ai = UnifiedDrone()
        stopped = ai.stop_follow()
        self.assertTrue(stopped["ok"], "stop_follow 失败: %s" % stopped["message"])
        self.assertEqual(controller.get_status().control_owner, "none")
        self.assertFalse(controller.is_following())
        self.assertTrue(wait_until(lambda: not follow_threads(), 2.0),
                        "跟随后台线程未退出: %s" % follow_threads())
        zeros = [e for e in self._velocity_events() if e["channels"] == (0, 0, 0, 0)]
        self.assertTrue(zeros, "停止跟随时应发出 0 速度悬停")

        # 3. 4组 接管并执行动作
        taken = ai.request_control("model")
        self.assertTrue(taken["ok"], "4组 接管失败: %s" % taken["message"])
        self.assertEqual(controller.get_status().control_owner, "model")
        moved = controller.move_forward("model", 50)
        self.assertTrue(moved.ok, "move_forward 失败: %s" % moved.reason)
        self.assertEqual(moved.status, "completed")

        # 4. 4组 释放，3组 恢复跟随
        released = ai.release_control("model")
        self.assertTrue(released["ok"])
        self.assertEqual(controller.get_status().control_owner, "none")

        self.feed.position = 0.5
        resumed = controller.start_follow()
        self.assertTrue(resumed.ok, "恢复跟随失败: %s" % resumed.reason)
        self.assertTrue(controller.is_following())
        self.assertEqual(controller.get_status().control_owner, "follow")

    def test_full_follow_ai_handover_without_deadlock(self):
        elapsed = self._run_with_deadlock_guard(self._scenario)
        print("      全场景耗时 %.2f 秒" % elapsed)
        self.assertLess(elapsed, SCENARIO_TIMEOUT)

    def test_follow_requires_frame_source(self):
        """没有画面来源时必须快速失败，不能假装已开始跟随。"""
        controller.set_frame_provider(None)
        result = controller.start_follow()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "follow_no_frame_source")
        self.assertEqual(controller.get_status().control_owner, "none")
        self.assertFalse(controller.is_following())


if __name__ == "__main__":
    unittest.main(verbosity=2)
