# Scripted fixture test, never a real arrival claim.
import time
drone.takeoff()
drone.start_follow()
for step in range(5):
 target=drone.observe('red tent')
 if target['width_fraction'] >= .35:
  drone.rotate(360)
  print('达到演示阈值；不是实际到达证据')
  break
 drone.move_forward(20)
else:
 raise RuntimeError('fixture did not converge')
