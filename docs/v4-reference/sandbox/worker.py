"""Runs ONLY inside the sandbox image. Never launch this on the host with model code."""
import builtins
import json
import sys
import traceback

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
    try:
        # Full Python is deliberate: OS/container isolation is the boundary.
        exec(compile(code, '<model-program>', 'exec'), scope, scope)
        send({'type':'done','ok':True})
    except BaseException as exc:
        send({'type':'done','ok':False,'error':type(exc).__name__ + ': ' + str(exc)[:1000]})

if __name__ == '__main__': main()
