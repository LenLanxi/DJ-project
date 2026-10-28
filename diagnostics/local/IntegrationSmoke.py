"""Current host-integration smoke: no device, cloud or real Docker execution."""
import argparse
import asyncio
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests' / 'helpers')]
from RuntimeFixture import container, reset as reset_sdk, runtime, sdk, settle

PASSED, FAILED = [], []

def check(name, condition, detail=''):
    (PASSED if condition else FAILED).append(name)
    print(('PASS ' if condition else 'FAIL ') + name + ('' if condition else ': '+str(detail)))

def reset(_unused=None): reset_sdk()

async def scenario():
    app = runtime()
    with container([('get_status', []), ('takeoff', []), ('move_forward',[30]), ('land', [])]):
        result = await app.run('status=drone.get_status()\ndrone.takeoff()\ndrone.move_forward(30)\ndrone.land()')
    check('current sandbox RPC gateway SDK chain', result['ok'], result)
    check('all four requested interfaces returned evidence', len(result['journal']) == 4, result)
    settle()
    check('simulated landing status confirmed separately', sdk.get_status().flight_state == 'landed')
    with container([('start_follow', [])]):
        no_frame = await app.run('drone.start_follow()')
    check('follow cannot start without a frame source', not no_frame['ok'], no_frame)
    check('failed startup leaves no owner', sdk.get_status().control_owner == 'none')

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
