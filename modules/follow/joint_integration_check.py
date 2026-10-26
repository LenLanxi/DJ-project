"""Guarded joint checks for the sole current runtime, never a legacy app entry."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
BASE=Path(__file__).resolve().parent
REPO=BASE.parents[1]
sys.path[:0]=[str(REPO),str(REPO/'modules'/'ai'),str(REPO/'tests')]


def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module


def run_suite(suite):
    stream=io.StringIO()
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    report=dict(run=result.testsRun,failures=len(result.failures),errors=len(result.errors),
        skipped=len(result.skipped),passed=result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
        successful=result.wasSuccessful() and not result.skipped)
    return report,stream.getvalue()


def reviewed_sdk(repo):
    """Use the same reviewed SDK hashes as the adapter, not dead AI snapshots."""
    from r3_follow.team2_adapter import load_reviewed_stub
    sdk=load_reviewed_stub(repo)
    if not sdk._driver.SIMULATED:
        raise RuntimeError('Offline check refuses an attached real aircraft')
    return sdk


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--repository',type=Path,default=REPO)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    repo=args.repository.resolve()
    if repo!=REPO:
        raise SystemExit('Run this runner from the checkout being reviewed; mixed SDK checkouts are rejected')
    import cv2
    import numpy as np
    if sys.version_info[:2]!=(3,11) or np.__version__!='1.26.4' or cv2.__version__!='4.8.0':
        raise SystemExit('Use the project uv Python 3.11 environment')
    cv2.setNumThreads(1)
    sdk=reviewed_sdk(repo)
    output=args.output.resolve() if args.output else BASE/'logs'/'Joint'/str(time.time_ns())
    output.mkdir(parents=True,exist_ok=True)
    report=dict(python=sys.version,repository=str(repo),camera_opened=False,desktop_opened=False,
        hardware_connected=False,real_model_used=False,real_docker_used=False,
        generated_images=True,scripted_container_rpc=True,suites={})
    console,details=io.StringIO(),[]
    with contextlib.redirect_stdout(console), \
         patch.object(sdk._driver,'connect',side_effect=AssertionError('Hardware forbidden')), \
         patch('cv2.VideoCapture',side_effect=AssertionError('Camera forbidden')):
        for name in ('test_team2_integration','test_team2_ack_semantics'):
            module=load(BASE/'tests'/(name+'.py'),name)
            module.REPO=repo
            result,text=run_suite(unittest.defaultTestLoader.loadTestsFromModule(module))
            report['suites'][name]=result; details.append(text)
        joint=load(BASE/'tests'/'test_team2_joint.py','CurrentJointCases')
        joint.REPO=repo
        for name,suite in (
            ('joint_behavior',unittest.defaultTestLoader.loadTestsFromTestCase(joint.JointTests)),
            ('runtime_interrupt',unittest.TestSuite([joint.UpstreamSessionTests(
                'test_updated_session_dispatches_stop_before_blocked_action_finishes')]))):
            result,text=run_suite(suite);report['suites'][name]=result;details.append(text)
        report['interrupt_measurements']=joint.MEASUREMENTS
        for name in ('ConcurrencyTests','FullIntegrationTests','RuntimeInterruptTests','UnknownCommandTests'):
            module=load(repo/'tests'/(name+'.py'),'Current'+name)
            result,text=run_suite(unittest.defaultTestLoader.loadTestsFromModule(module))
            report['suites'][name]=result;details.append(text)
        smoke=load(repo/'tests'/'IntegrationSmoke.py','CurrentSmoke')
        smoke.main()
        report['smoke']=dict(passed=len(smoke.PASSED),failed=len(smoke.FAILED),failures=smoke.FAILED)
    report['successful']=all(r['successful'] for r in report['suites'].values()) and not report['smoke']['failed']
    report['scope']='Actual ArUco/follow/SDK and current UnifiedRuntime/Gateway; scripted container RPC, no model inference'
    report['source_sha256']={str(p.relative_to(repo)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [*(repo/'drone_sdk').glob('*.py'),*(BASE/'r3_follow').glob('*.py'),BASE/'joint_integration_check.py']}
    (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'tests.txt').write_text('\n'.join(details),encoding='utf-8')
    (output/'console.txt').write_text(console.getvalue(),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('suites','smoke','successful')},indent=2))
    print('Evidence:',output)
    return 0 if report['successful'] else 1

if __name__=='__main__':raise SystemExit(main())
