"""Reusable voice pipeline with explicit transcription confirmation."""
import time

class VoicePipeline:
    def __init__(self, agent, service, recorder, player):
        self.agent, self.service = agent, service
        self.recorder, self.player = recorder, player

    def turn(self, confirm=lambda text: False):
        start = time.monotonic()
        audio = self.recorder()
        recorded = time.monotonic()
        text = self.service.transcribe(audio)
        transcribed = time.monotonic()
        # Until microphone and recognition are validated, confirm every transcript.
        if not confirm(text):
            return {'request': text, 'reply': '未确认转写，未调用模型和动作', 'cancelled': True}
        record = self.agent.handle(text)
        handled = time.monotonic()
        try:
            wav = self.service.synthesize(record['reply'][:2000])
            synthesized = time.monotonic()
            self.player(wav)
        except (ValueError, RuntimeError, OSError) as exc:
            record['speech_error'] = str(exc)
            record['timing_ms'] = {'total': round((time.monotonic()-start)*1000)}
            return record
        done = time.monotonic()
        record['timing_ms'] = {'record': round((recorded-start)*1000),
            'transcribe': round((transcribed-recorded)*1000),
            'confirm_and_agent': round((handled-transcribed)*1000),
            'synthesize': round((synthesized-handled)*1000), 'play': round((done-synthesized)*1000)}
        return record
