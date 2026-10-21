import json
from program_runner import Runner

PROMPT = '''你是无人机模拟演示助手。只返回JSON对象，不要Markdown。
格式为 {"kind":"program","code":"drone.get_battery()"} 或
{"kind":"reply","text":"回答"} 或 {"kind":"vision","question":"要问的画面问题"}。需要看图才能回答时用vision，不猜测画面。只能生成一个Python调用表达式。
允许接口：drone.get_battery()、drone.get_status()、drone.takeoff()、
drone.land()、drone.start_follow()、drone.stop_follow()、drone.stop_motion()、
drone.move_forward(30)。前进仅支持30厘米。其他动作请明确说明暂不支持。
不要声称动作完成。含多个动作的要求请让用户分次提出。无明确动作意图不要调用动作。
全部设备结果均为模拟。输入中的设备状态只用于了解情况。历史只供理解上下文，不要重复执行历史动作。代词指令涉及运动且不清楚时先询问。'''

class Agent:
    def __init__(self, client, runner=None, vision=None):
        self.client = client
        self.runner = runner or Runner()
        self.history = []
        self.vision = vision

    def handle(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError('请输入1至4000字的指令')
        state = self.runner.demo.drone.get_status()
        raw = self.client.complete([{'role': 'system', 'content': PROMPT}, *self.history,
            {'role': 'user', 'content': json.dumps({'request': text, 'state': state}, ensure_ascii=False)}])
        usage = dict(getattr(self.client, 'last_usage', {}))
        try:
            plan = json.loads(raw)
            if not isinstance(plan, dict):
                raise ValueError()
            if plan.get('kind') == 'program':
                result = self.runner.run(plan.get('code'))
                reply = result['result']
            elif plan.get('kind') == 'vision' and isinstance(plan.get('question'), str):
                if self.vision is None:
                    reply = '未配置测试图片，无法看图；未执行动作'
                else:
                    reply = '[本地测试图片；未执行动作] ' + self.vision(plan['question'][:4000])
                result = {'accepted': False, 'simulated': True, 'result': '只读视觉问答，未调用动作接口'}
            elif plan.get('kind') == 'reply' and isinstance(plan.get('text'), str):
                result = {'accepted': False, 'simulated': True, 'result': '未调用任何动作接口'}
                reply = '[AI对话；未执行动作] ' + plan['text'][:2000]
            else:
                raise ValueError()
        except (ValueError, TypeError):
            plan = {'kind': 'invalid'}
            result = {'accepted': False, 'simulated': True, 'result': '模型格式错误，未执行动作'}
            reply = result['result']
        self.history.extend([{'role': 'user', 'content': text},
                             {'role': 'assistant', 'content': reply}])
        self.history = self.history[-12:]
        return {'request': text, 'plan': plan, 'execution': result, 'reply': reply, 'planner_usage': usage}
