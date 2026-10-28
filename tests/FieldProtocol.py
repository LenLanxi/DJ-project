"""Opt-in single Tello protocol takeoff probe; no model, video or SDK controller."""
import argparse
import ctypes
from pathlib import Path
import select
import socket
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'modules'/'ai'))
from main import SessionLog

def rise_allowed(packet):
    height=packet.get('h')
    if type(height) is not int or height <= 0:
        height=packet.get('tof')
    return type(height) is int and 20 <= height <= 230

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--allow-takeoff',action='store_true')
    parser.add_argument('--rise-20',action='store_true',help='Only ascend after fresh airborne measurement')
    parser.add_argument('--send-up-20',action='store_true',help='Explicit physical test: send up 20 after takeoff even without airborne confirmation')
    parser.add_argument('--min-battery',type=int,default=30,help='0 disables project percentage threshold, not firmware protection')
    args=parser.parse_args()
    if not args.allow_takeoff: parser.error('explicit --allow-takeoff required')
    if not 0<=args.min_battery<=100: parser.error('--min-battery must be 0..100')
    if args.rise_20 and args.send_up_20: parser.error('choose one ascent mode to avoid duplicate up commands')
    log=SessionLog()
    log({'event':'protocol_test_configuration','min_battery':args.min_battery,
         'percentage_threshold_disabled':args.min_battery==0,'send_up_20':args.send_up_20})
    print('Log:',log.path,flush=True)
    command=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    telemetry=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    command.bind(('192.168.10.2',8889));telemetry.bind(('',8890))
    current={}; received=0.; last_log=0.; landing=False; attempted=False; airborne=False
    last_command=None;takeoff_acknowledged=False;land_acknowledged=False;ground_since=None
    keys=ctypes.windll.user32
    def send(value):
        nonlocal last_command
        last_command=value
        command.sendto(value.encode(),('192.168.10.1',8889))
        log({'event':'protocol_send','command':value})
        print('SEND',value,flush=True)
    def pump(seconds):
        nonlocal current,received,last_log,landing,takeoff_acknowledged,land_acknowledged,airborne,ground_since
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            chord=all(keys.GetAsyncKeyState(k)&0x8000 for k in (0x11,0x10,0x12,0x4C))
            if (keys.GetAsyncKeyState(0x7A)&0x8000 or chord) and not landing:
                landing=True;send('land')
            ready,_,_=select.select([command,telemetry],[],[],.05)
            for source in ready:
                data,address=source.recvfrom(4096)
                if address[0]!='192.168.10.1':continue
                if source is command:
                    if last_command=='takeoff' and data.strip()==b'ok':
                        takeoff_acknowledged=True
                    if last_command=='land' and data.strip()==b'ok':
                        land_acknowledged=True
                    log({'event':'protocol_reply','hex':data.hex(),'text':data.decode('utf-8',errors='replace')})
                    print('REPLY',data,flush=True)
                else:
                    raw={}
                    for field in data.decode('ascii',errors='replace').split(';'):
                        if ':' not in field:continue
                        key,value=field.split(':',1)
                        try:raw[key]=float(value) if '.' in value else int(value)
                        except ValueError:raw[key]=value
                    current=raw;received=time.monotonic()
                    if attempted and (type(raw.get('h')) is int and raw['h']>=20
                                      or type(raw.get('tof')) is int and 20<=raw['tof']<=300):
                        if not airborne:
                            log({'event':'protocol_airborne_measurement','raw':raw})
                        airborne=True
                    if raw.get('h')==0 and type(raw.get('tof')) is int and 0<=raw['tof']<20:
                        if ground_since is None:ground_since=received
                    else:ground_since=None
                    if received-last_log>=.2:
                        log({'event':'protocol_telemetry','raw':raw})
                        last_log=received
    def fresh(): return current and 0 <= time.monotonic()-received <= 1
    try:
        send('command');pump(2)
        assert fresh(),'no_fresh_telemetry'
        assert type(current.get('bat')) is int and args.min_battery<=current['bat']<=100,'invalid_or_low_battery'
        assert max(current.get('templ',150),current.get('temph',150))<95,'temperature_gate'
        assert current.get('h')==0 and 0<=current.get('tof',9999)<20,'not_confirmed_ground'
        assert not landing,'operator_cancelled'
        log({'event':'protocol_preflight','raw':current})
        print('TAKEOFF NOW: bounded 12 second observation, then land',flush=True)
        attempted=True;send('takeoff')
        if args.send_up_20:
            # The 1s source-age policy is not a takeoff completion timeout.
            ack_deadline=time.monotonic()+25
            while not takeoff_acknowledged and not landing and time.monotonic()<ack_deadline:
                pump(.1)
            assert takeoff_acknowledged,'no_takeoff_ack_before_requested_up'
            assert fresh(),'telemetry_lost_before_requested_up'
            assert max(current.get('templ',150),current.get('temph',150))<95,'temperature_gate'
            assert type(current.get('bat')) is int and args.min_battery<=current['bat']<=100,'invalid_or_low_battery_before_requested_up'
            assert current.get('h',9999)<=230 and current.get('tof',9999)<=230,'height_not_bounded_for_requested_up'
            if landing: raise RuntimeError('operator_cancelled_before_requested_up')
            log({'event':'protocol_explicit_up_test','airborne_confirmed':airborne,'raw':current})
            send('up 20')
        deadline=time.monotonic()+12
        while time.monotonic()<deadline and not landing:
            pump(.1)
            if fresh() and (type(current.get('h')) is int and current['h']>=20
                            or type(current.get('tof')) is int and 20<=current['tof']<=300):
                airborne=True
                log({'event':'protocol_airborne_measurement','raw':current})
                print('AIRBORNE MEASUREMENT',current.get('h'),current.get('tof'),flush=True)
                if args.rise_20:
                    if fresh() and rise_allowed(current) and not landing:
                        log({'event':'protocol_rise_gate','allowed':True,'raw':current})
                        send('up 20');pump(4)
                    else:
                        log({'event':'protocol_rise_gate','allowed':False,'raw':current})
                pump(2);break
    finally:
        if attempted:
            landing=True;ground_since=None
            land_deadline=time.monotonic()+25
            last_land=0.
            ground_confirmed=False
            while time.monotonic()<land_deadline:
                if time.monotonic()-last_land>=3:
                    send('land');last_land=time.monotonic()
                pump(.1)
                if land_acknowledged and fresh() and ground_since is not None and time.monotonic()-ground_since>=1:
                    ground_confirmed=True;break
            log({'event':'protocol_landing_result','ground_telemetry_confirmed':ground_confirmed,
                 'land_acknowledged':land_acknowledged,'raw':current})
            log({'event':'protocol_final','airborne_measurement':airborne,'fresh':bool(fresh()),'raw':current,
                 'physical_motor_stop_confirmed':False})
            print('FINAL airborne_measurement=',airborne,'h=',current.get('h'),'tof=',current.get('tof'),flush=True)
        command.close();telemetry.close();log.close()
    return 0 if airborne and ground_confirmed else 1

if __name__=='__main__':raise SystemExit(main())
