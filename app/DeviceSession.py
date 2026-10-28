"""Real-device preflight, resource ownership and confirmed shutdown."""
import asyncio
import json
import math
import os
from pathlib import Path
import time
import traceback

from app.Cleanup import cleanup_step, cleanup_record

def validate_follow_config(path=None):
    if path:
        config_path = Path(path).expanduser().resolve()
        if not config_path.is_file():
            raise ValueError('跟随配置不存在：' + str(config_path))
        os.environ['R3_FOLLOW_CONFIG'] = str(config_path)
    if not os.getenv('R3_FOLLOW_CONFIG'):
        raise ValueError('必须显式 --follow-config，禁止隐式退回旧控制器')
    from follow.r3_follow.config import load_config
    config = load_config()
    if config.control_profile != 'openpose_pid':
        raise ValueError('正式入口只接受显式openpose_pid配置')
    return config


def validate_health(health, args):
    if health.get('simulated') is not False or not health.get('connected') or health.get('link_ok') is not True:
        raise RuntimeError('真实设备/链路未确认；不会降级成模拟飞机')
    battery = health.get('battery')
    if type(battery) is not int or not args.min_battery <= battery <= 100:
        raise RuntimeError('电量未知或低于门限%d%%' % args.min_battery)
    temperature = health.get('temperature_c')
    if type(temperature) in (int, float) and math.isfinite(temperature):
        if temperature >= args.temp_max:
            raise RuntimeError('板/芯片温度达到上限：%s℃' % temperature)
        if temperature >= args.temp_warn:
            print('[预检] 板/芯片温度偏高：%s℃' % temperature, flush=True)
    elif args.allow_real_takeoff:
        raise RuntimeError('缺少温度遥测，起飞授权预检不完整')
    if args.allow_real_takeoff and health.get('telemetry_fresh') is not True:
        raise RuntimeError('遥测过期，起飞授权预检不完整')
    if not health.get('video'):
        raise RuntimeError('实时图传未启动，拒绝正式任务')


class DeviceSession:
    def __init__(self, args, log):
        self.args, self.log = args, log
        self.device = self.follow = None
        self.ready = False

    def diagnostics(self):
        from drone_sdk import _driver as driver
        return driver.diagnostics()

    async def open(self):
        args, log = self.args, self.log
        from drone_sdk import _driver as drv, controller as ctl
        from follow.r3_follow.service import FollowService
        config = validate_follow_config(args.follow_config)
        recovery_window = float(os.environ.get('R3_RECOVERY_WINDOW_S', '30'))
        self.device = ctl
        log({'event': 'follow_config', 'path': os.getenv('R3_FOLLOW_CONFIG'),
             'profile': config.control_profile, 'steering': config.steering_mode,
             'target_id': config.target_id})
        ctl.configure_takeoff(authorized=False, min_battery=args.min_battery)
        stage = 'sdk_connect'
        try:
            log({'event':'preflight_stage','stage':stage,'status':'start'})
            await asyncio.to_thread(drv.connect, args.address, timeout=drv.RESPONSE_TIMEOUT_S)
            self.follow = FollowService(ctl, config=config, recovery_window=recovery_window,
                                        log_sink=log, console_notices=True)
            log({'event':'preflight_stage','stage':stage,'status':'success','driver':drv.snapshot()})
            stage = 'video_start'
            log({'event':'preflight_stage','stage':stage,'status':'start'})
            if not await asyncio.to_thread(drv.start_video):
                raise RuntimeError('图传启动失败')
            stage = 'video_ready'
            log({'event':'preflight_stage','stage':stage,'status':'start'})
            deadline = time.monotonic() + args.frame_timeout
            first = None
            while time.monotonic() < deadline:
                packet = ctl.get_frame()
                if packet.frame is not None and not packet.stale:
                    if first is not None and packet.frame_id > first:
                        break
                    first = packet.frame_id
                await asyncio.sleep(.05)
            else:
                video = drv.video_stats()
                log({'event':'preflight_video_failed','video':video})
                print('[图传诊断] '+json.dumps(video,ensure_ascii=False),flush=True)
                raise RuntimeError('没有收到连续两张新鲜真实画面')
            stage = 'health_validation'
            health = await asyncio.to_thread(drv.health)
            log({'event':'preflight_health','health':health})
            print('[预检] 电量=%s%%，温度=%s℃，遥测新鲜=%s' %
                  (health.get('battery'),health.get('temperature_c'),health.get('telemetry_fresh')),flush=True)
            validate_health(health, args)
            ctl.configure_takeoff(authorized=args.allow_real_takeoff, min_battery=args.min_battery)
            log({'event': 'real_ready', 'health': health, 'takeoff_authorized': args.allow_real_takeoff})
            print('[设备] 真机已连接，起飞授权=%s，最低电量=%d%%' % (args.allow_real_takeoff, args.min_battery), flush=True)
            self.ready = True
            return self.device
        except BaseException as exc:
            log({'event':'preflight_failed','stage':stage,'error_type':type(exc).__name__,
                 'error':str(exc),'traceback':traceback.format_exc(),'driver':drv.snapshot(),
                 'video':drv.video_stats()})
            raise

    async def close(self, runtime=None):
        """Aggregate stop/landing/resource failures without skipping later cleanup."""
        args, log = self.args, self.log
        initializing = not self.ready
        from drone_sdk import _driver as drv, controller as ctl
        outcome = {'ok': True, 'steps': []}
        state = await cleanup_step(outcome, log, 'exit_state',
                                   lambda: asyncio.to_thread(ctl.get_status), 3)
        # Unknown state never justifies assuming airborne or landed.
        land_policy = not initializing and (args.exit_action == 'land'
                                           or bool(getattr(runtime, 'land_requested', False)))
        need_land = land_policy and getattr(state, 'flight_state', None) in ('flying', 'landing')
        if runtime:
            result = await cleanup_step(outcome, log, 'exit_action',
                                        lambda: runtime.interrupt(need_land), 12)
        else:
            if self.follow:
                async def stop_follow():
                    result = await asyncio.to_thread(self.follow.stop)
                    if not result.ok:
                        raise RuntimeError('follow_stop_failed: ' + result.reason)
                await cleanup_step(outcome, log, 'stop_follow', stop_follow, 3)
            await cleanup_step(outcome, log, 'cancel_commands',
                               lambda: asyncio.to_thread(drv.cancel_pending, 'runtime_exit'), 3)
            async def zero():
                if not await asyncio.to_thread(drv.set_velocity, (0, 0, 0, 0), 'runtime_exit'):
                    raise RuntimeError('stop_send_failed')
            result = await cleanup_step(outcome, log, 'exit_action', zero, 3)
        try:
            log({'event': 'exit_action', 'land': need_land, 'result': result})
        except Exception as exc:
            cleanup_record(outcome, log, 'exit_action_log', error=exc)

        async def confirm_landing():
            deadline = time.monotonic() + args.landing_timeout
            while True:
                current = await asyncio.to_thread(ctl.get_status)
                if current.flight_state == 'landed' and not drv.action_pending():
                    return current
                if not need_land or time.monotonic() >= deadline:
                    raise RuntimeError('landing_unconfirmed: ' + str(current.flight_state))
                await asyncio.sleep(.1)
        if land_policy:
            await cleanup_step(outcome, log, 'landing_confirmation', confirm_landing,
                               args.landing_timeout + 3)

        if runtime:
            await cleanup_step(outcome, log, 'pending_resources', runtime.retry_cleanup, 15)
        if self.follow:
            async def close_follow():
                result = await asyncio.to_thread(self.follow.close, wait=True)
                if not result.ok:
                    raise RuntimeError('follow_cleanup_failed: ' + result.reason)
            await cleanup_step(outcome, log, 'follow_workers', close_follow, 3)
        async def close_video():
            if not await asyncio.to_thread(drv.stop_video):
                raise RuntimeError('video_stop_failed')
        await cleanup_step(outcome, log, 'video', close_video, 25)

        async def disconnect():
            current = await asyncio.to_thread(ctl.get_status)
            if current.flight_state == 'unknown' or land_policy and current.flight_state != 'landed':
                raise RuntimeError('landing_unconfirmed: ' + str(current.flight_state))
            if current.flight_state != 'landed':
                return {'status': 'hover_connection_retained', 'flight_state': current.flight_state}
            # disconnect() clears decoder handles even on failed stop. Keep them
            # reachable for retry until the independent cleanup steps confirm exit.
            if (runtime and runtime.pending is not None) or any(
                    not step['ok'] for step in outcome['steps']
                    if step['step'] in ('video', 'follow_workers')):
                raise RuntimeError('resource_cleanup_unconfirmed_connection_retained')
            if not await asyncio.to_thread(drv.disconnect):
                raise RuntimeError('disconnect_unconfirmed')
        await cleanup_step(outcome, log, 'disconnect', disconnect, 25)
        return outcome
