"""Read-only fake-device query runner shared by opt-in cloud benchmarks."""
import ast
import json


def status_query_only(code):
    try:
        tree = ast.parse(code)
    except (SyntaxError, TypeError):
        return False
    found = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.With, ast.AsyncWith)):
            return False
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == 'print':
                continue
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id == 'drone' and func.attr == 'get_status'
                    and not node.args and not node.keywords):
                found = True
            else:
                return False
    return found


class QueryOnlyRunner:
    def __init__(self, real_sandbox=False, timeout=15):
        self.calls, self.interrupts, self.cancel_count = [], [], 0
        self.container_rpc = False
        self.sandbox = self.runtime = None
        if real_sandbox:
            from execution.sandbox_runner import SandboxRunner
            from Support import MockBackend
            from app.Runtime import UnifiedRuntime
            self.sandbox = SandboxRunner(timeout=timeout)
            device = MockBackend()
            self.runtime = UnifiedRuntime(device, device, runner=self.sandbox)

    async def run(self, code):
        allowed = status_query_only(code)
        self.calls.append({'code': code, 'allowed_status_query': allowed})
        if not allowed:
            return {'ok': False, 'status': 'failed', 'simulated': True,
                    'reason': 'real_test_only_allows_status_query_no_code_execution'}
        if self.sandbox is not None:
            result = await self.runtime.run(code)
            self.container_rpc = any(entry.get('method') == 'get_status'
                                     and 'result' in entry for entry in result.get('journal', []))
            return result
        state = {'battery': 73, 'flight_state': 'landed', 'owner': None,
                 'simulated': True, 'source': 'test_fixture_not_a_real_drone'}
        return {'ok': True, 'status': 'completed', 'simulated': True,
                'journal': [{'method': 'get_status', 'args': [], 'kwargs': {}, 'result': state}],
                'debug': json.dumps(state, ensure_ascii=False)}

    def cancel(self):
        self.cancel_count += 1
        if self.sandbox is not None:
            self.runtime.cancel()

    async def interrupt(self, land=False):
        self.interrupts.append(bool(land))
        if self.sandbox is not None:
            self.cancel()
            return await self.runtime.interrupt(bool(land))
        return {'ok': True, 'status': 'interrupted', 'land': bool(land),
                'simulated': True, 'source': 'test_fixture_not_a_real_drone'}
