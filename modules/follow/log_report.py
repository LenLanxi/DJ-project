"""Read existing local metadata logs; no camera, device, network or third-party imports."""
import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import statistics

BASE = Path(__file__).resolve().parent
REASONS = {
    "not_found": "未找到指定标记（仅凭日志不能区分遮挡、模糊、倾斜或距离）",
    "ambiguous_duplicate_id": "出现多个相同编号，无法确定跟随对象",
    "frame_gap": "新画面间隔超过控制器允许值",
    "frame_stale": "有效画面已过期",
    "invalid_input": "输入画面或识别结果无效",
    "recovery_timeout_requires_start": "等待恢复超时，需要手动开始",
    "target_lost": "目标丢失，需要手动开始",
}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def read_rows(path):
    rows, issues = [], []
    # Reading a snapshot also supports a still-open, flushed session.
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("not an object")
            rows.append(row)
        except (ValueError, TypeError):
            issues.append(f"第{line_no}行不完整或不是JSON对象，已跳过")
    return rows, issues


def frame_stats(rows, end=None):
    """Rows already deduplicated. Durations use sampling times, not guessed frame rates."""
    found = sum(r["found"] for r in rows)
    runs, start = [], None
    for row in rows:
        if not row["found"] and start is None:
            start = row["t"]
        elif row["found"] and start is not None:
            runs.append({"seconds": max(0, row["t"] - start), "ended": True})
            start = None
    if start is not None:
        boundary = end if end is not None else rows[-1]["t"]
        runs.append({"seconds": max(0, boundary - start), "ended": False})
    sizes = [r["size"] for r in rows if r["found"] and number(r.get("size"))]
    return {
        "processed_frames": len(rows), "found_frames": found, "missed_frames": len(rows)-found,
        "detection_rate": found/len(rows) if rows else None,
        "miss_runs": len(runs), "longest_miss_seconds": max((r["seconds"] for r in runs), default=0),
        "open_miss_run": start is not None, "miss_durations": runs,
        "miss_reasons": dict(Counter(r["reason"] for r in rows if not r["found"])),
        "successful_methods": dict(Counter(r["details"].get("detector_method", "unknown")
                                          for r in rows if r["found"])),
        "other_id_frames": sum(bool(r["details"].get("ignored_other_markers")) for r in rows),
        "successful_size_median_px": statistics.median(sizes) if sizes else None,
        "max_sample_gap_seconds": max((b["t"]-a["t"] for a, b in zip(rows, rows[1:])), default=0),
    }


def normalized_frames(rows, diagnostic=False):
    result, seen, issues = [], set(), []
    duplicates, invalid = 0, 0
    last_t = None
    for row in rows:
        if not diagnostic and row.get("event") != "frame":
            continue  # tick and state_transition repeat an observation; never count as new frames
        obs = row if diagnostic else row.get("frame_diagnostic") or row.get("observation") or {}
        seq = obs.get("frame_id")
        t = row.get("elapsed_seconds" if diagnostic else "time_monotonic")
        if not number(t) or not isinstance(seq, int) or type(obs.get("found")) is not bool:
            invalid += 1
            continue
        if seq in seen:
            duplicates += 1
            continue
        if last_t is not None and t < last_t:
            invalid += 1
            continue
        seen.add(seq)
        last_t = t
        details = obs.get("detector_details") or row.get("detector_details") or {}
        size = obs.get("size_px")
        if size is None and number(obs.get("size_ratio")) and number(obs.get("width")):
            size = obs["size_ratio"] * obs["width"]
        result.append({"t": t, "id": seq, "found": obs["found"], "size": size,
                       "reason": obs.get("reason") or ("detected" if obs["found"] else "not_found"),
                       "details": details, "phase": row.get("phase"),
                       "state": row.get("control_state") if diagnostic else
                       (row.get("request") or {}).get("state")})
    if duplicates:
        issues.append(f"跳过{duplicates}条重复帧编号；识别率按唯一处理帧统计")
    if invalid:
        issues.append(f"{invalid}条帧记录缺字段或时间倒退，未计入识别率")
    return result, issues


def diagnostic_report(path):
    rows, issues = read_rows(path)
    frames, frame_issues = normalized_frames(rows, True)
    completion = "unknown"
    text_path = path.with_suffix(".txt")
    if text_path.exists():
        header = text_path.read_text(encoding="utf-8-sig").splitlines()[:2]
        if "提前结束" in header:
            completion = "aborted"
        elif "测试结束" in header:
            completion = "completed"
    context = next((r.get("test_context") for r in rows if r.get("test_context")), {})
    phases = {}
    for name, boundary in (("站立不动", 10), ("缓慢左右移动", 20)):
        part = [r for r in frames if r["phase"] == name]
        phases[name] = frame_stats(part, boundary if completion == "completed" else None)
    return {"file": path.name, "completion": completion, "context": context,
            "phases": phases, "warnings": issues + frame_issues}


def session_report(path):
    path = Path(path)
    rows, issues = read_rows(path)
    if not any("event" in r for r in rows):
        raise ValueError("请选择demo_开头的会话日志；短测日志会随所属会话一起汇总。")
    frames, frame_issues = normalized_frames(rows)
    transitions = [r for r in rows if r.get("event") == "state_transition"]
    # One transition per timestamp/state tuple; do not use repeated last_stop_reason.
    unique = {}
    for r in transitions:
        key = (r.get("time_monotonic"), r.get("previous_state"), (r.get("request") or {}).get("state"))
        unique.setdefault(key, r)
    transitions = list(unique.values())
    recoveries = sum(r.get("previous_state") == "recovering_target" and
                     (r.get("request") or {}).get("state") == "tracking" for r in transitions)
    stops = Counter()
    for r in transitions:
        request = r.get("request") or {}
        if request.get("state") in ("target_lost", "frame_stale", "invalid_input"):
            stops[r.get("last_stop_reason") or request.get("reason") or request["state"]] += 1
    violation_rows = []
    for r in rows:
        if r.get("event") == "state_transition":
            continue
        request = r.get("request") or {}
        if request.get("state") and request["state"] != "tracking" and any(
                number(request.get(k)) and request[k] != 0 for k in ("right", "forward")):
            violation_rows.append(r.get("time_monotonic"))
    context = next((r for r in rows if r.get("event") == "session_start"), rows[0] if rows else {})
    auto = context.get("automated_gui_check")
    sidecar = path.with_suffix(".summary.json")
    if auto is None and sidecar.exists():
        try:
            auto = json.loads(sidecar.read_text(encoding="utf-8-sig")).get("automated_gui_check")
        except (ValueError, OSError):
            issues.append("会话附属摘要不可读取，自动测试标记未知")
    children = set()
    for r in rows:
        name = r.get("diagnostic_log")
        if r.get("event") in ("short_test_completed", "short_test_aborted") and r.get("report"):
            name = str(Path(r["report"]).with_suffix(".jsonl"))
        if isinstance(name, str) and Path(name).name == name and name.startswith("diagnostic_"):
            children.add(name)
    diagnostics = []
    for name in sorted(children):
        p = path.parent / name
        if not p.exists():
            issues.append(f"关联短测日志缺失：{name}")
            continue
        try:
            child = diagnostic_report(p)
            owner = child["context"].get("session_log")
            if owner and owner != path.name:
                issues.append(f"短测所属会话不一致，未合并：{name}")
                continue
            diagnostics.append(child)
        except (OSError, ValueError) as exc:
            issues.append(f"短测读取失败：{name}（{type(exc).__name__}）")
    active = [r for r in frames if r["state"] in ("tracking", "recovering_target")]
    # Filtered active frames can straddle stopped intervals: rates only, not gap/run durations.
    active_found = sum(r["found"] for r in active)
    return {"schema_version": 1, "file": path.name, "version": context.get("app_version", "未知"),
            "source": context.get("source", "未知"), "automated_gui_check": auto,
            "session_closed": any(r.get("event") == "session_summary" for r in rows),
            "all_frames": frame_stats(frames),
            "active_frames": {"processed_frames": len(active), "found_frames": active_found,
                              "detection_rate": active_found/len(active) if active else None},
            "automatic_recoveries": recoveries if transitions else None,
            "stop_events": dict(stops) if transitions else None,
            "nontracking_nonzero_log_records": len(violation_rows),
            "violation_times": violation_rows,
            "short_tests": diagnostics, "warnings": issues + frame_issues +
            ([] if transitions else ["没有状态切换事件，无法可靠统计恢复或异常停止次数"])}


def stats_line(stats):
    n = stats["processed_frames"]
    if not n:
        return "没有可用处理帧，无法判断。"
    return (f"识别到 {stats['found_frames']}/{n} 帧（{stats['detection_rate']:.1%}），"
            f"漏检 {stats['missed_frames']} 帧；漏检 {stats['miss_runs']} 段，"
            f"最长观测约 {stats['longest_miss_seconds']:.2f} 秒。")


def render(report):
    lines = ["三组测试结果自动汇总",
             f"原始日志：{report['file']}",
             f"版本：{report['version']}；输入：{report['source']}",
             "范围：整次窗口会话；不等于某一段录屏。只分析本地记录，未控制无人机。",
             "状态：" + ("已正常结束" if report["session_closed"] else "尚未正常结束，仅为当前记录快照"),
             "自动界面测试：" + {True: "是，不当作真人摄像头实验", False: "否", None: "旧日志未明确记录"}[report["automated_gui_check"]],
             "", "一、20秒短测（与整次窗口分开）"]
    if not report["short_tests"]:
        lines.append("未找到关联短测记录；不能当作完成了20秒短测。")
    for short in report["short_tests"]:
        status = {"completed": "已完成计时", "aborted": "提前结束，未完成", "unknown": "完成状态未知"}
        lines.append(f"{short['file']}：{status[short['completion']]}")
        for phase, stat in short["phases"].items():
            lines.append(f"  {phase}：{stats_line(stat)}")
            if stat["successful_size_median_px"] is not None:
                lines.append(f"    成功帧标记边长中位数 {stat['successful_size_median_px']:.1f} 像素")
        lines.extend("  注意：" + w for w in short["warnings"])
    lines += ["", "二、整次窗口统计（包含准备、暂停及收起标记）", stats_line(report["all_frames"])]
    if report["all_frames"]["open_miss_run"]:
        lines.append("末尾还有未结束的漏检段：只统计到最后一帧，真实持续时间可能更长。")
    active = report["active_frames"]
    lines.append(f"模拟跟随/等待恢复期间：{active['found_frames']}/{active['processed_frames']} 帧识别到目标"
                 + (f"（{active['detection_rate']:.1%}）。" if active["detection_rate"] is not None else "，没有有效样本。"))
    lines.append(f"自动恢复：{report['automatic_recoveries'] if report['automatic_recoveries'] is not None else '未知'} 次"
                 "（等待恢复→模拟跟随；不把手动开始算作自动恢复）。")
    lines.append(f"非跟随状态仍出现非零请求：{report['nontracking_nonzero_log_records']} 条日志记录"
                 "（不是实际发给飞机的指令数）。")
    lines += ["", "三、异常及原因"]
    stops = report["stop_events"]
    if stops is None:
        lines.append("旧日志缺少状态切换记录，异常停止次数未知。")
    elif not stops:
        lines.append("记录中没有目标丢失锁停、画面过期或输入无效的状态切换。")
    else:
        for reason, n in stops.items():
            lines.append(f"异常停止 {n} 次：{REASONS.get(reason, reason)} [{reason}]")
    for reason, n in report["all_frames"]["miss_reasons"].items():
        lines.append(f"未识别 {n} 帧：{REASONS.get(reason, reason)} [{reason}]")
    lines.append(f"出现其他编号的处理帧：{report['all_frames']['other_id_frames']}；不等于这些目标被跟随。")
    lines += ["", "四、如何理解结果",
              "识别率的分母是日志中唯一、可统计的处理帧；不含摄像头未处理的帧。",
              "漏检时长按采样估算；长采样间隔也可能来自画面中断，不能全部归因于识别算法。",
              "短测阶段名称按计时分配，不证明实际动作符合要求；本报告不自动判定通过。",
              "不同距离、标记大小、光照或动作的结果不能直接证明版本改进；识别率不等于飞行跟随成功率。",
              "发现异常时：先对齐日志时间和录像；not_found无法单独证明模糊、遮挡或距离问题。",
              "画面过期先查取流/处理间隔；重复编号先查是否有多个同编号标记；输入错误需查对应异常。",
              "", "日志完整性提示"]
    lines.extend(report["warnings"] or ["未发现格式或重复帧问题。"])
    return "\n".join(lines) + "\n"


def write_report(path):
    report = session_report(path)
    folder = Path(path).parent / "reports"
    folder.mkdir(exist_ok=True)
    dest = folder / (Path(path).stem + "_汇总.txt")
    dest.write_text(render(report), encoding="utf-8-sig")
    dest.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def latest_session(folder):
    # Filename contains creation timestamp. mtime may change after a delayed close.
    candidates = sorted(Path(folder).glob("demo_*.jsonl"),
                        key=lambda p: int(p.stem[5:]) if p.stem[5:].isdigit() else -1, reverse=True)
    manual = []
    for path in candidates:
        report = session_report(path)
        if report["automated_gui_check"] is True:
            continue
        manual.append((path, report))
        if report["source"] == "电脑摄像头":
            return path
    if manual:
        return manual[0][0]
    raise FileNotFoundError("没有可用的非自动测试会话日志")


def main():
    parser = argparse.ArgumentParser(description="本地日志汇总，无需重新测试")
    parser.add_argument("--log", type=Path, help="指定demo_*.jsonl；默认最新电脑摄像头会话")
    parser.add_argument("--logs-dir", type=Path, default=BASE/"logs")
    parser.add_argument("--open", action="store_true", help="生成后用系统默认文本应用打开")
    args = parser.parse_args()
    try:
        dest = write_report(args.log or latest_session(args.logs_dir))
        print(f"Report saved: {dest}")
        if args.open:
            os.startfile(str(dest))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"汇总未完成：{exc}\n原始日志未修改。\n")


if __name__ == "__main__":
    main()
