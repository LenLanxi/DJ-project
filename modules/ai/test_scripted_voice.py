"""离线语音口令归一化：实飞现场口语带标点也要能命中固定口令。"""
import json
import unittest

from ai_agent import Agent
from demo_ai import ScriptedClient, scripted_action


class ScriptedActionTests(unittest.TestCase):
    def test_canonical_phrases_still_work(self):
        self.assertEqual(scripted_action('起飞'), 'takeoff()')
        self.assertEqual(scripted_action('跟着我'), 'start_follow()')
        self.assertEqual(scripted_action('降落'), 'land()')
        self.assertEqual(scripted_action('还有多少电'), 'get_battery()')
        self.assertEqual(scripted_action('向前走30厘米'), 'move_forward(30)')

    def test_punctuation_and_filler_are_tolerated(self):
        for text in ('起飞。', '起飞！', '好的，起飞', '起飞吧', '开始起飞'):
            with self.subTest(text=text):
                self.assertEqual(scripted_action(text), 'takeoff()')
        for text in ('跟着我。', '跟随我！', '开始跟随', '请你跟着我'):
            with self.subTest(text=text):
                self.assertEqual(scripted_action(text), 'start_follow()')
        self.assertEqual(scripted_action('降落。'), 'land()')
        self.assertEqual(scripted_action('  还有多少电？ '), 'get_battery()')

    def test_stop_phrases_win_over_start_follow(self):
        self.assertEqual(scripted_action('停止跟随'), 'stop_follow()')
        self.assertEqual(scripted_action('别跟着我了'), 'stop_follow()')
        self.assertEqual(scripted_action('停在这里'), 'stop_motion()')
        self.assertEqual(scripted_action('悬停'), 'stop_motion()')
        self.assertEqual(scripted_action('急停'), 'stop_motion()')

    def test_unknown_still_declines(self):
        for text in ('今天天气怎么样', '', '   ', '帮我写个总结'):
            with self.subTest(text=text):
                self.assertIsNone(scripted_action(text))
                reply = json.loads(ScriptedClient().complete(
                    [{'role': 'user', 'content': '{"request": "%s"}' % text}]))
                self.assertEqual(reply['kind'], 'reply')
                self.assertIn('没有这个样例', reply['text'])

    def test_agent_executes_punctuated_voice_commands(self):
        agent = Agent(ScriptedClient())
        agent.handle('起飞。')
        self.assertTrue(agent.runner.demo.drone.flying)
        agent.handle('跟着我！')
        self.assertTrue(agent.runner.demo.drone.following)
        agent.handle('停止跟随')
        self.assertFalse(agent.runner.demo.drone.following)
        agent.handle('降落。')
        self.assertFalse(agent.runner.demo.drone.flying)


if __name__ == '__main__':
    unittest.main()
