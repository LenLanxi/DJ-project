"""Diagnostic single-turn Alibaba probe; never controls a physical device."""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import math
import re
import struct
import time
import uuid

from providers.AlibabaRealtime import (AlibabaConfig, AlibabaError, MAX_EVENT_BYTES,
    MAX_IMAGE_BASE64_BYTES, MAX_TOOL_BYTES, normalize_tools, session_update,
    strict_json, validate_arguments)


def speech_end_position(pcm16: bytes, sample_rate: int = 16000) -> float:
    """Provider-independent PCM peak threshold, not an ASR speech detector."""
    samples = struct.unpack('<' + 'h' * (len(pcm16) // 2), pcm16)
    for index in range(len(samples) - 1, -1, -1):
        if abs(samples[index]) >= 256:
            return (index + 1) / sample_rate
    raise AlibabaError('no_nonsilent_input_samples')


class PcmActivity:
    """RMS >=64 for two consecutive 10ms PCM16/24k windows.

    The returned time is when the first eligible window became available in a
    received chunk. Sample offsets are metadata, never added to receipt time to
    pretend that network arrival measured audible speaker playback.
    """
    def __init__(self):
        self.pending = bytearray()
        self.samples = 0
        self.consecutive = 0
        self.candidate = None
        self.detected = None

    def push(self, pcm, received_ms):
        if len(pcm) % 2:
            raise AlibabaError('odd_output_pcm')
        self.pending.extend(pcm)
        while len(self.pending) >= 480:
            window = bytes(self.pending[:480]); del self.pending[:480]
            values = struct.unpack('<240h', window)
            rms = math.sqrt(sum(value*value for value in values) / 240)
            if rms >= 64:
                if not self.consecutive:
                    self.candidate = {'first_window_received_ms': received_ms,
                                      'sample_offset': self.samples}
                self.consecutive += 1
                if self.consecutive >= 2 and self.detected is None:
                    self.detected = dict(self.candidate, confirmed_received_ms=received_ms,
                                         rms_threshold=64, window_ms=10, consecutive_windows=2)
            else:
                self.consecutive = 0
                self.candidate = None
            self.samples += 240
        return self.detected


class AlibabaRealtime:
    def __init__(self, config: AlibabaConfig, *, instructions: str, tools: list[dict],
                 tool_handler=None, on_event=None, connect=None, clock=time.monotonic,
                 sleep=asyncio.sleep):
        self.config, self.instructions = config, instructions
        self.tools = normalize_tools(tools)
        self.tool_handler, self.on_event = tool_handler, on_event
        self.connect, self.clock, self.sleep = connect, clock, sleep
        self._active_task = None
        self._epoch = 0

    def cancel(self):
        self._epoch += 1
        task = self._active_task
        if task is not None and not task.done():
            task.cancel()

    async def close(self):
        task = self._active_task
        self.cancel()
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)

    def _safe(self, value):
        if isinstance(value, str):
            return value.replace(self.config.api_key, '[REDACTED]') if self.config.api_key else value
        if isinstance(value, dict):
            return {str(key): '[REDACTED]' if any(word in str(key).lower() for word in
                    ('api_key', 'apikey', 'authorization', 'access_token')) else self._safe(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [self._safe(item) for item in value]
        return value

    async def run_turn(self, pcm16: bytes, image_jpeg: bytes | None = None, *, sample_rate=16000,
                       timeout_seconds=30, max_audio_seconds=15, realtime_pacing=True,
                       speech_end_seconds=None, mode='manual', vad_padding_seconds=1.,
                       silence_ms=800, image_interval_seconds=None) -> dict:
        if self._active_task is not None:
            raise AlibabaError('turn_already_running')
        url = self.config.url
        if (not isinstance(pcm16, bytes) or not pcm16 or len(pcm16) % 2 or sample_rate != 16000
                or not 0 < max_audio_seconds <= 30 or len(pcm16) / 32000 > max_audio_seconds
                or not 0 < timeout_seconds <= 60 or type(realtime_pacing) is not bool):
            raise AlibabaError('invalid_or_oversized_audio_turn')
        duration = len(pcm16) / 32000
        if type(vad_padding_seconds) not in (int, float) or not 0 <= vad_padding_seconds <= 2:
            raise AlibabaError('invalid_vad_padding')
        if image_interval_seconds is not None and (type(image_interval_seconds) not in (int, float)
                or not math.isfinite(image_interval_seconds) or image_interval_seconds < 1):
            raise AlibabaError('invalid_image_interval')
        padding = 0. if mode == 'manual' else float(vad_padding_seconds)
        if duration + padding > max_audio_seconds:
            raise AlibabaError('uploaded_audio_limit_including_padding')
        estimated = speech_end_seconds is None
        speech_end_seconds = speech_end_position(pcm16) if estimated else speech_end_seconds
        if type(speech_end_seconds) not in (int, float) or not 0 < speech_end_seconds <= duration:
            raise AlibabaError('invalid_speech_end_position')
        if image_jpeg is not None:
            if (not isinstance(image_jpeg, bytes) or not image_jpeg.startswith(b'\xff\xd8')
                    or not image_jpeg.endswith(b'\xff\xd9')
                    or len(base64.b64encode(image_jpeg)) > MAX_IMAGE_BASE64_BYTES):
                raise AlibabaError('invalid_or_oversized_jpeg')
            import cv2
            import numpy as np
            image = cv2.imdecode(np.frombuffer(image_jpeg, np.uint8), cv2.IMREAD_COLOR)
            if image is None or max(image.shape[:2]) > 1920 or min(image.shape[:2]) > 1080:
                raise AlibabaError('invalid_jpeg_dimensions')
        update = session_update(self.config, self.instructions, self.tools, mode=mode, silence_ms=silence_ms)
        self._epoch += 1
        epoch = self._epoch
        self._active_task = asyncio.current_task()
        origin = self.clock()
        result = {'ok': False, 'provider': 'alibaba', 'model': self.config.model,
                  'scope': 'bounded_native_audio_image_tool_roundtrip_not_flight_acceptance',
                  'connection_policy': 'fresh_per_turn', 'turn_mode': mode,
                  'audio_input_seconds': duration, 'input_sample_rate': sample_rate,
                  'uploaded_audio_seconds': duration + padding, 'vad_padding_seconds': padding,
                  'speech_end_seconds': speech_end_seconds,
                  'speech_end_source': 'pcm_peak_threshold_256' if estimated else 'caller_supplied_common_position',
                  'input_end_scope': 'send_completion_of_packet_containing_last_nonsilent_sample',
                  'realtime_pacing': realtime_pacing, 'image_sent': False,
                  'billing_verified': False,
                  'events': [], 'metrics': {}, 'usage': [], 'asr_text': '', 'reply_text': '',
                  'transcriptions': [], 'vad_silence_ms': silence_ms,
                  'image_interval_seconds': image_interval_seconds,
                  'tool_calls': [], 'tool_results': [], 'output_audio_bytes': 0,
                  'limitations': ['single_input_fixture_not_continuous_multiturn_acceptance',
                      'new_speech_during_tool_aborts_fixture_not_continuous_conversation'],
                  'hardware_verified': False, 'audio_played': False}
        metrics = result['metrics']
        send_lock = asyncio.Lock()
        sender = None
        receiver = None
        tool_work = None
        ws = None

        def emit(event, **fields):
            record = self._safe({'event': event, 't_ms': round((self.clock()-origin)*1000, 3), **fields})
            result['events'].append(record)
            if self.on_event is not None:
                self.on_event(record)
            return record['t_ms']

        async def send(event):
            if self._epoch != epoch:
                raise AlibabaError('obsolete_turn')
            payload = dict(event, event_id='client' + uuid.uuid4().hex)
            async with send_lock:
                await ws.send(json.dumps(payload, ensure_ascii=False, allow_nan=False))

        async def receive():
            raw = await ws.recv()
            if not isinstance(raw, str) or len(raw.encode()) > MAX_EVENT_BYTES:
                raise AlibabaError('invalid_server_event')
            event = strict_json(raw)
            if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                raise AlibabaError('invalid_server_event')
            kind = event['type']
            evidence = {key: event[key] for key in ('item_id', 'response_id', 'audio_start_ms', 'audio_end_ms')
                        if key in event}
            emit('server_event', type=kind, **evidence)
            if kind == 'session.updated':
                session = event.get('session', {})
                result['effective_session'] = self._safe({key: session[key] for key in
                    ('turn_detection', 'audio', 'modalities', 'video') if key in session})
            if kind in ('error', 'conversation.item.input_audio_transcription.failed'):
                # Preserve only bounded diagnostic fields, never the complete
                # event, request body or credentials echoed by a provider.
                details = event.get('error', {})
                if not isinstance(details, dict):
                    details = {'message': details} if isinstance(details, str) else {}
                safe_details = {}
                for field, limit in (('type', 100), ('code', 100), ('message', 512), ('param', 160)):
                    value = details.get(field)
                    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                        value = self._safe(str(value))
                        value = re.sub(r'[\x00-\x1f\x7f]', ' ', value)
                        safe_details[field] = value[:limit]
                result['error_details'] = safe_details
                emit('provider_error', details=safe_details)
                code = details.get('code', 'provider_error')
                safe_code = re.sub(r'[^A-Za-z0-9_.-]', '', str(code))[:100]
                raise AlibabaError(self._safe(safe_code) or 'provider_error')
            if kind == 'conversation.item.input_audio_transcription.completed':
                result['asr_text'] = self._safe(str(event.get('transcript', ''))[:4000])
                result['transcriptions'].append({'item_id': event.get('item_id'), 'text': result['asr_text']})
                metrics['asr_final_ms'] = emit('asr_final')
            return event

        async def send_input():
            start = self.clock()
            metrics['input_start_ms'] = emit('input_start')
            upload = pcm16 + b'\0\0' * int(padding * 16000)
            next_image_seconds = 0.
            for offset in range(0, len(upload), 640):
                end = min(offset+640, len(upload))
                if realtime_pacing:
                    await self.sleep(max(0., start + end/32000 - self.clock()))
                await send({'type': 'input_audio_buffer.append',
                            'audio': base64.b64encode(upload[offset:end]).decode('ascii')})
                if 'input_end_send_ms' not in metrics and end/32000 >= speech_end_seconds:
                    metrics['input_end_send_ms'] = emit('input_end_send')
                    metrics['input_end_packet_overshoot_ms'] = round((end/32000-speech_end_seconds)*1000, 6)
                    metrics['speech_end_anchor_ms'] = round(metrics['input_end_send_ms']
                        - metrics['input_end_packet_overshoot_ms'], 6)
                if image_jpeg is not None and (offset == 0 or image_interval_seconds is not None
                        and end/32000 >= next_image_seconds):
                    await send({'type': 'input_image_buffer.append',
                                'image': base64.b64encode(image_jpeg).decode('ascii')})
                    result['image_sent'] = True
                    metrics['image_sent_ms'] = emit('image_sent', bytes=len(image_jpeg))
                    next_image_seconds = end/32000 + (image_interval_seconds or math.inf)
            metrics['all_audio_sent_ms'] = emit('all_audio_sent')
            if mode == 'manual':
                await send({'type': 'input_audio_buffer.commit'})
                metrics['commit_ms'] = emit('commit_sent')

        seen_calls, pending, reply_parts = {}, {}, []
        input_committed = False
        activity, post_result_activity = PcmActivity(), PcmActivity()
        schemas = {item['function']['name']: item['function']['parameters'] for item in self.tools}

        def remember_call(event):
            if not input_committed:
                raise AlibabaError('tool_without_committed_audio')
            if 'input_end_send_ms' not in metrics:
                raise AlibabaError('tool_before_input_finished')
            call_id, name, arguments = event.get('call_id'), event.get('name'), event.get('arguments')
            if (not isinstance(call_id, str) or not call_id or len(call_id) > 256
                    or name not in schemas or not isinstance(arguments, str)
                    or len(arguments.encode()) > MAX_TOOL_BYTES):
                raise AlibabaError('invalid_or_unknown_tool_call')
            try:
                parsed = strict_json(arguments)
            except (ValueError, TypeError):
                raise AlibabaError('invalid_tool_arguments_json') from None
            validate_arguments(parsed, schemas[name])
            signature = json.dumps([name, parsed], sort_keys=True, allow_nan=False)
            if call_id in seen_calls:
                if seen_calls[call_id] != signature:
                    raise AlibabaError('tool_id_reused_with_changed_payload')
                return
            if len(seen_calls) >= 4:
                raise AlibabaError('tool_call_limit')
            seen_calls[call_id] = signature
            pending[call_id] = (name, parsed)
            result['tool_calls'].append(self._safe({'call_id': call_id, 'name': name, 'arguments': parsed}))
            moment = emit('tool_call', call_id=call_id, name=name)
            metrics.setdefault('tool_call_ms', moment)

        try:
            from websockets.asyncio.client import connect as websocket_connect
            connector = self.connect or websocket_connect
            async with asyncio.timeout(timeout_seconds):
                emit('connect_start')
                async with connector(url, additional_headers={'Authorization': 'Bearer ' + self.config.api_key},
                                     max_size=MAX_EVENT_BYTES, open_timeout=min(timeout_seconds, 10),
                                     close_timeout=1, ping_interval=10, proxy=None) as socket:
                    ws = socket
                    metrics['connected_ms'] = emit('connected')
                    while (await receive())['type'] != 'session.created':
                        pass
                    await send(update)
                    emit('session_update_sent')
                    while (await receive())['type'] != 'session.updated':
                        pass
                    metrics['session_ready_ms'] = emit('session_ready')
                    sender = asyncio.create_task(send_input())
                    response_requested = False
                    while True:
                        if sender.done():
                            await sender
                        receiver = asyncio.create_task(receive())
                        # A sender exception must surface while the server waits
                        # for input instead of being hidden until the deadline.
                        tasks = {receiver} | ({sender} if sender and not sender.done() else set())
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        if sender in done:
                            try:
                                await sender
                            except BaseException:
                                receiver.cancel()
                                await asyncio.gather(receiver, return_exceptions=True)
                                raise
                        event = await receiver
                        kind = event['type']
                        if kind == 'input_audio_buffer.committed':
                            input_committed = True
                            metrics['committed_ms'] = emit('input_committed')
                            if mode == 'manual' and not response_requested:
                                await send({'type': 'response.create'})
                                metrics['response_requested_ms'] = emit('response_requested')
                                response_requested = True
                        elif kind == 'response.created':
                            metrics.setdefault('response_created_ms', emit('response_created'))
                        elif kind in ('response.text.delta', 'response.audio_transcript.delta'):
                            text = event.get('delta', '')
                            if isinstance(text, str) and text:
                                reply_parts.append(text)
                                moment = emit('model_text_received')
                                metrics.setdefault('first_text_ms', moment)
                                if 'result_sent_ms' in metrics:
                                    metrics.setdefault('post_result_first_text_ms', moment)
                        elif kind in ('response.text.done', 'response.audio_transcript.done'):
                            text = event.get('text', event.get('transcript'))
                            if isinstance(text, str):
                                result['reply_text'] = self._safe(text[:8000])
                        elif kind == 'response.audio.delta':
                            try:
                                audio = base64.b64decode(event.get('delta', ''), validate=True)
                            except (ValueError, TypeError):
                                raise AlibabaError('invalid_output_audio') from None
                            if len(audio) % 2:
                                raise AlibabaError('odd_output_pcm')
                            if audio:
                                result['output_audio_bytes'] += len(audio)
                                if result['output_audio_bytes'] > 24000 * 2 * 30:
                                    raise AlibabaError('output_audio_limit')
                                moment = emit('model_audio_received', bytes=len(audio))
                                metrics.setdefault('first_audio_chunk_ms', moment)
                                audible = activity.push(audio, moment)
                                if audible and 'first_nonzero_audio_ms' not in metrics:
                                    metrics['first_nonzero_audio_ms'] = audible['first_window_received_ms']
                                    metrics['first_audio_ms'] = audible['first_window_received_ms']
                                    result['audio_activity'] = audible
                                    emit('nonzero_audio_detected', **audible)
                                if 'result_sent_ms' in metrics:
                                    metrics.setdefault('post_result_first_audio_chunk_ms', moment)
                                    audible = post_result_activity.push(audio, moment)
                                    if audible:
                                        metrics.setdefault('post_result_first_audio_ms', audible['first_window_received_ms'])
                        elif kind == 'response.function_call_arguments.done':
                            remember_call(event)
                        elif kind == 'response.done':
                            response = event.get('response', {})
                            if not isinstance(response, dict) or response.get('status') != 'completed':
                                raise AlibabaError('response_not_completed')
                            if isinstance(response.get('usage'), dict):
                                result['usage'].append(self._safe(response['usage']))
                            for item in response.get('output', []):
                                if isinstance(item, dict) and item.get('type') == 'function_call':
                                    remember_call(item)
                            if pending:
                                if self.tool_handler is None:
                                    raise AlibabaError('tool_dispatcher_missing')
                                for call_id, (name, arguments) in list(pending.items()):
                                    emit('tool_execution_started', call_id=call_id)
                                    value = self.tool_handler(name, arguments, call_id)
                                    if inspect.isawaitable(value):
                                        tool_work = asyncio.ensure_future(value)
                                        while not tool_work.done():
                                            # Continue draining input events while the tool
                                            # runs. A new utterance invalidates this bounded
                                            # fixture; never attach its old result to a new
                                            # cloud turn. Full conversation resumption is a
                                            # separate integration requirement.
                                            receiver = asyncio.create_task(receive())
                                            finished, _ = await asyncio.wait(
                                                {receiver, tool_work}, return_when=asyncio.FIRST_COMPLETED)
                                            if receiver in finished or receiver.done():
                                                interruption = await receiver
                                                if interruption['type'] == 'input_audio_buffer.speech_started':
                                                    raise AlibabaError('input_interrupted_during_tool')
                                            else:
                                                receiver.cancel()
                                                await asyncio.gather(receiver, return_exceptions=True)
                                        value = await tool_work
                                    if self._epoch != epoch:
                                        raise AlibabaError('obsolete_tool_result')
                                    value = self._safe(value)
                                    serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
                                    if len(serialized.encode()) > MAX_TOOL_BYTES:
                                        raise AlibabaError('tool_result_too_large')
                                    await send({'type': 'conversation.item.create', 'item': {
                                        'type': 'function_call_output', 'call_id': call_id, 'output': serialized}})
                                    result['tool_results'].append({'call_id': call_id, 'result': value})
                                    metrics['result_sent_ms'] = emit('tool_result_sent', call_id=call_id)
                                    post_result_activity = PcmActivity()
                                    del pending[call_id]
                                await send({'type': 'response.create'})
                                emit('continuation_requested')
                            else:
                                metrics['response_done_ms'] = emit('response_done')
                                result['ok'] = True
                                break
                    if sender:
                        await sender
        except asyncio.CancelledError:
            result['error_code'] = 'cancelled'
            emit('cancelled')
            raise
        except TimeoutError:
            result['error_code'] = 'deadline_exceeded'
            emit('deadline_exceeded')
        except Exception as error:
            if isinstance(error, AlibabaError):
                code = str(error)
            else:
                status = getattr(getattr(error, 'response', None), 'status_code', None)
                code = 'handshake_http_' + str(status) if status else type(error).__name__
            result['error_code'] = self._safe(code)
            emit('failed', error_code=result['error_code'])
        finally:
            if tool_work is not None and not tool_work.done():
                tool_work.cancel()
                await asyncio.gather(tool_work, return_exceptions=True)
            if receiver is not None:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            self._active_task = None
            result['cleanup'] = {
                'connection_opened': ws is not None,
                'connection_closed': ws is None or getattr(getattr(ws, 'state', None), 'name', '') == 'CLOSED',
                'sender_finished': sender is None or sender.done(),
                'receiver_finished': receiver is None or receiver.done()}
            emit('connection_closed', confirmed=result['cleanup']['connection_closed'])
            if not result['reply_text']:
                result['reply_text'] = self._safe(''.join(reply_parts)[:8000])
            if 'input_end_send_ms' in metrics:
                for field in ('first_text_ms', 'first_audio_ms', 'tool_call_ms', 'response_done_ms'):
                    if field in metrics:
                        metrics['input_end_to_' + field] = round(metrics[field]-metrics['input_end_send_ms'], 3)
            if 'result_sent_ms' in metrics:
                for field in ('post_result_first_text_ms', 'post_result_first_audio_ms'):
                    if field in metrics:
                        metrics['result_to_' + field] = round(metrics[field]-metrics['result_sent_ms'], 3)
        return self._safe(result)
