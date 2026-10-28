import json
from types import SimpleNamespace

import pytest
import RunRealtime
from RunRealtime import cleanup_confirmed


def test_retired_or_unknown_provider_cannot_pass_active_cleanup():
    cleanup={'ok':True,'cloud_stop_confirmed':True,'cloud_stop':{'ok':True},
             'native_exit_code':0,'node_exit_code':0,'short_token_file_deleted':True}
    assert not cleanup_confirmed('tencent',cleanup)
    assert not cleanup_confirmed('unknown',cleanup)


def test_closed_websocket_alone_does_not_prove_async_media_tasks_finished():
    assert not cleanup_confirmed('alibaba',{'connection_closed':True,'sender_finished':False})
    assert cleanup_confirmed('alibaba',{'connection_closed':True,'sender_finished':True,'receiver_finished':True})


def test_stopping_bot_alone_cannot_hide_a_native_client_or_short_token_file():
    value={'cloud_stop':{'ok':True},'native_exit_code':0,'node_exit_code':0,'short_token_file_deleted':True}
    assert cleanup_confirmed('doubao',value)
    for key, invalid in [('native_exit_code',None),('node_exit_code',1),('short_token_file_deleted',False)]:
        assert not cleanup_confirmed('doubao',dict(value,**{key:invalid}))


def test_three_rounds_use_only_active_drivers_and_preserve_every_receipt(tmp_path, monkeypatch):
    root=tmp_path/'Project'
    (root/'diagnostics'/'cloud').mkdir(parents=True)
    for name in ('AlibabaBenchmark.py','DoubaoBenchmark.py'):
        (root/'diagnostics'/'cloud'/name).write_text('',encoding='utf-8')
    credentials=tmp_path/'Credentials.json'
    credentials.write_text('{}',encoding='utf-8')
    output=tmp_path/'Receipts'
    monkeypatch.setattr(RunRealtime,'ROOT',root)
    invoked=[]
    def run(command, **kwargs):
        if '--real' in command:
            provider='alibaba' if command[4].endswith('AlibabaBenchmark.py') else 'doubao'
            invoked.append(provider)
            target=output/RunRealtime.FILES[provider]
            samples=RunRealtime.read_samples(target)
            cleanup=(dict(connection_closed=True,sender_finished=True,receiver_finished=True)
                     if provider=='alibaba' else dict(cloud_stop=dict(ok=True),native_exit_code=0,
                     node_exit_code=0,short_token_file_deleted=True))
            samples.append(dict(provider=RunRealtime.NAMES[provider],task='speech',turn_mode='automatic',
                                ok=False,cleanup=cleanup))
            target.write_text(json.dumps(dict(samples=samples)),encoding='utf-8')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(RunRealtime.subprocess,'run',run)
    args=SimpleNamespace(real=True,output_folder=output,alibaba_credentials=credentials,
                         doubao_credentials=credentials,rounds=3,capabilities=False)
    RunRealtime.main(args)
    assert invoked==['alibaba','doubao','doubao','alibaba','alibaba','doubao']
    assert len(json.loads((output/'Schedule.json').read_text(encoding='utf-8'))['order'])==6
    RunRealtime.main(args)
    assert len(invoked)==6  # Failed but cleaned-up attempts must not be cherry-picked away.


def test_archived_comparison_cannot_be_used_as_live_output(monkeypatch):
    args=SimpleNamespace(real=True,output_folder=RunRealtime.ARCHIVE)
    with pytest.raises(ValueError,match='archived_evidence_is_read_only'):
        RunRealtime.main(args)
