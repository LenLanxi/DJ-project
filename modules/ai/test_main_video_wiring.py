"""两个窗口：终端 + 图传。两条语音链路都必须挂上图传查看器。

踩过的坑：豆包/Realtime 走 run_modern，没有经典入口的轮播循环，`--show-video`
被静默忽略 —— 现场只剩一个终端窗口，三组跟随的绿框判读全没了。
"""
import unittest
from pathlib import Path

import main


def source():
    return Path(main.__file__).read_text(encoding='utf-8')


class VideoWiringTests(unittest.TestCase):
    def setUp(self):
        self.text = source()

    def _block(self, start_marker, end_marker):
        start = self.text.index(start_marker)
        end = self.text.index(end_marker, start)
        return self.text[start:end]

    def test_modern_path_starts_and_stops_viewer(self):
        block = self._block('async def run_modern', 'def main()')
        self.assertIn('start_view_monitor', block)
        self.assertIn('stop_view_monitor', block)

    def test_classic_path_starts_viewer(self):
        block = self._block('def _dispatch', "if __name__")
        self.assertIn('start_view_monitor', block)

    def test_viewer_adds_repo_root_before_importing_sdk(self):
        # 入口可能还没把仓库根加进 sys.path，否则报 No module named 'drone_sdk'
        block = self._block('def start_view_monitor', 'def stop_view_monitor')
        self.assertIn("sys.path.insert(0, _root)", block)

    def test_viewer_is_idempotent_and_stoppable(self):
        self.assertEqual(main.start_view_monitor(printer=lambda *_: None), True)
        main.stop_view_monitor()
        self.assertIsNone(main._VIEW['thread'])


if __name__ == '__main__':
    unittest.main()
