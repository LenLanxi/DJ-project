"""Read-only diagnostics enforce capabilities at the actual Host boundary."""
import asyncio
import pytest
from QueryProgram import QueryDevice, QUERY_METHODS
from execution.Host import Host

def test_query_device_exposes_status_but_cannot_dispatch_motion():
    async def exercise():
        device=QueryDevice()
        host=Host(device,device,methods=QUERY_METHODS)
        status=await host.dispatch('get_status',[],{})
        assert status['battery']==80 and status['simulated']
        assert await host.dispatch('is_following',[],{}) is False
        for method in ('takeoff','land','start_follow','observe','get_frame'):
            with pytest.raises(ValueError): await host.dispatch(method,[],{})
    asyncio.run(exercise())


def test_plan_generates_without_constructing_device(tmp_path, monkeypatch):
    from app.Application import main
    from unittest.mock import Mock
    hardware=Mock(side_effect=AssertionError('plan must not open hardware'))
    monkeypatch.setattr('app.Application.DeviceSession',hardware)
    monkeypatch.setattr('providers.DoubaoConfig.load_config',lambda *a,**kw: None)
    monkeypatch.setattr('providers.DoubaoVision.DoubaoVisionClient',lambda *a,**kw: type('Client',(),{'complete':lambda self,messages:'{"code":"print(1)"}'})())
    assert main(['plan','--text','hello','--log-file',str(tmp_path/'Plan.jsonl')])==0
    hardware.assert_not_called()


def test_rejected_program_finishes_cleanup_and_next_query_succeeds():
    from app.Runtime import UnifiedRuntime
    class Runner:
        async def run(self, method, host, outcome):
            try:
                value=await host.dispatch(method,[],{})
                outcome.result=dict(ok=True,journal=[dict(method=method,result=value)])
            except ValueError as error:
                outcome.result=dict(ok=False,status='failed',reason=str(error))
    async def exercise():
        device=QueryDevice()
        runtime=UnifiedRuntime(device,device,runner=Runner(),methods=QUERY_METHODS)
        rejected=await runtime.run('takeoff')
        assert not rejected['ok']
        assert rejected['follow_cleanup']['status']=='no_control'
        assert 'follow_cleanup_error' not in rejected
        assert (await runtime.retry_cleanup())['ok']
        result=await runtime.run('get_status')
        assert result['ok'] and result['journal'][0]['result']['battery']==80
    asyncio.run(exercise())


def test_invalid_doubao_credentials_fail_before_hardware_or_docker(tmp_path, monkeypatch):
    from app.Application import main
    from unittest.mock import Mock
    hardware = Mock(side_effect=AssertionError('credentials must precede hardware'))
    docker = Mock(side_effect=AssertionError('credentials must precede Docker'))
    monkeypatch.setattr('app.Application.DeviceSession', hardware)
    monkeypatch.setattr('execution.sandbox_runner.SandboxRunner', docker)
    assert main(['voice', '--real-drone', '--doubao-credentials', str(tmp_path/'Missing.json'),
                 '--log-file', str(tmp_path/'Failure.jsonl')]) == 1
    hardware.assert_not_called()
    docker.assert_not_called()
