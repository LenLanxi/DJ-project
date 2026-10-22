"""Generated-image integration checks. No UI, webcam, drone or networking."""
import json
from pathlib import Path
from unittest.mock import patch

from r3_follow.mock_port import MockPort
from r3_follow.service import FollowService
from r3_follow.vision import synthetic_frame


def run(output_dir=None):
    now = [10.0]
    port = MockPort(lambda: now[0])
    service = FollowService(port, monotonic=lambda: now[0],
                            wall_clock=lambda: 1800000000 + now[0])
    seq, checks = 0, []

    def frame(**kwargs):
        nonlocal seq
        now[0] += .1
        seq += 1
        port.publish(synthetic_frame(**kwargs), seq, 1800000000 + now[0])
        assert service.capture_once()
        service.control_once()

    def check(name, condition):
        assert condition, name
        checks.append(dict(name=name, passed=True, status=service.get_follow_status(),
                           simulated_channels=list(port.velocity)))

    try:
        with patch("socket.socket", side_effect=AssertionError("Networking forbidden")):
            frame()
            assert service.calibrate_reference().ok
            assert service.start_follow().ok
            frame(x=.8)
            check("真实识别生成标记并输出右移请求", port.velocity[0] > 0)

            result, ticket = service.pause_for_action()
            check("交接前暂停归零并释放控制权", result.ok and port.lease is None
                  and port.velocity == (0, 0, 0, 0))
            model_lease = port.request_control("model").lease
            frame(x=.2)
            accepted = service.finish_action(ticket, "accepted")
            check("仅收到accepted时不恢复跟随", accepted.status == "accepted"
                  and not service.is_following())
            port.release_control(model_lease)
            result = service.finish_action(ticket, "completed")
            check("完成动作后重新检查目标并恢复", result.ok and port.velocity[0] < 0)

            frame(visible=False)
            check("目标消失后归零", port.velocity == (0, 0, 0, 0)
                  and not service.is_following())
            frame(x=.8)
            check("默认模式目标重现不擅自重启", not service.is_following())
            assert service.start_follow().ok
            now[0] += .6
            service.capture_once()  # repeated snapshot, not a fresh image
            service.control_once()
            check("重复旧帧不能延长有效时间", port.velocity == (0, 0, 0, 0)
                  and service.get_follow_status()["state"] == "frame_stale")

            frame(x=.8)
            assert service.start_follow().ok
            _, ticket = service.pause_for_action()
            service.stop_follow("explicit_stop")
            frame(x=.2)
            result = service.finish_action(ticket, "completed")
            check("明确停止后晚到的动作完成通知不重启", not result.ok
                  and not service.is_following())

            assert service.start_follow().ok
            other = port.revoke_for_test("model")
            port.update_velocity(other, left_right=4, forward_back=0, up_down=0, yaw=0)
            service.control_once()
            check("控制权被接管后三组停止发送且不干扰新控制者", not service.is_following()
                  and port.velocity == (4, 0, 0, 0))
            port.release_control(other)
            service.close()
            check("关闭后不能重新启动", not service.start_follow().ok)
    finally:
        service.close()

    report = dict(component="R3 headless service preview 1", checks_passed=len(checks),
                  input="generated_ArUco_images", simulation_only=True,
                  hardware_verified=False, team2_repository_tested=False, checks=checks)
    destination = Path(output_dir) if output_dir else Path(__file__).parent / "logs" / "service_preview"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    records = [dict(component="service", **e) for e in service.events]
    records += [dict(component="mock_port", **e) for e in port.events]
    records.sort(key=lambda e: e["time"])
    (destination / "events.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in records), encoding="utf-8")
    summary = "三组后台跟随模块：生成图片离线验证\n\n" + "\n".join(
        "通过：" + entry["name"] for entry in checks)
    summary += "\n\n这是本地模拟验证，未测试二组仓库、无人机视频、实际飞行和实机中断。\n"
    (destination / "测试结果.txt").write_text(summary, encoding="utf-8-sig")
    print(f"PASS {len(checks)} generated-image scenarios; no UI/camera/drone/network.\n{destination}")
    return report


if __name__ == "__main__":
    run()
