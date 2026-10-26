"""Delegate to the sole repository offline suite; no legacy-snapshot fallback."""
import argparse
from pathlib import Path
import subprocess
import sys

BASE=Path(__file__).resolve().parent

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--repository',type=Path,default=BASE.parents[1])
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    repo=args.repository.resolve()
    if repo!=BASE.parents[1]:
        raise SystemExit('Run from the checkout being reviewed; mixed checkouts are rejected')
    entry=repo/'run_offline.py'
    if not entry.is_file():
        raise SystemExit('Missing canonical run_offline.py; no old app fallback exists')
    command=[sys.executable,str(entry),'--repository',str(repo)]
    if args.output: command+=['--output',str(args.output.resolve())]
    return subprocess.call(command,cwd=repo)

if __name__=='__main__':raise SystemExit(main())
