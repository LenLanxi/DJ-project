"""Local integration candidate for the reviewed SIMULATED group-2 SDK.

One instance owns the follow service and model handoffs. This does not install
callbacks into upstream globals or change group 4's Session. Its normal action
call runs outside the short coordination lock; stop/land invalidate that action's
return token. The upstream stub does NOT cancel its sleeping driver operation.
"""
import math
import threading
import time

from .team2_bridge import Team3FollowBridge


def outcome(status, reason="", **data):
    return dict(ok=status in ("accepted", "completed"), status=status,
                reason=reason, simulated=True, hardware_verified=False, **data)


class JointRuntime:
    def __init__(self, port, *, bridge=None, landing_timeout=10.0):
        if getattr(port, "simulation_only", None) is not True:
            raise ValueError("Reviewed simulation port required")
        self.port, self.sdk = port, port.sdk
        self.bridge = bridge or Team3FollowBridge(port)
        self.lock = threading.RLock()
        self.active = None
        self.generation = 0
        self.phase = "idle"
        self.landing_sent_at = None
        self.landing_deadline = None
        self.model_lease = None
        self.landing_lease = None
        if not math.isfinite(landing_timeout) or landing_timeout <= 0:
            raise ValueError("Invalid landing timeout")
        self.landing_timeout = landing_timeout
        self.events = []
        self.closed = False

    def _record(self, event, **data):
        self.events.append(dict(time=time.monotonic(), event=event, **data))

    def _device(self):
        state = self.sdk.get_status()
        stamp = state.timestamp
        fresh = (type(stamp) in (int, float) and math.isfinite(stamp)
                 and 0 <= time.time() - stamp <= .5)
        return state, fresh and state.connected is True

    def get_status(self):
        with self.lock:
            state, valid = self._device()
            follow = self.bridge.get_follow_status()
            obs = self.bridge.service.controller.observation
            visible = bool(follow["frame_ready"] and obs and obs.found and
                           0 <= self.bridge.service.clock() - obs.acquired_at <= .5)
            return dict(battery=state.battery, height_cm=state.height_cm,
                        connected=state.connected if valid else None,
                        flight_state=state.flight_state if valid else "unknown",
                        flying=valid and state.flight_state == "flying",
                        owner=None if state.control_owner == "none" else state.control_owner,
                        following=follow["following"], target_visible=visible,
                        frame_id=follow["frame_id"], timestamp=state.timestamp,
                        phase=self.phase, action_inflight=self.active is not None,
                        simulated=True, hardware_verified=False)

    def get_frame(self):
        return self.port.get_frame()

    def start_follow(self):
        with self.lock:
            if self.closed or self.phase != "idle" or self.active is not None:
                return outcome("failed", "closed_landing_or_action_inflight")
            result = self.bridge.start_follow()
            self._record("start_follow", ok=result.ok, reason=result.reason)
            return outcome("completed" if result.ok else "failed", result.reason)

    def stop_follow(self):
        with self.lock:
            self.generation += 1
            result = self.bridge.stop_follow()
            self._record("stop_follow", ok=result.ok)
            return outcome("completed" if result.ok else "failed", result.reason)

    def is_following(self):
        return self.bridge.is_following()

    @staticmethod
    def _valid_action(name, param):
        if name == "takeoff":
            return param is None
        if name in ("move_forward", "move_back", "move_left", "move_right",
                    "move_up", "move_down"):
            return type(param) is int and 20 <= param <= 500
        # This bounded demonstration intentionally supports translation only.
        return False

    def _release_model(self, lease):
        if lease is None:
            return True
        try:
            result = self.port.release_control(lease)
            if result.status == 'completed' and lease == self.model_lease:
                self.model_lease = None
            return result.status == "completed"
        except Exception as exc:
            self._record("release_exception", error=str(exc))
            return False

    def run_action(self, name, param=None):
        """One bounded SDK action; no generated code or arbitrary callbacks."""
        if name in ("land", "stop_motion") and param is None:
            return self.interrupt(land=name == "land")
        if not self._valid_action(name, param):
            return outcome("failed", "unsupported_action_or_parameter")
        with self.lock:
            if self.closed or self.phase != "idle" or self.active is not None:
                return outcome("failed", "closed_landing_or_action_inflight")
            state, valid = self._device()
            required_state = "landed" if name == "takeoff" else "flying"
            if not valid or state.flight_state != required_state:
                return outcome("failed", "fresh_device_state_required")
            paused, ticket = self.bridge.pause_for_action()
            if not paused.ok:
                return outcome("failed", paused.reason)
            grant = self.port.request_control("model")
            if not grant.ok:
                self.bridge.stop_follow()
                return outcome("failed", grant.reason)
            self.generation += 1
            token = self.generation
            self.active = token
            lease = grant.lease
            self.model_lease = lease
            epochs = (dict(control_epoch=lease.epoch,
                           command_epoch=self.sdk.get_status().command_epoch)
                      if hasattr(self.sdk, 'publish_frame') else {})
            self._record("action_started", action=name, token=token)
        # Deliberately outside self.lock AND the follow service/port locks.
        try:
            action = getattr(self.sdk, name)
            result = self.port._command(action("model", **epochs) if param is None
                                        else action("model", param, **epochs))
        except Exception as exc:
            from .service_ports import Result
            result = Result("failed", "action_exception: " + str(exc))
        with self.lock:
            self.active = None
            if token != self.generation or self.closed:
                # Landing owns the lease until fresh telemetry confirms landed.
                if self.phase == "idle":
                    self._release_model(lease)
                self._record("late_action_result_discarded", token=token)
                return outcome("failed", "cancelled_result_discarded", resumed=False)
            if result.status != "completed":
                self.bridge.stop_follow()
                try:
                    self.port.stop_motion(lease)
                except Exception as exc:
                    self._record("failure_cleanup_stop_exception", error=str(exc))
                finally:
                    released = self._release_model(lease)
                self._record("action_failed", status=result.status, reason=result.reason)
                # Unexpected accepted for a blocking action never restores follow.
                return outcome("failed" if result.ok else result.status,
                               result.reason or "action_not_confirmed_completed", resumed=False,
                               release_confirmed=released)
            if not self._release_model(lease):
                self.bridge.stop_follow()
                return outcome("failed", "release_unconfirmed", resumed=False)
            resumed, resume_reason = False, "not_previously_following"
            if ticket.was_following:
                restored = self.bridge.finish_action(ticket, "completed")
                resumed, resume_reason = restored.ok, restored.reason
            else:
                self.bridge.stop_follow()
            self._record("action_finished", action=name, resumed=resumed,
                         resume_reason=resume_reason)
            return outcome("completed", "simulated_action_completed", resumed=resumed,
                           resume_reason=resume_reason)

    def interrupt(self, *, land=False):
        """Send acknowledgement only. Never reports physical hover or landing."""
        with self.lock:
            if self.closed:
                return outcome("failed", "closed")
            if self.phase.startswith("landing") and not land:
                return outcome("failed", "landing_in_progress_stop_suppressed")
            if self.phase == "landing" and land:
                return outcome("accepted", "landing_already_requested", landed_confirmed=False)
            self.generation += 1
            stopped = self.bridge.stop_follow()  # also invalidates any recovery ticket
            if not stopped.ok:
                return outcome("failed", "follow_cleanup_unconfirmed")
            state = self.port.get_status()
            if state.lease is None:
                grant = self.port.request_control("model")
                if not grant.ok:
                    return outcome("failed", grant.reason)
                lease = grant.lease
            elif state.lease.owner == "model":
                lease = state.lease
            else:
                return outcome("failed", "busy")
            if land:
                self.phase = "landing"
                self.landing_lease = lease
                self.model_lease = lease
                self.landing_sent_at = time.time()
                self.landing_deadline = time.monotonic() + self.landing_timeout
            try:
                grant = {'control_epoch':lease.epoch} if hasattr(self.sdk,'publish_frame') else {}
                result = (self.port._command(self.sdk.land("model", **grant)) if land
                          else self.port.stop_motion(lease))
            except Exception as exc:
                if land:
                    self.phase = "landing_unknown"
                return outcome("failed", "interrupt_exception: " + str(exc))
            if land and not result.ok:
                self.phase = "landing_unknown"
            if not land and self.active is None:
                if not self._release_model(lease):
                    return outcome("failed", "stop_sent_release_unconfirmed",
                                   motion_stopped_confirmed=False)
            self._record("land_requested" if land else "stop_requested", sdk_status=result.status)
            return outcome("accepted" if result.ok else result.status,
                           "landing_request_sent" if land and result.ok else
                           "stop_request_sent" if result.ok else result.reason,
                           landed_confirmed=False, motion_stopped_confirmed=False)

    def poll_landing(self):
        with self.lock:
            if not self.phase.startswith("landing"):
                return outcome("failed", "no_landing_pending", landed_confirmed=False)
            state, valid = self._device()
            # Equal timestamps can be an old cached status (coarse clock tick).
            # Wait for a strictly newer sample instead of guessing its ordering.
            if (valid and state.timestamp > self.landing_sent_at
                    and state.flight_state == "landed"):
                # The reviewed SDK only offers simulated telemetry here.
                self.phase = "idle"
                if self.active is None:
                    lease = self.port.get_status().lease
                    if lease is not None and lease != self.landing_lease:
                        return outcome('failed','landing_owner_changed',landed_confirmed=True)
                    if lease is not None and not self._release_model(self.landing_lease):
                        self.phase = "landing_unknown"
                        return outcome("failed", "landing_release_unconfirmed", landed_confirmed=True)
                self._record("simulated_landed_status_confirmed")
                return outcome("completed", "simulated_landed_status", landed_confirmed=True)
            if time.monotonic() >= self.landing_deadline:
                self.phase = "landing_unknown"
                return outcome("timeout", "landing_status_unconfirmed", landed_confirmed=False)
            return outcome("accepted", "waiting_for_fresh_landed_status", landed_confirmed=False)

    def close(self):
        with self.lock:
            self.generation += 1
            self.closed = True
            lease = self.model_lease
            if lease is not None and self.phase == 'idle' and self.port.get_status().lease == lease:
                self.port.stop_motion(lease)
                self._release_model(lease)
            return self.bridge.close()


class JointDemo:
    """Explicit injection seam for the repository's OLD offline text Runner.

This independent demonstration uses JointRuntime's completion tokens. The
legacy Session and v0.4 Gateway are separately adapted and tested; this demo
alone is not evidence that their real voice or Docker paths have run.
"""
    def __init__(self, runtime):
        self.drone = runtime

    def handle(self, text):
        if text == "查看电量":
            return "[模拟] 当前电量为%d%%。" % self.drone.get_status()["battery"]
        if text == "查看状态":
            import json
            return "[模拟] " + json.dumps(self.drone.get_status(), ensure_ascii=False)
        if text == "跟着我":
            result = self.drone.start_follow()
        elif text == "停止跟随":
            result = self.drone.stop_follow()
        elif text in ("起飞", "前进30厘米"):
            result = self.drone.run_action("takeoff" if text == "起飞" else "move_forward",
                                          None if text == "起飞" else 30)
        elif text in ("降落", "停在这里"):
            result = self.drone.interrupt(land=text == "降落")
        else:
            return "[模拟] 未支持此指令，未执行"
        import json
        return "[模拟] " + json.dumps(result, ensure_ascii=False)
