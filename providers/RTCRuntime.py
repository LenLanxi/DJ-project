"""Hidden local RTC service lifecycle; no browser automation or cloud fallback."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from app.Results import close_result, close_step

ROOT=Path(__file__).resolve().parents[1]
REQUIRED=('RTC_APP_ID','RTC_APP_KEY','VOLCENGINE_ACCESS_KEY_ID','VOLCENGINE_SECRET_ACCESS_KEY',
          'VOLC_S2S_APP_ID','VOLC_S2S_TOKEN','RTC_VLM_MODEL')


def validate_environment(seconds, env=None):
    source=os.environ if env is None else env
    missing=[name for name in REQUIRED if not isinstance(source.get(name),str) or not source[name].strip()]
    if missing:
        raise ValueError('RTC正式语音缺少配置：'+', '.join(missing)+'；没有旧语音或模拟降级')
    if not 0 < seconds <= 30000:
        raise ValueError('RTC单次会话必须在0到30000秒之间')
    node=shutil.which('node')
    if not node:
        raise ValueError('需要Node.js 22+；请先准备项目RTC依赖')
    folder=ROOT/'web'
    for path in (folder/'node_modules'/'@volcengine'/'rtc'/'package.json',
                 folder/'node_modules'/'@volcengine'/'openapi'/'package.json'):
        if not path.is_file():
            raise ValueError('RTC Node依赖未准备：在web中运行npm ci；启动时不自动安装')
    return node


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise RuntimeError('Local RTC service must not redirect')


class RTCProcess:
    def __init__(self,log,env=None,*,node,web_port=0):
        if type(web_port) is not int or not 0 <= web_port <= 65535:
            raise ValueError('invalid_rtc_web_port')
        self.log=log
        self.node=node
        self.web_port=web_port
        self.environment=None if env is None else dict(env)
        self.proc=None
        self.url=None
        self.output=Path(log.path).with_name(log.session_id+'Rtc.log')
        self.cleanup_pending=False
        self.deadline=None

    def request(self,path,body=None):
        if not self.url or not self.url.startswith('http://127.0.0.1:'):
            raise RuntimeError('RTC local URL not available')
        raw=None if body is None else json.dumps(body).encode()
        req=Request(self.url+path,data=raw,headers={'Content-Type':'application/json'},
                    method='GET' if body is None else 'POST')
        try:
            response=build_opener(ProxyHandler({}),NoRedirect()).open(req,timeout=3)
        except HTTPError as exc:
            response=exc
        with response:
            data=response.read(65537)
        if len(data)>65536: raise RuntimeError('RTC service response too large')
        value=json.loads(data)
        if not isinstance(value,dict): raise RuntimeError('Invalid RTC service response')
        return value

    async def publish_result(self, identity, call_id, result):
        reply = {'ToolCallID': call_id, 'Content': json.dumps(result, ensure_ascii=False)}
        response = await asyncio.to_thread(self.request, '/api/update',
            {'sessionId': identity['sessionId'], 'command': 'function',
             'message': json.dumps(reply, ensure_ascii=False)})
        if response.get('ok') is not True:
            raise RuntimeError('rtc_tool_result_unconfirmed')

    async def start(self,bridge,seconds,instructions,tools):
        bridge.result_sink = self.publish_result
        self.deadline=time.time()+seconds
        env=dict(os.environ)
        if self.environment is not None:
            env.update(self.environment)
        env.update(RTC_BRIDGE_URL=bridge.url,RTC_BRIDGE_TOKEN=bridge.token,
                 RTC_PORT=str(self.web_port),RTC_SESSION_SECONDS=str(seconds),
                 RTC_RUNTIME_DEADLINE_MS=str(int(self.deadline*1000)),
                 RTC_INSTRUCTIONS=instructions,RTC_TOOLS_JSON=json.dumps(tools,ensure_ascii=False))
        capture = getattr(self.log, 'capture', None)
        env['RTC_RECORDING_DIR'] = str(capture.root) if capture is not None else ''
        self.output.parent.mkdir(parents=True,exist_ok=True)
        with self.output.open('ab') as output:
            self.proc=await asyncio.create_subprocess_exec(self.node,'RTCService.js',cwd=ROOT/'web',
                env=env,stdin=asyncio.subprocess.DEVNULL,stdout=output,stderr=output,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if self.proc.returncode is not None:
                raise RuntimeError('RTC本机服务启动失败；查看 '+str(self.output))
            text=self.output.read_text(encoding='utf-8',errors='replace')
            for line in text.splitlines():
                try: event=json.loads(line)
                except ValueError: continue
                if event.get('event')=='rtc_listening':
                    url=event.get('url','')
                    if not url.startswith('http://127.0.0.1:'):
                        raise RuntimeError('RTC服务监听地址不是本机')
                    self.url=url
                    status=await asyncio.to_thread(self.request,'/api/status')
                    if not status.get('configured'):
                        raise RuntimeError('RTC服务配置不完整：'+', '.join(status.get('missing',[])))
                    self.log({'event':'rtc_local_service_ready','url':url,'pid':self.proc.pid,
                              'service_log':str(self.output),'session_seconds':seconds})
                    return url
            await asyncio.sleep(.05)
        raise RuntimeError('RTC本机服务启动超时；查看 '+str(self.output))

    async def wait(self):
        """Observe the hidden process; the configured session deadline lives in Node."""
        code=await self.proc.wait()
        if code:
            raise RuntimeError('RTC本机服务异常退出：'+str(code))

    async def close(self):
        if self.proc is None or self.proc.returncode is not None:
            confirmed = self.proc is None or self.proc.returncode == 0
            return close_result(close_step('voice_service', confirmed,
                '' if confirmed else 'cloud_cleanup_process_unconfirmed: '+str(self.proc.returncode)))
        if not self.url:
            # No listening service means no session could have started.
            self.proc.terminate()
            await self.proc.wait()
            return close_result(close_step('voice_service', True))
        try:
            await asyncio.to_thread(self.request,'/api/shutdown',{})
        except Exception:
            pass  # HTTP may time out while Node is still confirming cloud stop.
        try:
            await asyncio.wait_for(asyncio.shield(self.proc.wait()),20)
        except Exception:
            self.cleanup_pending=self.proc.returncode is None
            if self.cleanup_pending:
                # TerminateProcess skips Node shutdown hooks on Windows. Leave
                # the bounded stop-only helper alive to finish confirmed cleanup.
                self.log({'event':'rtc_cloud_cleanup_pending','pid':self.proc.pid,
                          'service_log':str(self.output)})
                print('[RTC] 云端停止未确认，保留隐藏清理进程 PID=%s；日志 %s' %
                      (self.proc.pid,self.output),flush=True)
        confirmed = not self.cleanup_pending and self.proc.returncode == 0
        return close_result(close_step('voice_service', confirmed,
            '' if confirmed else 'cloud_cleanup_unconfirmed'))
