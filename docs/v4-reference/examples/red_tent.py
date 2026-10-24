"""Closed-loop fixture demonstration; NOT verified autonomous navigation.
Can run unchanged inside the container against --fixture-vision.
"""
import time

state = drone.get_status()
if state['flight_state'] == 'landed':
    drone.takeoff()

for step in range(12):
    target = drone.observe('红色帐篷')
    if not target['visible']:
        drone.stop_motion()
        print('目标丢失，已停止')
        break
    if target['width_fraction'] >= 0.35:
        drone.stop_motion()
        if not target.get('simulated',True):
            raise RuntimeError('画面占比不能证明到达；需要独立距离与通行验收，停止任务')
        drone.rotate(360)
        drone.start_follow()
        print('达到演示中的画面占比阈值；这不能证明实际距离或真正到达')
        break
    error = target['center_x'] - 0.5
    if abs(error) > 0.12:
        drone.rotate(10 if error > 0 else -10)
    else:
        drone.move_forward(20)
    time.sleep(0.1)
else:
    drone.stop_motion()
    print('达到迭代上限，已停止')
