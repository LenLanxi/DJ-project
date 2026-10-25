"""Replay recorded observations through the real service and MockPort.

No image re-decoding, aircraft or automatic re-start. Original observations
arrive at their logged completion times; the deterministic control tick is
50ms. This checks state/command eligibility, not counterfactual aircraft motion.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import r3_follow.core as core
from r3_follow.service import FollowService
from r3_follow.mock_port import MockPort
from pid_adaptation_check import write_csv


def replay(path,old_rule=False):
    records=[dict(json.loads(s),line=n) for n,s in enumerate(path.read_text(
        encoding='utf-8').splitlines(),1) if s.strip()]
    start=next(r for r in records if r['event']=='start')
    reference=next(r['reference_size'] for r in records if r['event']=='calibration' and r['ok'])
    seed=[r for r in records if r['event']=='observation' and r['line']<start['line']][-1]
    frames=[r for r in records if r['event']=='observation' and r['line']>start['line']]
    class Clock:
        now=10.
        def mono(self):return self.now
        def wall(self):return start['unix_time']+self.now-10.
    class Detector:
        def detect(self,r,frame_id,acquired_at,source):
            self.last_diagnostics={'duplicate_check':r.get('duplicate_check')}
            return core.Observation(frame_id,acquired_at,source,r['image_width'],
                r['image_height'],r['observation_target_id'],r['found'],r['center_x'],
                r['center_y'],r['size_ratio'],tuple(map(tuple,r['corners'])),r['reason'])
    clock=Clock();port=MockPort(clock.mono)
    reasons=tuple(x for x in core.RECOVERABLE_MISS_REASONS if
        not(old_rule and x=='ambiguous_scan_budget_exceeded'))
    trace=[]
    with patch.object(core,'RECOVERABLE_MISS_REASONS',reasons):
        svc=FollowService(port,tracker=Detector(),monotonic=clock.mono,wall_clock=clock.wall,
            control_profile='openpose_pid',recovery_window=3,log_directory=False)
        try:
            port.publish(seed,seed['frame_id'],seed['timestamp']);svc.capture_once()
            assert svc.calibrate_reference().ok
            svc.controller.reference_size=reference
            assert svc.start_follow().ok
            end=10+frames[-1]['unix_time']-start['unix_time']
            events=[(10+r['unix_time']-start['unix_time'],0,r) for r in frames]
            events += [(10+n*.05,1,None) for n in range(1,int((end-10)/.05)+1)]
            for t,kind,r in sorted(events,key=lambda e:(e[0],e[1])):
                clock.now=t
                if kind==0:
                    port.publish(r,r['frame_id'],r['timestamp']);svc.capture_once()
                else:
                    svc.control_once();s=svc.get_follow_status()
                    trace.append(dict(elapsed=round(t-10,5),state=s['state'],
                        forward_rc=port.velocity[1],yaw_rc=port.velocity[3],
                        reason=s['last_halt_reason'],frame_id=s['frame_id']))
            transitions=[];previous='tracking'
            for row in trace:
                if row['state']!=previous:transitions.append(row.copy())
                previous=row['state']
            metrics=dict(old_rule=old_rule,observations=len(frames),duration_s=end-10,
                state_seconds={k:round(v*.05,3) for k,v in Counter(r['state'] for r in trace).items()},
                transitions=transitions,
                recoveries=sum(r['state']=='tracking' for r in transitions),
                nontracking_nonzero=sum(r['state']!='tracking' and bool(r['forward_rc'] or r['yaw_rc']) for r in trace),
                motion_requests=sum(e['event']=='motion_request' for e in svc.events),
                nonzero_requests=sum(e['event']=='motion_request' and any(e['channels']) for e in svc.events),
                halt_reasons=[e['reason'] for e in svc.events if e['event']=='tracking_halted'])
        finally:svc.close()
    return trace,metrics


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--log',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    report=dict(source=args.log.name,sha256=hashlib.sha256(args.log.read_bytes()).hexdigest(),
        limits=['Fixed observations replayed through actual FollowService and MockPort, not flight.',
                'Old rule comparison changes only the incomplete-scan recoverability classification.',
                'No restart or clearing confirmed duplicate/stale/stop faults is injected.'])
    for name,old in [('before',True),('after',False)]:
        trace,metrics=replay(args.log,old);report[name]=metrics
        write_csv(args.output/f'{name}.csv',trace)
    (args.output/'recovery_replay.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:{n:v for n,v in report[k].items() if n!='transitions'} for k in ('before','after')},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
