"""Full public SDK entry, unchanged Group 2 code, real detector, fake flight."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_r3_only_runtime as baseline
from follow.r3_follow.core import State
from follow.r3_follow import FollowService
from follow.r3_follow.config import load_config
from FollowSupport import settle_workers


class VerticalSdkTests(unittest.TestCase):
    cleanup = baseline.ActualSdkYawRuntimeTests.cleanup
    feed = baseline.ActualSdkYawRuntimeTests.feed
    change = baseline.ActualSdkYawRuntimeTests.change
    wait_for = baseline.ActualSdkYawRuntimeTests.wait_for
    start = baseline.ActualSdkYawRuntimeTests.start
    velocity = baseline.ActualSdkYawRuntimeTests.velocity

    def setUp(self):
        baseline.ActualSdkYawRuntimeTests.setUp(self)
        config=(Path(__file__).resolve().parents[2] / 'follow')/'configs/aircraft_A_ID0.json'
        env=patch.dict(os.environ,{'R3_FOLLOW_CONFIG':str(config)})
        env.start();self.addCleanup(env.stop)
        self.follow.close(wait=True)
        self.follow = FollowService(self.sdk, config=load_config(config), frame_provider=self.feed)

    def test_public_entry_transmits_forward_backward_up_down_and_yaw(self):
        service=self.start()
        self.assertTrue(service.config.vertical_follow)
        self.assertEqual(service.config.control_profile,'openpose_pid')
        self.assertEqual(service.status()['control_profile'],'openpose_pid_marker_v2')
        self.change(x=.7,y=.3,size=.18)
        self.wait_for(lambda:self.velocity()[1]>0 and self.velocity()[2]>0 and self.velocity()[3]>0)
        self.assertEqual(self.velocity()[0],0)
        self.change(x=.3,y=.7,size=.32)
        self.wait_for(lambda:self.velocity()[1]<0 and self.velocity()[2]<0 and self.velocity()[3]<0)
        self.assertEqual(self.velocity()[0],0)
        self.change(visible=False)
        self.wait_for(lambda:service.controller.state==State.RECOVERING)
        self.assertEqual(self.velocity(),(0,0,0,0))
        self.change(visible=True,y=.3,size=.18)
        self.wait_for(lambda:self.follow.is_following() and self.velocity()[2]>0)
        self.assertTrue(self.follow.stop().ok)
        self.follow.close(wait=True)
        settle_workers(self.follow)
        self.assertEqual(self.velocity(),(0,0,0,0))

    def test_handoff_and_late_completion_cannot_retain_vertical(self):
        service=self.start();self.change(y=.3)
        self.wait_for(lambda:self.velocity()[2]>0)
        ticket=self.follow.pause_if_active()
        self.assertIsNotNone(ticket)
        self.assertEqual(self.velocity(),(0,0,0,0))

        self.assertTrue(self.sdk.request_control('model').ok)
        self.change(y=.7)
        self.wait_for(lambda:service.controller.observation.center_y>300)
        self.assertTrue(self.sdk.release_control('model').ok)
        self.assertTrue(self.follow.finish_action(ticket,True).ok)
        self.wait_for(lambda:self.velocity()[2]<0)
        old_ticket=self.follow.pause_if_active()
        self.assertTrue(self.follow.stop().ok)
        result=self.follow.finish_action(old_ticket,True)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason,'obsolete_ticket')
        self.assertEqual(self.velocity(),(0,0,0,0))

    def test_unknown_height_clears_vertical_without_freezing_forward(self):
        from dataclasses import asdict
        from types import SimpleNamespace
        import time
        service=self.start();self.change(y=.3,size=.18)
        self.wait_for(lambda:self.velocity()[1]>0 and self.velocity()[2]>0)
        original=self.sdk.get_status
        stamp=[None]
        def real_status_shape():
            data=asdict(original());data['simulated']=False
            if stamp[0] is not None:data['height_timestamp']=time.time()+stamp[0]
            return SimpleNamespace(**data)
        with patch.object(self.sdk,'get_status',side_effect=real_status_shape):
            self.wait_for(lambda:self.velocity()[2]==0)
            self.assertGreater(self.velocity()[1],0)
            self.assertIn('高度数据未知或过期',service.status()['status_message'])
            stamp[0]=0
            self.wait_for(lambda:self.velocity()[2]>0)
            stamp[0]=-1.01  # Strictly beyond the one-second height freshness limit.
            self.wait_for(lambda:self.velocity()[2]==0)



if __name__=='__main__':unittest.main()
