import json
import unittest
from ai_agent import Agent
from program_runner import Runner
from demo_ai import ScriptedClient

class Fake:
    def __init__(self, value): self.value = value
    def complete(self, messages): return self.value

class AgentTests(unittest.TestCase):
    def test_full_flow(self):
        a = Agent(ScriptedClient())
        for s in ('起飞', '跟着我', '向前走30厘米'):
            a.handle(s)
        self.assertTrue(a.runner.demo.drone.following)
        a.handle('降落')
        self.assertFalse(a.runner.demo.drone.flying)

    def test_reject_code_without_side_effects(self):
        for code in ('import os', 'drone.takeoff(); drone.land()',
                     'drone.__class__()', 'drone.move_forward(True)',
                     'drone.move_forward(100)', 'drone.takeoff(x=1)',
                     'drone.takeoff(*[])', 'while True: pass',
                     'x = drone.takeoff()', None):
            with self.subTest(code=code):
                r = Runner()
                self.assertFalse(r.run(code)['accepted'])
                self.assertFalse(r.demo.drone.flying)

    def test_invalid_model_json(self):
        for raw in ('not json', '[]', '{"kind":"program","code":null}'):
            a = Agent(Fake(raw))
            self.assertFalse(a.handle('起飞')['execution']['accepted'])
            self.assertFalse(a.runner.demo.drone.flying)

    def test_busy_result_not_success(self):
        a = Agent(ScriptedClient())
        a.runner.demo.drone.request_control('manual')
        self.assertIn('未执行动作', a.handle('起飞')['reply'])

    def test_failure_no_resume(self):
        a = Agent(ScriptedClient())
        a.handle('起飞'); a.handle('跟着我')
        a.runner.demo.drone.fail_next_move = True
        self.assertIn('失败', a.handle('向前走30厘米')['reply'])
        self.assertFalse(a.runner.demo.drone.following)

    def test_model_error_no_action(self):
        class Broken:
            def complete(self, messages): raise RuntimeError('timeout')
        a = Agent(Broken())
        with self.assertRaises(RuntimeError): a.handle('起飞')
        self.assertFalse(a.runner.demo.drone.flying)

    def test_reply_no_action(self):
        a = Agent(Fake(json.dumps({'kind':'reply','text':'你好'})))
        self.assertIn('未执行动作', a.handle('你好')['reply'])

if __name__ == '__main__': unittest.main()
