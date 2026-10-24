import argparse
import asyncio
import importlib.util
import json
import os
import shutil
import sys
import time
from pathlib import Path

# 音频配置模块统一放在 modules/ai；从 docs/v4-reference 启动时补上路径。
_AUDIO_MODULES = Path(__file__).resolve().parents[2] / 'modules' / 'ai'
if str(_AUDIO_MODULES) not in sys.path:
    sys.path.insert(0, str(_AUDIO_MODULES))

from backends import MockBackend,SDKBackend
from gateway import Gateway
from sandbox_runner import SandboxRunner

DEPRECATED_NOTICE = (
    '============ 已废弃的运行入口 ============\n'
    '本目录（docs/v4-reference）是 4 组早期参考实现，仅用于比对设计，不参与联调。\n'
    '正式代码请使用：modules/ai/main.py\n'
    '  语音全链路（免按键）：python main.py --live --always-on --config ..\\..\\config.demo.json\n'
    '  真机：                python main.py --live --always-on --real-drone --allow-real-takeoff\n'
    '确需复现历史行为：设置环境变量 V4_REFERENCE_RUN=1 后重试。\n'
    '=========================================')

def deprecated_guard():
    """主入口只能有一个：这里直接拒绝启动，避免有人误把它当成正式程序跑。"""
    if os.getenv('V4_REFERENCE_RUN') == '1':
        print('警告：正在运行已废弃的参考实现（V4_REFERENCE_RUN=1），请勿用于演示或联调。')
        return
    print(DEPRECATED_NOTICE)
    raise SystemExit(2)

async def main():
    deprecated_guard()
    p=argparse.ArgumentParser(description='4组重构版：独立沙箱 + 实时语音（已废弃，仅参考）')
    modes=p.add_mutually_exclusive_group(required=True)
    modes.add_argument('--voice',action='store_true',help='耳机实时对话，不需要按键确认')
    modes.add_argument('--program',help='在容器内运行Python文件')
    modes.add_argument('--check',action='store_true',help='本地依赖诊断，不联网')
    modes.add_argument('--list-audio-devices',action='store_true',help='列出音频设备索引，不需要Docker/API')
    p.add_argument('--fixture-vision',action='store_true',help='显式使用离线语义夹具，绝非真实视觉')
    p.add_argument('--image',help='本地测试图片，使用单独视觉API')
    p.add_argument('--sdk-stub-root',help='显式接2组当前模拟SDK；未验证实机')
    p.add_argument('--input-device',type=int)
    p.add_argument('--output-device',type=int)
    p.add_argument('--audio-config',help='音频JSON配置；未指定时自动读取当前目录的audio_config.json')
    p.add_argument('--log',help='可选JSONL日志路径，包含程序和结果')
    p.add_argument('--timeout',type=float,default=90)
    args=p.parse_args()
    if args.list_audio_devices:
        import sounddevice as sd
        from audio_devices import list_audio_devices
        list_audio_devices(sd)
        return
    if args.check:
        print('Python需3.11+；容器使用3.11')
        print('Docker:',bool(shutil.which('docker')))
        for mod in ('websockets','sounddevice','cv2'):
            print(mod+':',bool(importlib.util.find_spec(mod)))
        print('Realtime账号已配置:',bool(os.getenv('AI_API_KEY')))
        print('Realtime模型已配置:',bool(os.getenv('AI_REALTIME_MODEL') or os.getenv('AI_MODEL')))
        print('Realtime地址已配置:',bool(os.getenv('AI_REALTIME_URL')))
        return
    if not 1<=args.timeout<=600: raise ValueError('timeout须为1至600秒')
    backend=SDKBackend(args.sdk_stub_root,simulated=True) if args.sdk_stub_root else MockBackend()
    if args.fixture_vision and args.sdk_stub_root:
        raise ValueError('语义夹具仅配合内置Mock使用，不能装成SDK画面')
    vision=None
    if args.image or args.sdk_stub_root:
        from vision import Vision
        from model_client import ModelClient
        vision=Vision(ModelClient() if args.image or os.getenv('AI_CHAT_URL') else None,backend,args.image)
    gateway=Gateway(backend,vision,args.fixture_vision)
    runner=SandboxRunner(gateway,timeout=args.timeout)
    def log(record):
        if args.log:
            path=Path(args.log); path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('a',encoding='utf-8') as f:
                f.write(json.dumps({'timestamp':time.time(),**record},ensure_ascii=False)+'\n')
    if args.program:
        code=Path(args.program).read_text(encoding='utf-8')
        result=await runner.run(code)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        log({'event':'program_result','code':code,'result':result})
        if not result.get('ok'): raise SystemExit(1)
    else:
        from audio_config import load_audio_config
        from duplex_audio import DuplexAudio
        from realtime import RealtimeSession
        config=load_audio_config(args.audio_config,args.input_device,args.output_device)
        # Program logs are diagnostic only; interaction is entirely via headset audio.
        print('实时语音启动后直接对耳机说话；声音由AI生成。当前设备为模拟/未验证SDK。')
        print('可口头要求停止或降落；Ctrl+C仅用于结束开发程序。')
        print(f'云端输入降噪={config.noise_reduction}；VAD阈值={config.vad_threshold}；'
              f'静音判定={config.silence_duration_ms}ms。未启用应用级AEC，需耳机现场验证。')
        await RealtimeSession(runner,DuplexAudio(config=config),log,audio_config=config).run()

if __name__=='__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: print('会话结束')
    except (ValueError,RuntimeError,ImportError,OSError) as exc: print('未启动或会话已停止：'+str(exc)); raise SystemExit(1)
