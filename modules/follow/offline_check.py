"""Offline tests only. Run from the repository root; no installation."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--repository', type=Path, default=root.parents[1])
parser.add_argument('--require-team-env', action='store_true')
args = parser.parse_args()
try:
    import cv2
    import numpy as np
    from r3_follow.vision import MarkerTracker, synthetic_frame
    from r3_follow.team2_adapter import COMMIT
except ImportError as exc:
    raise SystemExit('Missing dependency. Use the team Python 3.11 environment: ' + str(exc))

environment = dict(python=sys.version, numpy=np.__version__, opencv=cv2.__version__)
team_env = (sys.version_info[:2] == (3, 11) and np.__version__ == '1.26.4'
            and cv2.__version__ == '4.8.0')
print(json.dumps(environment))
if args.require_team_env and not team_env:
    raise SystemExit('NOT the agreed Python 3.11 / NumPy 1.26.4 / OpenCV 4.8.0 environment')
if not team_env:
    print('NOTE: tests below do not certify the agreed team environment.')
for name in ('ArucoDetector', 'DetectorParameters', 'generateImageMarker'):
    if not hasattr(getattr(cv2, 'aruco', None), name):
        raise SystemExit('Missing cv2.aruco.' + name + '; report the installed OpenCV package to group 2')

suite = unittest.TestSuite()
for file in sorted((root/'tests').glob('test_*.py')):
    spec = importlib.util.spec_from_file_location(file.stem, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.REPO = args.repository.resolve()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
console, testlog = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(console), \
     patch('socket.socket', side_effect=AssertionError('No network')), \
     patch('cv2.VideoCapture', side_effect=AssertionError('No camera')), \
     patch('tkinter.Tk', side_effect=AssertionError('No desktop')):
    tracker = MarkerTracker()
    observation = tracker.detect(synthetic_frame(), 1, 0.0, 'generated-preflight')
    if not observation.found:
        raise SystemExit('Generated ID0 marker preflight failed')
    result = unittest.TextTestRunner(stream=testlog, verbosity=2).run(
        suite)
report = dict(commit=COMMIT, environment=environment, agreed_environment_verified=team_env,
              tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
              skipped=len(result.skipped), successful=result.wasSuccessful() and not result.skipped,
              generated_marker_detected=True, camera_opened=False, desktop_opened=False,
              hardware_connected=False, dependencies_installed=False,
              known_upstream_gaps_reproduced_by_tests=True)
report['source_sha256'] = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in sorted(root.rglob('*.py'))}
evidence = root/'evidence'
evidence.mkdir(exist_ok=True)
(evidence/'offline_result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
(evidence/'offline_tests.txt').write_text(testlog.getvalue(), encoding='utf-8')
(evidence/'offline_sdk_output.txt').write_text(console.getvalue(), encoding='utf-8')
print(testlog.getvalue())
print('Evidence: ' + str(evidence))
if not report['successful']:
    raise SystemExit(1)
