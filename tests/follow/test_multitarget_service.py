"""Configuration is validated once and binds the requested marker."""
from pathlib import Path
import pytest
from follow.r3_follow.config import FollowConfig, load_config
from follow.r3_follow.service import FollowService
from FollowFixture import FakeDevice


def test_supplied_aircraft_configs_bind_their_marker_and_rc_budget():
    folder=Path(__file__).resolve().parents[2]/'follow'/'configs'
    for name, target in [('aircraft_A_ID0_motion_follow.json',0)]:
        config=load_config(folder/name)
        service=FollowService(FakeDevice(),config=config,log_directory=False)
        try:
            assert service.tracker.target_id == target
            assert service.rc_caps[0] == round(config.horizontal_limit_rc)
            assert service.rc_caps[1] == round(config.forward_limit_rc)
        finally:
            service.close()


@pytest.mark.parametrize('changes',[{'horizontal_limit_rc':True},{'horizontal_limit_rc':101},
    {'forward_limit_rc':0},{'slew_rc_per_second':-1},{'target_id':50}])
def test_invalid_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        FollowConfig(**changes)
