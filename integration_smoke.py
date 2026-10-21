"""集成冒烟测试：4组 AI 框架（modules/ai） ↔ 2组统一 SDK（drone_sdk）。

全部为模拟，不连接任何无人机。4组代码不做任何修改。

运行：
    .venv\\Scripts\\python.exe integration_smoke.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AI_DIR = ROOT / "modules" / "ai"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(AI_DIR))

from drone_sdk.ai_bridge import UnifiedDrone  # noqa: E402
import program_runner  # noqa: E402
from demo_text import Demo  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  [PASS] " if condition else "  [FAIL] ") + name + ("" if condition else " -> " + str(detail)))


def reset(drone):
    drone.release_control("model")
    drone.release_control("follow")
    drone.stop_follow()


def main():
    print("=== 1. 桥接层直接调用 ===")
    drone = UnifiedDrone()
    status = drone.get_status()
    check("get_status 返回 dict 且字段齐全",
          isinstance(status, dict) and {"battery", "flying", "owner", "following", "timestamp"} <= set(status),
          status)
    check("get_battery 可用", isinstance(drone.get_battery(), int), drone.get_battery())
    check("起飞前 request_control 成功", drone.request_control("model")["ok"])
    check("控制权被占用时返回 ok=False",
          drone.request_control("follow")["ok"] is False)
    check("占用时提示带 owner",
          "model" in drone.request_control("follow")["message"])
    check("未起飞时 move_forward 被拒",
          drone.move_forward("model", 30)["ok"] is False)

    print("\n=== 2. 4组 Runner 的类型检查（已知障碍） ===")
    try:
        program_runner.Runner(Demo(drone=UnifiedDrone()))
        check("未打补丁时 Runner 应拒绝非 MockDrone", False, "竟然通过了")
    except ValueError as exc:
        check("未打补丁时 Runner 明确拒绝（需4组改1行）", "仅允许本地模拟器" in str(exc), exc)

    original = program_runner.MockDrone
    program_runner.MockDrone = UnifiedDrone  # 名字重绑定：零改动接入
    try:
        print("\n=== 3. 走通 4组的完整调用链（Runner -> Demo -> UnifiedDrone -> controller） ===")
        drone = UnifiedDrone()
        runner = program_runner.Runner(Demo(drone=drone))

        r = runner.run("drone.get_battery()")
        check("get_battery 程序被接受", r["accepted"] and "电量" in r["result"], r)
        check("推演状态正确（未起飞）", r["state"]["flying"] is False, r.get("state"))

        r = runner.run("drone.takeoff()")
        check("takeoff 被接受且动作完成", r["accepted"] and r["state"]["flying"] is True, r)

        r = runner.run("drone.move_forward(30)")
        check("move_forward(30) 成功", r["accepted"] and "前进" in r["result"], r)

        r = runner.run("drone.move_forward(50)")
        check("非白名单参数被拦截", r["accepted"] is False, r)

        r = runner.run("drone.stop_motion()")
        check("stop_motion 成功", r["accepted"] and "紧急悬停" in r["result"], r)

        r = runner.run("drone.land()")
        check("land 成功且状态回到地面", r["accepted"] and r["state"]["flying"] is False, r)

        r = runner.run("drone.import os")
        check("非法语法被拦截", r["accepted"] is False, r)

        print("\n=== 4. 跟随接口（含控制权仲裁） ===")
        drone.request_control("model")
        blocked = drone.start_follow()
        check("model 占用时 start_follow 被拒", blocked["ok"] is False, blocked)
        drone.release_control("model")

        started = drone.start_follow()
        check("空闲时 start_follow 成功", started["ok"] is True, started)
        check("start_follow 后 owner=follow", drone.get_status()["owner"] == "follow", drone.get_status())
        check("is_following 为真", drone.is_following() is True)

        stopped = drone.stop_follow()
        check("stop_follow 成功", stopped["ok"] is True, stopped)
        check("停止后 owner 被释放", drone.get_status()["owner"] is None, drone.get_status())

        print("\n=== 5. 走通 4组的 AI 全链路（Agent + 离线脚本模型） ===")
        from ai_agent import Agent
        from demo_ai import ScriptedClient

        drone = UnifiedDrone()
        runner = program_runner.Runner(Demo(drone=drone))
        agent = Agent(ScriptedClient(), runner=runner)

        record = agent.handle("还有多少电")
        check("Agent 识别电量意图", record["plan"].get("kind") == "program", record.get("plan"))

        record = agent.handle("起飞")
        check("Agent 执行起飞", record["execution"]["accepted"] is True and drone.get_status()["flying"] is True, record)

        record = agent.handle("向前走30厘米")
        check("Agent 执行前进", record["execution"]["accepted"] is True, record)

        record = agent.handle("降落")
        check("Agent 执行降落", drone.get_status()["flying"] is False, record)

        record = agent.handle("随便说点什么")
        check("非动作请求不触发动作", record["execution"]["accepted"] is False, record)
    finally:
        program_runner.MockDrone = original

    print("\n" + "=" * 52)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  失败：" + name)
    print("=" * 52)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
