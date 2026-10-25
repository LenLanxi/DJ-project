"""飞行前检查的电量结论：硬门限才拦，固件拒飞线只警告。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from flight_preflight import battery_verdict  # noqa: E402


class BatteryVerdictTests(unittest.TestCase):
    def test_unknown_battery_gives_no_verdict(self):
        self.assertEqual(battery_verdict(None, 10, 50), (None, None))

    def test_below_hard_limit_blocks(self):
        self.assertEqual(battery_verdict(5, 10, 50), ('block:battery_low', None))
        self.assertEqual(battery_verdict(79, 80, 50), ('block:battery_low', None))

    def test_very_low_battery_is_a_loud_warning(self):
        self.assertEqual(battery_verdict(15, 10, 50), (None, 'battery_very_low'))

    def test_between_hard_limit_and_firmware_line_only_warns(self):
        # 今晚遇到的情况：51% 不该被我们拦，但要提醒固件可能拒飞
        self.assertEqual(battery_verdict(51, 10, 50), (None, None))
        self.assertEqual(battery_verdict(30, 10, 50), (None, 'battery_below_firmware_line'))

    def test_healthy_battery_passes_quietly(self):
        self.assertEqual(battery_verdict(68, 10, 50), (None, None))
        self.assertEqual(battery_verdict(100, 10, 50), (None, None))

    def test_hard_limit_still_wins_over_warning(self):
        block, warn = battery_verdict(9, 10, 50)
        self.assertEqual(block, 'block:battery_low')
        self.assertIsNone(warn)


if __name__ == '__main__':
    unittest.main()
