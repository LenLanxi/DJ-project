"""豆包全双工实时语音「凭据自检」：不占用声卡、不连飞机。

用来快速区分「密钥不对」和「代码不对」。

用法：
  $env:VOLC_API_KEY="控制台 API Key 的密钥值（不是名称）"
  python test_volc_connect.py            # 只验证握手 + 建会话
  python test_volc_connect.py --hello    # 再让服务端合成一句话，统计回传音频
"""
import asyncio
import base64
import json
import os
import sys

from websockets.asyncio.client import connect

from volc_dialog import (EV_AUDIO_DELTA, EV_AUDIO_DONE, EV_ERROR, EV_RESPONSE_DONE,
                         EV_SESSION_CREATED, build_session,
                         DEFAULT_URL, DEFAULT_MODEL, DEFAULT_VOICE)

URL = os.getenv('VOLC_DIALOG_URL') or DEFAULT_URL
KEY = os.getenv('VOLC_API_KEY', '')
HELLO = '你好，我是无人机语音助手。'


async def probe(want_hello):
    audio_bytes = bytearray()
    payload = build_session(model=os.getenv('VOLC_MODEL') or DEFAULT_MODEL,
                            voice=os.getenv('VOLC_VOICE') or DEFAULT_VOICE,
                            instructions='你是无人机语音助手。')
    async with connect(URL, additional_headers={'X-Api-Key': KEY},
                       open_timeout=20, ping_interval=20, max_size=16_000_000) as ws:
        log_id = ws.response.headers.get('X-Tt-Logid') if ws.response else None
        print('[OK] WebSocket 握手成功  logid=%s' % log_id)
        await ws.send(json.dumps(payload, ensure_ascii=False))

        deadline = asyncio.get_event_loop().time() + 20
        created = False
        while asyncio.get_event_loop().time() < deadline and not created:
            try:
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            kind = event.get('type')
            if kind == EV_SESSION_CREATED:
                created = True
                print('[OK] 会话已建立  session.id=%s'
                      % (event.get('session') or {}).get('id'))
            elif kind == EV_ERROR:
                print('[FAIL] 服务端错误:', json.dumps(event, ensure_ascii=False)[:300])
                return 2
        if not created:
            print('[FAIL] 20 秒内没收到 session.created')
            return 2

        if want_hello:
            print('[TTS] 请求合成：%s' % HELLO)
            await ws.send(json.dumps({'type': 'speech_text_buffer.commit',
                                      'text': HELLO}, ensure_ascii=False))
            deadline = asyncio.get_event_loop().time() + 20
            done = False
            while asyncio.get_event_loop().time() < deadline and not done:
                try:
                    event = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                except asyncio.TimeoutError:
                    continue
                kind = event.get('type')
                if kind == EV_AUDIO_DELTA:
                    audio_bytes.extend(base64.b64decode(event.get('delta') or ''))
                elif kind == EV_AUDIO_DONE:
                    print('[OK] TTS 结束 status_code=%s' % event.get('status_code'))
                elif kind == EV_RESPONSE_DONE:
                    done = True
                elif kind == EV_ERROR:
                    print('[FAIL] 服务端错误:', json.dumps(event, ensure_ascii=False)[:300])
                    return 2
            if audio_bytes:
                print('[OK] 收到音频 %d 字节（%.1f 秒 @24kHz/16bit）'
                      % (len(audio_bytes), len(audio_bytes) / 2 / 24000))
            else:
                print('[FAIL] 没有收到任何 TTS 音频')
                return 2

        try:
            await ws.send(json.dumps({'type': 'session.close'}))
        except Exception:
            pass
    print('[OK] 全部通过')
    return 0


def main():
    if not KEY:
        print('缺少 VOLC_API_KEY。先设置：\n'
              '  $env:VOLC_API_KEY="控制台 API Key 的密钥值"\n'
              '注意：形如 api-key-20260101000000 的是「名称」，不是密钥值。')
        return 1
    print('[配置] 端点 %s' % URL)
    print('[配置] 模型 %s / 音色 %s' % (os.getenv('VOLC_MODEL') or DEFAULT_MODEL,
                                       os.getenv('VOLC_VOICE') or DEFAULT_VOICE))
    print('[配置] Key 前缀 %s...' % KEY[:8])
    try:
        return asyncio.run(probe('--hello' in sys.argv[1:]))
    except Exception as exc:
        print('[FAIL] %s: %s' % (type(exc).__name__, exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
