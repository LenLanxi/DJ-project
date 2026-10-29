"""Cloud plans retain failures and stop on uncertain resource cleanup."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from RouteSelection import ARCHIVE, main
from Trials import execute


def test_archive_and_cloud_opt_in_are_checked_before_any_calls():
    with pytest.raises(ValueError): asyncio.run(main(SimpleNamespace(real=False)))
    with pytest.raises(ValueError,match='archived_evidence'):
        asyncio.run(main(SimpleNamespace(real=True,output_folder=ARCHIVE,stage='speech')))


def test_attempt_cleanup_failure_is_persisted_and_blocks_next_call(tmp_path):
    report=dict(design=[dict(id='first'),dict(id='second')],attempts=[])
    calls=[]
    async def run(spec):
        calls.append(spec['id'])
        return dict(provider='AlibabaRealtime',ok=True,cleanup={})
    path=tmp_path/'Trial.json'
    with pytest.raises(ValueError,match='cleanup_unconfirmed'):
        asyncio.run(execute(report,path,run))
    assert calls==['first']
    stored=json.loads(path.read_text(encoding='utf-8'))
    assert stored['attempts'][0]['state']=='finished'
    assert not stored['attempts'][0]['cleanup_confirmed']
    with pytest.raises(ValueError,match='previous_attempt'):
        asyncio.run(execute(stored,path,run))
    assert calls==['first']


def test_resume_skips_finished_attempts_including_unsuccessful_answers(tmp_path):
    report=dict(design=[dict(id='first'),dict(id='second')],attempts=[
        dict(spec=dict(id='first'),state='finished',cleanup_confirmed=True,sample=dict(ok=False))])
    calls=[]
    async def run(spec):
        calls.append(spec['id'])
        return dict(provider='AlibabaRealtime',ok=False,cleanup=dict(connection_closed=True,sender_finished=True,receiver_finished=True))
    asyncio.run(execute(report,tmp_path/'Trial.json',run))
    assert calls==['second'] and len(report['attempts'])==2
