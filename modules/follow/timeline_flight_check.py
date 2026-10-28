"""Single timeline: real detector/service, simulated concurrent workers and physics.

No camera, SDK or aircraft. During detector work the virtual world, camera and
20 ms control worker continue. A result is published ONLY on completion; old
capture timestamps and lifecycle tokens stay intact. The native detector is
actually run, but its measured CPU duration is advanced as virtual elapsed time
after that call (not a claim about OS scheduling). A recorded duration profile
can instead be replayed for fair, repeatable controller comparisons. The plant
is invented, so virtual metres/seconds are NOT calibrated Tello performance.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import time
from dataclasses import replace
from collections import Counter
from unittest.mock import patch

import cv2
import numpy as np
from response_flight_check import render, W, H, F
from r3_follow.config import load_config
from r3_follow.mock_port import MockPort
from r3_follow.service import FollowService
from r3_follow.vision import MarkerTracker
from r3_follow import __version__ as CONTROLLER_VERSION

VERSION = 'r3-4f58686-braking1'
STEP = .01  # physics/event quantisation <= 10 ms; control at exact 20 ms ticks
EPOCH = 1800000000.


def target_at(case, t):
    x, y, z, angle, visible, marker_id = 0., 2., 1.2, 0., True, 0
    # Stop at t=6. Reverse cases turn back at t=6, stop again at t=10.
    speed = .65 if case == 'chase_fast' else .3
    if case not in ('side_turn', 'loss', 'wrong_id'):
        y += speed * max(0., min(t-2., 4.))
    if case == 'reverse':
        y -= .5 * max(0., min(t-6., 4.))
    if case == 'lateral_reverse':
        x = .5 * math.sin(max(0., min(t-2., 8.)) * .8)
    if case == 'side_turn':
        angle = math.radians(45) * min(1., max(0., t-2.), max(0., 7.-t))
        y += .3 * max(0., min(t-8., 2.))
    if case in ('loss', 'delay_and_loss'):
        visible = not 3 <= t < 4
    if case == 'wrong_id':
        marker_id = 1 if 3 <= t < 5 else 0
    return (x, y, z), angle, visible, marker_id


class TimedTracker(MarkerTracker):
    def __init__(self, world):
        # In duration replay, do not let this machine's incidental CPU load
        # change a shared trace. Apply the scheduled work to the SAME service
        # deadline after decoding. The model is a noninterruptible work block;
        # it cannot claim to model early cancellation between native stages.
        super().__init__(target_id=world.config.target_id, processing_budget_ms=120.,
                         clock=time.perf_counter if world.costs is None else lambda: 0.)
        self.world = world

    def detect(self, frame, frame_id, acquired_at, **options):
        began = time.perf_counter()
        observation = super().detect(frame, frame_id, acquired_at, **options)
        measured = time.perf_counter() - began
        world = self.world
        sequence = len(world.jobs)
        # Profile entries are durations previously measured on real generated
        # pixels, or explicit injected costs. Neither refreshes capture age.
        cost = measured if world.costs is None else world.costs[sequence % len(world.costs)]
        if world.case == 'cpu_spike' and 3 <= world.t < 4:
            cost = max(cost, 1.15)  # single native call may exceed cooperative budget
        limit = options.get('processing_budget_ms',120.)/1000
        scheduled_budget_exceeded = cost >= limit
        if scheduled_budget_exceeded:
            observation = replace(observation,found=False,center_x=None,center_y=None,
                                  size_ratio=None,corners=(),reason='detection_budget_exceeded')
            self.last_diagnostics['processing_budget'].update(
                exceeded=True,elapsed_ms=cost*1000,exhausted_at='scheduled_work_complete')
            self.last_diagnostics['detected_ids']=[]
            self.last_diagnostics['detected_markers']=[]
            self.last_diagnostics.pop('target_quality',None)
        start = world.t
        job = dict(frame_id=frame_id, capture_at=acquired_at, started_at=start,
                   measured_cpu_ms=measured*1000, scheduled_cost_ms=cost*1000,
                   found=observation.found, reason=observation.reason,
                   budget_ms=options.get('processing_budget_ms'),
                   scheduled_budget_exceeded=scheduled_budget_exceeded,
                   geometric_visible=world.metadata.get(frame_id, {}).get('visible'))
        world.advance_to(start + cost)
        job.update(completed_at=world.t, result_age_ms=(world.t-acquired_at)*1000)
        world.jobs.append(job)
        return observation


class Timeline:
    def __init__(self, *, case='chase_stop', costs=None, delay=0., response=.25,
                 gain=2., mode='relative_size', config=None, calibration=None):
        self.config = load_config(config or Path(__file__).parent/'configs/aircraft_A_ID0.json')
        self.case, self.costs = case, costs
        self.delay, self.response, self.gain = delay, response, gain
        if costs is not None and (not costs or any(not math.isfinite(c) or c < 0 for c in costs)):
            raise ValueError('cost trace must contain finite nonnegative seconds')
        if any(not math.isfinite(v) or v < 0 for v in (delay, response, gain)) or response == 0:
            raise ValueError('invalid virtual delay/plant parameters')
        self.t, self.step, self.frame_id, self.next_capture = 0., 0, 0, 0.
        self.drone = np.array([0., 0., 1.2, 0.])
        self.velocity = np.zeros(4)
        self.pending, self.metadata, self.jobs, self.rows = [], {}, [], []
        self.started, self.stop_sent = False, False
        self.port = MockPort(monotonic=lambda: self.t)
        kwargs = self.config.to_dict()
        kwargs['distance_mode'] = mode
        if mode == 'pose':
            if calibration is None:
                raise ValueError('pose simulation requires explicit synthetic calibration file')
            kwargs.update(camera_calibration=str(Path(calibration).resolve()),
                          camera_id='timeline-synthetic-only', marker_side_m=.24)
        self.service = FollowService(self.port, tracker=TimedTracker(self), monotonic=lambda: self.t,
            wall_clock=lambda: EPOCH+self.t, recovery_window=3., log_directory=False, **kwargs)
        self.camera_tick()

    def camera_tick(self):
        if self.t + 1e-9 >= self.next_capture:
            target, angle, visible, marker_id = target_at(self.case, self.t)
            marker_id = (self.config.target_id + marker_id) % 50
            frame, geometric_visible = render(self.drone, target, angle, visible, marker_id)
            if self.case == 'blur_jitter' and 3 <= self.t < 5:
                frame = cv2.GaussianBlur(frame, (7, 7), 1.5)
            if not (self.case == 'frozen' and 3 <= self.t < 4):
                self.metadata[self.frame_id] = dict(visible=geometric_visible, target_id=marker_id)
                self.pending.append((self.t+self.delay, self.frame_id, self.t, frame))
                self.frame_id += 1
            interval = (1/30 if self.case != 'blur_jitter'
                        else (.03, .06, .13, .04, .08)[self.frame_id % 5])
            self.next_capture += interval
        available = [v for v in self.pending if v[0] <= self.t+1e-9]
        if available:
            _, seq, acquired, frame = available[-1]
            self.port.publish(frame, seq, EPOCH+acquired)
            self.pending = [v for v in self.pending if v[1] > seq]

    def advance_to(self, end):
        # Native work is pending; capture_once is deliberately not re-entered.
        while self.t < end-1e-9:
            next_tick = (self.step+1)*STEP
            dt = min(next_tick, end)-self.t
            setpoint = np.array(self.port.velocity)/100 * np.array([self.gain]*3+[2.])
            alpha = -math.expm1(-dt/self.response)
            # Analytic first-order displacement, avoiding Euler braking bias.
            displacement = setpoint*dt + (self.velocity-setpoint)*self.response*alpha
            self.velocity += alpha*(setpoint-self.velocity)
            heading = self.drone[3]
            self.drone += [math.cos(heading)*displacement[0]+math.sin(heading)*displacement[1],
                           -math.sin(heading)*displacement[0]+math.cos(heading)*displacement[1],
                           displacement[2], displacement[3]]
            self.t = min(next_tick, end)
            if self.t >= next_tick-1e-9:
                self.step += 1
                self.camera_tick()
                if self.case == 'explicit_stop' and self.t >= 6 and not self.stop_sent:
                    self.service.stop_follow(); self.stop_sent = True
                self.port.height_cm = self.drone[2]*100
                self.port.watchdog_step()
                if self.step % 2 == 0:
                    self.service.control_once()
                    self.record()

    def record(self):
        obs = self.service.controller.observation
        state = str(self.service.controller.state)
        channels = self.port.velocity
        if state != 'tracking':
            assert not any(channels), 'nontracking retained motion'
        assert all(type(v) is int and abs(v) <= cap for v, cap in zip(channels, self.service.rc_caps))
        target = target_at(self.case, self.t)[0]
        depth = ((target[0]-self.drone[0])*math.sin(self.drone[3])
                 +(target[1]-self.drone[1])*math.cos(self.drone[3]))
        distance = self.service.controller.distance_status()
        self.rows.append(dict(t=self.t, state=state, started=self.started,
            rc_right=channels[0], rc_forward=channels[1], rc_up=channels[2], rc_yaw=channels[3],
            virtual_forward_m_s=self.velocity[1], virtual_yaw_rad_s=self.velocity[3],
            target_y=target[1], drone_y=self.drone[1], virtual_depth_m=depth,
            depth_error_virtual_m=depth-2., lateral_error_virtual_m=target[0]-self.drone[0],
            latest_capture_id=self.port.snapshot.frame_id,
            published_frame_id=obs.frame_id if obs else None,
            observation_age_ms=(self.t-obs.acquired_at)*1000 if obs else None,
            found=bool(obs and obs.found), vision_busy=self.service.vision_in_progress is not None,
            size_ratio=obs.size_ratio if obs else None,
            size_error=(self.service.controller.reference_size-obs.size_ratio
                        if obs and obs.found and self.service.controller.reference_size else None),
            measured_relative_depth_error=self.service.controller._openpose.relative_depth_error,
            depth_allowed=distance['forward_allowed'], depth_reason=distance['reason'],
            reason=self.service.controller.last_request.reason))

    def run(self, duration=14.):
        while self.t < duration:
            # No warm-up bypass: initial packets travel the same delayed path.
            if self.port.snapshot.frame is not None:
                self.service.capture_once()
                if not self.started and self.t < 2.:
                    if self.service.calibrate_reference().ok:
                        self.started = self.service.start_follow().ok
            self.advance_to(self.t+.03)  # real service vision worker's idle interval
        rows = [r for r in self.rows if r['started']]
        active = [r for r in rows if r['state']=='tracking']
        events = list(self.service.events)
        self.service.close(wait=False)
        ranges = [r['virtual_depth_m'] for r in rows]
        def reversals(key):
            signs = [int(math.copysign(1,r[key])) for r in active if abs(r[key]) >= 1]
            return sum(a!=b for a,b in zip(signs, signs[1:]))
        post_stop = [r for r in rows if r['t'] >= 6]
        braking = [r for r in rows if 6 <= r['t'] <= 8]
        close = next((r for r in rows if r['t'] >= 6
                      and r['measured_relative_depth_error'] is not None
                      and r['measured_relative_depth_error'] < -.01),None)
        negative = next((r for r in rows if close and r['t'] >= close['t'] and r['rc_forward'] < 0),None)
        after_stop = [r for r in rows if r['t'] >= 6] if self.case=='explicit_stop' else []
        summary = dict(case=self.case, distance_mode=self.service.config.distance_mode,
            controller_version=CONTROLLER_VERSION,
            started=self.started, duration_s=self.t, input_delay_s=self.delay,
            assumed_response_s=self.response, assumed_rc_gain=self.gain,
            cost_mode='measured' if self.costs is None else 'replayed_duration_trace',
            detected_frames=sum(j['found'] for j in self.jobs), processed_frames=len(self.jobs),
            last_state=self.rows[-1]['state'], states=dict(Counter(r['state'] for r in rows)),
            measured_cpu_ms_p50=statistics.median(j['measured_cpu_ms'] for j in self.jobs) if self.jobs else None,
            measured_cpu_ms_max=max((j['measured_cpu_ms'] for j in self.jobs),default=None),
            scheduled_cost_ms_max=max((j['scheduled_cost_ms'] for j in self.jobs),default=None),
            budget_exceeded_jobs=sum(j['scheduled_budget_exceeded'] for j in self.jobs),
            result_age_ms_max=max((j['result_age_ms'] for j in self.jobs),default=None),
            control_ticks_while_vision_busy=sum(r['vision_busy'] for r in self.rows),
            min_virtual_depth_m=min(ranges,default=None),max_virtual_depth_m=max(ranges,default=None),
            final_virtual_depth_m=ranges[-1] if ranges else None,
            post_stop_min_virtual_depth_m=min((r['virtual_depth_m'] for r in post_stop),default=None),
            post_stop_two_s_travel_virtual_m=(braking[-1]['drone_y']-braking[0]['drone_y'] if braking else None),
            target_stop_travel_is_not_emergency_braking_distance=True,
            first_close_measurement_t=close['t'] if close else None,
            first_negative_request_after_close_s=negative['t']-close['t'] if negative else None,
            explicit_stop_coast_virtual_m=(after_stop[-1]['drone_y']-after_stop[0]['drone_y'] if after_stop else None),
            forward_request_sign_changes=reversals('rc_forward'),yaw_request_sign_changes=reversals('rc_yaw'),
            visible_depth_blocked_ticks=sum(r['found'] and not r['depth_allowed'] for r in active),
            nontracking_nonzero_ticks=0, # asserted above on ALL control ticks
            faults=[dict(time=e['time'],reason=e['reason']) for e in events if e['event']=='fault'],
            hardware_verified=False, operating_system_thread_timing_verified=False)
        return summary, events


def save_run(directory, name, world, summary, events):
    directory.mkdir(parents=True, exist_ok=True)
    for suffix, data in (('steps',world.rows),('vision',world.jobs)):
        if not data: continue
        with (directory/f'{name}_{suffix}.csv').open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(data[0]));w.writeheader();w.writerows(data)
    (directory/f'{name}_events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf8')
    # Standalone vector plot with distinct units, no plotting dependency needed.
    panels=[('virtual_depth_m','Virtual depth (m; assumed plant)',2.),
            ('rc_forward','Forward request (RC; NOT cm/s)',0.),
            ('virtual_forward_m_s','Virtual forward velocity (m/s)',0.),
            ('observation_age_ms','Published observation age (ms)',1000.)]
    lines=['<svg xmlns="http://www.w3.org/2000/svg" width="960" height="820" viewBox="0 0 960 820">',
           '<rect width="960" height="820" fill="white"/>',
           f'<text x="60" y="24" font-family="sans-serif" font-size="17">{name} — synthetic plant, no flight validation</text>']
    for n,(key,label,ref) in enumerate(panels):
        points=[(r['t'],r[key]) for r in world.rows if r[key] is not None]
        low=min([v for _,v in points]+[ref]);high=max([v for _,v in points]+[ref]);span=max(.01,high-low)
        top=60+n*185
        def y(v):return top+130-(v-low)/span*120
        lines += [f'<text x="60" y="{top-8}" font-family="sans-serif" font-size="14">{label}</text>',
                  f'<path d="M60 {y(ref):.2f}H920" stroke="#999" stroke-dasharray="5 4"/>',
                  f'<text x="2" y="{y(high):.1f}" font-size="12">{high:.2f}</text>',
                  f'<text x="2" y="{y(low):.1f}" font-size="12">{low:.2f}</text>']
        path=' '.join(('M' if i==0 else 'L')+f'{60+t/world.t*860:.2f} {y(v):.2f}' for i,(t,v) in enumerate(points))
        lines.append(f'<path d="{path}" fill="none" stroke="#167b88" stroke-width="1.4"/>')
        for t in (0,2,6,10,14):
            x=60+t/world.t*860
            lines.append(f'<text x="{x:.1f}" y="{top+150}" font-size="12">{t}s</text>')
    lines.append('</svg>')
    (directory/f'{name}.svg').write_text('\n'.join(lines),encoding='utf8')
    summary['name']=name


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--mode',choices=('relative_size','pose'),default='relative_size')
    p.add_argument('--matrix',action='store_true')
    p.add_argument('--trace',type=Path,help='common previously recorded measured durations (seconds)')
    args=p.parse_args()
    if sys.version_info[:2]!=(3,11):raise SystemExit('Use Python 3.11')
    args.output.mkdir(parents=True,exist_ok=True);cv2.setNumThreads(1)
    synthetic=args.output/'SYNTHETIC_not_for_flight.json'
    synthetic.write_text(json.dumps(dict(schema='r3_camera_calibration_v1',camera_id='timeline-synthetic-only',
        image_size=[W,H],camera_matrix=[[F,0,W/2],[0,F,H/2],[0,0,1]],dist_coeffs=[0]*5,rms_px=0.,
        source='synthetic_not_for_flight')),encoding='utf8')
    results=[]
    with patch('socket.socket',side_effect=AssertionError('Network forbidden')), \
         patch('cv2.VideoCapture',side_effect=AssertionError('Camera forbidden')), \
         patch('tkinter.Tk',side_effect=AssertionError('Desktop forbidden')):
        if args.trace:
            costs=json.loads(args.trace.read_text(encoding='utf8'))['costs_s']
        else:
            world=Timeline(case='chase_stop',mode=args.mode,calibration=synthetic)
            result,events=world.run();save_run(args.output,'actual_measured_cost',world,result,events);results.append(result)
            costs=[j['measured_cpu_ms']/1000 for j in world.jobs]
        trace=dict(costs_s=costs,source='actual native detector on synthetic frames; machine dependent',
                   physical_performance=False)
        trace_text=json.dumps(trace,indent=2);(args.output/'cost_trace.json').write_text(trace_text,encoding='utf8')
        for case in ('chase_stop','chase_fast','reverse','lateral_reverse','side_turn','loss',
                     'wrong_id','explicit_stop','frozen','cpu_spike','blur_jitter','delay_and_loss'):
            delay=.2 if case=='delay_and_loss' else 0.
            world=Timeline(case=case,costs=costs,delay=delay,mode=args.mode,calibration=synthetic)
            result,events=world.run();save_run(args.output,case,world,result,events);results.append(result)
        if args.matrix:
            for response in (.1,.25,1.):
                for delay in (0.,.2,.4,.6,1.1):
                    for cost in (.03,.10,.3):
                        name=f'matrix_tau{response}_delay{delay}_cost{cost}'
                        world=Timeline(case='reverse',costs=[cost],delay=delay,response=response,
                                       mode=args.mode,calibration=synthetic)
                        result,events=world.run();save_run(args.output,name,world,result,events);results.append(result)
        (args.output/'summary.json').write_text(json.dumps(dict(version=VERSION,python=sys.version,
            runs=results,trace_sha256=hashlib.sha256(trace_text.encode()).hexdigest(),
            assumptions=dict(physics_step_s=STEP,camera_interval_quantisation_s=STEP,
                control_tick_s=.02,vision_worker_idle_s=.03,motion_send_interval_s=.1,
                detector_blocks_only_vision_worker=True,actual_service_used=True,
                replay_budget_policy='one noninterruptible work block, discard at service deadline',
                rc_to_speed='invented gain; not Tello calibration',marker_side_virtual_m=.24,
                reference_depth_virtual_m=2.,cpu_mode='measured run then common measured duration replay',
                camera_read_decode_transport_cost='only explicit delay, not measured',
                hardware_verified=False)),ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(dict(runs=len(results),started=sum(r['started'] for r in results),output=str(args.output))))


if __name__=='__main__':main()
