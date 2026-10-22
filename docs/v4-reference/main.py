import argparse
import asyncio
import importlib.util
import json
import os
import shutil
import time
from pathlib import Path
from backends import MockBackend,SDKBackend
from gateway import Gateway
from sandbox_runner import SandboxRunner

async def main():
    p=argparse.ArgumentParser(description='4组重构版：独立沙箱 + 实时语音')
    modes=p.add_mutually_exclusive_group(required=True)
    modes.add_argument('--voice',action='store_true',help='耳机实时对话，不需要按键确认')
    modes.add_argument('--program',help='在容器内运行Python文件')
    modes.add_argument('--check',action='store_true',help='本地依赖诊断，不联网')
    p.add_argument('--fixture-vision',action='store_true',help='显式使用离线语义夹具，绝非真实视觉')
    p.add_argument('--image',help='本地测试图片，使用单独视觉API')
    p.add_argument('--sdk-stub-root',help='显式接2组当前模拟SDK；未验证实机')
    p.add_argument('--input-device',type=int)
    p.add_argument('--output-device',type=int)
    p.add_argument('--log',help='可选JSONL日志路径，包含程序和结果')
    p.add_argument('--timeout',type=float,default=90)
    args=p.parse_args()
    if args.check:
        print('Python需3.11+；容器使用3.11')
        print('Docker:',bool(shutil.which('docker')))
        for mod in ('websockets','sounddevice','cv2'):
            print(mod+':',bool(importlib.util.find_spec(mod)))
        print('Realtime账号已配置:',bool(os.getenv('OPENAI_API_KEY') or os.getenv('AI_API_KEY')))
        print('Realtime模型已配置:',bool(os.getenv('REALTIME_MODEL')))
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
        from duplex_audio import DuplexAudio
        from realtime import RealtimeSession
        # Program logs are diagnostic only; interaction is entirely via headset audio.
        print('实时语音启动后直接对耳机说话；声音由AI生成。当前设备为模拟/未验证SDK。')
        print('可口头要求停止或降落；Ctrl+C仅用于结束开发程序。')
        await RealtimeSession(runner,DuplexAudio(args.input_device,args.output_device),log).run()

if __name__=='__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: print('会话结束')
    except (ValueError,RuntimeError,ImportError,OSError) as exc: print('未启动或会话已停止：'+str(exc)); raise SystemExit(1)
