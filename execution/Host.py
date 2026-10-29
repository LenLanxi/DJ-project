"""One host gateway for every model program; device grants never cross the RPC boundary."""
import asyncio
from dataclasses import asdict
import math
import threading
import time

DISTANCE = {'move_forward', 'move_back', 'move_left', 'move_right', 'move_up', 'move_down'}
MOTION = DISTANCE | {'takeoff', 'land', 'stop_motion', 'rotate', 'update_velocity'}
LAND_CONFIRM_TIMEOUT_S = 25.


class SDKActionError(RuntimeError):
    def __init__(self, result):
        self.result = {key: value for key, value in result.items() if key != 'grant'}
        super().__init__('SDK_action_failed: ' + str(self.result))


class NavigationGuard:
    """Carry the timestamp validated by Host to the final UDP send deadline."""
    def __init__(self, event, timestamp, *, clock=time.monotonic, wall_clock=time.time):
        self.event,self.timestamp = event,timestamp
        self.clock,self.wall_clock = clock,wall_clock
        self.deadline = clock()+max(0.,1.0-(wall_clock()-timestamp))
    def is_set(self):
        return (self.event.is_set() or self.clock()>self.deadline
                or not 0 <= self.wall_clock()-self.timestamp <= 1.0)


async def interrupt_device(device, follow, land=False):
    result = asdict(await asyncio.to_thread(device.priority_interrupt, bool(land)))
    try:
        stopped = await asyncio.to_thread(follow.stop)
        if not stopped.ok:
            result = dict(result, ok=False, reason=result.get('reason') or 'follow_stop_unconfirmed',
                          follow_cleanup=asdict(stopped))
    except Exception as exc:
        result = dict(result, ok=False, reason=result.get('reason') or 'follow_stop_unconfirmed',
                      follow_cleanup_error=str(exc))
    return {key: value for key, value in result.items() if key != 'grant'}


class Host:
    """Validate sandbox RPC; trust the fixed Vision and SDK result contracts.

    Single-use program state. Its event is never cleared or replaced.
    """
    def __init__(self, device, follow, vision=None, *, recovery=False, methods=None, motion=None):
        self.device, self.follow, self.vision = device, follow, vision
        self.simulated = device.get_status().simulated
        self.recovery = recovery
        self.methods = methods
        self.motion = motion
        self.cancelled = threading.Event()
        self.control_lock = asyncio.Lock()
        self.acquired = False
        self.closed = self.cleanup_pending = False
        self._control_request = self._action_request = None
        self.grant = self.ticket = None
        self.resume = True
        self.observation = None
        self.observations = set()
        self.cleanup = {'status': 'pending'}
        self.follow_adjust_axis = self.last_translation = self.translation_completed_at = None

    def close_admission(self):
        self.closed = True
        self.cleanup_pending = True

    def cancel(self):
        self.cancelled.set()
        self.resume = False

    def _current(self):
        if self.cancelled.is_set():
            raise RuntimeError('program_cancelled')
        if self.closed:
            raise RuntimeError('execution_closed')

    async def _action(self, name, *args, grant=None, cleanup=False, operation=None):
        lease = self.grant if grant is None else grant
        cancelled = None if cleanup else operation if operation is not None else self.cancelled
        request = asyncio.create_task(asyncio.to_thread(getattr(self.device, name), lease, *args, cancelled=cancelled))
        if name in ('stop_motion', 'land'):
            self._action_request = request
            request.add_done_callback(lambda task: self._capture_action(task, lease))
        result = await asyncio.shield(request) if name in ('stop_motion', 'land') else await request
        if name in ('stop_motion', 'land'):
            self._capture_action(request, lease)
        return dict(asdict(result), grant=result.grant, simulated=self.simulated)

    def _capture_action(self, task, lease):
        """Keep only the grant returned by this action, including a late thread."""
        try:
            result = task.result()
            if (result.grant is not None and self.grant == lease
                    and result.grant.control_epoch == lease.control_epoch):
                self.grant = result.grant
        except BaseException:
            pass

    async def _follow(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, cancelled=self.cancelled, **kwargs)

    async def acquire(self):
        async with self.control_lock:
            self._current()
            if self.acquired:
                return
            if self.motion is not None:
                await self.motion.stop_velocity()
                self._current()
            self.ticket = await self._follow(self.follow.pause_if_active)
            self._current()
            # Thread cancellation cannot undo an SDK grant. Keep the request
            # alive and record its lease even if the awaiting RPC is cancelled.
            self.cleanup_pending = True
            self._control_request = asyncio.create_task(
                asyncio.to_thread(self.device.request_control, 'model', cancelled=self.cancelled))
            self._control_request.add_done_callback(self._capture_control)
            try:
                result = await asyncio.shield(self._control_request)
            except asyncio.CancelledError:
                self.cancel()
                raise
            self._current()
            if not result.ok:
                self.resume = False
                raise RuntimeError('control_busy: ' + result.reason)

    def _capture_control(self, request):
        # Record late grants and consume exceptions even if the old RPC was cancelled.
        try:
            result = request.result()
            if result.ok:
                self.grant = result.grant
                self.acquired = True
        except BaseException:
            pass  # The fixed device contract raises only before granting control.

    def _navigation_evidence(self, method, args):
        observation = self.observation
        if observation is None:
            # Direct low-level movement is a supported API; it does not claim
            # autonomous target navigation or obstacle clearance.
            return
        if method == 'rotate':
            # Rotation may deliberately inspect a direction with no target in
            # sight. It never establishes distance, clearance or arrival.
            return
        if method == 'update_velocity' and not any(args):
            return
        if observation.get('consumed'):
            raise RuntimeError('navigation_requires_new_observation')
        if observation.get('visible') is not True:
            raise RuntimeError('navigation_target_not_visible')
        if not self.simulated and observation.get('source') != 'sdk_camera':
            raise RuntimeError('real_navigation_requires_live_camera')
        if observation.get('source') == 'sdk_camera':
            stamp = observation.get('timestamp')
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 <= time.time() - stamp <= 1.0:
                raise RuntimeError('navigation_observation_expired')

    async def _confirm_landed(self):
        deadline = time.monotonic() + LAND_CONFIRM_TIMEOUT_S
        while time.monotonic() < deadline:
            self._current()
            state = await asyncio.to_thread(self.device.get_status)
            self._current()
            stamp = state.timestamp
            if (state.connected is True and state.flight_state == 'landed'
                    and state.height_cm == 0 and type(stamp) in (int, float)
                    and math.isfinite(stamp) and 0 <= time.time() - stamp <= 1.0):
                return
            await asyncio.sleep(min(.05, max(0., deadline - time.monotonic())))
        raise RuntimeError('landing_confirmation_timeout')

    async def dispatch(self, method, args, kwargs):
        self._current()
        if self.methods is not None and method not in self.methods:
            raise ValueError('RPC method not permitted in this session')
        if not isinstance(args, list) or not isinstance(kwargs, dict) or kwargs:
            raise ValueError('RPC accepts positional arguments only')
        if self.recovery and method not in ('get_status', 'is_following', 'land', 'stop_motion'):
            raise RuntimeError('pending_cleanup_allows_readonly_or_priority_stop_only')
        if self.recovery and method in ('land', 'stop_motion'):
            if args:
                raise TypeError('priority stop takes no arguments')
            return await interrupt_device(self.device, self.follow, method == 'land')
        if method in ('get_status', 'is_following'):
            if args:
                raise ValueError('read-only status takes no arguments')
            result = (asdict(await asyncio.to_thread(self.device.get_status)) if method == 'get_status'
                      else await asyncio.to_thread(self.follow.is_following))
            self._current()
            if method == 'get_status' and self.motion is not None:
                result['continuous_motion'] = self.motion.velocity_status()
            return result
        if method == 'start_velocity':
            if self.follow_adjust_axis is not None:
                raise RuntimeError('follow_adjustment_must_be_last_motion')
            if self.motion is None:
                raise RuntimeError('continuous_motion_not_configured')
            return await self.motion.start_velocity(self, args)
        if method in ('observe', 'describe'):
            if len(args) != 1 or not isinstance(args[0], str) or not 0 < len(args[0]) <= 2000:
                raise ValueError('vision requires a target/question of 1 to 2000 characters')
            if self.vision is None:
                raise RuntimeError('vision_model_not_configured')
            if method == 'observe':
                # A navigation observation is an intentional temporary takeover,
                # unlike read-only describe. Preserve the existing follow ticket.
                await self.acquire()
                self._current()
                stopped = await self._action('stop_motion')
                self._current()
                if stopped.get('ok') is not True:
                    self.resume = False
                    raise RuntimeError('navigation_hover_stop_unconfirmed')
                if stopped.get('grant') is not None: self.grant = stopped['grant']
                await asyncio.to_thread(self.vision.prepare_navigation, time.time(), self.cancelled)
                self._current()
            result = await asyncio.to_thread(getattr(self.vision, method), args[0])
            self._current()
            if method == 'observe':
                if not result['visible'] or result.get('ambiguous') is True:
                    self.resume = False
                    result = dict(result,visible=False)
                identity = (result.get('source'), result.get('frame_id'))
                if identity in self.observations and result.get('source') == 'sdk_camera':
                    raise RuntimeError('navigation_requires_new_frame')
                self.observations.add(identity)
                self.observation = result
            # describe is read-only and must never authorize a later motion.
            return result
        if method == 'get_frame':
            if args or self.vision is None:
                raise ValueError('get_frame needs a configured camera and no arguments')
            result = await asyncio.to_thread(self.vision.frame)
            self._current()
            return result
        if method == 'adjust_follow_reference':
            axes = {'depth': ('move_forward','move_back'),
                    'horizontal': ('move_left','move_right'),
                    'vertical': ('move_up','move_down')}
            if len(args) != 1 or not isinstance(args[0],str) or args[0] not in axes:
                raise ValueError('follow adjustment axis must be depth, horizontal or vertical')
            if not self.acquired or self.ticket is None:
                raise RuntimeError('follow_adjustment_requires_paused_follow')
            if self.last_translation not in axes[args[0]] or self.translation_completed_at is None:
                raise RuntimeError('follow_adjustment_requires_completed_matching_translation')
            self.follow_adjust_axis = args[0]
            return {'ok':True,'status':'accepted','axis':args[0],
                    'reason':'applied_only_after_success_and_fresh_post_action_frame'}
        if method in ('start_follow', 'stop_follow'):
            if args:
                raise ValueError('follow takes no arguments')
            if self.motion is not None:
                await self.motion.stop_velocity()
                self._current()
            if self.follow_adjust_axis is not None:
                if method == 'start_follow':
                    raise RuntimeError('follow_adjustment_must_be_last_motion')
                self.follow_adjust_axis = None
            if method == 'start_follow' and self.ticket is not None and self.acquired:
                self.resume = True
                return {'ok':True,'status':'accepted','reason':'resume_after_program_success',
                        'reference_preserved':True,'deferred':True,'simulated':self.simulated}
            self.resume = False
            if method == 'stop_follow':
                result = await self._follow(self.follow.stop)
                self._current()
                if not result.ok:
                    raise RuntimeError('follow_stop_unconfirmed: ' + result.reason)
                self.ticket = None
                return asdict(result)
            if await asyncio.to_thread(self.follow.is_following):
                self._current()
                return {'ok': True, 'status': 'already_following', 'simulated': self.simulated}
            # The priority stop lease may remain after an interrupted program.
            # Acquire a NEW lease before releasing model ownership to follow.
            await self.acquire()
            self._current()
            released = await asyncio.to_thread(self.device.release_control, self.grant, cancelled=self.cancelled)
            if not released.ok:
                raise RuntimeError('follow_handoff_release_failed: ' + released.reason)
            self.acquired = False
            self._current()
            result = await self._follow(self.follow.start)
            self._current()
            if not result.ok:
                raise RuntimeError('follow_start_failed: ' + result.reason)
            return asdict(result)
        if method not in MOTION:
            raise ValueError('host capability not exposed: ' + str(method))
        if self.follow_adjust_axis is not None:
            if method not in ('land','stop_motion'):
                raise RuntimeError('follow_adjustment_must_be_last_motion')
            self.follow_adjust_axis = None
        if method in DISTANCE and self.observation is not None and args != [20]:
            raise ValueError('observed_navigation_step_must_be_20_cm')
        navigation_guard = None
        observed_motion = method in DISTANCE or method == 'update_velocity' and any(args)
        if observed_motion and self.observation is not None and self.observation.get('visible') is True and not self.observation.get('consumed'):
            self.observation = await asyncio.to_thread(self.vision.refresh_observation, dict(self.observation))
            self._current()
        if method in DISTANCE | {'rotate', 'update_velocity'}:
            self._navigation_evidence(method, args)
        if observed_motion and self.observation is not None and self.observation.get('source') == 'sdk_camera':
            # Capture once at RPC entry. A later background frame cannot renew
            # this command while it waits for control, SDK locks or UDP send.
            navigation_guard = NavigationGuard(self.cancelled,self.observation['timestamp'])
        if method in ('land', 'stop_motion'):
            self.resume = False
        await self.acquire()
        self._current()
        if navigation_guard is not None and navigation_guard.is_set():
            raise RuntimeError('navigation_observation_expired')
        if observed_motion: self._navigation_evidence(method,args)
        result = await self._action(method, *args, operation=navigation_guard)
        self._current()
        permitted = ('completed', 'accepted') if method in ('stop_motion', 'land', 'update_velocity') else ('completed',)
        if not result.get('ok') or result.get('status') not in permitted:
            self.resume = False
            raise SDKActionError(result)
        if result.get('grant') is not None:
            self.grant = result['grant']
        if method == 'land':
            await self._confirm_landed()
            result = dict(result, status='completed', is_blocking=True, landed_confirmed=True)
        if method in DISTANCE:
            self.last_translation, self.translation_completed_at = method, time.time()
        else:
            self.last_translation = self.translation_completed_at = None
        if method in DISTANCE | {'rotate', 'update_velocity'} and self.observation is not None:
            # One image cannot justify repeated moves after the view changed.
            self.observation = dict(self.observation, consumed=True)
        return {key: value for key, value in result.items() if key != 'grant'}

    async def interrupt(self, land=False):
        self.cancel()
        # The old task's grant is never replaced with the emergency grant.
        return await interrupt_device(self.device, self.follow, land)

    async def _superseded(self, grant):
        state = await asyncio.to_thread(self.device.get_status)
        return (state.control_epoch != grant.control_epoch
                or state.command_epoch != grant.command_epoch
                or state.control_owner != 'model')

    async def finish(self, success):
        if self.closed and not self.cleanup_pending:
            return
        self.closed = True
        self.cleanup_pending = True
        confirmed = False
        try:
            await self._finish_control(success)
            confirmed = True
        finally:
            if self.vision is not None:
                self.vision.close()
            if confirmed:
                self.cleanup_pending = False

    async def _finish_control(self, success):
        if self._action_request is not None and not self._action_request.done():
            self.cleanup = {'status': 'control_action_pending'}
            raise RuntimeError('control_action_pending')
        if self._control_request is not None and not self._control_request.done():
            self.cancel()
            self.cleanup = {'status': 'control_acquire_pending'}
            raise RuntimeError('control_acquire_pending')
        # request_control fails before granting or returns the captured lease.
        # A raised request therefore owns no device lease to reconcile.
        if not success or self.cancelled.is_set():
            self.cancelled.set()
            cleaned = await asyncio.to_thread(self.follow.stop_cancelled_operation, self.cancelled)
            if not cleaned.ok:
                raise RuntimeError('cancelled_follow_cleanup_failed: ' + str(cleaned))
        if not self.acquired:
            self.cleanup = {'status': 'no_control'}
            return
        grant = self.grant
        ticket = self.ticket
        if await self._superseded(grant):
            self.acquired = False
            self.cleanup = {'status': 'superseded'}
            return
        if not success or self.cancelled.is_set():
            stopped = await self._action('stop_motion', grant=grant, cleanup=True)
            # Even a failed zero can advance this lease's command epoch.
            grant = self.grant
            if not stopped.get('ok'):
                if await self._superseded(grant):
                    self.acquired = False
                    self.cleanup = {'status': 'superseded'}
                    return
                self.cleanup = {'status': 'stop_unconfirmed', 'result': stopped}
                raise RuntimeError('cleanup_stop_failed: ' + str(stopped))
        released = await asyncio.to_thread(self.device.release_control, grant)
        if not released.ok:
            if await self._superseded(grant):
                self.acquired = False
                self.cleanup = {'status': 'superseded'}
                return
            self.cleanup = {'status': 'release_unconfirmed', 'result': released}
            raise RuntimeError('cleanup_release_failed: ' + str(released))
        self.acquired = False
        self.cleanup = {'status': 'released'}
        if (success and self.resume
                and ticket is not None and not self.cancelled.is_set()):
            if self.follow_adjust_axis is None:
                recovered = await self._follow(self.follow.finish_action, ticket, True, timeout=.5)
            else:
                recovered = await self._follow(self.follow.finish_action, ticket, True,
                    adjust_axis=self.follow_adjust_axis, not_before=self.translation_completed_at, timeout=.5)
            if self.cancelled.is_set():
                raise RuntimeError('program_cancelled')
            if not recovered.ok:
                self.cleanup = {'status': 'follow_resume_failed', 'reason': recovered.reason, 'result': recovered}
                if self.follow_adjust_axis is not None or self.follow.cleanup_unconfirmed:
                    raise RuntimeError('follow_resume_failed: ' + str(recovered))
                return  # The action and control release succeeded; report resume separately.
            self.cleanup = {'status': 'follow_resumed'}
            if self.follow_adjust_axis is not None:
                self.cleanup['reference_adjusted'] = self.follow_adjust_axis
