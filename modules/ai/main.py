"""Formal entry: explicit mock/real mode, legacy text and Docker/Realtime paths."""
import argparse
import json
import os
import sys
import time
import threading
from pathlib import Path
from ai_agent import Agent
from demo_ai import ScriptedClient
from model_client import ModelClient
from voice_service import VoiceService
from vision_service import VisionService
from session import Session
from settings import load
import audio_io

# --- 全局急停热键（Windows）：不依赖终端是否在前台 ---
# 用 GetAsyncKeyState 读全局键盘状态，无需消息循环、无需管理员权限。
_VK_F12, _VK_F11 = 0x7B, 0x7A
try:
    import ctypes
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
except Exception:                      # 非 Windows 或受限环境：热键自动失效
    _user32 = _kernel32 = None
_hotkey_prev = {_VK_F12: False, _VK_F11: False}


def key_down(vk):
    if _user32 is None:
        return False
    try:
        return bool(_user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


def hotkey_edge(vk):
    """只在按下的那一瞬间返回 True，按住不会连续触发。"""
    now = key_down(vk)
    was = _hotkey_prev.get(vk, False)
    _hotkey_prev[vk] = now
    return now and not was


def focus_console():
    """把控制台窗口拉到最前，方便现场随时看到日志并 Ctrl+C。

    Windows 只允许"当前有焦点的进程"调 SetForegroundWindow，因此失败时
    用一次 ALT 按键解除该限制后再试。
    """
    if _user32 is None or _kernel32 is None:
        return False
    try:
        hwnd = _kernel32.GetConsoleWindow()
        if not hwnd:
            return False
        _user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        if _user32.SetForegroundWindow(hwnd):
            return True
        _user32.keybd_event(0x12, 0, 0, 0)   # ALT down，解除前台锁定
        try:
            return bool(_user32.SetForegroundWindow(hwnd))
        finally:
            _user32.keybd_event(0x12, 0, 2, 0)  # ALT up
    except Exception:
        return False


EMERGENCY_HELP = '急停：F12=零速度悬停  F11=立即降落（终端在后台也有效）'


HELP = '''输入文字与模拟飞机互动。命令：
 /listen 开关免按键全双工语音（自动 VAD 录音→识别→执行，默认启动时 --always-on 打开）
 /voice 录音（回车开始，固定时长，识别后直接执行；加 --voice-confirm 可二次确认）
 /image 图片路径 | 问题   本地测试图片问答
 /stop 停止模拟运动；/land 模拟降落（均绕过模型）
 /mute 停止当前播报；/reset 清空对话；/status 查看模拟状态；/quit 退出
离线样例：还有多少电、起飞、跟着我、向前走30厘米、降落'''

def build_real_runner(args):
    """真机模式：连接 Tello、给"起飞"上闸、把状态换成真实读数。

    连接之后任何一步失败都必须先断开，不能把半连接状态留给调用者。
    """
    root = str(Path(__file__).resolve().parents[2])      # main.py → modules/ai → modules → 仓库根
    if root not in sys.path:
        sys.path.insert(0, root)
    import drone_sdk._driver as drv
    from drone_sdk.ai_bridge import UnifiedDrone
    from program_runner import Runner
    from demo_text import Demo

    try:
        drv.connect(args.address, timeout=drv.RESPONSE_TIMEOUT_S)
    except Exception as exc:          # djitellopy 抛的是自定义 TelloException，统一转成 RuntimeError
        raise RuntimeError('真机连接失败：%s' % exc) from None
    try:
        return _wire_real_runner(args, drv, UnifiedDrone, Runner, Demo)
    except BaseException:
        shutdown_real(force=True)     # 半初始化也必须断开/关图传
        raise


def _wire_real_runner(args, drv, UnifiedDrone, Runner, Demo):
    from drone_sdk import controller as ctl
    ctl.configure_takeoff(authorized=args.allow_real_takeoff, min_battery=args.min_battery)
    try: video_ready=drv.start_video()
    except Exception as exc:
        video_ready=False
        print('图传不可用：%s；视觉动作将拒绝执行' % exc)
    if video_ready:
        from run_final_real_sim import FrameBridge
        frame_bridge = FrameBridge()
        frame_bridge.start()
        drv._frame_bridge=frame_bridge
    health = drv.health()
    print('真机已连接：电量 %s%%  高度 %scm  链路 %s  图传 %s' % (
        health['battery'], health['height_cm'],
        'ok' if health['link_ok'] else 'FAIL', 'on' if health['video'] else 'off'))
    if health['link_ok'] is not True:
        raise RuntimeError('真机链路异常，请先跑 python test_live_view.py --props-removed')
    if not health['video']:
        print('提示：图传未开启（不影响起飞/电机指令，坐标系内的动作将无法验证）')

    drone = UnifiedDrone()

    # 1) 状态换成真实读数（controller.get_status() 的 battery 是模拟常量 88）
    original_status = drone.get_status
    def real_status():
        status = original_status()
        info = drv.health()
        status.update(battery=info['battery'], height_cm=info['height_cm'],
                      connected=info['connected'], flying=info['flying'], simulated=False)
        return status
    drone.get_status = real_status
    drone.get_battery = lambda: drv.health()['battery']

    # 2) 起飞闸门：默认禁止；授权后还要过电量
    original_takeoff = drone.takeoff
    def guarded_takeoff(owner):
        if not args.allow_real_takeoff:
            print('[安全] 真机起飞被拒绝：启动时加 --allow-real-takeoff 才允许')
            return dict(ok=False, status='failed', simulated=False,
                        message='真机起飞未授权（需 --allow-real-takeoff）；未执行动作')
        battery = drv.health()['battery']
        if type(battery) is not int or not args.min_battery <= battery <= 100:
            print('[安全] 真机起飞被拒绝：电量 %s%% < %d%%' % (battery, args.min_battery))
            return dict(ok=False, status='failed', simulated=False,
                        message='电量 %s%% 未满足 %d%%门限，拒绝起飞' % (battery, args.min_battery))
        print('[安全] 真机起飞已授权（电量 %s%%，桨叶必须已拆除）' % (battery,))
        result = original_takeoff(owner)
        if not result.get('ok'):
            last_error = str(drv.health().get('last_error') or '')
            if 'takeoff_rejected' in last_error:
                print('[安全] 飞控拒绝了 takeoff：%s' % last_error)
                if 'battery_low' in last_error:
                    print('       → 电量太低。实测这台 Tello 在 %d%% 会直接回 error，'
                          '请充到 60%% 以上再起飞。' % drv.TAKEOFF_FIRMWARE_BATTERY)
                else:
                    print('       → 检查桨叶是否装反（正/反桨必须对角对应）、飞机是否水平静置。')
        return result
    drone.takeoff = guarded_takeoff

    print('真机动作授权：起飞=%s，电量阈值 %d%%' % (
        '允许' if args.allow_real_takeoff else '禁止', args.min_battery))
    return Runner(Demo(drone), allow_simulated_sdk=True, allow_programs=True), drone


_SHUTDOWN_DONE = {'done': False}


def shutdown_real(force=False):
    """真机模式统一收尾：停画面转发 → 停速度 → 关图传 → 断开。幂等、绝不抛异常。

    force=True 用于半初始化/异常路径：即使仍在空中也要断开（进程即将退出）。
    正常退出时若设备仍在空中，则保留链路（避免擅自放弃控制），但一定关掉图传与转发。
    """
    import drone_sdk._driver as drv
    bridge = getattr(drv, '_frame_bridge', None)
    if bridge is not None:
        try: bridge.stop(); bridge.join(1)
        except Exception as exc: print('[清理] 停止画面转发失败：%s' % exc)
        try: drv._frame_bridge = None
        except Exception: pass
    flying = False
    try: flying = bool(drv.health()['flying'])
    except Exception as exc: print('[清理] 读取飞行状态失败（仍继续清理）：%s' % exc)
    try: drv.cancel_pending('main_exit')
    except Exception: pass
    try: drv.set_velocity((0, 0, 0, 0), 'main_exit')
    except Exception as exc: print('[清理] 零速度发送失败：%s' % exc)
    try: drv.stop_video()
    except Exception as exc: print('[清理] 关闭图传失败：%s' % exc)
    if flying and not force:
        print('[清理] 设备仍在空中：已零速度并关闭图传，但保留链路；'
              '退出进程不代表设备已停止，需要独立人工降落确认')
        return
    try:
        drv.disconnect()
        print('[清理] 已零速度、关图传并断开真机')
    except Exception as exc:
        print('[清理] 断开失败（请手动断电）：%s' % exc)
    finally:
        _SHUTDOWN_DONE['done'] = True


async def run_modern(args):
    from backends import SDKBackend,MockBackend
    from gateway import Gateway
    from sandbox_runner import SandboxRunner
    from vision import Vision
    import asyncio
    backend=MockBackend()
    try:
        if args.real_drone:
            build_real_runner(args)
            backend=SDKBackend(Path(__file__).resolve().parents[2])
        if args.image and not backend.simulated: raise ValueError('真机模式禁止静态测试图片')
        client=ModelClient() if os.getenv('AI_CHAT_URL') else None
        vision=Vision(client,backend,args.image)
        gateway=Gateway(backend,vision,args.fixture_vision)
        if args.fixture_vision and not backend.simulated: raise ValueError('真机模式禁止视觉夹具')
        runner=SandboxRunner(gateway)
        if args.program:
            result=await runner.run(Path(args.program).read_text(encoding='utf-8'))
            print(json.dumps(result,ensure_ascii=False,indent=2))
            if not result['ok']: raise RuntimeError('沙箱任务未完成')
        else:
            from realtime import RealtimeSession
            from duplex_audio import DuplexAudio
            from audio_config import load_audio_config
            config=load_audio_config(args.audio_config,args.input_device,args.output_device)
            await RealtimeSession(runner,DuplexAudio(config=config),audio_config=config).run()
    finally:
        # 无论正常返回、异常还是 Ctrl+C：真机必须断开、关图传、停止画面转发。
        if getattr(args,'real_drone',False): shutdown_real()


VOLC_INSTRUCTIONS = (
    '你是无人机语音助手，通过耳机用中文简短交流。'
    '需要操作无人机时调用 run_program，参数 code 是一行 Python，格式 drone.<接口>(参数)。'
    '允许接口：get_status()、get_battery()、takeoff()、land()、start_follow()、'
    'stop_follow()、stop_motion()、move_forward(20到500的整数，单位厘米)。'
    '用户说停、别动、急停或降落时，立即调用 interrupt（land=true 表示降落）。'
    '只根据工具返回值回答；绝不要编造电量、高度、是否已起飞或画面内容。'
    '指令不清楚、超出允许范围或你听不懂时，不要调用任何工具，'
    '直接说：抱歉，我没听清，请再说一遍。'
)

VOLC_TOOLS = [
    {'type': 'function', 'name': 'run_program',
     'description': '执行一行无人机控制代码，例如 drone.takeoff()',
     'parameters': {'type': 'object',
                    'properties': {'code': {'type': 'string',
                                            'description': '形如 drone.takeoff() 的单行调用'}},
                    'required': ['code'], 'additionalProperties': False}},
    {'type': 'function', 'name': 'interrupt',
     'description': '立即停止当前动作；land=true 时直接降落',
     'parameters': {'type': 'object',
                    'properties': {'land': {'type': 'boolean'}},
                    'required': ['land'], 'additionalProperties': False}},
]


async def run_volc_dialog(args):
    """豆包全双工实时语音（Seeduplex 3.0）。

    与 --always-on 的差别：识别、理解、播报全部由服务端完成，本地只负责执行
    模型下发的工具调用。模型拿不到真实状态就不会乱说——它只能通过工具返回值
    了解执行结果。
    """
    from audio_config import load_audio_config
    from duplex_audio import DuplexAudio
    from volc_dialog import VolcDuplex

    if args.program:
        raise ValueError('--volc-dialog 不接受 --program')
    # 凭据检查放在最前面：没有密钥时不应先去连真机
    if not os.getenv('VOLC_API_KEY'):
        raise ValueError('缺少 VOLC_API_KEY：到火山控制台「API Key 管理」复制密钥值（不是名称），然后\n'
                         '  $env:VOLC_API_KEY="xxxx"')
    if args.real_drone:
        runner, _drone = build_real_runner(args)
    else:
        from demo_text import Demo
        from program_runner import Runner
        runner = Runner(Demo(), allow_programs=True)
    config = load_audio_config(args.audio_config, args.input_device, args.output_device)
    audio = DuplexAudio(config=config)

    def handle_log(event):
        name = event.get('event')
        if name in ('websocket_open', 'session_started', 'response_done', 'tts_started'):
            print('[豆包] ' + json.dumps(event, ensure_ascii=False), flush=True)

    def handle_user_text(text):
        print('\n[STT] 识别文本：' + str(text)[:200], flush=True)

    def handle_reply_text(text):
        print('[豆包] 回复：' + str(text)[:200], flush=True)

    def handle_speech_started():
        print('[打断] 用户开口，已停止当前播报', flush=True)

    dialog = VolcDuplex(runner, audio, instructions=VOLC_INSTRUCTIONS, tools=VOLC_TOOLS,
                        on_log=handle_log, on_user_text=handle_user_text,
                        on_reply_text=handle_reply_text,
                        on_speech_started=handle_speech_started)
    dialog.check_credentials()
    print('[豆包] 模型 %s / 音色 %s' % (dialog.model, dialog.voice))
    print('[豆包] 端点 %s' % dialog.url)
    print('[豆包] 已开启工具调用：run_program / interrupt')
    print(EMERGENCY_HELP)
    focus_console()

    async def watch_hotkeys():
        """全局热键：终端在后台也能急停。"""
        while True:
            stop_now = hotkey_edge(_VK_F12)
            land_now = hotkey_edge(_VK_F11)
            if stop_now:
                print('\n[急停] F12 → 零速度悬停', flush=True)
                try:
                    await dialog.emergency(False)
                    print('[急停] 指令已下发', flush=True)
                except Exception as exc:
                    print('[急停] 下发失败：%s' % exc, flush=True)
            if land_now:
                print('\n[急停] F11 → 立即降落', flush=True)
                try:
                    await dialog.emergency(True)
                    print('[急停] 降落已下发', flush=True)
                except Exception as exc:
                    print('[急停] 下发失败：%s' % exc, flush=True)
            await asyncio.sleep(0.05)

    import asyncio
    watcher = asyncio.create_task(watch_hotkeys())
    try:
        await dialog.run()
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        # 与 --realtime 一致：无论正常返回、异常还是 Ctrl+C，真机必须断开并关图传。
        if getattr(args, 'real_drone', False):
            shutdown_real()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='启用已配置的真实模型；飞机仍模拟')
    parser.add_argument('--config')
    parser.add_argument('--image', help='本地测试图片；允许文字或语音提出看图问题')
    parser.add_argument('--devices', action='store_true')
    parser.add_argument('--sdk-stub-root', help='显式使用已核对的三组联合模拟SDK；不连接飞机')
    parser.add_argument('--voice-confirm', action='store_true',
                        help='录音识别后二次确认（默认直接执行，减少现场拖节奏）')
    parser.add_argument('--voice-confirm-timeout', type=float, default=3.0,
                        help='配合 --voice-confirm：等待秒数，超时自动执行（0=一直等）')
    parser.add_argument('--real-drone', action='store_true',
                        help='连接真实 Tello（drone_sdk._driver.connect）；默认仍是模拟')
    parser.add_argument('--allow-real-takeoff', action='store_true',
                        help='真机模式下授权"起飞"（默认拒绝，防止误触）')
    parser.add_argument('--address', default=None, help='飞机 IP，默认 192.168.10.1')
    parser.add_argument('--min-battery', type=int, default=10,
                        help='真机起飞的最低电量（默认 10%%；实测 Tello 固件在 50%% 会自己拒绝 takeoff）')
    parser.add_argument('--always-on', action='store_true',
                        help='免按键全双工语音：持续监听，说话自动识别并执行（也可运行时 /listen 开启）')
    parser.add_argument('--show-video', action='store_true',
                        help='语音模式同时弹出无人机实时画面窗口（需配合 --always-on；q 关窗口）')
    parser.add_argument('--silence-s', type=float, default=1.5,
                        help='说多长时间的静音算"说完了"（默认 1.5 秒）')
    parser.add_argument('--max-utterance', type=float, default=10.0,
                        help='单句最长录音时长（默认 10 秒，防止一直录）')
    parser.add_argument('--offline-voice', action='store_true',
                        help='离线语音：音频链路（本地 whisper + SAPI 播报）走真声，理解用离线样例，免 API Key')
    parser.add_argument('--realtime',action='store_true',help='使用独立配置的兼容Realtime API，全双工；不回退分段语音')
    parser.add_argument('--volc-dialog', action='store_true',
                        help='豆包全双工实时语音（Seeduplex 3.0）：识别与播报在云端，'
                             '本地执行 run_program / interrupt 工具调用；需要 VOLC_API_KEY')
    parser.add_argument('--program',help='在Docker沙箱执行完整Python文件')
    parser.add_argument('--fixture-vision',action='store_true')
    parser.add_argument('--audio-config')
    parser.add_argument('--input-device',type=int)
    parser.add_argument('--output-device',type=int)
    args = parser.parse_args()
    try:
        _dispatch(args)
    finally:
        # 兜底：任何分支（含 setup 中途异常、Ctrl+C、正常 return）都要收尾。
        if getattr(args, 'real_drone', False):
            shutdown_real()


def _dispatch(args):
    # 语音链路是否可用：真实模型（--live）或离线样例（--offline-voice，免 Key）
    voice_ready = bool(args.live or getattr(args, 'offline_voice', False))
    if args.volc_dialog:
        import asyncio
        try: asyncio.run(run_volc_dialog(args))
        except KeyboardInterrupt: print('\n[退出] 已中断')
        except (ValueError,RuntimeError,OSError) as exc: print('执行失败：',exc); raise SystemExit(1)
        return
    if args.realtime or args.program:
        import asyncio
        try: asyncio.run(run_modern(args))
        except (ValueError,RuntimeError,OSError) as exc: print('执行失败：',exc); raise SystemExit(1)
        return
    try:
        config = load(args.config)
        if args.devices:
            print(audio_io.devices()); return
        client = ModelClient() if args.live else ScriptedClient()
        if args.image and not args.live:
            raise ValueError('--image需要--live及视觉模型，离线样例不会识别图片')
        visual = (lambda question: VisionService(client).ask(args.image, question)) if args.image else None
        runner = None
        if args.sdk_stub_root and args.real_drone:
            raise ValueError('--sdk-stub-root 与 --real-drone 不能同时使用')
        if args.sdk_stub_root:
            import sys
            sys.path.insert(0, str(Path(args.sdk_stub_root).resolve()/'modules'/'follow'))
            from r3_follow.team2_adapter import load_reviewed_stub
            load_reviewed_stub(args.sdk_stub_root)
            from drone_sdk.ai_bridge import UnifiedDrone
            from program_runner import Runner
            from demo_text import Demo
            runner = Runner(Demo(UnifiedDrone()), allow_simulated_sdk=True)
        elif args.real_drone:
            runner, real_drone = build_real_runner(args)
        if args.real_drone:
            from vision import Vision
            from backends import SDKBackend
            observation=Vision(client,SDKBackend(Path(__file__).resolve().parents[2]))
            visual=lambda target: json.dumps(observation.observe(target),ensure_ascii=False)
            runner.structured_vision=observation
        agent = Agent(client, runner=runner, vision=visual)
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print('启动失败：', exc); return
    session = Session(agent)
    voice = VoiceService()
    cancel_audio = threading.Event()
    output_lock = threading.Lock()
    speech_worker = __import__('concurrent.futures', fromlist=['ThreadPoolExecutor']).ThreadPoolExecutor(max_workers=1)
    # set = 暂停监听：TTS 播报、动作执行、以及键盘路径手动录音期间都不该再收声音
    listen_pause = threading.Event()
    listener = {'thread': None, 'stop': threading.Event()}
    def output(text):
        with output_lock: print('\n' + text, flush=True)
    def transcribe(wav):
        # 先走已配置的 STT API；没配或失败时回落本地 faster-whisper（无需 Key）
        try:
            return voice.transcribe(wav)
        except Exception as exc:
            output('语音识别 API 未就绪：' + str(exc))
            import local_stt
            if not local_stt.installed():
                raise
            output('改用本地 faster-whisper 识别……')
            return local_stt.LocalWhisper().transcribe(wav)
    def speak(text, audio_event):
        if audio_event.is_set():
            output('[TTS] 跳过播报：本轮已被取消')
            return
        try:
            wav = voice.synthesize(text[:2000])
            if not audio_event.is_set():
                audio_io.play(wav, config['output_device'], audio_event)
            output('[TTS] 已通过语音 API 播放（%d 字节 WAV）' % len(wav))
            return
        except Exception as exc:
            output('[TTS] 语音 API 不可用（%s: %s），改试 Windows SAPI'
                   % (type(exc).__name__, exc))
        try:
            import offline_tts
            if not offline_tts.available():
                output('[TTS] 未播报：当前系统不支持 Windows SAPI')
                return
            offline_tts.speak(text[:2000])
            output('[TTS] 已通过 Windows SAPI 播放（离线降级，无需 Key）')
        except Exception as exc:
            output('[TTS] 播放失败（%s: %s）；文字结果仍有效' % (type(exc).__name__, exc))
    def show(future, started, audio_event):
        resumed = False
        try:
            record = future.result()
            record['elapsed_ms'] = round((time.monotonic()-started)*1000)
            output(str(record.get('reply', record.get('result', record))))
            if config['log_path']:
                try:
                    with output_lock:
                        path = Path(config['log_path']); path.parent.mkdir(parents=True, exist_ok=True)
                        with path.open('a', encoding='utf-8') as f:
                            f.write(json.dumps(record, ensure_ascii=False) + '\n')
                except OSError:
                    output('日志保存失败；以上执行结果仍有效，请勿因此重复执行动作')
            speech_text = str(record.get('spoken') or record.get('reply', record.get('result', '')))
            cancelled = bool(record.get('cancelled'))
            if not voice_ready:
                output('[TTS] 跳过播报：当前是离线样例模式（需 --live 或 --offline-voice）')
            elif not config['speak_replies']:
                output('[TTS] 跳过播报：speak_replies=false（在配置里设为 true 就会念出来）')
            elif cancelled:
                output('[TTS] 跳过播报：本轮已取消')
            elif not speech_text.strip():
                output('[TTS] 跳过播报：没有可播报的文本')
            else:
                output('[TTS] 准备播报：' + speech_text[:80])
                # 播报期间继续闭嘴，播完（成功或异常）才恢复监听，避免把自己的声音当指令
                resumed = True
                speech_future = speech_worker.submit(speak, speech_text, audio_event)
                speech_future.add_done_callback(
                    lambda f: (not f.cancelled()) and f.exception() is not None
                    and output('[TTS] 播报线程异常：%r' % (f.exception(),)))
                speech_future.add_done_callback(lambda f: listen_pause.clear())
        except Exception as exc:
            output('本轮未完成（不自动重试）：' + str(exc))
        finally:
            if not resumed:
                listen_pause.clear()
    if args.real_drone:
        print('真实模型+真实飞机' if args.live else '离线样例（不是AI）+真实飞机')
        print('⚠ 真机模式：起飞%s（电量阈值 %d%%）' % (
            '已授权' if args.allow_real_takeoff else '被禁止，需 --allow-real-takeoff',
            args.min_battery))
    else:
        print('真实模型+本地模拟飞机' if args.live else '离线样例脚本（不是AI）+本地模拟飞机')
    print('语音合成播报：%s' % ('开（每轮回复会念出来）' if (voice_ready and config['speak_replies'])
                            else '关%s' % ('（需 --live 或 --offline-voice）' if not voice_ready
                                           else '（config 里设 "speak_replies": true 打开）')))
    print('语音由AI合成；本版本不能连接真实无人机。\n' + HELP)

    def start_round(text):
        """提交一轮对话（识别和键盘路径共用），show() 负责在播报结束后恢复监听。"""
        nonlocal cancel_audio
        listen_pause.set()
        cancel_audio.set()
        cancel_audio = threading.Event()
        started = time.monotonic()
        future = session.submit(text)
        future.add_done_callback(lambda f, s=started, e=cancel_audio: show(f, s, e))
        return future

    def say_blocking(text):
        """监听线程里的即时播报：说完再继续听，避免自己打断自己。"""
        if not (voice_ready and config['speak_replies']):
            output('[TTS] 跳过播报：需要 --live/--offline-voice 且 speak_replies=true')
            return
        output('[TTS] 准备播报：' + text[:80])
        speak(text, threading.Event())

    def listen_loop(stop_event):
        """免按键全双工：等唤醒词一样的自然停顿，识别后直接执行，不弹确认。"""
        from ai_agent import NO_UNDERSTAND
        output('[监听] 已开启：直接说话即可（说"结束"或输入 /listen 关闭）')
        while not stop_event.is_set():
            if listen_pause.is_set() or session.busy:
                stop_event.wait(0.05)
                continue
            try:
                wav, info = audio_io.listen_once(
                    config['input_device'], silence_s=args.silence_s,
                    max_s=args.max_utterance, pause=listen_pause)
            except RuntimeError as exc:
                output('[监听] 无法打开麦克风：%s（输入 /listen 可关闭，改用 /voice）' % exc)
                return
            except Exception as exc:
                output('[监听] 采集异常（%s: %s），1 秒后重试' % (type(exc).__name__, exc))
                stop_event.wait(1.0)
                continue
            if wav is None:
                continue
            listen_pause.set()          # 识别→执行→播报 全程闭嘴
            output('[录音] 捕获到 %.1f 秒音频（峰值 %.0f dBFS，自适应门限 %.0f dBFS）'
                   % (info['speech_s'], info['peak_dbfs'], info['gate_dbfs']))
            try:
                text = transcribe(wav)
            except Exception as exc:
                output('[STT] 识别失败：%s' % exc)
                text = ''
            if stop_event.is_set():
                return
            if not text.strip():
                output('[STT] 没有识别到内容，已忽略；继续监听')
                say_blocking(NO_UNDERSTAND)
                listen_pause.clear()
                continue
            output('[STT] 识别文本：' + text)
            if text.strip() in ('结束', '退出', '停止监听', '关闭监听'):
                output('[监听] 收到结束指令，已关闭监听')
                stop_event.set()
                listen_pause.clear()
                return
            try:
                start_round(text)
            except (ValueError, RuntimeError, OSError) as exc:
                output('[监听] 未执行：%s' % exc)
                listen_pause.clear()

    def start_listener():
        if listener['thread'] is not None and listener['thread'].is_alive():
            return False
        if not voice_ready:
            output('[监听] 需要 --live（真实模型）或 --offline-voice（离线样例，免 Key）')
            return False
        listener['stop'] = threading.Event()
        thread = threading.Thread(target=listen_loop, args=(listener['stop'],),
                                  name='voice-listener', daemon=True)
        listener['thread'] = thread
        thread.start()
        return True

    def stop_listener():
        listener['stop'].set()
        listen_pause.set()              # 让正在采集的那一轮立刻返回
        thread = listener['thread']
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        if not session.busy:
            listen_pause.clear()
        output('[监听] 已停止')

    def listener_running():
        thread = listener['thread']
        return bool(thread is not None and thread.is_alive())

    def confirm_execute(recognized, timeout):
        """--voice-confirm 时才二次确认；默认直接执行。超时可自动放行。"""
        if not args.voice_confirm:
            return True
        suffix = ('（%.0f 秒后自动执行）' % timeout) if timeout and timeout > 0 else ''
        prompt = '确认执行「%s」？按 y 确认，其它键取消%s：' % (recognized[:30], suffix)
        if timeout and timeout > 0 and os.name == 'nt':
            import msvcrt
            print(prompt, end='', flush=True)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if msvcrt.kbhit():
                    char = msvcrt.getwch()
                    print(char, flush=True)
                    return char.lower() == 'y'
                time.sleep(.05)
            print(' [超时，自动执行]', flush=True)
            return True
        return input(prompt).strip().lower() == 'y'

    empty_streak, last_empty = 0, 0.0
    try:
        interactive = bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        interactive = False
    # 免按键语音模式在非终端 stdin（后台/管道/nohup）下不能靠 input() 存活：
    # 一旦 EOF 主循环就退出，飞机会留在空中而控制端已经没了。改为轮询保持存活。
    headless = (not interactive) and (args.always_on or bool(getattr(args, 'offline_voice', False)))
    # --show-video 必须跑在 headless 轮询里：input() 会阻塞主线程，窗口就刷不出来。
    show_video = bool(getattr(args, 'show_video', False))
    video_window = 'Tello 实时画面 (q 关窗口 / Ctrl+C 结束语音)'
    if show_video:
        if not (args.always_on or bool(getattr(args, 'offline_voice', False))):
            print('[画面] --show-video 需要配合 --always-on（语音模式）使用，已忽略')
            show_video = False
        else:
            headless = True
            try:
                import cv2
                from drone_sdk import controller as _view_ctl
                print('[画面] 实时预览已开启；画面取的是 controller 里 3 组同一份真帧')
            except Exception as exc:
                print('[画面] 预览不可用：%s' % exc)
                show_video = False

    follow_hud = {'obs': None, 'mot': None, 'halt': None, 'at': 0.0, 'printed': 0.0}

    def follow_snapshot():
        """从 3 组跟随服务的 session log 事件里取最新一帧状态。"""
        try:
            records = _view_ctl._follow.events()
        except Exception:
            return None, None, None
        obs = mot = halt = None
        for record in reversed(records):
            name = record.get('event')
            if name == 'observation' and obs is None:
                obs = record
            elif name == 'motion_request' and mot is None:
                mot = record
            elif name == 'tracking_halted' and halt is None:
                halt = record
            if obs is not None and mot is not None:
                break
        return obs, mot, halt

    def draw_follow_hud(frame, obs, mot, halt):
        """在画面上标出目标位置与当前速度，跟随好不好一眼就能看出来。"""
        out = frame
        height, width = out.shape[:2]
        cv2.line(out, (width // 2, 0), (width // 2, height), (0, 255, 0), 1)
        if obs and obs.get('found'):
            cx = int(obs.get('center_x') or 0)
            cy = int(obs.get('center_y') or 0)
            cv2.drawMarker(out, (cx, cy), (0, 0, 255), cv2.MARKER_CROSS, 40, 2)
        lines = []
        if obs is None:
            lines.append('follow: idle (no observation yet)')
        else:
            lines.append('found=%s  cx=%.0f  sr=%.3f' % (
                obs.get('found'), obs.get('center_x') or 0, obs.get('size_ratio') or 0))
            if not obs.get('found') and obs.get('reason'):
                lines.append('lost: %s' % obs.get('reason'))
        if mot is not None:
            ch = mot.get('channels') or [0, 0, 0, 0]
            lines.append('v  L%+d F%+d U%+d Y%+d  %s' % (
                ch[0], ch[1], ch[2], ch[3], mot.get('state')))
            lines.append('reason: %s' % mot.get('reason'))
        if halt is not None:
            lines.append('HALTED: %s' % halt.get('reason'))
        for index, text in enumerate(lines):
            y = 24 + index * 24
            cv2.putText(out, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(out, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1, cv2.LINE_AA)
        return out

    def log_follow_line(obs, mot):
        """终端每 0.5 秒一行，落地后可以直接回看跟随表现。"""
        if obs is None:
            return
        if mot is not None:
            ch = mot.get('channels') or [0, 0, 0, 0]
            velocity = 'L%+d F%+d U%+d Y%+d' % (ch[0], ch[1], ch[2], ch[3])
            state = mot.get('state')
            reason = mot.get('reason')
        else:
            velocity, state, reason = 'L+0 F+0 U+0 Y+0', '-', '-'
        print('[跟随] %s cx=%6.1f sr=%.3f | %s | %s | %s' % (
            '可见' if obs.get('found') else '丢失',
            obs.get('center_x') or 0, obs.get('size_ratio') or 0,
            velocity, state, reason), flush=True)
    if headless:
        print('[监听] stdin 非终端 → 无键盘模式：只用语音控制，Ctrl+C 结束')
        print(EMERGENCY_HELP)
        focus_console()
    if args.always_on and not start_listener():
        output('[监听] 未能开启，继续用键盘或 /voice 模式')
    print('免按键语音：%s' % ('开（说话即识别，静音 %.1f 秒判停；输入 /listen 关闭）' % args.silence_s
                          if listener_running() else '关（输入 /listen 开启）'))
    try:
        while True:
            if headless:
                # 全局热键：终端不在前台也能急停
                if hotkey_edge(_VK_F12):
                    output('[急停] F12 → 零速度悬停')
                    try:
                        session.interrupt(False)
                    except Exception as exc:
                        output('[急停] 下发失败：%s' % exc)
                if hotkey_edge(_VK_F11):
                    output('[急停] F11 → 立即降落')
                    try:
                        session.interrupt(True)
                    except Exception as exc:
                        output('[急停] 下发失败：%s' % exc)
                if show_video:
                    try:
                        now = time.monotonic()
                        if now - follow_hud['at'] >= 0.2:
                            (follow_hud['obs'], follow_hud['mot'],
                             follow_hud['halt']) = follow_snapshot()
                            follow_hud['at'] = now
                        packet = _view_ctl.get_frame()
                        frame = packet.frame
                        if frame is not None:
                            frame = draw_follow_hud(frame, follow_hud['obs'],
                                                    follow_hud['mot'], follow_hud['halt'])
                            cv2.imshow(video_window, frame)
                        if now - follow_hud['printed'] >= 0.5:
                            follow_hud['printed'] = now
                            log_follow_line(follow_hud['obs'], follow_hud['mot'])
                        key = cv2.waitKey(1) & 0xFF
                        if key in (ord('q'), 27):
                            show_video = False
                            cv2.destroyWindow(video_window)
                            print('[画面] 预览已关闭（语音继续）')
                    except Exception as exc:
                        print('[画面] 预览异常，已关闭：%s' % exc)
                        show_video = False
                        try:
                            cv2.destroyAllWindows()
                        except Exception:
                            pass
                else:
                    time.sleep(0.2)          # 语音线程负责识别与执行，主线程只保活并等待退出
                continue
            try: text = input('你：').strip()
            except (EOFError, KeyboardInterrupt): break
            except OSError as exc:          # 控制台句柄异常：绝不能 continue，否则会死循环刷屏
                output('读取输入失败，已退出会话：' + str(exc)); break
            # 键盘路径也先停一下监听：手动 /voice 录音和常驻监听不能同时占麦克风
            listen_pause.set()
            submitted = False
            try:
                if not text:
                    # 交互式终端里空回车是正常操作，直接重画提示符；
                    # 只有输入流非终端（管道/重定向）快速返回空串时才判定为跑飞
                    if interactive:
                        continue
                    now = time.monotonic()
                    empty_streak = empty_streak + 1 if now - last_empty < .2 else 0
                    last_empty = now
                    if empty_streak >= 3:
                        output('检测到输入流异常（连续空输入），已安全退出，避免死循环')
                        break
                    continue
                empty_streak = 0
                if text in ('/quit', '退出'): break
                if text == '/listen':
                    if listener_running(): stop_listener()
                    else: start_listener()
                    continue
                if text == '/mute': cancel_audio.set(); continue
                if text in ('/stop', '/land'):
                    cancel_audio.set()
                    try:
                        started = time.monotonic()
                        f = session.interrupt(text == '/land')
                        submitted = True
                        f.add_done_callback(lambda f, e=cancel_audio, s=started: show(f, s, e))
                    except RuntimeError as exc:
                        output(str(exc))
                    continue
                if text == '/status':
                    output(json.dumps(agent.runner.demo.drone.get_status(), ensure_ascii=False)); continue
                if text == '/reset':
                    if session.busy: output('请等待本轮结束，或使用/stop取消')
                    else: agent.history.clear(); output('对话已清空')
                    continue
                try:
                    if text == '/voice':
                        if not voice_ready: raise ValueError('语音识别需要 --live 或 --offline-voice（本地 faster-whisper）')
                        if session.busy: raise ValueError('请先等待当前任务结束')
                        cancel_audio.set()
                        # Sounddevice allows one recorder/player at a time. Wait for prior speech to end.
                        speech_worker.submit(lambda: None).result()
                        input('按回车开始录音，持续%s秒：' % config['record_seconds'])
                        wav = audio_io.record(config['record_seconds'], config['input_device'])
                        text = transcribe(wav)
                        print('识别结果：' + text)
                        if not text.strip():
                            print('没有识别到有效内容，未执行任何动作；请再说一遍')
                            continue
                        if not confirm_execute(text, args.voice_confirm_timeout):
                            print('已取消，未调用模型和动作'); continue
                    if text.startswith('/image '):
                        if not args.live: raise ValueError('图片问答需要--live及支持视觉的模型')
                        if session.busy: raise ValueError('请等待当前任务结束')
                        path, _, question = text[7:].partition('|')
                        reply = VisionService(client).ask(path.strip(), question.strip() or '画面中有什么？')
                        output('[本地测试图片] ' + reply)
                        agent.history.extend([{'role':'user','content':'[测试图片问题] ' + question},
                                              {'role':'assistant','content':'[测试图片回答] ' + reply[:2000]}])
                        agent.history = agent.history[-12:]
                        continue
                    start_round(text)
                    submitted = True
                except (ValueError, RuntimeError, OSError) as exc:
                    # EOFError 不在这里吞：输入流结束必须让主循环退出，否则会刷屏死循环
                    output('未继续处理：' + str(exc))
            finally:
                # 没真正开始一轮（空输入/无效命令/取消）时立刻恢复监听，别把耳朵关死
                if not submitted:
                    listen_pause.clear()
    except KeyboardInterrupt:
        print('\n结束会话')
    except EOFError:
        print('\n输入流已结束（EOF），退出会话')
    finally:
        # 会话级清理（真机断开由外层 main() 的 finally 统一兜底，避免重复执行）
        if getattr(args, 'show_video', False):
            try:
                import cv2
                cv2.destroyAllWindows()
            except Exception:
                pass
        listener['stop'].set()
        listen_pause.set()
        cancel_audio.set()
        session.close()
        audio_io.close()
        speech_worker.shutdown(wait=True, cancel_futures=True)

if __name__ == '__main__': main()
