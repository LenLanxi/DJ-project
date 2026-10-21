from drone_sdk.controller import *
import time

print("=== 1. 测试获取状态与画面 ===")
print(get_status())
print(get_frame())

print("\n=== 2. 测试控制权切换 ===")
print(request_control("model"))
print(request_control("follow")) # 应该返回 busy

print("\n=== 3. 测试基础动作 ===")
print(takeoff("model"))
print(move_forward("model", 50))

print("\n=== 4. 测试连续控制与看门狗 ===")
# 发一次速度
print(update_velocity("model", 0, 50, 0, 0)) 
print("等待 0.6 秒，看门狗将自动触发...")
time.sleep(0.6)  # 这里会看到看门狗打印

print("\n=== 5. 测试控制权释放 ===")
print(release_control("model"))