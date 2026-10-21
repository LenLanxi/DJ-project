"""Synchronous group-4 adapter for the public SDK contract.

No Tello connection is created here. The shared SDK owns all device access.
The current SDK is a stub; this adapter makes no claim of physical completion.
"""
from dataclasses import fields, is_dataclass

from drone_sdk.controller import (
    get_frame, get_status, is_following, land, move_forward,
    release_control, request_control, stop_motion, takeoff,
)

from .program_runner import validate


def snapshot(value):
    """Convert SDK dataclasses without copying a potentially large image array."""
    if not is_dataclass(value) or isinstance(value, type):
        raise TypeError('公共SDK应返回contract.py定义的dataclass')
    return {field.name: getattr(value, field.name) for field in fields(value)}


class SdkRunner:
    # The SDK contract has no simulation indicator: unknown is represented by null.
    simulated = None
    supports_priority_interrupt = False

    def get_status(self):
        state = snapshot(get_status())
        return {**state, 'backend': 'sdk', 'simulated': self.simulated}

    def get_frame(self):
        return snapshot(get_frame())

    def _record(self, accepted, result, command=None, **extra):
        return {'accepted': accepted, 'backend': 'sdk', 'simulated': self.simulated,
                'result': '[公共SDK] ' + result, 'command': command, **extra}

    def run(self, code):
        try:
            name = validate(code)
        except (SyntaxError, ValueError, RecursionError):
            return self._record(False, '程序检查未通过，未执行动作')

        try:
            state = self.get_status()
            if name in ('get_status', 'get_battery'):
                message = ('当前电量为%d%%。' % state['battery']
                           if name == 'get_battery' else str(state))
                return self._record(True, message, state=state)

            if name in ('start_follow', 'stop_follow'):
                return self._record(True, '跟随控制权交接尚未完成集成，未执行动作',
                                    {'ok': False, 'status': 'failed',
                                     'reason': 'follow_not_integrated'})

            if not state['connected']:
                return self._record(True, '设备未连接，未执行动作',
                                    {'ok': False, 'status': 'failed', 'reason': 'disconnected'})
            # Do not infer that the current start/stop-follow stubs transferred ownership.
            if state['control_owner'] == 'follow' or is_following():
                return self._record(True, '跟随尚未确认停止，未申请接管或执行动作',
                                    {'ok': False, 'status': 'failed',
                                     'reason': 'follow_handoff_unavailable'})

            control = snapshot(request_control('model'))
            if not control['ok']:
                return self._record(True, '未取得控制权，未执行动作：' + control['reason'],
                                    {'ok': False, 'status': 'failed',
                                     'reason': control['reason']}, control=control)

            command = None
            cleanup = None
            try:
                action = {'takeoff': lambda: takeoff('model'),
                          'land': lambda: land('model'),
                          'stop_motion': lambda: stop_motion('model'),
                          'move_forward': lambda: move_forward('model', 30)}[name]
                try:
                    command = snapshot(action())
                except TimeoutError:
                    command = {'ok': False, 'status': 'timeout', 'reason': 'sdk_timeout'}
                except Exception as exc:
                    command = {'ok': False, 'status': 'failed',
                               'reason': 'sdk_exception:' + type(exc).__name__}
            finally:
                try:
                    cleanup = snapshot(release_control('model'))
                except Exception as exc:
                    cleanup = {'ok': False, 'reason': 'release_exception:' + type(exc).__name__}

            completed = command['ok'] and command['status'] == 'completed'
            message = ('SDK报告动作完成（未验证物理飞行）' if completed
                       else 'SDK未报告动作完成：' + command['status'] + '/' + command['reason'])
            if not cleanup['ok']:
                message += '；控制权释放失败，请检查共享SDK状态'
            return self._record(True, message, command, control=control,
                                release=cleanup, state=self.get_status())
        except Exception as exc:
            # Do not retry a command when its execution outcome is uncertain.
            return self._record(True, 'SDK调用或状态读取异常，不自动重试：' + type(exc).__name__,
                                {'ok': False, 'status': 'failed', 'reason': 'sdk_error'})
