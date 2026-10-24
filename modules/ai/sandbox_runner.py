"""Full Python inside a locked-down Docker container, host-side RPC dispatcher.
No Docker? Fail closed. There is no host-exec fallback.
"""
import asyncio
import json
import shutil
import uuid

class SandboxUnavailable(RuntimeError): pass

class SandboxRunner:
    def __init__(self, gateway, image='companion-sandbox:0.4', timeout=90, max_rpc=2000):
        self.gateway, self.image = gateway, image
        self.timeout, self.max_rpc = timeout, max_rpc
        self.cancelled = asyncio.Event()
        self.running = False

    @staticmethod
    def command(image, name):
        return ['docker','run','--rm','--init','--pull=never','--name',name,'-i',
                '--network=none','--read-only','--cap-drop=ALL',
                '--security-opt=no-new-privileges','--pids-limit=64','--memory=128m',
                '--memory-swap=128m','--cpus=0.5','--ulimit','nofile=128:128',
                '--user=65534:65534','--tmpfs','/tmp:rw,noexec,nosuid,size=16m',image]

    def cancel(self): self.cancelled.set()

    async def check(self):
        if not shutil.which('docker'): raise SandboxUnavailable('需要Docker；禁止退回宿主机exec')
        p = await asyncio.create_subprocess_exec('docker','image','inspect',self.image,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            if await asyncio.wait_for(p.wait(), 10) != 0:
                raise SandboxUnavailable('Docker未启动或沙箱镜像未构建')
        except asyncio.TimeoutError:
            p.kill(); await p.wait(); raise SandboxUnavailable('Docker检查超时') from None

    async def run(self, code):
        if self.running: return {'ok':False,'status':'busy','reason':'another_program_running'}
        if not isinstance(code, str) or not code.strip() or len(code.encode()) > 60000:
            return {'ok':False,'status':'failed','reason':'invalid_program_size'}
        self.running = True
        self.cancelled.clear()
        self.gateway.begin()
        journal, debug = [], bytearray()
        name = 'companion-' + uuid.uuid4().hex
        proc = None
        stderr_task = None
        io_tasks = set()
        result = {'ok':False,'status':'failed'}
        try:
            await self.check()
            async with asyncio.timeout(self.timeout):
                # Acquire only if the program requests motion (gateway lazy-acquires).
                proc = await asyncio.create_subprocess_exec(*self.command(self.image,name),
                    stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,limit=1_000_001)
                proc.stdin.write(json.dumps({'code':code}).encode()+b'\n')
                await proc.stdin.drain()
                async def drain_errors():
                    count = 0
                    while chunk := await proc.stderr.read(4096):
                        count += len(chunk)
                        debug.extend(chunk[:max(0,16000-len(debug))])
                        if count > 64000:
                            proc.kill()
                            return
                stderr_task = asyncio.create_task(drain_errors())
                for _ in range(self.max_rpc + 1):
                    line_task=asyncio.create_task(proc.stdout.readline())
                    cancel_task=asyncio.create_task(self.cancelled.wait())
                    io_tasks.update((line_task,cancel_task))
                    done,_=await asyncio.wait([line_task,cancel_task],return_when=asyncio.FIRST_COMPLETED)
                    if cancel_task in done:
                        line_task.cancel(); await asyncio.gather(line_task,return_exceptions=True)
                        result={'ok':False,'status':'cancelled'}; break
                    cancel_task.cancel(); await asyncio.gather(cancel_task,return_exceptions=True)
                    io_tasks.difference_update((line_task,cancel_task))
                    line=line_task.result()
                    if not line:
                        result={'ok':False,'status':'failed','reason':'sandbox_closed_without_result'}; break
                    if len(line)>16000: raise ValueError('sandbox RPC request too large')
                    message=json.loads(line)
                    if not isinstance(message,dict): raise ValueError('invalid RPC message')
                    if message.get('type') == 'done':
                        result={'ok':message.get('ok') is True,
                                'status':'completed' if message.get('ok') is True else 'failed',
                                'reason':message.get('error','')}; break
                    if message.get('type') != 'rpc': raise ValueError('invalid sandbox protocol')
                    if len(journal) >= self.max_rpc: raise ValueError('RPC budget exhausted')
                    entry={'method':message.get('method'),'args':message.get('args'), 'kwargs':message.get('kwargs')}
                    journal.append(entry)
                    try:
                        value = await self.gateway.dispatch(message['method'],message.get('args',[]),message.get('kwargs',{}))
                        entry['result']=value
                        reply={'id':message['id'],'result':value}
                    except Exception as exc:
                        entry['error']=str(exc)[:1000]
                        reply={'id':message['id'],'error':entry['error']}
                    # Cancellation is checked again after any slow host-side operation.
                    if self.cancelled.is_set():
                        result={'ok':False,'status':'cancelled'}; break
                    proc.stdin.write(json.dumps(reply,ensure_ascii=False).encode()+b'\n')
                    await proc.stdin.drain()
                else:
                    result={'ok':False,'status':'failed','reason':'RPC budget exhausted'}
                if result.get('ok') and any('error' in e or (isinstance(e.get('result'),dict) and e['result'].get('ok') is False) for e in journal):
                    result={'ok':False,'status':'failed','reason':'one_or_more_interface_calls_failed'}
        except SandboxUnavailable as exc:
            result={'ok':False,'status':'unavailable','reason':str(exc)}
        except (TimeoutError, asyncio.TimeoutError):
            result={'ok':False,'status':'timeout','reason':'program_deadline_exceeded'}
        except asyncio.CancelledError:
            result={'ok':False,'status':'cancelled'}
            raise
        except Exception as exc:
            result={'ok':False,'status':'failed','reason':type(exc).__name__+': '+str(exc)[:500]}
        finally:
            for task in io_tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*io_tasks,return_exceptions=True)
            # Kill both the CLI and named container; terminating the CLI alone is insufficient.
            if proc is not None:
                if proc.returncode is None:
                    try: proc.kill()
                    except ProcessLookupError: pass
                await proc.wait()
                try:
                    cleanup=await asyncio.create_subprocess_exec('docker','rm','-f',name,
                        stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
                    try: await asyncio.wait_for(cleanup.wait(),5)
                    except asyncio.TimeoutError:
                        cleanup.kill(); await cleanup.wait()
                except OSError: pass
            if stderr_task:
                if not stderr_task.done(): stderr_task.cancel()
                await asyncio.gather(stderr_task,return_exceptions=True)
            try:
                await self.gateway.finish(result.get('ok') is True)
            except Exception as exc:
                result={'ok':False,'status':'failed','reason':'cleanup_or_follow_resume_failed: '+str(exc)[:500]}
            finally: self.running=False
        return {**result,'simulated':self.gateway.simulated,'journal':journal,
                'debug':debug.decode(errors='replace')}
