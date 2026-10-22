"""Verify existing offline source tests in the separate team environment."""
import contextlib
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=None)
    args = parser.parse_args()
    import cv2
    import numpy as np
    if sys.version_info[:2] != (3, 11) or np.__version__ != '1.26.4' or cv2.__version__ != '4.8.0':
        raise SystemExit('Use the separate Python 3.11 / NumPy 1.26.4 / OpenCV 4.8.0 environment')
    cv2.setNumThreads(1)
    output = (args.output.resolve() if args.output else
              BASE/'logs'/'checks311'/f'run_{time.time_ns()}')
    output.mkdir(parents=True, exist_ok=True)
    console, tests = io.StringIO(), io.StringIO()
    loader = unittest.TestLoader()
    # The archived aa40228 integration test is run separately against the
    # delivered current adapter, not counted here as current SDK integration.
    suite = unittest.TestSuite()
    with contextlib.redirect_stdout(console), \
         patch('socket.socket', side_effect=AssertionError('No network during tests')), \
         patch('cv2.VideoCapture', side_effect=AssertionError('No camera')), \
         patch('tkinter.Tk', side_effect=AssertionError('No desktop')):
        for file in sorted((BASE/'tests').glob('test_*.py')):
            if not file.name.startswith('test_team2_'):
                suite.addTests(loader.discover(str(BASE/'tests'), pattern=file.name))
        result = unittest.TextTestRunner(stream=tests, verbosity=2).run(suite)
    paths = [*BASE.glob('*.py'), *(BASE/'r3_follow').glob('*.py'),
             *(BASE/'tests').glob('test_*.py'), *(BASE/'tests'/'fixtures').glob('*')]
    report = dict(python=sys.version, executable=sys.executable, numpy=np.__version__,
                  opencv=cv2.__version__, tests_run=result.testsRun,
                  failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
                  successful=result.wasSuccessful() and not result.skipped,
                  desktop_opened=False, camera_opened=False, hardware_connected=False,
                  scope='offline source regression; current SDK adapter is verified separately',
                  source_sha256={p.relative_to(BASE).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted(paths) if p.is_file()})
    (output/'source_tests.txt').write_text(tests.getvalue(), encoding='utf-8')
    (output/'source_console.txt').write_text(console.getvalue(), encoding='utf-8')
    (output/'source_result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(tests.getvalue()[-6000:])
    print('Evidence:', output)
    if not report['successful']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
