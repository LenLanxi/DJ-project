"""Explicitly paid, bounded connection probe. No microphone, speaker or drone.

Default invocation never connects. --real opts into a billable session;
--hello additionally asks for one fixed TTS greeting, not an ASR/tool test.
Keys are read from VOLC_API_KEY and never included in output or evidence.
"""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import time
import uuid

from VoiceProtocol import (DEFAULT_URL, DEFAULT_MODEL, DEFAULT_VOICE, EV_SESSION_CREATED,
                         EV_SESSION_CLOSE, EV_AUDIO_DELTA, EV_AUDIO_DONE, EV_RESPONSE_DONE,
                         EV_AUDIO_MUTE, EV_SPEECH_TEXT_COMMIT, EV_ERROR,
                         VolcDialogError, build_session, strict_json, validate_url)

HELLO = '你好，这是连接测试。'


async def probe(want_hello=False, *, max_seconds=20, url=None, key=None, model=None):
    """Called only by an explicit real-test command or an authorized test driver."""
    if not 1 <= max_seconds <= 30:
        raise ValueError('max_seconds 必须在1..30秒之间')
    url = url or os.getenv('VOLC_DIALOG_URL') or DEFAULT_URL
    key = os.getenv('VOLC_API_KEY', '') if key is None else key
    model = model or os.getenv('VOLC_MODEL') or DEFAULT_MODEL
    validate_url(url)
    if not key:
        raise VolcDialogError('缺少 VOLC_API_KEY')
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    report = {'protocol': 'volc_full_duplex_json', 'endpoint': url, 'model': model,
              'handshake': False, 'session_created': False, 'tts_requested': bool(want_hello),
              'tts_audio_bytes': 0, 'tts_done': False, 'usage': [], 'event_types': [],
              'verified': {'microphone': False, 'speaker': False, 'asr': False,
                           'tools': False, 'drone': False}, 'ok': False}
    started = time.perf_counter()
    try:
        async with asyncio.timeout(max_seconds):
            async with connect(url, additional_headers={
                    'X-Api-Key': key, 'X-Api-Connect-Id': str(uuid.uuid4())},
                    open_timeout=min(10, max_seconds), close_timeout=2,
                    ping_interval=20, max_size=1_000_000) as ws:
                report['handshake'] = True
                response = getattr(ws, 'response', None)
                report['logid'] = response.headers.get('X-Tt-Logid') if response else None
                await ws.send(json.dumps(build_session(model=model, instructions='仅进行连接测试。',
                                                        voice=os.getenv('VOLC_VOICE') or DEFAULT_VOICE),
                                         ensure_ascii=False))
                try:
                    async for message in ws:
                        if isinstance(message, bytes):
                            raise VolcDialogError('收到非预期二进制协议事件')
                        event = strict_json(message)
                        kind = event.get('type')
                        report['event_types'].append(kind)
                        if kind == EV_SESSION_CREATED:
                            report['session_created'] = True
                            report['session_id'] = (event.get('session') or {}).get('id')
                            await ws.send(json.dumps({'type': EV_AUDIO_MUTE, 'event_id': str(uuid.uuid4())}))
                            if not want_hello:
                                report['ok'] = True
                                break
                            await ws.send(json.dumps({'type': EV_SPEECH_TEXT_COMMIT,
                                                      'event_id': str(uuid.uuid4()), 'text': HELLO},
                                                     ensure_ascii=False))
                        elif kind == EV_AUDIO_DELTA:
                            data = base64.b64decode(event.get('delta', ''), validate=True)
                            if len(data) % 2:
                                raise VolcDialogError('服务端音频长度不是PCM16')
                            report['tts_audio_bytes'] += len(data)
                        elif kind == EV_AUDIO_DONE:
                            report['tts_done'] = True
                        elif kind == EV_RESPONSE_DONE:
                            report['usage'].append(event.get('usage') or
                                                   (event.get('response') or {}).get('usage') or {})
                            if want_hello:
                                report['ok'] = report['tts_audio_bytes'] > 0 and report['tts_done']
                                break
                        elif kind == EV_ERROR:
                            error = event.get('error') or {}
                            report['error_code'] = error.get('code') if isinstance(error, dict) else 'unknown'
                            break
                finally:
                    try:
                        await asyncio.wait_for(ws.send(json.dumps({'type': EV_SESSION_CLOSE,
                                                                  'event_id': str(uuid.uuid4())})), 1)
                    except Exception:
                        pass
    except InvalidStatus as exc:
        report['http_status'] = getattr(getattr(exc, 'response', None), 'status_code', None)
        report['error'] = 'handshake_rejected'
    except TimeoutError:
        report['error'] = 'probe_deadline'
    except Exception as exc:
        # Do not copy exception bodies that might contain authorization headers.
        report['error'] = type(exc).__name__
    report['elapsed_seconds'] = round(time.perf_counter() - started, 3)
    report['tts_seconds_received'] = report['tts_audio_bytes'] / 48000
    return report


def main(argv=None):
    import sys
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real', action='store_true', help='明确允许创建收费会话')
    parser.add_argument('--hello', action='store_true', help='额外合成一句固定测试语')
    parser.add_argument('--max-seconds', type=int, default=20)
    parser.add_argument('--output', type=Path, help='可选：将不含密钥的结果保存为UTF-8 JSON')
    args = parser.parse_args(argv)
    if not args.real:
        print('未连接服务。真实收费测试必须显式传入 --real；不会打开音频设备或连接飞机。')
        return 2
    try:
        result = asyncio.run(probe(args.hello, max_seconds=args.max_seconds))
    except (ValueError, VolcDialogError) as exc:
        print(str(exc))
        return 2
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.write_text(payload + '\n', encoding='utf-8')
    print(payload)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
