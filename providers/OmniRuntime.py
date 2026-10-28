"""Loopback-only web audio/image transport for the production Alibaba route.

All frames come from the SDK. Only ephemeral loopback authority reaches the
webpage. Disconnect/fault/expiry stops the shared runtime before cloud teardown.
"""
import asyncio
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import math
from pathlib import Path
import secrets
import socket
import threading
import time
import traceback
from urllib.parse import urlsplit, parse_qs

from providers.OmniSession import OmniSession
from app.Results import close_result, close_step

ROOT = Path(__file__).resolve().parents[1]


class LoopbackHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR permits two live servers to bind the same port.
    # Use exclusive ownership there; preserve normal restart behavior elsewhere.
    allow_reuse_address = not hasattr(socket, 'SO_EXCLUSIVEADDRUSE')

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class OmniService:
    def __init__(self, runtime, device, log, config, instructions, tools, *, seconds=30000,
                 connect=None, http_port=0):
        if not 0 < seconds <= 30000: raise ValueError('invalid_omni_session_seconds')
        if type(http_port) is not int or not 0 <= http_port <= 65535:
            raise ValueError('invalid_omni_http_port')
        self.runtime, self.device, self.log, self.config = runtime, device, log, config
        self.instructions, self.tools, self.seconds = instructions, tools, seconds
        self.connect = connect
        self.http_port = http_port
        self.token = secrets.token_urlsafe(32)
        self.deadline = None
        self.server = self.http = self.thread = self.client = self.cloud = None
        self.url = self.ws_url = None
        self.finished = asyncio.Event()
        self.closed = False
        self.cleanup_pending = False
        self.stop_result = None
        self.stop_land = False
        self.land_requested = False
        self.stopping = False
        self.background = set()
        self.failure = None
        self.closing = None
        self.handle_closing = False

    async def start(self):
        from websockets.asyncio.server import serve
        self.config.validate()
        if hasattr(self.log, 'secrets'):
            self.log.secrets = tuple(self.log.secrets) + (self.token,)
        self.deadline = time.time()+self.seconds
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                host = '127.0.0.1:'+str(owner.http.server_port)
                if (self.headers.get('Host') != host or self.headers.get('Sec-Fetch-Site') == 'cross-site'
                    or self.headers.get('Origin') not in (None,'http://'+host)):
                    self.send_error(403); return
                route = urlsplit(self.path).path
                if route == '/api/session':
                    if owner.closed or time.time() >= owner.deadline:
                        self.send_error(409); return
                    raw = json.dumps(dict(ok=True,url=owner.ws_url,expiresAt=int(owner.deadline*1000),
                                          provider='alibaba'),ensure_ascii=False).encode()
                    mime = 'application/json'
                else:
                    files = {'/':('Omni.html','text/html'), '/OmniClient.js':('OmniClient.js','text/javascript'),
                             '/OmniAudio.js':('OmniAudio.js','text/javascript')}
                    if route not in files: self.send_error(404); return
                    name,mime = files[route]
                    raw = (ROOT/'web'/name).read_bytes()
                self.send_response(200)
                self.send_header('Content-Type',mime+'; charset=utf-8')
                self.send_header('Content-Length',str(len(raw)))
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('X-Frame-Options','DENY')
                self.send_header('Permissions-Policy','camera=()')
                self.end_headers(); self.wfile.write(raw)
        try:
            self.http = LoopbackHTTPServer(('127.0.0.1',self.http_port),Handler)
        except OSError as exc:
            raise RuntimeError('本机网页端口%s无法监听；请关闭占用该端口的旧会话，或使用-WebPort指定端口。不会自动换端口。' % self.http_port) from exc
        try:
            self.server = await serve(self.handle, '127.0.0.1', 0, max_size=256*1024,
                                      max_queue=8, ping_interval=3, ping_timeout=5)
        except BaseException:
            self.http.server_close()
            self.http = None
            raise
        port = self.server.sockets[0].getsockname()[1]
        self.ws_url = f'ws://127.0.0.1:{port}/?token={self.token}'
        self.thread = threading.Thread(target=self.http.serve_forever,daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:'+str(self.http.server_port)
        self.log({'event':'omni_local_ready','url':self.url,'provider':'alibaba',
                  'max_session_seconds':self.seconds,'hardware_verified':False})
        return self.url

    async def send(self, value):
        if self.client is not None:
            await asyncio.wait_for(self.client.send(json.dumps(value,ensure_ascii=False,allow_nan=False)),2)

    async def frames(self):
        previous = None
        previous_stamp = None
        sent_at = 0.
        logged_at = 0.
        unavailable = False
        pending = None
        try:
            while True:
                if self.handle_closing: return
                if self.stopping:
                    await asyncio.sleep(.1)
                    continue
                if time.time() >= self.deadline: raise TimeoutError('omni_session_expired')
                if time.monotonic()-self.last_heartbeat > 8: raise TimeoutError('omni_heartbeat_expired')
                if self.cloud is not None:
                    if self.cloud.fault is not None: raise self.cloud.fault
                    if not self.cloud.authority_current():
                        raise RuntimeError('omni_runtime_authority_changed')
                try:
                    if pending is None:
                        pending = asyncio.create_task(asyncio.to_thread(self.device.get_frame))
                    packet = await asyncio.wait_for(asyncio.shield(pending),.75)
                    pending = None
                    stamp, identity = packet.timestamp, packet.frame_id
                    now = time.time()
                    if now-logged_at >= 1:
                        self.log({'event':'video_sample','frame_id':identity,'source_timestamp':stamp,
                                  'age_seconds':now-stamp if type(stamp) in (int,float) and math.isfinite(stamp) else None,
                                  'stale':bool(packet.stale),'has_frame':packet.frame is not None})
                        logged_at = now
                    if (packet.stale or packet.frame is None or type(stamp) not in (int,float)
                            or not math.isfinite(stamp) or not 0 <= time.time()-stamp <= 1.0
                            or type(identity) is not int or identity < 0
                            or previous is not None and (identity < previous or stamp < previous_stamp
                                or identity == previous and stamp != previous_stamp)):
                        raise ValueError('omni_sdk_frame_unavailable')
                    if identity == previous:
                        await asyncio.sleep(.1)
                        continue
                    import cv2
                    frame = packet.frame
                    h,w = frame.shape[:2]
                    scale = min(1.,1280/w,720/h)
                    if scale < 1: frame = cv2.resize(frame,(int(w*scale),int(h*scale)))
                    ok,encoded = cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,75])
                    if not ok: raise RuntimeError('omni_frame_encoding_failed')
                    jpeg = encoded.tobytes()
                    if len(base64.b64encode(jpeg)) > 255*1024 or time.time()-stamp > 1:
                        raise ValueError('omni_frame_unavailable')
                except Exception:
                    if pending is not None and pending.done(): pending = None
                    if not unavailable:
                        await self.send({'type':'invalid_frame','reason':'sdk_frame_unavailable'})
                    unavailable = True
                else:
                    unavailable = False
                    previous, previous_stamp = identity, stamp
                    await self.send(dict(type='frame',id=previous,timestamp=stamp,jpeg=base64.b64encode(jpeg).decode()))
                    if self.cloud is not None and time.monotonic()-sent_at >= 1:
                        if await self.cloud.image(jpeg,stamp): sent_at = time.monotonic()
                await asyncio.sleep(.1)
        finally:
            if pending is not None: pending.cancel()

    async def handle(self, socket):
        token = parse_qs(urlsplit(socket.request.path).query).get('token',[''])[0]
        origin = socket.request.headers.get('Origin')
        if not hmac.compare_digest(token.encode(),self.token.encode()) or origin not in (self.url,):
            await socket.close(1008,'unauthorized'); return
        if self.closed or self.client is not None or self.finished.is_set():
            await socket.close(1008,'session_not_available'); return
        self.client = socket
        self.last_heartbeat = time.monotonic()
        self.cloud = OmniSession(self.runtime,self.config,self.instructions,self.tools,
                                 self.send,self.log,connect=self.connect)
        self.cloud.fault = None
        cloud_task = None
        pump = asyncio.create_task(self.frames())
        started = None
        packets = 0
        phase = 'local_client'
        self.log({'event':'omni_client_connected'})
        try:
            async def receive():
                nonlocal cloud_task, started, packets
                async for raw in socket:
                    if self.handle_closing: return
                    value = json.loads(raw)
                    if not isinstance(value,dict): raise ValueError('omni_invalid_client_message')
                    kind = value.get('type')
                    if kind == 'heartbeat': self.last_heartbeat = time.monotonic()
                    elif kind == 'start':
                        if cloud_task is not None: raise ValueError('omni_already_started')
                        cloud_task = asyncio.create_task(self.cloud.run())
                        self.background.add(cloud_task)
                    elif kind == 'audio':
                        if cloud_task is None or cloud_task.done(): raise ValueError('omni_not_ready')
                        pcm = base64.b64decode(value.get('audio',''),validate=True)
                        if started is None: started = time.monotonic()
                        packets += 1
                        if packets*.02 > time.monotonic()-started+1:
                            raise ValueError('omni_audio_upload_rate')
                        await self.cloud.audio(pcm)
                    elif kind == 'audio_reset':
                        await self.cloud.clear_audio()
                    elif kind == 'interrupt':
                        if type(value.get('land')) is not bool: raise ValueError('omni_land_requires_boolean')
                        self.land_requested = self.land_requested or value['land']
                        self.stopping = True
                        self.runtime.cancel()
                        await self.send({'type':'interrupted'})
                        land = self.land_requested
                        self.stop_result = await asyncio.wait_for(self.runtime.interrupt(land),3)
                        self.stop_land = land
                        return  # No old cloud result may regain authority.
                    elif kind == 'stop':
                        details = value.get('diagnostics',{})
                        details = details if isinstance(details,dict) else {}
                        diagnostics = {key:details[key] for key in
                            ('scheduled_audio_seconds','pending_audio_seconds','frame_age_ms')
                            if type(details.get(key)) in (int,float) and math.isfinite(details[key])}
                        if isinstance(details.get('audio_state'),str):
                            diagnostics['audio_state'] = details['audio_state'][:32]
                        self.log({'event':'omni_client_stop',
                                  'source':str(value.get('source','unspecified'))[:32],
                                  'reason':str(value.get('reason','unspecified'))[:512],
                                  'diagnostics':diagnostics})
                        self.stopping = True
                        return
                    else: raise ValueError('omni_unknown_client_message')
            receiver = asyncio.create_task(receive())
            self.background.add(receiver)
            while True:
                watches = {pump,receiver} | ({cloud_task} if cloud_task is not None else set())
                done,_ = await asyncio.wait(watches,timeout=.1,return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    phase = 'cloud_session' if task is cloud_task else 'video_pump' if task is pump else 'local_client_receive'
                    await task
                if done: break
        except Exception as exc:
            primary = getattr(self.cloud, 'fault', None)
            if primary is not None:
                # Cloud processing may already be closing its socket; report
                # its saved cause instead of a secondary audio-send failure.
                exc = primary
                phase = 'cloud_session'
            self.failure = type(exc).__name__+': '+str(exc)[:120]
            # Only safe, local fixed messages are transmitted/logged.
            self.runtime.cancel()
            try: await self.send({'type':'error','reason':'语音或图传异常，已撤销本轮授权。请核对飞机状态并重新启动入口。'})
            except Exception: pass
            self.log({'event':'omni_transport_failed','error_type':type(exc).__name__,
                      'stage':phase,'error':str(exc),
                      'traceback':''.join(traceback.format_exception(type(exc),exc,exc.__traceback__))})
        finally:
            self.handle_closing = True
            self.stopping = True
            self.runtime.cancel()
            self.land_requested = (self.land_requested or getattr(self.runtime, 'land_requested', False)
                                   or getattr(self.cloud, 'land_requested', False))
            try:
                if self.stop_result is None or self.stop_result.get('ok') is not True or self.land_requested and not self.stop_land:
                    self.stop_result = await asyncio.wait_for(self.runtime.interrupt(self.land_requested),3)
                    self.stop_land = self.land_requested
            except Exception:
                self.stop_result = {'ok':False,'reason':'omni_stop_unconfirmed'}
            self.cleanup_pending = self.stop_result.get('ok') is not True
            pump.cancel()
            for task in self.background: task.cancel()
            try:
                await self.cloud.close()
            except Exception:
                self.cleanup_pending = True
            finally:
                try: await socket.close()
                finally:
                    # Closing sockets also wakes an IO loop whose nested wait
                    # completed concurrently with cancellation. Do not wait for
                    # that loop before closing the resource it is waiting on.
                    await asyncio.gather(pump,*self.background,return_exceptions=True)
                    self.background.clear()
                    self.client = None
                    self.finished.set()

    async def wait(self):
        await self.finished.wait()
        if self.failure: raise RuntimeError('omni_session_failed')
        if self.cleanup_pending: raise RuntimeError('omni_stop_unconfirmed')

    def revoke(self, land=False):
        self.land_requested = self.land_requested or bool(land) or getattr(self.runtime,'land_requested',False)
        self.stopping = True
        self.runtime.cancel()

    async def close(self):
        if self.closing is not None:
            return await asyncio.shield(self.closing)
        async def finish():
            steps = []
            self.closed = True
            self.revoke(self.land_requested)
            try:
                if self.server is not None:
                    self.server.close()
                    await self.server.wait_closed()
                if self.cloud is not None: await self.cloud.close()
            except Exception:
                self.cleanup_pending = True
            finally:
                if self.stop_result is None or self.land_requested and not self.stop_land:
                    try: self.stop_result = await asyncio.wait_for(self.runtime.interrupt(self.land_requested),3)
                    except Exception: self.stop_result = {'ok':False,'reason':'omni_stop_unconfirmed'}
                    self.stop_land = self.land_requested
                    self.cleanup_pending = self.cleanup_pending or self.stop_result.get('ok') is not True
                if self.http is not None:
                    await asyncio.to_thread(self.http.shutdown)
                    self.http.server_close()
                    await asyncio.to_thread(self.thread.join,2)
                    if self.thread.is_alive():
                        self.cleanup_pending = True
                steps.append(close_step('voice_service', not self.cleanup_pending,
                                        'omni_stop_unconfirmed' if self.cleanup_pending else ''))
                self.finished.set()
            return close_result(*steps)
        self.closing = asyncio.create_task(finish())
        return await asyncio.shield(self.closing)
