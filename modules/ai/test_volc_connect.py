"""火山豆包实时对话「凭据自检」：只验证握手与建会话，可选验证 TTS 回传。

不占用声卡、不连飞机，用来快速区分「密钥不对」和「代码不对」。

用法：
  $env:VOLC_APP_ID="数字 APP ID"
  $env:VOLC_ACCESS_KEY="真正的 Access Token（不是控制台里的密钥名称）"
  python test_volc_connect.py            # 只验证鉴权 + 建会话
  python test_volc_connect.py --hello    # 再让服务端合成一句话，统计回传音频
"""
import asyncio
import os
import sys

from duplex_audio import PlaybackBuffer
from volc_dialog import VolcDialog, VolcDialogError


class SilentCapture:
    """替身声卡：只发静音 PCM，不出声，避免测试时打扰别人。"""

    def __init__(self):
        self.speaker = PlaybackBuffer(rate=24000)
        self.started = False

    def start(self):
        self.started = True

    def close(self):
        pass

    def level(self):
        return 0.0

    async def chunks(self):
        while True:
            yield b'\x00\x00' * 480          # 20ms 静音 @24kHz
            await asyncio.sleep(0.02)


async def probe(want_hello):
    audio = SilentCapture()
    seen = []

    def on_log(event):
        seen.append(event)
        name = event.get('event')
        if name in ('websocket_open', 'session_started', 'usage'):
            print('[豆包] ' + str(event))
        elif name == 'tts_sentence':
            print('[豆包] 合成句子：' + str(event.get('text')))

    dialog = VolcDialog(audio, on_log=on_log)
    dialog.check_credentials()
    print('[配置] 端点 %s' % dialog.url)
    print('[配置] 模型 %s / 音色 %s' % (dialog.model, dialog.speaker))
    print('[配置] APP ID %s / Access Key %s...' % (dialog.app_id, dialog.access_key[:6]))

    task = asyncio.create_task(dialog.run())
    ready_task = asyncio.create_task(dialog.ready.wait())
    try:
        done, _ = await asyncio.wait({task, ready_task}, timeout=25,
                                     return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            task.result()                    # 抛出手握手的真实失败原因
        if not dialog.ready.is_set():
            raise VolcDialogError('20 秒内没有收到 SessionStarted')
        print('[OK] 鉴权通过，会话已建立')
        if want_hello:
            await dialog.say_text('你好，我是无人机语音助手')
            print('[TTS] 已请求合成，等待回传音频…')
            await asyncio.sleep(5)
            total = sum(audio.speaker.generated.values())
            if total > 0:
                print('[OK] 收到 TTS 音频 %d 字节（%.1f 秒 @24kHz/16bit）'
                      % (total, total / 2 / 24000))
            else:
                print('[FAIL] 5 秒内没有收到任何 TTS 音频')
                return 2
        return 0
    finally:
        task.cancel()
        ready_task.cancel()
        await asyncio.gather(task, ready_task, return_exceptions=True)


def main():
    if not os.getenv('VOLC_APP_ID') or not os.getenv('VOLC_ACCESS_KEY'):
        print('缺少环境变量。先设置：\n'
              '  $env:VOLC_APP_ID="1234567890"        # 控制台里的数字 APP ID\n'
              '  $env:VOLC_ACCESS_KEY="xxxxx"          # Access Token，不是密钥名称')
        return 1
    try:
        return asyncio.run(probe('--hello' in sys.argv[1:]))
    except VolcDialogError as exc:
        print('[FAIL] ' + str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
