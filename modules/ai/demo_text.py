"""固定指令的本地演示；不是AI，不执行生成代码，不支持实机。"""
import json
from drone_adapter import MockDrone

HELP = '可输入：查看电量、查看状态、起飞、跟着我、前进30厘米、停止跟随、停在这里、降落、退出'

class Demo:
    def __init__(self, drone=None):
        self.drone = drone or MockDrone()

    def handle(self, text):
        text = text.strip()
        drone = self.drone
        if text == '查看电量':
            return '[模拟] 当前电量为%d%%。' % drone.get_status()['battery']
        if text == '查看状态':
            return '[模拟] ' + json.dumps(drone.get_status(), ensure_ascii=False)
        if text == '跟着我':
            return '[模拟] ' + ('已开始跟随' if drone.start_follow()['ok'] else '无法开始跟随，请查看状态')
        if text == '停止跟随':
            return '[模拟] ' + drone.stop_follow()['message']
        actions = {
            '起飞': lambda: drone.takeoff('model'),
            '降落': lambda: drone.land('model'),
            '停在这里': lambda: drone.stop_motion('model'),
            '前进30厘米': lambda: drone.move_forward('model', 30),
        }
        if text not in actions:
            # 未知/听写错误的指令：绝不执行动作，并且必须让 TTS 能播报这句反问
            return '抱歉，我没听清，请再说一遍；未识别固定指令，没有执行动作。' + HELP
        was_following = drone.is_following()
        owns_follow = drone.get_status().get('owner') == 'follow'
        ticket = None
        ticket_handoff = hasattr(drone, 'pause_follow_for_action')
        if text in ('降落', '停在这里'):
            # Also invalidate a paused action's ticket when following=False.
            result = drone.stop_follow()
            if not result['ok']:
                return '[模拟] 停止跟随未确认，未继续执行动作'
        if was_following or owns_follow:
            result = (drone.pause_follow_for_action() if ticket_handoff and
                      text == '前进30厘米' else drone.stop_follow())
            ticket = result.get('ticket')
            if not result['ok']:
                return '[模拟] 无法暂停跟随，取消动作'
        result = drone.request_control('model')
        if not result['ok']:
            if ticket is not None:
                drone.finish_follow_action(ticket, 'failed')
            return '[模拟] ' + result['message'] + '；未执行动作'
        result = None
        try:
            result = actions[text]()
            message = result['message']
            if not result['ok']:
                stopped = drone.stop_motion('model')
                message += '；' + stopped['message'] + '；不恢复跟随'
        except Exception as exc:
            result = {'ok': False}
            stopped = drone.stop_motion('model')
            message = '模拟执行异常：' + str(exc) + '；' + stopped['message'] + '；不恢复跟随'
        finally:
            released = drone.release_control('model')
        if result['ok'] and was_following and text == '前进30厘米':
            if not released['ok'] or (ticket_handoff and result.get('status') != 'completed'):
                if ticket is not None:
                    drone.finish_follow_action(ticket, 'failed')
                return '[模拟] ' + message + '；动作完成或控制权释放未确认，不恢复跟随'
            resumed = (drone.finish_follow_action(ticket, 'completed') if ticket_handoff
                       else drone.start_follow())
            message += '；' + ('已恢复模拟跟随' if resumed['ok'] else '未恢复跟随：' + resumed['message'])
        elif ticket is not None:
            drone.finish_follow_action(ticket, 'failed')
        return '[模拟] ' + message

if __name__ == '__main__':
    print('AI随行小伙伴 · 接口流程演示 v0.1')
    print('固定文字指令，无大模型、无语音、无真实无人机连接。')
    print(HELP)
    demo = Demo()
    while True:
        try:
            text = input('你：')
        except (EOFError, KeyboardInterrupt):
            break
        if text.strip() == '退出':
            break
        print('程序：' + demo.handle(text))
