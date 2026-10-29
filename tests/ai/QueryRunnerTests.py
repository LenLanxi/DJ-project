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
    monkeypatch.setattr('providers.DoubaoVision.DoubaoVisionClient',lambda *a,**kw: type('Client',(),{'complete':lambda self,messages:'{"action_request":{"action":"get_status","args":[]},"explanation":"查询"}'})())
    assert main(['plan','--text','hello','--log-file',str(tmp_path/'Plan.jsonl')])==0
    hardware.assert_not_called()


def test_readonly_runtime_rejects_motion_and_next_query_succeeds():
    from app.Runtime import UnifiedRuntime
    async def exercise():
        device=QueryDevice()
        runtime=UnifiedRuntime(device,device,methods=QUERY_METHODS)
        rejected=await runtime.run({'action':'takeoff','args':[]})
        assert not rejected['ok']
        assert rejected['follow_cleanup']['status']=='no_control'
        assert 'follow_cleanup_error' not in rejected
        assert (await runtime.retry_cleanup())['ok']
        result=await runtime.run({'action':'get_status','args':[]})
        assert result['ok'] and result['journal'][0]['result']['battery']==80
    asyncio.run(exercise())


def test_invalid_doubao_credentials_fail_before_hardware(tmp_path, monkeypatch):
    from app.Application import main
    from unittest.mock import Mock
    hardware = Mock(side_effect=AssertionError('credentials must precede hardware'))
    monkeypatch.setattr('app.Application.DeviceSession', hardware)
    assert main(['voice', '--real-drone', '--doubao-credentials', str(tmp_path/'Missing.json'),
                 '--log-file', str(tmp_path/'Failure.jsonl')]) == 1
    hardware.assert_not_called()


@pytest.mark.parametrize('response', [
    '{"code":"print(1)","explanation":"old format"}',
    '{"action_request":{"action":"eval","args":[]},"explanation":"invalid"}',
    '{"action_request":{"action":"move_forward","args":[true]},"explanation":"invalid"}',
])
def test_plan_rejects_code_or_invalid_actions(response):
    from app.Application import plan_action
    from types import SimpleNamespace
    with pytest.raises(ValueError):
        plan_action(SimpleNamespace(complete=lambda messages: response), 'query')


def test_invalid_action_file_fails_before_hardware(tmp_path, monkeypatch):
    from app.Application import main
    from unittest.mock import Mock
    hardware = Mock(side_effect=AssertionError('invalid request must precede hardware'))
    monkeypatch.setattr('app.Application.DeviceSession', hardware)
    request = tmp_path / 'Action.json'
    request.write_text('{"action":"move_forward","args":[true]}', encoding='utf-8')
    assert main(['run', '--real-drone', '--action-file', str(request),
                 '--log-file', str(tmp_path/'Failure.jsonl')]) == 1
    hardware.assert_not_called()
