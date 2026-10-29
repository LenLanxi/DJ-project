"""Current host-integration smoke: no device, cloud or real Docker execution."""
import argparse
import asyncio
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests' / 'helpers')]
from RuntimeFixture import reset as reset_sdk, runtime, sdk, settle

PASSED, FAILED = [], []

def check(name, condition, detail=''):
    (PASSED if condition else FAILED).append(name)
    print(('PASS ' if condition else 'FAIL ') + name + ('' if condition else ': '+str(detail)))

def reset(_unused=None): reset_sdk()

class ScriptRunner:
    def __init__(self, calls): self.calls=calls
    async def run(self, code, host, outcome):
        journal=[]
        try:
            for method,args in self.calls:
                journal.append(dict(method=method,result=await host.dispatch(method,args,{})))
            outcome.result=dict(ok=True,status='completed',journal=journal)
        except Exception as error:
            outcome.result=dict(ok=False,status='failed',reason=str(error),journal=journal)

async def scenario():
    app=runtime(runner=ScriptRunner([('get_status',[]),('takeoff',[]),('move_forward',[30]),('land',[])]))
    result=await app.run('scripted host exercise; Python execution is not tested here')
    check('current Host SDK chain',result['ok'],result)
    check('all four interfaces returned evidence',len(result['journal'])==4,result)
    settle()
    check('simulated landing confirmed',sdk.get_status().flight_state=='landed')
    app=runtime(runner=ScriptRunner([('start_follow',[])]))
    result=await app.run('scripted missing-frame exercise')
    check('follow requires a frame source',not result['ok'],result)
    check('failure leaves no owner',sdk.get_status().control_owner=='none')


def main():
    PASSED.clear(); FAILED.clear()
    reset_sdk()
    try: asyncio.run(scenario())
    finally: reset_sdk()
    print('Host smoke only; Python execution, real models and hardware are checked separately.')
    return 1 if FAILED else 0

if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    raise SystemExit(main())
