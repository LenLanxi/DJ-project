if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Sandbox payload; run through the authorized sandbox host with an injected drone object.')
    parser.parse_args()
    parser.error('This payload requires the sandbox host; direct execution is unsupported.')

import time

print('FIELD_START: host has checked 10 seconds of fresh frames', flush=True)
status = drone.get_status()
print('GROUND', status, flush=True)
assert status['flight_state'] == 'landed'
assert status['battery'] >= 30

requested = False
try:
    print('TAKEOFF_NOW', flush=True)
    requested = True
    result = drone.takeoff()
    print('TAKEOFF_RESULT', result, flush=True)
    assert result['ok'], 'takeoff_rejected'
    deadline = time.time()+12
    while time.time() < deadline:
        status = drone.get_status()
        print('TAKEOFF_TELEMETRY', status, flush=True)
        if (status['flight_state'] == 'flying' and type(status.get('height_cm')) is int
                and status['height_cm'] >= 20):
            break
        time.sleep(0.2)
    else:
        raise RuntimeError('takeoff_not_confirmed')
    print('AIRBORNE_CONFIRMED', flush=True)
    time.sleep(2)
finally:
    if requested:
        print('LAND_NOW', flush=True)
        print('LAND_RESULT', drone.land(), flush=True)
        deadline = time.time()+18
        while time.time() < deadline:
            status = drone.get_status()
            print('LAND_TELEMETRY', status, flush=True)
            if status['flight_state'] == 'landed' and status['height_cm'] == 0:
                print('LANDED_CONFIRMED', flush=True)
                break
            time.sleep(0.3)
        else:
            raise RuntimeError('landing_not_confirmed')
