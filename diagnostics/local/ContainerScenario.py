if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Sandbox payload; run through the authorized sandbox host with an injected drone object.')
    parser.parse_args()
    parser.error('This payload requires the sandbox host; direct execution is unsupported.')

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
