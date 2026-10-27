"""Compare depth braking on a common causal timeline; no devices, no network.

--source may point at a preserved Group 3 directory for before/after replay.
--without-lead is an offline ablation only; no flight configuration is changed.
The dynamics, image perturbations, and metre values below are synthetic.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import sys
from unittest.mock import patch


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=Path(__file__).parent)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--without-lead',action='store_true')
    p.add_argument('--matrix',action='store_true')
    p.add_argument('--config',type=Path)
    p.add_argument('--mode',choices=('relative_size','pose'),default='relative_size')
    p.add_argument('--only',nargs='+',help='explicit subset of named scenarios')
    a=p.parse_args()
    if sys.version_info[:2]!=(3,11):raise SystemExit('Use Python 3.11')
    sys.path.insert(0,str(a.source.resolve()))
    import cv2
    import timeline_flight_check as model
    from r3_follow import __version__
    if a.without_lead:
        from r3_follow.depth_brake import DepthBrake
        # Isolate immediate deceleration/sending from the added depth lead.
        DepthBrake.limit=lambda self,request,**kw:request
    cv2.setNumThreads(1)
    a.output.mkdir(parents=True,exist_ok=True)
    calibration=a.output/'SYNTHETIC_not_for_flight.json'
    calibration.write_text(json.dumps(dict(schema='r3_camera_calibration_v1',camera_id='timeline-synthetic-only',
        image_size=[model.W,model.H],camera_matrix=[[model.F,0,model.W/2],[0,model.F,model.H/2],[0,0,1]],
        dist_coeffs=[0]*5,rms_px=0.,source='synthetic_not_for_flight')),encoding='utf8')
    base_target=model.target_at
    def target(case,t):
        if case in ('depth_noise','small_side_turn'):
            angle=math.radians(20)*min(1,max(0,t-2),max(0,7-t)) if case=='small_side_turn' else 0
            return (0.,2.,1.2),angle,True,0
        if case=='long_chase':
            return (0.,2.+.3*max(0,min(t-2,18)),1.2),0.,True,0
        return base_target(case,t)
    specs=[dict(name=f'{case}_tau{response}',case=case,response=response,delay=delay,costs=[cost],duration=14.)
        for response,delay,cost in ((.1,0.,.03),(.25,0.,.03),(1.,.2,.10))
        for case in ('chase_stop','chase_fast','reverse')]
    specs += [dict(name=case,case=case,response=.25,delay=0.,costs=[.03,.10,.04,.06],duration=14.)
              for case in ('lateral_reverse','side_turn','small_side_turn','depth_noise','loss','wrong_id',
                           'explicit_stop','frozen','cpu_spike','blur_jitter','delay_and_loss')]
    specs.append(dict(name='long_chase',case='long_chase',response=1.,delay=.2,costs=[.10],duration=32.))
    if a.matrix:
        specs += [dict(name=f'matrix_{case}_tau{response}_delay{delay}_cost{cost}',case=case,response=response,
                       delay=delay,costs=[cost],duration=14.)
            for case in ('chase_stop','reverse') for response in (.1,.25,1.)
            for delay in (0.,.2,.4,.6) for cost in (.03,.10,.30)]
    if a.only:
        unknown=set(a.only)-{s['name'] for s in specs}
        if unknown:raise ValueError('Unknown scenarios: '+str(sorted(unknown)))
        specs=[s for s in specs if s['name'] in a.only]
    class RecordedTimeline(model.Timeline):
        def record(self):
            super().record()
            brake=getattr(self.service.controller,'_depth_brake',None)
            state=brake.status() if brake else {}
            self.rows[-1].update(depth_lead_correction_rc=state.get('correction',0)*100,
                depth_lead_reason=state.get('reason','previous_no_depth_lead'),
                depth_lead_rate_per_s=state.get('relative_rate_per_s'),
                pid_raw_forward_rc=self.service.controller._openpose._cached[1]*100)
    # Deterministic input specification for both implementations, independent
    # of this machine's transient CPU load. Decoder work is still budgeted.
    spec_text=json.dumps(specs,sort_keys=True)
    (a.output/'scenario_spec.json').write_text(spec_text,encoding='utf8')
    results=[]
    with patch('socket.socket',side_effect=AssertionError('Network forbidden')), \
         patch('cv2.VideoCapture',side_effect=AssertionError('Camera forbidden')), \
         patch('tkinter.Tk',side_effect=AssertionError('Desktop forbidden')),patch.object(model,'target_at',target):
        for spec in specs:
            kwargs={k:spec[k] for k in ('case','response','delay','costs')}
            if spec['case']=='delay_and_loss':kwargs['delay']=.2
            w=RecordedTimeline(**kwargs,mode=a.mode,config=a.config,calibration=calibration)
            actual_render=model.render
            def perturb(drone,pos,angle,visible=True,marker_id=0):
                if spec['case']=='depth_noise':
                    # Perturb measured size only; true target remains fixed.
                    pos=(pos[0],pos[1]+.025*math.sin(w.t*43),pos[2])
                return actual_render(drone,pos,angle,visible,marker_id)
            with patch.object(model,'render',perturb):
                result,events=w.run(spec['duration'])
            result['max_request_step_rc']=max((abs(b['rc_forward']-c['rc_forward']) for c,b in zip(w.rows,w.rows[1:])),default=0)
            moving=[r for r in w.rows if 2<=r['t']<6 and r['started']]
            result['chase_mean_lag_virtual_m']=sum(r['virtual_depth_m']-2 for r in moving)/len(moving) if moving else None
            settled=[r for r in w.rows if r['t']>=spec['duration']-2 and r['started']]
            result['final_two_s_max_abs_error_virtual_m']=max((abs(r['virtual_depth_m']-2) for r in settled),default=None)
            result['depth_brake_events']=sum(e.get('depth_brake',{}).get('reason')=='approach_braking' for e in events)
            result['spec']=spec
            model.save_run(a.output,spec['name'],w,result,events);results.append(result)
            print(json.dumps(dict(name=spec['name'],minimum=result['min_virtual_depth_m'],
                                 maximum=result['max_virtual_depth_m'],state=result['last_state'])),flush=True)
    report=dict(controller_version=__version__,python=sys.version,mode=a.mode,config=str(a.config),
        ablation_without_lead=a.without_lead,scenario_sha256=hashlib.sha256(spec_text.encode()).hexdigest(),
        runs=results,assumptions=dict(rc_gain=2.,response_seconds='varied',reference_depth_m=2.,
            capture_rate_hz=30.,control_tick_s=.02,regular_send_hz=10.,
            hardware_verified=False,CPU_work_seconds='injected identical duration sequence, budget enforced',
            physical_model='first order; ignores airframe attitude/collision/network/OS effects'))
    (a.output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(dict(runs=len(results),version=__version__)))


if __name__=='__main__':main()
