"""Runs ONLY inside the sandbox image. Never launch this on the host with model code."""
import builtins
import json
import os
from pathlib import Path
import shutil
import signal
import time
import sys

wire_in, wire_out = sys.stdin, sys.stdout
counter = 0

def send(value):
    wire_out.write(json.dumps(value, ensure_ascii=False) + '\n')
    wire_out.flush()

class Drone:
    def __getattr__(self, method):
        def call(*args, **kwargs):
            global counter
            counter += 1
            send({'type':'rpc','id':counter,'method':method,'args':args,'kwargs':kwargs})
            line = wire_in.readline(8_000_001)
            if not line: raise RuntimeError('Host closed the task')
            result = json.loads(line)
            if result.get('id') != counter: raise RuntimeError('RPC sequence error')
            if result.get('error'): raise RuntimeError(result['error'])
            return result.get('result')
        return call

def main():
    request = json.loads(wire_in.readline(500_001))
    code = request['code']
    # User printing is debug output on stderr; stdout is the bounded RPC channel.
    sys.stdout = sys.stderr
    scope = {'__builtins__':builtins.__dict__, 'drone':Drone(), '__name__':'__task__'}
    result = None
    try:
        # Full Python is deliberate: OS/container isolation is the boundary.
        exec(compile(code, '<model-program>', 'exec'), scope, scope)
        result = {'type':'done','ok':True}
    except BaseException as exc:
        result = {'type':'done','ok':False,'error':type(exc).__name__ + ': ' + str(exc)[:1000]}
    send(dict(result, worker_pid=os.getpid()))


def cleanup():
    # The exec client can stay attached while a child inherits its pipes.
    # Observe the actual interpreter PID rather than waiting for that client.
    worker = Path('/proc') / sys.argv[2]
    until = time.monotonic() + 2
    while worker.exists():
        try:
            if worker.joinpath('stat').read_text().split(') ', 1)[1].startswith('Z'): break
        except FileNotFoundError: break
        if time.monotonic() >= until: raise RuntimeError('sandbox_worker_still_running')
        time.sleep(.01)
    # Per-program processes and writable scratch never survive into the next run.
    # PID 1 is the resident reaper; this exec worker is otherwise alone.
    deadline = time.monotonic() + 1
    while True:
        others = []
        for entry in Path('/proc').iterdir():
            if entry.name.isdigit() and int(entry.name) not in (1, os.getpid()):
                try:
                    if entry.joinpath('stat').read_text().split(') ', 1)[1].startswith('Z'):
                        continue
                    others.append(int(entry.name))
                    os.kill(int(entry.name), signal.SIGKILL)
                except ProcessLookupError: pass
                except FileNotFoundError: pass
        if not others: break
        if time.monotonic() >= deadline: raise RuntimeError('sandbox_descendants_unconfirmed')
        time.sleep(.01)
    for folder in ('/tmp', '/dev/shm'):
        for entry in Path(folder).iterdir():
            if entry.is_dir() and not entry.is_symlink(): shutil.rmtree(entry)
            else: entry.unlink(missing_ok=True)

if __name__ == '__main__':
    cleanup() if sys.argv[1:2] == ['--cleanup'] else main()
