import unittest
from .demo_text import Demo

class ControlFlowTests(unittest.TestCase):
    def flying_follow(self):
        demo = Demo()
        demo.handle('起飞')
        demo.handle('跟着我')
        return demo

    def test_read_does_not_interrupt(self):
        d = self.flying_follow()
        self.assertIn('80%', d.handle('查看电量'))
        self.assertTrue(d.drone.following)
        self.assertEqual(d.drone.owner, 'follow')

    def test_temporary_move_resumes(self):
        d = self.flying_follow()
        d.handle('前进30厘米')
        self.assertTrue(d.drone.following)
        self.assertEqual(d.drone.owner, 'follow')

    def test_stop_and_land_do_not_resume(self):
        for command in ('停在这里', '停止跟随', '降落'):
            d = self.flying_follow()
            d.handle(command)
            self.assertFalse(d.drone.following)
            self.assertIsNone(d.drone.owner)
        self.assertFalse(d.drone.flying)

    def test_failure_no_resume(self):
        d = self.flying_follow()
        d.drone.fail_next_move = True
        self.assertIn('失败', d.handle('前进30厘米'))
        self.assertFalse(d.drone.following)
        self.assertIsNone(d.drone.owner)

    def test_target_lost_no_resume(self):
        d = self.flying_follow()
        d.drone.target_visible = False
        self.assertIn('未恢复', d.handle('前进30厘米'))
        self.assertFalse(d.drone.following)

    def test_manual_owner_blocks_model(self):
        d = Demo()
        d.drone.request_control('manual')
        self.assertIn('未执行', d.handle('起飞'))
        self.assertFalse(d.drone.flying)
        self.assertEqual(d.drone.owner, 'manual')

    def test_unknown_and_grounded(self):
        d = Demo()
        self.assertIn('未识别', d.handle('随便飞一下'))
        self.assertIn('未起飞', d.handle('前进30厘米'))

if __name__ == '__main__':
    unittest.main()
