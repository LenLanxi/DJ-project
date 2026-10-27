from RunRealtime import cleanup_confirmed


def test_tencent_top_level_stop_confirmation_cannot_be_skipped_as_missing_nested_key():
    assert cleanup_confirmed('tencent',{'ok':True,'cloud_stop_confirmed':True})
    assert not cleanup_confirmed('tencent',{'ok':True,'cloud_stop_confirmed':False})
    assert not cleanup_confirmed('tencent',{'ok':False,'status':'cleanup_pending'})
    assert not cleanup_confirmed('tencent',{})


def test_closed_websocket_alone_does_not_prove_async_media_tasks_finished():
    assert not cleanup_confirmed('alibaba',{'connection_closed':True,'sender_finished':False})
    assert cleanup_confirmed('alibaba',{'connection_closed':True,'sender_finished':True,'receiver_finished':True})


def test_stopping_bot_alone_cannot_hide_a_native_client_or_short_token_file():
    value={'cloud_stop':{'ok':True},'native_exit_code':0,'node_exit_code':0,'short_token_file_deleted':True}
    assert cleanup_confirmed('doubao',value)
    for key, invalid in [('native_exit_code',None),('node_exit_code',1),('short_token_file_deleted',False)]:
        assert not cleanup_confirmed('doubao',dict(value,**{key:invalid}))
