"""Tests wait for simulated telemetry; sending land is not completion."""
import time

def settle_landing(sdk, timeout=2):
    deadline=time.monotonic()+timeout
    while sdk.get_status().flight_state=='landing':
        if time.monotonic()>=deadline: raise AssertionError('simulated landing not confirmed')
        time.sleep(.005)

def settle_workers(sdk, timeout=2):
    deadline=time.monotonic()+timeout
    while sdk._follow.status().get('background_running'):
        if time.monotonic()>=deadline: raise AssertionError('capture worker not closed')
        time.sleep(.005)
