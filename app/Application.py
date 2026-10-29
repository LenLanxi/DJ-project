"""One production entry: real voice/text -> Python sandbox -> Host -> real SDK.

Hardware is opt-in. No canned intent parser, simulated-aircraft mode, STT/TTS
fallback, legacy execution chain or host Python exec is selectable here.
"""
import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import sys
import time

from app.Runtime import UnifiedRuntime
from app.SessionLog import SessionLog
from app.Cleanup import cleanup_step, cleanup_record
from app.Hud import EMERGENCY_HELP, operator_controls
from app.DeviceSession import DeviceSession
from app.Tools import (PROGRAM_INSTRUCTIONS, RTC_INSTRUCTIONS, RTC_TOOLS,
                       OMNI_INSTRUCTIONS, OMNI_TOOLS, PROGRAM_MAX_BYTES)


def plan_program(client, text, image=None):
    content = text
    if image:
        from navigation.vision import Vision
        frame = Vision(None, None, image).frame()
        content = [{'type': 'text', 'text': text},
                   {'type': 'image_url', 'image_url': {'url': frame['image_url']}}]
    raw = client.complete([
        {'role': 'system', 'content': PROGRAM_INSTRUCTIONS + '\n只返回JSON对象：{"code":"完整Python", "explanation":"说明"}。不执行就code为空。'},
        {'role': 'user', 'content': content}])
    result = json.loads(raw)
    if not isinstance(result, dict) or not isinstance(result.get('code'), str):
        raise ValueError('模型未返回有效程序JSON')
    if len(result['code'].encode()) > PROGRAM_MAX_BYTES:
        raise ValueError('模型程序超过60KB')
    return result


async def supervise(operation, controls):
    """A dead emergency/HUD task must not leave the main task silently running."""
    task = asyncio.create_task(operation)
    try:
        done, _ = await asyncio.wait((task, controls), return_when=asyncio.FIRST_COMPLETED)
        if controls in done:
            await controls
            raise RuntimeError('操作员控制任务意外结束，停止会话')
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class Session:
    """Own every component created for one application run and its shutdown order."""
    def __init__(self, args, log):
        self.args, self.log = args, log
        self.device = self.runtime = self.controls = None
        self.bridge = self.service = self.voice_route = None

    async def run(self):
        args, log = self.args, self.log
        from providers.model_client import ModelClient
        if not args.real_drone:
            raise ValueError('执行或语音必须显式--real-drone；无模拟模式。只生成程序使用plan --text')
        code = None
        if args.program:
            code = Path(args.program).read_text(encoding='utf-8-sig')
            if not code.strip() or len(code.encode()) > PROGRAM_MAX_BYTES:
                raise ValueError('程序必须非空且不超过60KB')
        text_client = ModelClient(on_usage=log) if args.text else None
        voice = args.command == 'voice'
        voice_route = voice_client = omni_service_class = rtc_node = None
        if voice:
            from providers.VoiceRoutes import load_voice_route
            voice_route = load_voice_route(args.voice_provider, alibaba_credentials=args.alibaba_credentials,
                doubao_credentials=args.doubao_credentials, log=log)
            voice_client = voice_route.vision_client(log)
            if voice_route.provider == 'doubao':
                from providers.RTCRuntime import validate_environment
                rtc_node = validate_environment(args.max_session_seconds, voice_route.rtc_environment)
            else:
                from providers.OmniRuntime import OmniService
                omni_service_class = OmniService
            log({'event': 'voice_provider_selected', 'provider': voice_route.provider,
                 'code_limit_bytes': voice_route.code_limit,
                 'automatic_provider_switch': False, 'billing_verified': False})
        from execution.sandbox_runner import SandboxRunner
        probe = SandboxRunner(image=args.sandbox_image, timeout=args.program_timeout)
        await probe.check()  # Docker/RTC credentials validated before connecting hardware
        self.voice_route = voice_route
        status = 0
        try:
            self.device = DeviceSession(args, log)
            device = await self.device.open()
            from navigation.vision import Vision
            client = voice_client if voice else text_client
            if client is None and args.vision:
                client = ModelClient(on_usage=log)
            log({'event': 'vision_capability', 'configured': client is not None})
            self.runtime = UnifiedRuntime(device, self.device.follow, Vision(client, device), log,
                runner=SandboxRunner(image=args.sandbox_image, timeout=args.program_timeout))
            self.controls = asyncio.create_task(operator_controls(self.runtime, args, log, self.device))
            print(EMERGENCY_HELP, flush=True)
            if args.program:
                result = await supervise(self.runtime.run(code), self.controls)
            elif args.text:
                generation = self.runtime.generation
                plan = await supervise(asyncio.to_thread(plan_program, client, args.text), self.controls)
                log({'event': 'model_plan', 'plan': plan, 'usage_source': 'model_request_end_by_request_id'})
                if not plan['code'].strip():
                    print(plan.get('explanation') or '模型未生成动作程序', flush=True)
                    result = None
                else:
                    result = await supervise(self.runtime.run(plan['code'], expected_generation=generation), self.controls)
            else:
                if self.voice_route.provider == 'alibaba':
                    self.service = omni_service_class(self.runtime, device, log, self.voice_route.config,
                        OMNI_INSTRUCTIONS, OMNI_TOOLS, seconds=args.max_session_seconds,
                        http_port=args.web_port)
                    url = await self.service.start()
                else:
                    from providers.RTCBridge import RTCBridge
                    from providers.RTCRuntime import RTCProcess
                    self.bridge = RTCBridge(self.runtime, device, log, max_session_seconds=args.max_session_seconds)
                    await self.bridge.start()
                    log.secrets += (self.bridge.token,)
                    self.service = RTCProcess(log, env=self.voice_route.rtc_environment, node=rtc_node, web_port=args.web_port)
                    url = await self.service.start(self.bridge, args.max_session_seconds, RTC_INSTRUCTIONS, RTC_TOOLS)
                print('[语音/%s] 请手动打开 %s，授权麦克风并明确选择蓝牙耳机输入和输出。' %
                      (self.voice_route.provider, url), flush=True)
                print('[语音] 不自动打开浏览器或电脑摄像头；每次连接最多%s秒。Ctrl+C结束并执行退出策略。' % args.max_session_seconds, flush=True)
                try:
                    remaining = max(0., self.service.deadline - time.time())
                    await asyncio.wait_for(supervise(self.service.wait(), self.controls), remaining)
                except asyncio.TimeoutError:
                    log({'event': 'runtime_duration_limit', 'seconds': args.max_session_seconds})
                    print('[语音] 达到全局时限，先停止/降落，再关闭云端会话。', flush=True)
                result = None
            if result is not None:
                print(json.dumps(log.clean(result), ensure_ascii=False, indent=2))
                status = 0 if result.get('ok') else 2
        finally:
            # Cancellation requests shutdown; shield the owner until cleanup finishes.
            cleanup_task = asyncio.create_task(self.close())
            try:
                cleanup = await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await asyncio.shield(cleanup_task)
                raise
        return status if status else (0 if cleanup['ok'] else 1)

    async def close(self):
        args, log = self.args, self.log
        outcome = {'ok': True, 'steps': []}
        omni = self.service and self.voice_route and self.voice_route.provider == 'alibaba'
        if omni:
            async def revoke():
                # Stop media pumps before revoking self.runtime generation.
                return self.service.revoke(args.exit_action == 'land')
            await cleanup_step(outcome, log, 'voice_revoke', revoke, 1)
        if self.runtime:
            async def cancel():
                self.runtime.close()
            await cleanup_step(outcome, log, 'runtime_cancel', cancel, 1)
        async def close_voice():
            return await self.service.close()
        if omni:
            await cleanup_step(outcome, log, 'voice_service', close_voice, 30)
        if self.bridge:
            await cleanup_step(outcome, log, 'rtc_bridge', self.bridge.close, 3)
        if self.device:
            await cleanup_step(outcome, log, 'real_resources',
                               lambda: self.device.close(self.runtime), args.landing_timeout + 100)
        if self.controls:
            async def close_controls():
                self.controls.cancel()
                try:
                    await self.controls
                except asyncio.CancelledError:
                    pass  # Expected only for this explicitly cancelled task.
            await cleanup_step(outcome, log, 'operator_controls', close_controls, 5)
        if self.service and not omni:
            await cleanup_step(outcome, log, 'voice_service', close_voice, 30)
        try:
            log({'event': 'runtime_cleanup', 'result': outcome})
        except Exception as exc:
            cleanup_record(outcome, log, 'cleanup_log', error=exc)
        return outcome


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('plan', help='只生成程序，不连接设备')
    plan.add_argument('--text', required=True)
    plan.add_argument('--image', help='静态图片，仅用于计划')
    plan.add_argument('--log-file')
    device = argparse.ArgumentParser(add_help=False)
    device.add_argument('--real-drone', action='store_true', help='显式连接真实Tello；没有模拟模式')
    device.add_argument('--allow-real-takeoff', action='store_true', help='预检后允许模型请求起飞，默认禁止')
    device.add_argument('--address', default=None)
    device.add_argument('--follow-config', help='显式openpose_pid JSON配置')
    device.add_argument('--min-battery', type=int, default=30,
                        help='项目电量门限；0仅关闭百分比门限，仍要求有效遥测，不绕过飞控保护')
    device.add_argument('--temp-max', type=float, default=95)
    device.add_argument('--temp-warn', type=float, default=85)
    device.add_argument('--frame-timeout', type=float, default=10)
    device.add_argument('--landing-timeout', type=float, default=15)
    device.add_argument('--exit-action', choices=('land', 'hover'), default='land',
                        help='结束/失败默认请求降落；hover只归零，需人工继续控制')
    device.add_argument('--show-video', action='store_true', help='显示实时HUD；q只关显示，不结束任务')
    device.add_argument('--program-timeout', type=float, default=90)
    device.add_argument('--sandbox-image', default='companion-sandbox:0.4')
    device.add_argument('--log-file', help='JSONL日志路径；不记录密钥、图片或音频')

    device.set_defaults(program=None, text=None)
    run = commands.add_parser('run', parents=[device], help='执行文件或根据文字生成后执行')
    run.add_argument('--vision', action='store_true', help='文件程序启用环境配置的视觉客户端')
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument('--program', help='UTF-8 Python文件')
    source.add_argument('--text', help='由真实模型生成程序并执行')
    voice = commands.add_parser('voice', parents=[device], help='持续语音会话')
    voice.set_defaults(vision=False)
    voice.add_argument('--voice-provider', choices=('alibaba', 'doubao'), default='alibaba',
                        help='默认阿里云；豆包为启动前显式选择的备选，不在飞行中自动切换或重放')
    voice.add_argument('--alibaba-credentials', default=os.getenv('ALIBABA_CREDENTIALS_FILE'),
                        help='服务端阿里云凭据JSON路径；也可设置ALIBABA_CREDENTIALS_FILE')
    voice.add_argument('--doubao-credentials', default=os.getenv('DOUBAO_CREDENTIALS_FILE'),
                        help='仅豆包备选路线使用；阿里路线不读取此文件')
    voice.add_argument('--max-session-seconds', type=float, default=30000,
                        help='会话默认30000秒，到期停止云端并退出')
    voice.add_argument('--web-port', type=int, default=18848,
                        help='两条语音路线的固定本机网页端口，默认18848；占用时失败，不自动换端口')
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if callable(reconfigure):
            reconfigure(encoding='utf-8', errors='replace')
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.command == 'voice' and not 1 <= args.web_port <= 65535:
        parser.error('--web-port必须在1到65535之间')
    if args.command != 'plan' and not 0 <= args.min_battery <= 100:
        parser.error('--min-battery必须在0到100之间；0关闭项目百分比门限')
    if args.command != 'plan' and not 0 < args.temp_warn < args.temp_max <= 100:
        parser.error('温度阈值须满足0 < warn < max <= 100')
    limits = () if args.command == 'plan' else ('program_timeout', 'frame_timeout', 'landing_timeout')
    if args.command == 'voice': limits += ('max_session_seconds',)
    for name in limits:
        value = getattr(args, name)
        maximum = 30000 if name == 'max_session_seconds' else 900
        if not math.isfinite(value) or not 0 < value <= maximum:
            parser.error('--%s必须在0到%s秒之间' % (name.replace('_', '-'), maximum))
    log = SessionLog(args.log_file)
    print('[日志] ' + str(log.path.resolve()), flush=True)
    try:
        log({'event': 'runtime_start', 'mode': args.command})
        if args.command == 'plan':
            from providers.model_client import ModelClient
            result = plan_program(ModelClient(on_usage=log), args.text, args.image)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        return asyncio.run(Session(args, log).run())
    except KeyboardInterrupt:
        print('已中断；请核对实际落地状态。', flush=True)
        return 130
    except Exception as exc:
        message = log.clean(str(exc))
        try: log({'event': 'runtime_error', 'error': message})
        except Exception: pass
        print('[错误] ' + message, file=sys.stderr, flush=True)
        return 1
    finally:
        log.close()


if __name__ == '__main__':
    raise SystemExit(main())
