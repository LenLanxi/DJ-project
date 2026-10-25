"""Interpret a tiny Python subset; never eval/exec model output. Mock only."""
import ast
from demo_text import Demo
from drone_adapter import MockDrone

COMMANDS = {'get_status': '查看状态', 'get_battery': '查看电量',
            'takeoff': '起飞', 'land': '降落', 'start_follow': '跟着我',
            'stop_follow': '停止跟随', 'stop_motion': '停在这里',
            'move_forward': '前进30厘米', 'move_up': '升高30厘米',
            'move_down': '降低30厘米'}

# 允许带"厘米"参数的接口（20~100cm，与 move_forward 同一限制）
DISTANCE_COMMANDS = ('move_forward', 'move_up', 'move_down')

def validate(code):
    if not isinstance(code, str) or len(code) > 2000:
        raise ValueError('程序为空或过长')
    tree = ast.parse(code)
    if len(tree.body) != 1:
        raise ValueError('当前每次只接受一个接口调用')
    node = tree.body[0]
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        raise ValueError('只允许直接调用接口')
    call = node.value
    if not (isinstance(call.func, ast.Attribute) and
            isinstance(call.func.value, ast.Name) and call.func.value.id == 'drone' and
            call.func.attr in COMMANDS and not call.keywords):
        raise ValueError('接口不在允许列表')
    name = call.func.attr
    if name in DISTANCE_COMMANDS:
        if not (len(call.args) == 1 and isinstance(call.args[0], ast.Constant)
                and type(call.args[0].value) is int and 20 <= call.args[0].value <= 100):
            raise ValueError('移动类接口只接受 20~100 厘米的整数参数')
    elif call.args:
        raise ValueError('此接口不接受参数')
    return name

class Runner:
    def __init__(self, demo=None, *, allow_simulated_sdk=False, allow_programs=False):
        self.demo = demo or Demo()
        self.allow_programs=allow_programs
        allowed = type(self.demo.drone) is MockDrone
        if allow_simulated_sdk:
            from drone_sdk.ai_bridge import UnifiedDrone
            from drone_sdk import controller
            allowed = type(self.demo.drone) is UnifiedDrone and controller.SIMULATED is True
        if not allowed:
            raise ValueError('此版本执行器仅允许本地模拟器')

    def run_sandbox(self, code):
        # Parse for syntax only; generated programs execute exclusively inside Docker.
        try:
            if not isinstance(code,str) or not code.strip(): raise ValueError('empty_program')
            ast.parse(code)
        except (SyntaxError,ValueError,TypeError):
            return {'accepted':False,'ok':False,'status':'failed','result':'invalid_program'}
        import asyncio
        from backends import MockBackend,SDKBackend
        from gateway import Gateway
        from sandbox_runner import SandboxRunner
        from pathlib import Path
        drone=self.demo.drone
        if type(drone) is MockDrone:
            backend=MockBackend()
            backend.flying=drone.flying
        else:
            backend=SDKBackend(Path(__file__).resolve().parents[2])
        gateway=Gateway(backend,getattr(self,'structured_vision',None))
        result=asyncio.run(SandboxRunner(gateway).run(code))
        if type(drone) is MockDrone:
            drone.flying=backend.flying
        return {'accepted':True,'ok':result['ok'],'status':result['status'],
                'simulated':backend.simulated,'result':str(result),'execution':result,
                'state':drone.get_status()}

    def run(self, code):
        try:
            tree=ast.parse(code) if isinstance(code,str) else None
            legacy=(tree is not None and len(tree.body)==1 and isinstance(tree.body[0],ast.Expr)
                    and isinstance(tree.body[0].value,ast.Call)
                    and isinstance(tree.body[0].value.func,ast.Attribute)
                    and tree.body[0].value.func.attr in COMMANDS)
        except (SyntaxError,ValueError,TypeError): legacy=True
        if not legacy:
            if not self.allow_programs: return {'accepted':False,'result':'程序检查未通过，未执行动作'}
            return self.run_sandbox(code)
        try:
            name = validate(code)
        except (SyntaxError, ValueError, RecursionError):
            return {'accepted': False, 'simulated': True, 'result': '程序检查未通过，未执行动作'}
        if name=='move_forward':
            distance=ast.parse(code).body[0].value.args[0].value
            command='前进%d厘米' % distance
        else: command=COMMANDS[name]
        return {'accepted': True, 'simulated': self.demo.drone.get_status().get('simulated', True),
                'result': self.demo.handle(command),
                'state': self.demo.drone.get_status()}
