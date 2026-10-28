"""Scripted visual evidence belongs to tests only; it is never aircraft perception."""
import time


class FixtureVision:
    def __init__(self):
        self.index = 0

    def reset(self):
        pass

    def close(self):
        pass

    def prepare_navigation(self, not_before, cancelled):
        pass

    def refresh_observation(self, observation):
        return observation

    def frame(self):
        raise RuntimeError('scripted fixture has no camera')

    def observe(self, target):
        self.index += 1
        return {'source': 'scripted_fixture', 'simulated': True,
                'frame_id': self.index, 'timestamp': time.time(), 'visible': True,
                'center_x': 0.5, 'width_fraction': min(.4, .1 * self.index),
                'description': 'SCRIPTED TEST FIXTURE: ' + target}
