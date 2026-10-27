"""Run Group 3 offline regressions once per defined method, no GUI/network."""
import argparse
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 11):
        raise SystemExit("Use the team's Python 3.11 environment")
    base = Path(__file__).resolve().parent
    repo = base.parents[1]
    sys.path[:0] = [str(base), str(repo), str(repo / "modules" / "ai")]
    import cv2
    import numpy
    cv2.setNumThreads(1)
    def flatten(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                yield from flatten(item)
            else:
                yield item
    tests, seen, omitted = [], set(), []
    for test in flatten(unittest.defaultTestLoader.discover(str(base / "tests"), pattern="test_*.py")):
        method = getattr(type(test), test._testMethodName)
        key = (method.__module__, method.__qualname__)
        if key in seen:
            omitted.append(test.id())
        else:
            seen.add(key)
            tests.append(test)
    out = args.output.absolute()
    out.mkdir(parents=True, exist_ok=True)
    with (out / "tests.txt").open("w", encoding="utf-8") as stream, \
         patch("socket.socket", side_effect=AssertionError("Network forbidden")), \
         patch("cv2.VideoCapture", side_effect=AssertionError("Camera forbidden")), \
         patch("tkinter.Tk", side_effect=AssertionError("Desktop forbidden")):
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.TestSuite(tests))
    report = dict(base_commit="9a7d2a17c4634b0c26bcab82a48410021031e714",
        python=sys.version, executable=sys.executable, opencv=cv2.__version__, numpy=numpy.__version__,
        run=result.testsRun, passed=result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
        failures=[test.id() for test, _ in result.failures], errors=[test.id() for test, _ in result.errors],
        skipped=[test.id() for test, _ in result.skipped], omitted_inherited_duplicates=omitted,
        successful=result.wasSuccessful() and not result.skipped,
        hardware_connected=False, camera_opened=False, desktop_opened=False,
        scope="Group 3 regressions plus SDK simulation and legacy AI-entry fixtures; not whole-system acceptance")
    (out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("run","passed","failures","errors","successful")}, indent=2))
    return 0 if report["successful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
