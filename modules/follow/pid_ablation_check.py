"""Offline-only ablation: same samples, same caps, one setting changed at a time.

Factories override public PID settings only in this diagnostic process. They
do not add runtime profiles or mutate vendor code. No camera/network/device.
Recorded observations are open-loop inputs, including those acquired after a
real stop; their hypothetical commands are NOT the original session commands.
"""
import argparse
import hashlib
import json
import platform
from pathlib import Path
from unittest.mock import patch

import pid_adaptation_check as existing
from r3_follow.openpose_control import OpenPoseMarkerControl
from r3_follow.core import ApparentSizeGuard, Observation

VARIANTS = ('upstream_reference','marker_v2','marker_no_filter',
            'marker_upstream_D','marker_upstream_I','adapted_without_integral_guards',
            'marker_no_I')


def factory(name):
    if name not in VARIANTS:raise ValueError(name)
    a=OpenPoseMarkerControl(tuning='upstream_reference' if name in
        ('upstream_reference','adapted_without_integral_guards') else 'marker_v2')
    if name=='marker_no_filter':a.depth_filter_tau=0.
    if name=='marker_upstream_D':a.pitch_pid.Kd=.3
    if name=='marker_upstream_I':a.pitch_pid.Ki=.04
    if name=='marker_no_I':a.pitch_pid.Ki=0.
    if name=='adapted_without_integral_guards':
        a.pitch_pid.tunings=(.5,.01,.06);a.depth_filter_tau=.08
    a.pitch_gains=a.pitch_pid.tunings
    return a


def run_samples(samples,name,reference=.2):
    with patch.object(existing,'OpenPoseMarkerControl',lambda **kw:factory(name)):
        return existing.replay_samples(samples,name,reference)


def webcam_samples(path):
    records=[dict(json.loads(s),source_line=n) for n,s in enumerate(
        path.read_text(encoding='utf-8').splitlines(),1) if s.strip()]
    calibration=next(r for r in records if r['event']=='calibration' and r['ok'])
    guard=ApparentSizeGuard(required=True)
    samples=[]
    for r in records:
        if r['event']!='observation' or r['source_line']<calibration['source_line']:continue
        o=Observation(r['frame_id'],r['timestamp'],'recorded-camera',r['image_width'],
            r['image_height'],r['observation_target_id'],r['found'],r['center_x'],
            r['center_y'],r['size_ratio'],tuple(map(tuple,r['corners'])),r['reason'])
        guard.observe(o)
        samples.append(dict(frame_id=o.frame_id,timestamp=o.acquired_at,found=o.found,
            center_x=o.center_x,size_ratio=o.size_ratio,image_width=o.width,
            forward_allowed=not guard.blocked,source_line=r['source_line']))
    return samples,calibration['reference_size']


def long_error(size,duration=60.):
    return [dict(frame_id=n,timestamp=n*.1,found=True,center_x=320.,
                 size_ratio=size if n<duration*10 else .2,forward_allowed=True)
            for n in range(int((duration+3)*10)+1)]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--webcam-log',type=Path)
    parser.add_argument('--flight-log',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    report=dict(python=platform.python_version(),settings={},synthetic={},recorded={},
        virtual=[],integral=[],limits=[
            'Upstream reference keeps original gains, but shares marker input adaptation, 20/30 RC caps and frame/reset handling.',
            'Synthetic open-loop scenarios do not model aircraft response; direction reversals are not flight oscillations.',
            'Virtual plant is dimensionless and invented, not a Tello model; same .8/s request ramp and .5s expiry for all variants.',
            'Recorded frames cannot change in response to hypothetical requests. No closed-loop flight conclusion.',
            'All production variants use the same outer identity, shape, ownership and stop rules; those rules are not relaxed.'])
    for name in VARIANTS:
        a=factory(name);report['settings'][name]=dict(gains=a.pitch_pid.tunings,
            filter_tau_s=a.depth_filter_tau,integral_guards=a.tuning=='marker_v2')
        for scenario in ('step','ramp','noise','irregular_noise','loss_and_shape','approach_braking'):
            rows=run_samples(existing.synthetic_samples(scenario),name)
            existing.write_csv(args.output/f'{scenario}_{name}.csv',rows)
            report['synthetic'].setdefault(scenario,{})[name]=existing.synthetic_metrics(rows,scenario)
        for response in (.1,.25,1.):
            for latency in (0.,.2,.4,.6):
                for noise in (False,True):
                    with patch.object(existing,'OpenPoseMarkerControl',lambda **kw:factory(name)):
                        rows,metrics=existing.virtual_step(name,response,latency,noise)
                    report['virtual'].append(dict(variant=name,**metrics))
                    existing.write_csv(args.output/f'virtual_{name}_{response}_{latency}_{noise}.csv',rows)
        for size in (.1,.16,.24,.3):
            rows=run_samples(long_error(size),name)
            returned=[r for r in rows if r['timestamp']>=60]
            report['integral'].append(dict(variant=name,size=size,
                before_return_integral_rc=rows[599]['i_rc'],
                return_first_rc=returned[0]['forward_rc'],
                return_1s_rc=returned[10]['forward_rc'],return_3s_rc=returned[-1]['forward_rc']))
            existing.write_csv(args.output/f'long_error_{size}_{name}.csv',rows)
    for label,path,loader in [('webcam',args.webcam_log,webcam_samples),
                              ('flight',args.flight_log,existing.field_samples)]:
        if path is None:continue
        samples,ref=loader(path)
        item={'source':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
              'reference':ref,'samples':len(samples),'variants':{}}
        for name in VARIANTS:
            rows=run_samples(samples,name,ref)
            existing.write_csv(args.output/f'{label}_{name}.csv',rows)
            item['variants'][name]=existing.summary(rows)
        report['recorded'][label]=item
    (args.output/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'variants':len(VARIANTS),'virtual_runs':len(report['virtual']),
        'recordings':{k:v['samples'] for k,v in report['recorded'].items()}},ensure_ascii=False))


if __name__=='__main__':main()
