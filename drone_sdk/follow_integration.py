"""Reviewed offline candidate: cancellable startup and retained handoff state.

Keep one follow service during a normal model action. Explicit stop invalidates
its ticket and closes workers. No hardware connection is created here.
"""
import sys
import threading
import time
from pathlib import Path
from .contract import ControlResult

REPOSITORY = Path(__file__).resolve().parent.parent
MODULES_DIR = REPOSITORY / 'modules' / 'follow'
FIRST_FRAME_TIMEOUT = 1.0
RECOVERY_WINDOW = 3


class FollowIntegration:
    def __init__(self, owner_getter=None):
        self._lock = threading.RLock()
        self._owner_getter = owner_getter or (lambda: 'none')
        self._bridge = self._port = None
        self._frame_provider = None
        self._last_events = []
        self._generation = 0
        self._starting = False
        self._closing = False
        self._retired_workers = []
        self._pending_cleanup = []
        self._operation_source = None

    @staticmethod
    def _cancelled():
        from .controller import operation_cancelled
        return operation_cancelled()

    @staticmethod
    def _operation():
        from .controller import operation_event
        return operation_event()

    def set_frame_provider(self, provider):
        with self._lock:
            self._frame_provider = provider
            if self._port is not None:
                self._port.frame_provider = provider

    def _fail(self, reason):
        return ControlResult(ok=False, current_owner=self._owner_getter(), reason=reason)

    def _ensure_bridge(self):
        if self._bridge is not None:
            return self._bridge
        if str(MODULES_DIR) not in sys.path:
            sys.path.insert(0, str(MODULES_DIR))
        from r3_follow.team2_adapter import Team2StubPort
        from r3_follow.team2_bridge import Team3FollowBridge
        port = Team2StubPort(REPOSITORY, frame_provider=self._frame_provider)
        bridge = Team3FollowBridge(port, recovery_window=RECOVERY_WINDOW)
        self._port, self._bridge = port, bridge
        self._last_events = []
        bridge.service.start_background()
        return bridge

    def _current(self, bridge, generation):
        return bridge is self._bridge and generation == self._generation

    def _failed_start(self, bridge, generation, reason):
        with self._lock:
            if not self._current(bridge, generation):
                return self._fail('follow_start_cancelled')
        # stop checks the expected generation again; cannot close a newer start.
        from .controller import operation_guard
        with operation_guard(None):
            self._stop(expected=(bridge, generation))
        return self._fail(reason)

    def start(self, timeout=FIRST_FRAME_TIMEOUT):
        operation = self._operation()
        deadline = time.monotonic() + max(0., float(timeout))
        with self._lock:
            if self._cancelled(): return self._fail('program_cancelled')
            pending=list(self._pending_cleanup)
        for service in pending:
            with service.lock: result=service._cleanup()
            if not result.ok: return self._fail('previous_cleanup_unconfirmed')
            with self._lock:
                if service in self._pending_cleanup: self._pending_cleanup.remove(service)
        while True:
            with self._lock:
                self._retired_workers = [w for w in self._retired_workers if w.is_alive()]
                retired = list(self._retired_workers)
                if not retired:
                    break
            # A previous stop retires its capture worker without waiting. Give it
            # the caller's timeout budget to exit, so a restart does not fail on a
            # worker that is merely finishing teardown. Two providers never overlap.
            budget = deadline - time.monotonic()
            if budget <= 0:
                return self._fail('previous_frame_provider_still_running')
            for worker in retired:
                worker.join(min(budget, .05))
        with self._lock:
            if self._cancelled(): return self._fail('program_cancelled')
            if self._starting or self._closing:
                return self._fail('follow_start_or_close_busy')
            if self._bridge is not None and self._bridge.is_following():
                return ControlResult(ok=True, current_owner='follow')
            self._generation += 1
            generation = self._generation
            self._starting = True
            try:
                bridge = self._ensure_bridge()
            except Exception as exc:
                self._starting = False
                return self._fail('follow_unavailable: %s' % exc)
            has_provider = self._frame_provider is not None
        # Never hold the lifecycle lock while waiting for a frame/provider.
        try:
            if not has_provider and bridge.service.port.get_frame().frame is None:
                return self._failed_start(bridge, generation, 'follow_no_frame_source')
            while True:
                with self._lock:
                    if not self._current(bridge, generation):
                        return self._fail('follow_start_cancelled')
                    with bridge.service.lock:
                        observation = bridge.service.controller.observation
                        fresh = bool(observation is not None and bridge.service.frame_ready
                            and 0 <= bridge.service.clock()-observation.acquired_at
                            <= bridge.service.controller.max_age)
                if fresh:
                    break
                if time.monotonic() >= deadline:
                    return self._failed_start(bridge, generation, 'follow_no_fresh_frame')
                time.sleep(.01)
            with self._lock:
                if self._cancelled():
                    return self._failed_start(bridge, generation, 'program_cancelled')
                if not self._current(bridge, generation):
                    return self._fail('follow_start_cancelled')
                if bridge.service.controller.reference_size is None:
                    calibrated = bridge.service.calibrate_reference()
                    if not calibrated.ok:
                        reason = calibrated.reason or 'follow_calibration_failed'
                    else:
                        reason = None
                else:
                    reason = None
                if reason is None:
                    result = bridge.start_follow()
                    if result.ok:
                        if self._cancelled():
                            return self._failed_start(bridge, generation, 'program_cancelled')
                        self._operation_source = operation
                        return result
                    reason = result.reason
            return self._failed_start(bridge, generation, reason)
        except Exception as exc:
            return self._failed_start(bridge, generation, 'follow_start_error: %s' % exc)
        finally:
            with self._lock:
                if self._current(bridge, generation):
                    self._starting = False

    def _stop(self, expected=None):
        with self._lock:
            if expected is None and self._cancelled():
                return self._fail('program_cancelled')
            if expected is not None and not self._current(*expected):
                return self._fail('follow_start_cancelled')
            self._generation += 1
            self._starting = False
            bridge, port = self._bridge, self._port
            self._bridge = self._port = None
            self._operation_source = None
            if bridge is None:
                return ControlResult(ok=True, current_owner=self._owner_getter())
            self._closing = True
            result = bridge.stop_follow()  # Short zero/release, invalidates ticket.
        try:
            # Provider may be blocked. Movement has already been stopped/released;
            # never delay the priority caller while joining a capture worker.
            bridge.service.close(wait=False)
        finally:
            with self._lock:
                if bridge.service.lease is not None:
                    self._pending_cleanup.append(bridge.service)
                self._retired_workers.extend(w for w in bridge.service.workers if w.is_alive())
                self._last_events = list(port.events)
                self._closing = False
        return result

    def stop(self):
        return self._stop()

    def stop_cancelled_operation(self, operation):
        """Close only follow started/resumed by this failed host program."""
        with self._lock:
            if (operation is None or not operation.is_set()
                    or self._operation_source is not operation or self._bridge is None):
                return ControlResult(ok=True, current_owner=self._owner_getter(), reason='not_this_operation')
            from .controller import operation_guard
            with operation_guard(None):
                return self._stop(expected=(self._bridge, self._generation))

    def is_following(self):
        with self._lock:
            return bool(self._bridge and self._bridge.is_following())

    def status(self):
        with self._lock:
            if self._bridge is None:
                return dict(state='stopped', following=False,
                            background_running=any(w.is_alive() for w in self._retired_workers),
                            target_visible=False,
                            cleanup_pending=bool(self._pending_cleanup),
                            cleanup_pending_count=len(self._pending_cleanup),
                            cleanup_confirmed=not self._pending_cleanup)
            service = self._bridge.service
            state = self._bridge.get_follow_status()
            with service.lock:
                obs = service.controller.observation
                state['target_visible'] = bool(service.frame_ready and obs and obs.found
                    and 0 <= service.clock()-obs.acquired_at <= service.controller.max_age)
            return state

    def events(self):
        with self._lock:
            return list(self._port.events if self._port is not None else self._last_events)

    def pause_for_action(self):
        """Internal Python ticket; do not expose it to model-generated programs."""
        with self._lock:
            if self._cancelled(): return self._fail('program_cancelled'), None
            if self._starting or self._closing:
                return self._fail('follow_start_or_close_busy'), None
            if self._bridge is None:
                return ControlResult(ok=True, current_owner=self._owner_getter()), None
            result, ticket = self._bridge.pause_for_action()
            return self._bridge._control_result(result), (self._bridge, self._generation, ticket)

    def finish_action(self, ticket, status):
        with self._lock:
            if self._cancelled(): return self._fail('program_cancelled')
            if ticket is None:
                return self._fail('not_previously_following')
            bridge, generation, inner = ticket
            if not self._current(bridge, generation):
                return self._fail('obsolete_ticket')
            result = bridge.finish_action(inner, status)
            if result.ok:
                self._operation_source = self._operation()
            return bridge._control_result(result)
