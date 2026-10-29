"""Local structured-action integration smoke; simulated aircraft, no cloud."""
import argparse
import asyncio
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests' / 'helpers')]
from RuntimeFixture import reset, runtime, sdk, settle

async def scenario():
    app = runtime()
    try:
        for method, args in [('get_status', []), ('takeoff', []), ('move_forward', [30]), ('land', [])]:
            result = await app.run({'action': method, 'args': args})
            assert result['ok'], result
            print('PASS', method)
        settle()
        assert sdk.get_status().flight_state == 'landed'
        result = await app.run({'action': 'start_follow', 'args': []})
        assert not result['ok'], 'follow without images must fail'
        assert sdk.get_status().control_owner == 'none'
        print('PASS missing-frame follow cleanup')
    finally:
        app.close()
        await app.retry_cleanup()

def main():
    reset()
    try:
        asyncio.run(scenario())
    finally:
        reset()
    return 0

if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    raise SystemExit(main())
