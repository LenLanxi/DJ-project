"""Original vs Plan A on identical delayed image/physics timelines, OFFLINE.

Uses actual ArUco decoding and FollowService. Dynamics, metres and RC-to-speed
mapping are invented assumptions; no device is accessed. Replayed detection
durations are explicit stress inputs, not a claim of measured flight timing.
"""
import argparse
from dataclasses import replace
import json
import math
from pathlib import Path
import sys
from unittest.mock import patch

import cv2
import timeline_flight_check as model

BASE=Path(__file__).resolve().parent


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if sys.version_info[:2]!=(3,11):raise SystemExit('Use Python 3.11')
    a.output.mkdir(parents=True,exist_ok=True);cv2.setNumThreads(1)
    costs=[.019,.024,.029,.018,.105,.020]
    specs=[dict(name=f'{case}_{label}',case=case,response=tau,delay=delay,costs=costs)
        for label,tau,delay in (('normal',.25,0.),('slow_delay',1.,.2))
        for case in ('chase_stop','chase_fast','reverse','lateral_reverse')]
    specs += [dict(name=case,case=case,response=.25,delay=.1,costs=costs)
        for case in ('side_turn','loss','wrong_id','explicit_stop','frozen','cpu_spike','lateral_edge')]
    specs += [dict(name=f'late_{case}',case=case,response=1.,delay=.4,costs=[.1])
              for case in ('chase_stop','reverse')]
    original_target=model.target_at
    def target(case,t):
        if case=='lateral_edge':
            x=1.2*min(1.,max(0.,t-2.)/2.)
            if t>7:x-=1.2*min(1.,(t-7.)/2.)
            return (x,2.,1.2),0.,True,0
        return original_target(case,t)
    result=[]
    with patch('socket.socket',side_effect=AssertionError('No network')), \
         patch('cv2.VideoCapture',side_effect=AssertionError('No camera')), \
         patch('tkinter.Tk',side_effect=AssertionError('No desktop')), \
         patch.object(model,'target_at',target):
        for spec in specs:
            pair={}
            for label,file in (('before','aircraft_A_ID0.json'),('plan_a','aircraft_A_ID0_plan_a.json')):
                world=model.Timeline(**{k:spec[k] for k in ('case','response','delay','costs')},
                                     config=BASE/'configs'/file)
                # Model the v4.1 missing height timestamp: vertical requests
                # must remain blocked exactly as on the real original SDK.
                world.port.height_fresh=False
                summary,events=world.run(14.)
                moving=[r for r in world.rows if r['started'] and 2<=r['t']<6]
                summary['chase_mean_abs_depth_error_virtual_m']=sum(abs(r['depth_error_virtual_m']) for r in moving)/max(1,len(moving))
                summary['mean_abs_lateral_error_virtual_m']=sum(abs(r['lateral_error_virtual_m']) for r in moving)/max(1,len(moving))
                summary['peak_rc']={axis:max(abs(r['rc_'+axis]) for r in world.rows) for axis in ('right','forward','up','yaw')}
                tail=[r for r in world.rows if r['started'] and r['t']>=12]
                summary['tail_mean_abs_depth_error_virtual_m']=sum(abs(r['depth_error_virtual_m']) for r in tail)/max(1,len(tail))
                assert summary['peak_rc']['up']==0, 'Original SDK height guard was bypassed'
                pair[label]=summary
                model.save_run(a.output/f"{spec['name']}_{label}",'trace',world,summary,events)
            result.append(dict(spec=spec,results=pair))
            print(json.dumps(dict(name=spec['name'],
                chase=[pair[k]['chase_mean_abs_depth_error_virtual_m'] for k in pair],
                minimum=[pair[k]['min_virtual_depth_m'] for k in pair],
                faults=[len(pair[k]['faults']) for k in pair])),flush=True)
    report=dict(python=sys.version,runs=result,hardware_verified=False,
        assumptions=dict(rc_gain=2.,virtual_reference_m=2.,response='first order',
                         latency='identical injected durations; actual decoder still runs',
                         obstacle_avoidance=False,height_telemetry_fresh=False))
    (a.output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print('Finished',len(result),'pairs. No real flight validation.')


if __name__=='__main__':main()
