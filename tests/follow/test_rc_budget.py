"""Sending units, derivative jitter, and real height policy boundaries."""
from dataclasses import replace
from drone_sdk.contract import StatusResult
from follow.r3_follow.config import FollowConfig
from follow.r3_follow.core import FollowController, Observation
from follow.r3_follow.service import FollowService
from FollowFixture import FakeDevice


def test_one_millisecond_image_jitter_does_not_generate_a_yaw_spike():
    control=FollowController(config=FollowConfig(horizontal_limit_rc=50,slew_rc_per_second=None))
    for i,(t,x) in enumerate(((100.,600),(100.033,601),(100.034,602))):
        control.observe(Observation(i,t,'fixture',960,720,0,True,x,360,.2))
        if not i:
            control.calibrate(t);control.start(t)
        assert abs(control.tick(t).yaw)<20


def test_vertical_guard_zeros_only_the_outward_axis_or_unknown_height():
    service=FollowService(FakeDevice(),config=FollowConfig(vertical_follow=True),log_directory=False)
    try:
        status=service.device.get_status()
        assert service._guard_vertical((3,4,20,5),replace(status,height_cm=190)) == (3,4,0,5)
        assert service._guard_vertical((3,4,-20,5),replace(status,height_cm=60)) == (3,4,0,5)
        assert service._guard_vertical((3,4,20,5),replace(status,height_cm=None)) == (3,4,0,5)
        assert service._guard_vertical((3,4,20,5),replace(status,height_cm=120)) == (3,4,20,5)
    finally:
        service.close()
