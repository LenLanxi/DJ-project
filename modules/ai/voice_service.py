"""OpenAI-format transcription and speech adapters; endpoints are configurable."""
import json
import os
import uuid
from audio_io import decode_wav
from http_transport import post, post_json

def multipart(fields, data):
    boundary = 'companion-' + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="' + name +
                      '"\r\n\r\n' + value + '\r\n').encode())
    parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="input.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode() + data + b'\r\n')
    parts.append(('--' + boundary + '--\r\n').encode())
    return b''.join(parts), 'multipart/form-data; boundary=' + boundary

class VoiceService:
    def __init__(self):
        self.stt_url = os.getenv('AI_STT_URL', '')
        self.tts_url = os.getenv('AI_TTS_URL', '')
        self.stt_model = os.getenv('AI_STT_MODEL', '')
        self.tts_model = os.getenv('AI_TTS_MODEL', '')
        self.stt_key = os.getenv('AI_STT_KEY') or os.getenv('AI_API_KEY', '')
        self.tts_key = os.getenv('AI_TTS_KEY') or os.getenv('AI_API_KEY', '')
        self.voice = os.getenv('AI_TTS_VOICE', '')

    def transcribe(self, wav):
        if not self.stt_model: raise ValueError('缺少AI_STT_MODEL')
        pcm, rate, channels = decode_wav(wav)
        if not pcm or len(wav) > 5000000:
            raise ValueError('录音为空或超过5MB')
        body, mime = multipart({'model': self.stt_model, 'response_format': 'json'}, wav)
        try:
            text = json.loads(post(self.stt_url, self.stt_key, body, mime))['text']
        except (KeyError, TypeError, json.JSONDecodeError, UnicodeError):
            raise RuntimeError('转写服务返回格式不兼容') from None
        if not isinstance(text, str) or not text.strip():
            raise ValueError('没有识别到文字，不执行任何动作')
        return text.strip()

    def synthesize(self, text):
        if not self.tts_model or not self.voice:
            raise ValueError('缺少AI_TTS_MODEL或AI_TTS_VOICE')
        if not text or len(text) > 2000:
            raise ValueError('播报文本须为1至2000字')
        wav = post(self.tts_url, self.tts_key, json.dumps({'model': self.tts_model,
            'voice': self.voice, 'input': text, 'response_format': 'wav'}).encode(), limit=15000000)
        decode_wav(wav)
        return wav
