"""Generated ArUco pixels -> real detector -> service -> isolated mock ports.

Detector time is injected with a logical clock AFTER real OpenCV calls. This
checks lifecycle timing, not native execution speed or real camera performance.
The 434 ms construction reproduces supplied timing relationships, not video.
"""
import argparse
from collections import deque
import csv
import json
from pathlib import Path
import socket
import time
from unittest.mock import patch

import cv2
import numpy as np

from multi_target_demo import marker_scene, expected_geometry
from r3_follow.mock_port import MockPort
from r3_follow.service import FollowService
from r3_follow.vision import MarkerTracker


class Clock:
    def __init__(self):
        self.value = 100.

    def __call__(self):
        return self.value


class DelayScript:
    def __init__(self, clock):
        self.clock, self.costs = clock, deque()
        self.callbacks, self.control_tick = [], lambda: None
        self.native_calls = 0

    def advance(self, seconds):
        finish = self.clock.value+seconds
        while self.clock.value < finish-1e-10:
            due = [at for at, _ in self.callbacks if self.clock.value < at <= finish]
            self.clock.value = min(finish, self.clock.value+.02, min(due) if due else finish)
            callbacks = [(at, callback) for at, callback in self.callbacks
                         if at <= self.clock.value+1e-10]
            self.callbacks = [(at, callback) for at, callback in self.callbacks
                              if at > self.clock.value+1e-10]
            for _, callback in callbacks:
                callback()
            self.control_tick()

    def wrap(self, native):
        script = self
        class Decoder:
            def detectMarkers(self, image):
                # Always perform actual current-pixel decoding. Only its
                # elapsed logical time is controlled by this demonstration.
                result = native.detectMarkers(image)
                script.native_calls += 1
                if script.costs:
                    script.advance(script.costs.popleft())
                return result
        return Decoder()


class UnboundedReference:
    """Same real decoder without cooperative cancellation, for timing only."""
    def __init__(self, tracker):
        self.tracker = tracker
        self.target_id = tracker.target_id

    def detect(self, *args, **kwargs):
        return self.tracker.detect(*args, **kwargs)

    @property
    def last_diagnostics(self):
        return self.tracker.last_diagnostics


def make_service(clock, target, *, unbounded=False):
    script = DelayScript(clock)
    tracker = MarkerTracker(target, processing_budget_ms=None if unbounded else 120, clock=clock)
    for name in ('detector', 'fallback_detector', 'motion_detector', 'side_detector'):
        setattr(tracker, name, script.wrap(getattr(tracker, name)))
    port = MockPort(clock)
    service = FollowService(port, tracker=UnboundedReference(tracker) if unbounded else tracker,
                            monotonic=clock, wall_clock=clock, recovery_window=3,
                            control_profile='openpose_pid', target_id=target,
                            instance_id=f'latency-ID{target}', log_directory=False)
    return service, port, tracker, script


def run(output):
    import sys
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError('Use Python 3.11')
    cv2.setNumThreads(1)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    rows, checks, violations, all_services, all_scripts = [], {}, [], [], []
    started = time.perf_counter()

    def snapshot(scenario, phase, step, service, port, tracker, *, truth=True):
        status = service.get_follow_status()
        obs = service.controller.observation
        channels = tuple(port.velocity)
        if any(channels) and (status['state'] != 'tracking' or not status['observation_fresh']):
            violations.append({'scenario': scenario, 'step': step, 'target': service.config.target_id,
                               'reason': 'inactive_or_stale_nonzero'})
        if obs and obs.found and truth:
            x, y, size = expected_geometry(obs.frame_id, service.config.target_id)
            if (obs.target_id != service.config.target_id or abs(obs.center_x-x) > 2
                    or abs(obs.center_y-y) > 2 or abs(obs.size_ratio-size) > .003):
                violations.append({'scenario': scenario, 'step': step, 'target': service.config.target_id,
                                   'reason': 'wrong_marker_geometry'})
        rows.append(dict(scenario=scenario, phase=phase, step=step,
            logical_time=service.clock(), target_id=service.config.target_id,
            state=status['state'], following=status['following'], restart_required=status['restart_required'],
            frame_id=None if obs is None else obs.frame_id,
            found=False if obs is None else obs.found,
            observation_reason=None if obs is None else obs.reason,
            center_x=None if obs is None else obs.center_x,
            frame_age_ms=status['observation_age_ms'], left_right=channels[0],
            forward_back=channels[1], up_down=channels[2], yaw=channels[3],
            budget_exceeded=tracker.last_diagnostics.get('processing_budget', {}).get('exceeded', False),
            injected_detection_ms=tracker.last_diagnostics.get('detect_ms'),
            status_message=status['status_message']))
        return rows[-1]

    def group(targets=(0,), unbounded=False):
        clock = Clock()
        members = [make_service(clock, target, unbounded=unbounded) for target in targets]
        all_services.extend(member[0] for member in members)
        all_scripts.extend(member[3] for member in members)
        def tick():
            for service, *_ in members:
                service.control_once()
        for *_, script in members:
            script.control_tick = tick
        return clock, members

    def frame_step(scenario, phase, step, clock, members, costs=None):
        frame = marker_scene(step, {0, 1, 2})
        stamp = clock.value
        for service, port, tracker, script in members:
            size = getattr(service, '_benchmark_image_size', None)
            pixels = frame
            if size:
                scale = min(size[0]/frame.shape[1], size[1]/frame.shape[0])
                resized = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
                pixels = np.full((size[1],size[0],3),245,dtype=np.uint8)
                left,top = (size[0]-resized.shape[1])//2,(size[1]-resized.shape[0])//2
                pixels[top:top+resized.shape[0],left:left+resized.shape[1]] = resized
            script.costs = deque((costs or {}).get(service.config.target_id, ()))
            port.publish(pixels, step, stamp)
            service.capture_once()
            if step == 0:
                if not service.calibrate_reference().ok or not service.start_follow().ok:
                    violations.append({'scenario': scenario, 'reason': 'cannot_start_valid_marker'})
            service.control_once()
            snapshot(scenario, phase, step, service, port, tracker, truth=size is None)

    def prime(scenario, clock, members):
        for step in range(6):
            if step:
                clock.value += .1
            frame_step(scenario, 'tracking', step, clock, members)

    try:
        # Shared pixels, independent target IDs/ports: only ID1 is slowed.
        clock, members = group((0, 1, 2))
        scenario = 'three_ids_short_budget'
        prime(scenario, clock, members)
        for step in range(6, 13):
            clock.value += .1
            frame_step(scenario, 'budget_failure' if step == 6 else 'recovery', step,
                       clock, members, {1: [.130]} if step == 6 else None)
        own = [r for r in rows if r['scenario'] == scenario]
        for target in range(3):
            selected = [r for r in own if r['target_id'] == target]
            checks[f'ID{target}_actually_moves'] = any(r['yaw'] != 0 for r in selected if 2 <= r['step'] <= 5)
        failed = [r for r in own if r['step'] == 6 and r['target_id'] == 1][0]
        checks['short_budget_zero'] = (failed['state'] == 'recovering_target'
            and failed['observation_reason'] == 'detection_budget_exceeded'
            and failed['forward_back'] == failed['yaw'] == 0)
        recovering = [r for r in own if r['target_id'] == 1 and r['step'] in (7, 8)]
        checks['first_two_good_frames_still_zero'] = all(r['state'] == 'recovering_target'
            and r['forward_back'] == r['yaw'] == 0 for r in recovering)
        checks['third_good_frame_recovers'] = any(r['step'] == 9 and r['target_id'] == 1
                                                and r['state'] == 'tracking' for r in own)
        checks['unaffected_ID0_ID2_continue'] = all(r['state'] == 'tracking'
            for r in own if r['target_id'] in (0, 2))

        clock, members = group()
        scenario = 'persistent_budget_timeout'
        prime(scenario, clock, members)
        for step in range(6, 25):
            members[0][3].advance(.08)
            frame_step(scenario, 'budget_failure', step, clock, members, {0: [.130]})
        for step in range(25, 28):
            clock.value += .1
            frame_step(scenario, 'visible_after_timeout', step, clock, members)
        own = [r for r in rows if r['scenario'] == scenario]
        checks['persistent_budget_zeros_all_requests'] = all(r['forward_back'] == r['yaw'] == 0
            for r in own if r['step'] >= 6)
        checks['persistent_budget_latches_after_three_seconds'] = any(
            r['state'] == 'target_lost' and r['restart_required'] for r in own if r['step'] < 25)
        first_loss_at = next(r['logical_time'] for r in own if r['step'] == 6)
        timeout_events = [event for event in members[0][0].events
                          if event['event'] == 'tracking_halted'
                          and event['reason'] == 'recovery_timeout_requires_start']
        checks['persistent_budget_respects_full_three_second_window'] = bool(timeout_events) and (
            3.-1e-8 <= timeout_events[0]['time']-first_loss_at <= 3.03)
        checks['visible_after_timeout_does_not_restart'] = all(r['found'] and r['state'] == 'target_lost'
            and r['restart_required'] for r in own if r['step'] >= 25)

        clock, members = group((2,))
        scenario = 'true_frame_gap'
        prime(scenario, clock, members)
        service, port, tracker, script = members[0]
        script.advance(.51)
        lost = snapshot(scenario, 'no_new_frame_510ms', 6, service, port, tracker)
        checks['real_gap_still_latches'] = (lost['state'] == 'frame_stale'
                                           and lost['forward_back'] == lost['yaw'] == 0)
        clock.value += .01
        frame_step(scenario, 'new_visible_frame_after_hard_stop', 7, clock, members)
        checks['fresh_after_stale_does_not_auto_restart'] = rows[-1]['state'] == 'frame_stale'

        clock, members = group((1,))
        scenario = 'stop_during_detection'
        prime(scenario, clock, members)
        service, port, tracker, script = members[0]
        clock.value += .1
        script.callbacks.append((clock.value+.02, service.stop_follow))
        frame_step(scenario, 'stop_before_detector_returns', 6, clock, members, {1: [.040]})
        for step in (7, 8, 9):
            clock.value += .1
            frame_step(scenario, 'new_visible_frame_after_stop', step, clock, members)
        own = [r for r in rows if r['scenario'] == scenario and r['step'] >= 6]
        checks['stop_then_late_detection_never_restarts'] = all(r['state'] == 'stopped'
            and r['forward_back'] == r['yaw'] == 0 for r in own)
        checks['late_observation_explicitly_discarded'] = any(
            event['event'] == 'observation_discarded'
            and event['reason'] == 'lifecycle_changed_during_detection' for event in service.events)

        # Match supplied timing structure: last good capture t, next capture
        # t+.080, read t+.094, rescue chain .434; old result reaches age .504
        # before the new result returns. These are injected delays, not pixels
        # from the real flight or a claim about the same image's runtime.
        contrast = cv2.imdecode(np.frombuffer(
            (Path(__file__).parent/'tests'/'fixtures'/'contrast_rescue.png').read_bytes(), np.uint8), 0)
        timeline = {}
        for unbounded in (True, False):
            scenario = 'constructed_434ms_'+('unbounded' if unbounded else 'cooperative')
            clock, members = group(unbounded=unbounded)
            # Keep a single camera resolution throughout this timing scenario;
            # a genuine mid-session format change now correctly stops control.
            for member, *_ in members:
                member._benchmark_image_size = (contrast.shape[1], contrast.shape[0])
            prime(scenario, clock, members)
            service, port, tracker, script = members[0]
            last_good_at = clock.value
            clock.value = last_good_at+.094
            def timeline_tick():
                service.control_once()
                snapshot(scenario, 'during_rescue_chain', 6, service, port, tracker, truth=False)
            script.control_tick = timeline_tick
            script.callbacks.append((last_good_at+.504, lambda: None))
            script.costs = deque((.030, .075, .070, .080, .179))
            port.publish(contrast, 6, last_good_at+.080)
            service.capture_once()
            result = snapshot(scenario, 'rescue_returned', 6, service, port, tracker, truth=False)
            timeline[scenario] = {'last_good_capture': last_good_at,
                'new_frame_capture': last_good_at+.080, 'read_at': last_good_at+.094,
                'returned_at': clock.value, 'state_on_return': result['state'],
                'observation_reason': result['observation_reason'],
                'deadline': tracker.last_diagnostics.get('processing_budget'),
                'faults': [event for event in service.events if event['event'] in ('tracking_halted', 'fault')]}
            for step in (7, 8, 9, 10):
                script.control_tick = service.control_once
                script.costs.clear()
                clock.value += .1
                frame_step(scenario, 'subsequent_good_frame', step, clock, members)
            expected = 'frame_stale' if unbounded else 'tracking'
            checks['constructed_chain_'+('unbounded_latches' if unbounded else 'budget_recovers')] = rows[-1]['state'] == expected
        checks['constructed_unbounded_did_run_434ms'] = abs(
            timeline['constructed_434ms_unbounded']['returned_at']
            - timeline['constructed_434ms_unbounded']['read_at']-.434) < 1e-8
        checks['constructed_budget_cuts_chain_at_175ms'] = abs(
            timeline['constructed_434ms_cooperative']['returned_at']
            - timeline['constructed_434ms_cooperative']['read_at']-.175) < 1e-8
        checks['actual_marker_decoding_was_exercised'] = sum(s.native_calls for s in all_scripts) > 100
    finally:
        for service in all_services:
            service.close()

    with (output/'steps.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    result = dict(mode='offline_actual_marker_pixels_injected_timing_mock_ports',
        hardware_verified=False, camera_opened=False, steps=len(rows),
        checks=checks, violations=violations, constructed_field_timing=timeline,
        native_decoder_calls=sum(s.native_calls for s in all_scripts),
        processing_wall_s=time.perf_counter()-started,
        all_stopped=all(not any(s.port.velocity) and s.port.lease is None for s in all_services),
        limitations=['Delays are logical injections after real OpenCV decoding; this is not video replay.',
                     'Neither the original 434ms camera frame nor physical velocity is known.',
                     'Independent mock ports do not verify three-device radio/network/SDK routing.'])
    result['passed'] = all(checks.values()) and not violations and result['all_stopped']
    (output/'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with patch.object(socket, 'socket', side_effect=AssertionError('Network forbidden')), \
         patch('cv2.VideoCapture', side_effect=AssertionError('Camera forbidden')), \
         patch('cv2.imshow', side_effect=AssertionError('GUI forbidden')):
        raise SystemExit(run(args.output))
