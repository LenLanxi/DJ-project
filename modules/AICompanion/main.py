"""Unified terminal entry. The aircraft is ALWAYS a local mock."""
import argparse
import json
import time
import threading
from pathlib import Path
from .ai_agent import Agent
from .demo_ai import ScriptedClient
from .model_client import ModelClient
from .voice_service import VoiceService
from .vision_service import VisionService
from .session import Session
from .settings import load
from . import audio_io

HELP = '''输入文字与模拟飞机互动。命令：
 /voice 录音（回车开始，固定时长，转写后需确认）
 /image 图片路径 | 问题   本地测试图片问答
 /stop 停止模拟运动；/land 模拟降落（均绕过模型）
 /mute 停止当前播报；/reset 清空对话；/status 查看模拟状态；/quit 退出
离线样例：还有多少电、起飞、跟着我、向前走30厘米、降落'''

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='启用已配置的真实模型；飞机仍模拟')
    parser.add_argument('--config')
    parser.add_argument('--image', help='本地测试图片；允许文字或语音提出看图问题')
    parser.add_argument('--devices', action='store_true')
    args = parser.parse_args()
    try:
        config = load(args.config)
        if args.devices:
            print(audio_io.devices()); return
        client = ModelClient() if args.live else ScriptedClient()
        if args.image and not args.live:
            raise ValueError('--image需要--live及视觉模型，离线样例不会识别图片')
        visual = (lambda question: VisionService(client).ask(args.image, question)) if args.image else None
        agent = Agent(client, vision=visual)
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print('启动失败：', exc); return
    session = Session(agent)
    voice = VoiceService()
    cancel_audio = threading.Event()
    output_lock = threading.Lock()
    speech_worker = __import__('concurrent.futures', fromlist=['ThreadPoolExecutor']).ThreadPoolExecutor(max_workers=1)
    def output(text):
        with output_lock: print('\n' + text, flush=True)
    def speak(text, audio_event):
        if audio_event.is_set(): return
        try:
            wav = voice.synthesize(text[:2000])
            if not audio_event.is_set():
                audio_io.play(wav, config['output_device'], audio_event)
        except (ValueError, RuntimeError, OSError) as exc:
            output('语音播放未完成：' + str(exc) + '；文字结果仍有效')
    def show(future, started, audio_event):
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
            if args.live and config['speak_replies'] and not record.get('cancelled'):
                speech_worker.submit(speak, str(record.get('reply', record.get('result', ''))), audio_event)
        except Exception as exc:
            output('本轮未完成（不自动重试）：' + str(exc))
    print('真实模型+本地模拟飞机' if args.live else '离线样例脚本（不是AI）+本地模拟飞机')
    print('语音由AI合成；本版本不能连接真实无人机。\n' + HELP)
    try:
        while True:
            try: text = input('你：').strip()
            except EOFError: break
            if not text: continue
            if text in ('/quit', '退出'): break
            if text == '/mute': cancel_audio.set(); continue
            if text in ('/stop', '/land'):
                cancel_audio.set()
                try:
                    started = time.monotonic()
                    f = session.interrupt(text == '/land')
                    f.add_done_callback(lambda f, e=cancel_audio, s=started: show(f, s, e))
                except RuntimeError as exc:
                    output(str(exc))
                continue
            if text == '/status':
                output(json.dumps(agent.runner.get_status(), ensure_ascii=False)); continue
            if text == '/reset':
                if session.busy: output('请等待本轮结束，或使用/stop取消')
                else: agent.history.clear(); output('对话已清空')
                continue
            try:
                if text == '/voice':
                    if not args.live: raise ValueError('语音识别需要--live及语音API配置')
                    if session.busy: raise ValueError('请先等待当前任务结束')
                    cancel_audio.set()
                    # Sounddevice allows one recorder/player at a time. Wait for prior speech to end.
                    speech_worker.submit(lambda: None).result()
                    input('按回车开始录音，持续%s秒：' % config['record_seconds'])
                    wav = audio_io.record(config['record_seconds'], config['input_device'])
                    text = voice.transcribe(wav)
                    print('识别结果：' + text)
                    if input('确认发送？输入 y：').lower() != 'y':
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
                cancel_audio.set()
                cancel_audio = threading.Event()
                started = time.monotonic()
                f = session.submit(text)
                f.add_done_callback(lambda f, s=started, e=cancel_audio: show(f, s, e))
            except (ValueError, RuntimeError, OSError, EOFError) as exc:
                output('未继续处理：' + str(exc))
    except KeyboardInterrupt:
        print('\n结束会话')
    finally:
        cancel_audio.set()
        session.close()
        speech_worker.shutdown(wait=True, cancel_futures=True)

if __name__ == '__main__': main()
