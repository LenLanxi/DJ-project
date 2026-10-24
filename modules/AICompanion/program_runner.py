"""Interpret a tiny Python subset; never eval/exec model output. Mock only."""
import ast
from demo_text import Demo
from drone_adapter import MockDrone

COMMANDS = {'get_status': '查看状态', 'get_battery': '查看电量',
            'takeoff': '起飞', 'land': '降落', 'start_follow': '跟着我',
            'stop_follow': '停止跟随', 'stop_motion': '停在这里',
            'move_forward': '前进30厘米'}

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
    if name == 'move_forward':
        if not (len(call.args) == 1 and isinstance(call.args[0], ast.Constant)
                and type(call.args[0].value) is int and call.args[0].value == 30):
            raise ValueError('当前演示仅支持前进30厘米')
    elif call.args:
        raise ValueError('此接口不接受参数')
    return name

class Runner:
    def __init__(self, demo=None):
        self.demo = demo or Demo()
        if type(self.demo.drone) is not MockDrone:
            raise ValueError('此版本执行器仅允许本地模拟器')

    def run(self, code):
        try:
            name = validate(code)
        except (SyntaxError, ValueError, RecursionError):
            return {'accepted': False, 'simulated': True, 'result': '程序检查未通过，未执行动作'}
        return {'accepted': True, 'simulated': True,
                'result': self.demo.handle(COMMANDS[name]),
                'state': self.demo.drone.get_status()}
