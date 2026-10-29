import asyncio
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


def test_archived_comparison_cannot_be_used_as_live_output(monkeypatch):
    args=SimpleNamespace(real=True,output_folder=RunRealtime.ARCHIVE)
    with pytest.raises(ValueError,match='archived_evidence_is_read_only'):
        asyncio.run(RunRealtime.main(args))
