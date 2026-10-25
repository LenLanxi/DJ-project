"""播报文本：TTS 只能念人话，绝不能把 status 字典原样念出去。

实测踩过：`查看状态` 直接返回 json.dumps(status)，SAPI 就把
"bracket 真机 battery colon 94 comma height_cm..." 一整串英文键名念了出来。
"""
import unittest

from ai_agent import speech_text
from demo_text import Demo
from drone_adapter import MockDrone


class RealLikeDrone(MockDrone):
    """模仿真机状态：真实电量、真实高度、simulated=False。"""

    def get_status(self):
        status = dict(super().get_status())
        status.update(battery=94, height_cm=0, connected=True, flying=False, simulated=False)
        return status


class StatusSummaryTests(unittest.TestCase):
    def setUp(self):
        self.demo = Demo(RealLikeDrone())

    def test_describe_is_chinese_sentence(self):
        text = self.demo.describe()
        self.assertIn('当前电量 94%', text)
        self.assertIn('高度 0 厘米', text)
        self.assertIn('未起飞', text)
        self.assertTrue(text.endswith('。'))
        self.assertNotIn('{', text)
        self.assertNotIn('battery', text)

    def test_status_command_has_no_json(self):
        reply = self.demo.handle('查看状态')
        self.assertNotIn('{', reply)
        self.assertNotIn('"battery"', reply)
        self.assertIn('当前电量', reply)

    def test_battery_command_stays_short(self):
        reply = self.demo.handle('查看电量')
        self.assertIn('94%', reply)
        self.assertNotIn('{', reply)

    def test_flying_state_is_reported(self):
        class FlyingDrone(RealLikeDrone):
            def get_status(self):
                status = dict(super().get_status())
                status.update(flying=True, height_cm=120)
                return status
        text = Demo(FlyingDrone()).describe()
        self.assertIn('正在飞行', text)
        self.assertIn('高度 120 厘米', text)


class SpeechTextTests(unittest.TestCase):
    def test_bracket_prefix_is_dropped(self):
        self.assertEqual(speech_text('[真机] 已开始跟随'), '已开始跟随')
        self.assertEqual(speech_text('[模拟] 当前电量为88%。'), '当前电量为88%。')
        self.assertEqual(speech_text('[AI对话；未执行动作] 好的'), '好的')

    def test_raw_json_is_never_spoken(self):
        self.assertEqual(speech_text('[真机] {"battery": 94}'), '状态已显示在屏幕上')
        self.assertEqual(speech_text('{"battery": 94}'), '状态已显示在屏幕上')

    def test_plain_text_passes_through(self):
        self.assertEqual(speech_text('当前电量 94%，高度 0 厘米'), '当前电量 94%，高度 0 厘米')

    def test_explicit_spoken_wins(self):
        self.assertEqual(speech_text('[真机] 长回复文本', '短句'), '短句')

    def test_empty_input_is_safe(self):
        self.assertEqual(speech_text(None), '')
        self.assertEqual(speech_text(''), '')

    def test_unknown_command_does_not_read_the_help_list(self):
        # 早先这里把 HELP 整串拼进去，TTS 会把所有可用指令念一遍
        demo = Demo(RealLikeDrone())
        reply = demo.handle('查看天气')
        self.assertIn('没听清', reply)
        self.assertNotIn('可输入', reply)
        self.assertLess(len(speech_text(reply)), 40)


if __name__ == '__main__':
    unittest.main()
