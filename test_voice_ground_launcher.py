"""AI 语音地面测试的护栏：启动器不许授权起飞，自检项必须和真入口对得上。

这两个断言都很朴素，但都是被真实事故逼出来的：
- 四组的 main.py 整包覆盖过一次，2组 补的参数（--offline-voice/--log-file 等）全部消失；
- 地面测试如果哪天不小心带上 --allow-real-takeoff，桨叶拔着的飞机就会原地起飞。
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LAUNCHER = ROOT / 'start_voice_ground_test.ps1'
CMD_WRAPPER = ROOT / 'start_voice_ground_test.cmd'
CHECKER = ROOT / 'tools' / 'voice_ground_check.py'
ENTRY = ROOT / 'modules' / 'ai' / 'main.py'


def load_checker():
    spec = importlib.util.spec_from_file_location('voice_ground_check', CHECKER)
    module = importlib.util.module_from_spec(spec)
    sys.modules['voice_ground_check'] = module
    spec.loader.exec_module(module)
    return module


class LauncherFilesTests(unittest.TestCase):
    def test_launcher_and_wrapper_exist(self):
        self.assertTrue(LAUNCHER.is_file(), '缺少 start_voice_ground_test.ps1')
        self.assertTrue(CMD_WRAPPER.is_file(), '缺少 start_voice_ground_test.cmd')
        self.assertTrue(CHECKER.is_file(), '缺少 tools/voice_ground_check.py')

    def test_wrapper_forwards_arguments(self):
        # cmd 包装必须把 %* 透传给 ps1，否则 -ListDevices / -InputDevice 全部失效
        self.assertIn('%*', CMD_WRAPPER.read_text(encoding='utf-8'))


class LauncherSafetyTests(unittest.TestCase):
    def setUp(self):
        text = LAUNCHER.read_text(encoding='utf-8-sig')
        start = text.index('$argv = @(')
        end = text.index("if ($argv -contains '--allow-real-takeoff')", start)
        self.argv_block = text[start:end]
        self.full_text = text

    def test_argv_never_authorizes_takeoff(self):
        self.assertNotIn("'--allow-real-takeoff'", self.argv_block,
                         '地面测试的启动参数里出现了 --allow-real-takeoff')

    def test_has_guard_that_strips_takeoff_authorization(self):
        self.assertIn("Where-Object { $_ -ne '--allow-real-takeoff' }", self.full_text)

    def test_default_mode_is_offline_voice_and_always_on(self):
        self.assertIn("'--offline-voice'", self.argv_block)
        self.assertIn("'--always-on'", self.argv_block)

    def test_connects_real_drone(self):
        self.assertIn("'--real-drone'", self.argv_block)

    def test_writes_a_log_file(self):
        self.assertIn("'--log-file'", self.argv_block)


class CheckerMatchesEntryTests(unittest.TestCase):
    def test_required_flags_still_exist_in_entry(self):
        checker = load_checker()
        text = ENTRY.read_text(encoding='utf-8', errors='replace')
        missing = [flag for flag in checker.REQUIRED_FLAGS if flag not in text]
        self.assertEqual(missing, [], '实飞入口缺少这些参数：%s' % ', '.join(missing))

    def test_reporting_counts_failures(self):
        checker = load_checker()
        checker.results.clear()
        self.assertTrue(checker.report(True, 'ok', 'detail'))
        self.assertFalse(checker.report(False, 'bad', 'detail'))
        statuses = [row[0] for row in checker.results]
        self.assertEqual(statuses, ['PASS', 'FAIL'])
        checker.results.clear()

    def test_bluetooth_hint_matches_common_names(self):
        checker = load_checker()
        self.assertTrue(checker._looks_bluetooth('WH-1000XM4 (Bluetooth)'))
        self.assertTrue(checker._looks_bluetooth('蓝牙耳机'))
        self.assertFalse(checker._looks_bluetooth('本机扬声器 (Audio Device)'))


if __name__ == '__main__':
    unittest.main()
