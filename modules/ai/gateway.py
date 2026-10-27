"""One host gateway for every model program; device grants never cross the RPC boundary."""
import asyncio
import math
import threading
import time
from backends import GRANT, OPERATION

DISTANCE = {'move_forward', 'move_back', 'move_left', 'move_right', 'move_up', 'move_down'}
MOTION = DISTANCE | {'takeoff', 'land', 'stop_motion', 'rotate', 'update_velocity'}
LAND_CONFIRM_TIMEOUT_S = 25.


class Gateway:
    def __init__(self, backend, vision=None):
        self.backend, self.vision = backend, vision
        self.simulated = backend.simulated
        self.cancelled = threading.Event()
        self.control_lock = asyncio.Lock()
        self.generation = 0
        self.acquired = False
        self.grant = None
        self.ticket = None
        self.was_following = False
        self.resume = False
        self.observation = None
        self.observations = set()
        self.cleanup = {'status': 'idle'}
        self.follow_adjust_axis = self.last_translation = self.translation_completed_at = None

    def begin(self):
        if self.acquired:
            raise RuntimeError('previous_control_cleanup_unconfirmed')
        self.generation += 1
        self.cancelled.set()
        self.cancelled = threading.Event()  # old worker references never revive
        self.grant = self.ticket = self.observation = None
        self.was_following = False
        self.resume = True
        self.observations.clear()
        self.cleanup = {'status': 'pending'}
        self.follow_adjust_axis = self.last_translation = self.translation_completed_at = None

    def _current(self, generation):
        if self.cancelled.is_set() or generation != self.generation:
            raise RuntimeError('program_cancelled')

    async def _bound(self, fn, *args, grant=None, cleanup=False):
        # asyncio.to_thread propagates this context. A new priority controller
        # may mutate backend.grant while an old program is awaiting a result;
        # that old program and its cleanup must retain their original lease.
        token = GRANT.set(dict(self.grant if grant is None else grant) if (self.grant if grant is None else grant) is not None else {})
        operation = OPERATION.set(None if cleanup else self.cancelled)
        try:
            return await asyncio.to_thread(fn, *args)
        finally:
            GRANT.reset(token)
            OPERATION.reset(operation)

    async def acquire(self):
        generation = self.generation
        async with self.control_lock:
            self._current(generation)
            if self.acquired:
                return
            following = await asyncio.to_thread(self.backend.is_following)
            self._current(generation)
            if following:
                if not hasattr(self.backend, 'pause_follow_for_action'):
                    raise RuntimeError('follow_handoff_not_supported')
                paused = await self._bound(self.backend.pause_follow_for_action)
                self._current(generation)
                if not paused.get('ok'):
                    raise RuntimeError('cannot_pause_follow: ' + str(paused))
                self.ticket = paused.get('ticket')
                self.was_following = True
            result = await self._bound(self.backend.request_control, 'model')
            self._current(generation)
            if not result.get('ok'):
                # No forced stop or recovery is safe if somebody else acquired
                # the drone meanwhile. Leave the ticket failed and report it.
                self.resume = False
                raise RuntimeError('control_busy: ' + str(result.get('reason', 'unknown')))
            grant = {key: result.get(key) for key in ('control_epoch', 'command_epoch')}
            if any(type(v) is not int for v in grant.values()):
                # Test-only substitutes may have no SDK epochs; their action
                # contract remains explicit. Real SDK always returns both.
                grant = dict(getattr(self.backend, 'grant', None) or {})
                if not self.simulated:
                    raise RuntimeError('control_grant_missing')
            self.grant, self.acquired = grant, True

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
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 <= time.time() - stamp <= .5:
                raise RuntimeError('navigation_observation_expired')

    async def _confirm_landed(self, generation):
        deadline = time.monotonic() + LAND_CONFIRM_TIMEOUT_S
        while time.monotonic() < deadline:
            self._current(generation)
            state = await asyncio.to_thread(self.backend.get_status)
            self._current(generation)
            stamp = state.get('timestamp')
            if (state.get('connected') is True and state.get('flight_state') == 'landed'
                    and state.get('height_cm') == 0 and type(stamp) in (int, float)
                    and math.isfinite(stamp) and 0 <= time.time() - stamp <= .5):
                return
            await asyncio.sleep(min(.05, max(0., deadline - time.monotonic())))
        raise RuntimeError('landing_confirmation_timeout')

    async def dispatch(self, method, args, kwargs):
        generation = self.generation
        self._current(generation)
        if not isinstance(args, list) or not isinstance(kwargs, dict) or kwargs:
            raise ValueError('RPC accepts positional arguments only')
        if method in ('get_status', 'is_following'):
            if args:
                raise ValueError('read-only status takes no arguments')
            result = await asyncio.to_thread(getattr(self.backend, method))
            self._current(generation)
            return result
        if method in ('observe', 'describe'):
            if len(args) != 1 or not isinstance(args[0], str) or not 0 < len(args[0]) <= 2000:
                raise ValueError('vision requires a target/question of 1 to 2000 characters')
            if self.vision is None:
                raise RuntimeError('vision_model_not_configured')
            result = await asyncio.to_thread(getattr(self.vision, method), args[0])
            self._current(generation)
            if method == 'observe':
                if not isinstance(result, dict) or type(result.get('visible')) is not bool:
                    raise ValueError('invalid_navigation_observation')
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
            self._current(generation)
            return result
        if method == 'adjust_follow_reference':
            axes = {'depth': ('move_forward','move_back'),
                    'horizontal': ('move_left','move_right'),
                    'vertical': ('move_up','move_down')}
            if len(args) != 1 or not isinstance(args[0],str) or args[0] not in axes:
                raise ValueError('follow adjustment axis must be depth, horizontal or vertical')
            if not self.acquired or not self.was_following or self.ticket is None:
                raise RuntimeError('follow_adjustment_requires_paused_follow')
            if self.last_translation not in axes[args[0]] or self.translation_completed_at is None:
                raise RuntimeError('follow_adjustment_requires_completed_matching_translation')
            self.follow_adjust_axis = args[0]
            return {'ok':True,'status':'accepted','axis':args[0],
                    'reason':'applied_only_after_success_and_fresh_post_action_frame'}
        if method in ('start_follow', 'stop_follow'):
            if args:
                raise ValueError('follow takes no arguments')
            if self.follow_adjust_axis is not None:
                if method == 'start_follow':
                    raise RuntimeError('follow_adjustment_must_be_last_motion')
                self.follow_adjust_axis = None
            self.resume = False
            if method == 'stop_follow':
                result = await self._bound(self.backend.stop_follow)
                self._current(generation)
                if not result.get('ok'):
                    raise RuntimeError('follow_stop_unconfirmed: ' + str(result))
                return result
            if await asyncio.to_thread(self.backend.is_following):
                self._current(generation)
                return {'ok': True, 'status': 'already_following', 'simulated': self.simulated}
            # The priority stop lease may remain after an interrupted program.
            # Acquire a NEW lease before releasing model ownership to follow.
            await self.acquire()
            self._current(generation)
            released = await self._bound(self.backend.release_control, 'model')
            if not released.get('ok'):
                raise RuntimeError('follow_handoff_release_failed: ' + str(released))
            self.acquired = False
            self._current(generation)
            result = await self._bound(self.backend.start_follow)
            self._current(generation)
            if not result.get('ok'):
                raise RuntimeError('follow_start_failed: ' + str(result))
            return result
        if method not in MOTION:
            raise ValueError('host capability not exposed: ' + str(method))
        if self.follow_adjust_axis is not None:
            if method not in ('land','stop_motion'):
                raise RuntimeError('follow_adjustment_must_be_last_motion')
            self.follow_adjust_axis = None
        if method in DISTANCE:
            if len(args) != 1 or type(args[0]) is not int or not 20 <= args[0] <= 500:
                raise ValueError('distance must be an integer from 20 to 500 cm')
        elif method == 'rotate':
            if len(args) != 1 or type(args[0]) is not int or not -360 <= args[0] <= 360 or args[0] == 0:
                raise ValueError('rotation must be a nonzero integer from -360 to 360 degrees')
        elif method == 'update_velocity':
            if len(args) != 4 or any(type(v) is not int or not -100 <= v <= 100 for v in args):
                raise ValueError('RC requires four integers from -100 to 100')
        elif args:
            raise ValueError('action takes no arguments')
        if method in DISTANCE | {'rotate', 'update_velocity'}:
            self._navigation_evidence(method, args)
        if method in ('land', 'stop_motion'):
            self.resume = False
        await self.acquire()
        self._current(generation)
        result = await self._bound(self.backend.action, method, *args)
        self._current(generation)
        permitted = ('completed', 'accepted') if method in ('stop_motion', 'land', 'update_velocity') else ('completed',)
        if not result.get('ok') or result.get('status') not in permitted:
            self.resume = False
            raise RuntimeError('SDK_action_failed: ' + str(result))
        if isinstance(result.get('grant'), dict):
            self.grant = dict(result['grant'])
        if method == 'land':
            await self._confirm_landed(generation)
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
        self.cancelled.set()
        self.resume = False
        # Dispatch directly first. A follow cleanup lock, a model action, or a
        # network wait must not sit in front of the trusted emergency channel.
        takeover = getattr(self.backend, 'priority_stop', None)
        if not callable(takeover):
            return {'ok': False, 'reason': 'priority_channel_unavailable'}
        result = await asyncio.to_thread(takeover, bool(land))
        try:
            stopped = await asyncio.to_thread(self.backend.stop_follow)
            if not stopped.get('ok'):
                result = dict(result, follow_cleanup=stopped)
        except Exception as exc:
            result = dict(result, follow_cleanup_error=str(exc))
        # Do NOT replace self.grant with the emergency grant. An old finish()
        # can only operate on its old lease, which the priority action revoked.
        return {key: value for key, value in result.items() if key != 'grant'}

    async def _superseded(self, grant):
        state = await asyncio.to_thread(self.backend.get_status)
        epoch = grant.get('control_epoch')
        command = grant.get('command_epoch')
        return (type(epoch) is int and state.get('control_epoch') != epoch
                or type(command) is int and state.get('command_epoch') != command
                or state.get('control_owner') not in ('model',))

    async def finish(self, success):
        if not success or self.cancelled.is_set():
            self.cancelled.set()
            cleanup_follow = getattr(self.backend, 'stop_cancelled_follow', None)
            if cleanup_follow is not None:
                cleaned = await asyncio.to_thread(cleanup_follow, self.cancelled)
                if not cleaned.get('ok'):
                    raise RuntimeError('cancelled_follow_cleanup_failed: ' + str(cleaned))
        if not self.acquired:
            self.cleanup = {'status': 'no_control'}
            return
        generation = self.generation
        grant = dict(self.grant or {})
        ticket = self.ticket
        if await self._superseded(grant):
            self.acquired = False
            self.cleanup = {'status': 'superseded'}
            return
        if not success or self.cancelled.is_set():
            stopped = await self._bound(self.backend.action, 'stop_motion', grant=grant, cleanup=True)
            if not stopped.get('ok'):
                if await self._superseded(grant):
                    self.acquired = False
                    self.cleanup = {'status': 'superseded'}
                    return
                self.cleanup = {'status': 'stop_unconfirmed', 'result': stopped}
                raise RuntimeError('cleanup_stop_failed: ' + str(stopped))
            if isinstance(stopped.get('grant'), dict):
                grant = self.grant = dict(stopped['grant'])
        released = await self._bound(self.backend.release_control, 'model', grant=grant, cleanup=True)
        if not released.get('ok'):
            if await self._superseded(grant):
                self.acquired = False
                self.cleanup = {'status': 'superseded'}
                return
            self.cleanup = {'status': 'release_unconfirmed', 'result': released}
            raise RuntimeError('cleanup_release_failed: ' + str(released))
        self.acquired = False
        self.cleanup = {'status': 'released'}
        if (generation == self.generation and success and self.resume
                and self.was_following and not self.cancelled.is_set()):
            if ticket is None:
                raise RuntimeError('follow_recovery_ticket_missing')
            if self.follow_adjust_axis is None:
                recovered = await self._bound(self.backend.finish_follow_action, ticket, True)
            else:
                recovered = await self._bound(self.backend.finish_follow_action, ticket, True,
                                              self.follow_adjust_axis, self.translation_completed_at)
            self._current(generation)
            if not recovered.get('ok'):
                self.cleanup = {'status': 'follow_resume_failed', 'result': recovered}
                raise RuntimeError('follow_resume_failed: ' + str(recovered))
            self.cleanup = {'status': 'follow_resumed'}
            if self.follow_adjust_axis is not None:
                self.cleanup['reference_adjusted'] = self.follow_adjust_axis
