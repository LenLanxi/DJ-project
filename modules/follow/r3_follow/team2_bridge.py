"""Candidate follow callbacks for team 2 to register. No monkeypatch of SDK."""
from .service import FollowService
from .config import load_config


class Team3FollowBridge:
    def __init__(self, port, recovery_window=3, *, config=None):
        # No change to team 2's constructor calls is required: each process can
        # select its own R3_FOLLOW_CONFIG before the bridge is constructed.
        self.config = load_config(config)
        self.port=port
        self.service=FollowService(port, recovery_window=recovery_window, console_notices=True,
                                   **self.config.to_dict())

    def _control_result(self, result):
        # Match team 2's public follow result, retain internal action tickets.
        status=self.port.sdk.get_status()
        return self.port.sdk.ControlResult(ok=result.status=='completed',
                                           current_owner=status.control_owner,
                                           reason=result.reason)

    def start_follow(self):
        return self._control_result(self.service.start_follow())

    def stop_follow(self):
        return self._control_result(self.service.stop_follow())

    def is_following(self):
        return self.service.is_following()

    def get_follow_status(self):
        return self.service.get_follow_status()

    def pause_for_action(self):
        return self.service.pause_for_action()

    def finish_action(self, ticket, status):
        return self.service.finish_action(ticket,status)

    def close(self):
        return self.service.close()
