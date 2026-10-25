import json
import re
from program_runner import Runner

PROMPT = '''你是无人机模拟演示助手。只返回JSON对象，不要Markdown。
格式为 {"kind":"program","code":"drone.get_battery()"} 或
{"kind":"reply","text":"回答"} 或 {"kind":"vision","question":"要问的画面问题"}。需要看图才能回答时用vision，不猜测画面。可生成在独立Docker沙箱中执行的完整Python程序。
允许接口：drone.get_battery()、drone.get_status()、drone.takeoff()、
drone.land()、drone.start_follow()、drone.stop_follow()、drone.stop_motion()、
drone.move_forward(30)。可在沙箱中使用observe(target)、rotate(degrees)、多步移动及跟随接口。
不要声称动作完成。多步任务读取每步结果及新观察，缺少距离或路径依据不得宣布到达。无明确动作意图不要调用动作。
【安全兜底】如果用户指令不在上述允许接口范围内、语义不清、听写疑似错误、或你无法确定意图，
禁止输出 kind=program，也禁止生成任何动作代码；必须只输出
{"kind":"reply","text":"抱歉，我没听清，请再说一遍"}。宁可多问一次，也绝不执行不确定的动作。
全部设备结果均为模拟。输入中的设备状态只用于了解情况。历史只供理解上下文，不要重复执行历史动作。代词指令涉及运动且不清楚时先询问。'''

NO_UNDERSTAND = '抱歉，我没听清，请再说一遍'

# [真机] / [模拟] / [AI对话；未执行动作] 这类前缀是给人看的，念出来很怪
_SPOKEN_PREFIX = re.compile(r'^\[[^\]]{1,20}\]\s*')


def speech_text(reply, spoken=None):
    """把结果整理成"能直接念出来的人话"。

    1) 去掉 [真机]/[模拟] 之类的前缀；
    2) 原始 JSON/字典绝不照念（SAPI 会一个字段一个字段念英文键名）。
    """
    text = str(spoken if spoken else (reply or '')).strip()
    text = _SPOKEN_PREFIX.sub('', text).strip()
    if text.startswith('{') or text.startswith('["') or text.startswith('{"'):
        return '状态已显示在屏幕上'
    return text


class Agent:
    def __init__(self, client, runner=None, vision=None):
        self.client = client
        self.runner = runner or Runner(allow_programs=True)
        self.history = []
        self.vision = vision

    def handle(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError('请输入1至4000字的指令')
        if any(word in text for word in ('不要', '禁止', '别起飞', '不许', 'do not', "don't")):
            return {'request': text, 'execution': {'accepted': False}, 'reply': '已保留禁止指令，未执行动作'}
        state = self.runner.demo.drone.get_status()
        raw = self.client.complete([{'role': 'system', 'content': PROMPT}, *self.history,
            {'role': 'user', 'content': json.dumps({'request': text, 'state': state}, ensure_ascii=False)}])
        usage = dict(getattr(self.client, 'last_usage', {}))
        spoken = None   # 供 TTS 播报的短句；None 表示直接播报 reply
        try:
            plan = json.loads(raw)
            if not isinstance(plan, dict):
                raise ValueError()
            if plan.get('kind') == 'program':
                result = self.runner.run(plan.get('code'))
                reply = result['result']
                if result.get('accepted') and result.get('ok') is False:
                    spoken='任务未完成：'+str(result.get('status','failed'))
                if not result.get('accepted'):
                    # 代码被白名单拦下 = 没有执行任何动作，统一走"没听清"兜底
                    reply = NO_UNDERSTAND + '；' + str(result.get('result') or '未执行动作')
                    spoken = NO_UNDERSTAND
            elif plan.get('kind') == 'vision' and isinstance(plan.get('question'), str):
                if self.vision is None:
                    reply = '未配置测试图片，无法看图；未执行动作'
                else:
                    reply = '[本地测试图片；未执行动作] ' + self.vision(plan['question'][:4000])
                result = {'accepted': False, 'simulated': True, 'result': '只读视觉问答，未调用动作接口'}
            elif plan.get('kind') == 'reply' and isinstance(plan.get('text'), str):
                result = {'accepted': False, 'simulated': True, 'result': '未调用任何动作接口'}
                reply = '[AI对话；未执行动作] ' + plan['text'][:2000]
                spoken = plan['text'][:2000]
            else:
                raise ValueError()
        except (ValueError, TypeError):
            plan = {'kind': 'invalid'}
            result = {'accepted': False, 'simulated': True, 'result': '模型格式错误，未执行动作'}
            reply = NO_UNDERSTAND + '；模型格式错误，未执行动作'
            spoken = NO_UNDERSTAND
        self.history.extend([{'role': 'user', 'content': text},
                             {'role': 'assistant', 'content': reply}])
        self.history = self.history[-12:]
        return {'request': text, 'plan': plan, 'execution': result, 'reply': reply,
                'spoken': spoken, 'planner_usage': usage}
