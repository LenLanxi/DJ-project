"""Execute the current behavioral test suite from any working directory.

Historic Cases.json and Evidence/ files describe their recorded Git revisions;
they are evidence archives, not an assertion that current requirements passed.
There is no known-gap-to-success conversion and no legacy application loader.
"""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

if __name__ == '__main__':
    environment = dict(os.environ, PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
    raise SystemExit(subprocess.run([sys.executable, '-B', '-m', 'pytest', *sys.argv[1:]],
                                  cwd=ROOT, env=environment).returncode)
