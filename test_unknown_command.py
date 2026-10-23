"""未知指令安全兜底回归测试（不连飞机、不联网、不发声）。

验收两条硬规则：
  1. 听不懂 / 不在动作库 / 大模型返回非法格式 → **绝不执行任何动作**；
  2. 必须给出统一的语音回复话术「抱歉，我没听清，请再说一遍」。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
AI_DIR = os.path.join(ROOT, 'modules', 'ai')
if AI_DIR not in sys.path:
    sys.path.insert(0, AI_DIR)

from ai_agent import NO_UNDERSTAND, Agent, PROMPT      # noqa: E402
from demo_text import Demo                             # noqa: E402
from drone_adapter import MockDrone                    # noqa: E402
from program_runner import Runner                      # noqa: E402


class Fake:
    """按预设返回内容的假模型客户端。"""

    def __init__(self, reply):
        self.reply = reply
        self.last_usage = {'total_tokens': 7}

    def complete(self, messages):
        return self.reply


class UnknownCommandSafetyTest(unittest.TestCase):
    def test_prompt_contains_hard_rule(self):
        self.assertIn('抱歉，我没听清，请再说一遍', PROMPT)
        self.assertIn('禁止输出 kind=program', PROMPT)

    def test_demo_unknown_does_nothing_and_asks_back(self):
        drone = MockDrone()
        demo = Demo(drone)
        before = dict(drone.get_status())
        reply = demo.handle('原定')            # 真实的误识别文本
        self.assertIn('抱歉，我没听清，请再说一遍', reply)
        self.assertIn('未识别', reply)
        self.assertFalse(drone.flying)
        self.assertIsNone(drone.owner)
        self.assertEqual(before['battery'], drone.get_status()['battery'])

    def test_agent_invalid_json_does_nothing(self):
        agent = Agent(Fake('我不知道你在说什么'), Runner())
        result = agent.handle('原定')
        self.assertFalse(result['execution']['accepted'])
        self.assertEqual(result['plan'], {'kind': 'invalid'})
        self.assertIn(NO_UNDERSTAND, result['reply'])
        self.assertEqual(result['spoken'], NO_UNDERSTAND)
        self.assertFalse(agent.runner.demo.drone.flying)

    def test_agent_out_of_whitelist_code_is_blocked(self):
        # 模型自作主张生成未授权接口：白名单必须拦下，且回复统一话术
        agent = Agent(Fake('{"kind":"program","code":"drone.emergency()"}'), Runner())
        result = agent.handle('帮我翻个跟头')
        self.assertFalse(result['execution']['accepted'])
        self.assertIn('未执行动作', result['reply'])
        self.assertEqual(result['spoken'], NO_UNDERSTAND)
        self.assertFalse(agent.runner.demo.drone.flying)

    def test_agent_multi_statement_is_blocked(self):
        agent = Agent(Fake('{"kind":"program","code":"drone.takeoff(); drone.land()"}'), Runner())
        result = agent.handle('起飞然后降落')
        self.assertFalse(result['execution']['accepted'])
        self.assertEqual(result['spoken'], NO_UNDERSTAND)
        self.assertFalse(agent.runner.demo.drone.flying)

    def test_agent_reply_text_is_spoken_without_prefix(self):
        agent = Agent(Fake('{"kind":"reply","text":"抱歉，我没听清，请再说一遍"}'), Runner())
        result = agent.handle('原定')
        self.assertEqual(result['spoken'], '抱歉，我没听清，请再说一遍')
        self.assertIn('未执行动作', result['reply'])   # 控制台仍保留技术前缀
        self.assertFalse(result['execution']['accepted'])

    def test_valid_action_still_works(self):
        agent = Agent(Fake('{"kind":"program","code":"drone.get_battery()"}'), Runner())
        result = agent.handle('还有多少电')
        self.assertTrue(result['execution']['accepted'])
        self.assertIsNone(result['spoken'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
