"""Exercise our own Tk callbacks using generated frames, no camera or drone."""
import json
from pathlib import Path
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from follow_demo import gui
from r3_follow.core import State

results = []
errors = []


def driver(root, controller, experiment, command, buttons, choice, close, begin):
    def check():
        try:
            assert controller.recovery_window == 3
            command("calibrate")
            command("start")
            assert controller.state == State.TRACKING
            begin()
            assert controller.state == State.STOPPED
            command("start")  # programmatic bypass must also be blocked
            assert controller.state == State.STOPPED
            results.append("short_test_blocks_start")
            root.after(120, locked)
        except Exception:
            errors.append(traceback.format_exc())
            close()

    def locked():
        try:
            for name in ("calibrate", "start", "pause", "resume"):
                assert buttons[name].instate(["disabled"]), name
            assert choice.instate(["disabled"])
            buttons["start"].invoke()
            assert controller.state == State.STOPPED
            results.append("buttons_and_setting_locked")
            buttons["stop"].invoke()
            assert not experiment.testing
            report = experiment.short_test.path.with_suffix(".txt").read_text(encoding="utf-8-sig")
            assert "未完成" in report
            results.append("stop_aborts_short_test")
            root.after(120, unlocked)
        except Exception:
            errors.append(traceback.format_exc())
            close()

    def unlocked():
        try:
            assert not buttons["start"].instate(["disabled"])
            assert choice.instate(["readonly"])
            choice.set("1.5")
            choice.event_generate("<<ComboboxSelected>>")
            assert controller.recovery_window == 1.5
            results.append("setting_changes_only_when_stopped")
            begin()
            # Simulate elapsed test duration without waiting 25 real seconds.
            experiment.short_test.start = time.monotonic()-26
            experiment.short_test.end = experiment.short_test.start+20
            root.after(120, completed)
        except Exception:
            errors.append(traceback.format_exc())
            close()

    def completed():
        try:
            assert not experiment.testing
            assert controller.state == State.STOPPED
            assert not buttons["start"].instate(["disabled"])
            results.append("test_completion_does_not_start_following")
        except Exception:
            errors.append(traceback.format_exc())
        finally:
            close()

    check()


gui(diagnostics=True, test_driver=driver)
print(json.dumps({"checks": results, "errors": errors}, ensure_ascii=False))
if errors:
    raise SystemExit(1)
