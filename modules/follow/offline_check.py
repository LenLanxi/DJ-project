"""One Python 3.11 command for source, integration and public-path checks."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository', type=Path, default=BASE.parents[1])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--require-team-env', action='store_true')
    args = parser.parse_args()
    consolidated=BASE.parents[1]/'run_offline.py'
    if consolidated.is_file():
        # Resolve caller-relative paths before changing the working directory.
        extra=['--repository',str(args.repository.resolve())]
        if args.output:
            extra += ['--output',str(args.output.resolve())]
        return subprocess.call([sys.executable,str(consolidated),*extra],cwd=consolidated.parent)
    import cv2
    import numpy as np
    if sys.version_info[:2] != (3,11) or np.__version__ != '1.26.4' or cv2.__version__ != '4.8.0':
        raise SystemExit('Use Python 3.11 / NumPy 1.26.4 / OpenCV 4.8.0; nothing will be installed')
    repo = args.repository.resolve()
    profile = None
    for name in ('upstream', 'candidate'):
        pin_file = 'team2_reviewed_sources.json' if name == 'upstream' else 'team2_candidate_sources.json'
        pin = json.loads((BASE/pin_file).read_text(encoding='utf-8'))
        if all((repo/file).is_file() and hashlib.sha256((repo/file).read_bytes().replace(b'\r\n',b'\n')).hexdigest()==digest
               for file,digest in pin['sha256_lf'].items()):
            profile = name
            break
    if profile is None:
        raise SystemExit('Repository source differs from both reviewed versions; review before running')
    output = args.output.resolve() if args.output else BASE/'evidence'/('run_'+str(time.time_ns()))
    output.mkdir(parents=True,exist_ok=True)
    runs = {}
    jobs = [
        ('source', 'verify_python311.py', []),
        ('joint', 'joint_integration_check.py', ['--repository',str(repo),'--profile',profile]),
        ('public_path', 'upstream_contract_check.py', ['--repository',str(repo),'--profile',profile]),
    ]
    for name,script,extra in jobs:
        result = subprocess.run([sys.executable, str(BASE/script), *extra, '--output',str(output/name)],
                                cwd=BASE, capture_output=True, timeout=180)
        (output/(name+'_stdout.txt')).write_bytes(result.stdout)
        (output/(name+'_stderr.txt')).write_bytes(result.stderr)
        runs[name] = dict(returncode=result.returncode, successful=result.returncode==0)
        print(name, 'PASS' if result.returncode==0 else 'FAIL', flush=True)
    report = dict(python=sys.version, repository=str(repo), profile=profile,
                  upstream_commit=pin['commit'], results=runs,
                  successful=all(item['successful'] for item in runs.values()),
                  desktop_opened=False,camera_opened=False,hardware_connected=False,
                  model_or_docker_used=False,
                  full_runtime_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in (BASE/'r3_follow').glob('*.py')})
    (output/'delivery_result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('Evidence:', output)
    return 0 if report['successful'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
