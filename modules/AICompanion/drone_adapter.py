"""临时模拟器，非2组最终接口；不导入DJITelloPy，不连接无人机。"""
from time import time

class MockDrone:
    def __init__(self):
        self.flying = False
        self.following = False
        self.owner = None
        self.battery = 80
        self.target_visible = True
        self.fail_next_move = False

    def result(self, ok, message):
        return dict(ok=ok, message=message, simulated=True)

    def get_status(self):
        return dict(battery=self.battery, height_cm=80 if self.flying else 0,
                    connected=True, flying=self.flying, owner=self.owner,
                    following=self.following, target_visible=self.target_visible,
                    timestamp=time(), simulated=True)

    def request_control(self, owner):
        if owner not in ('model', 'follow', 'manual'):
            return self.result(False, '未知控制者')
        if self.owner not in (None, owner):
            return self.result(False, '控制权被占用：' + self.owner)
        self.owner = owner
        return self.result(True, '已获得模拟控制权')

    def release_control(self, owner):
        if self.owner != owner:
            return self.result(False, '无权释放其他控制者的权限')
        self.owner = None
        return self.result(True, '已释放模拟控制权')

    def takeoff(self, owner):
        if self.owner != owner or self.flying:
            return self.result(False, '无控制权或已经起飞')
        self.flying = True
        return self.result(True, '模拟起飞完成')

    def land(self, owner):
        if self.owner != owner:
            return self.result(False, '无控制权')
        self.flying = self.following = False
        return self.result(True, '模拟降落完成')

    def stop_motion(self, owner):
        if self.owner != owner:
            return self.result(False, '无控制权')
        return self.result(True, '模拟停止运动')

    def move_forward(self, owner, cm):
        if self.owner != owner or not self.flying:
            return self.result(False, '无控制权或未起飞')
        if type(cm) is not int or not 20 <= cm <= 100:
            return self.result(False, '本演示仅允许20至100厘米整数，实机范围待确认')
        if self.fail_next_move:
            self.fail_next_move = False
            return self.result(False, '注入的模拟移动失败')
        return self.result(True, '模拟前进%d厘米完成' % cm)

    def start_follow(self):
        if not self.flying or not self.target_visible:
            return self.result(False, '未起飞或目标不可见，未开始跟随')
        result = self.request_control('follow')
        if result['ok']:
            self.following = True
        return result

    def stop_follow(self):
        if self.following:
            if self.owner != 'follow':
                return self.result(False, '跟随状态与控制权不一致')
            self.following = False
            self.release_control('follow')
        return self.result(True, '模拟跟随已停止')

    def is_following(self):
        return self.following
