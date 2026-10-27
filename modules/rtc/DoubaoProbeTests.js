'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {Probe}=require('./DoubaoProbe');
const credentials={RTC:{AppId:'fake',AppKey:'fake'},OpenAPI:{AccessKeyId:'fake',SecretAccessKey:'fake'},
  Speech:{AppId:'fake',AccessToken:'fake'},Ark:{ModelName:'fake',APIKey:'fake'}};

test('uncertain start keeps identity for explicit cloud cleanup',async()=>{
  const calls=[];
  const probe=new Probe(credentials,async(action,body)=>{
    calls.push({action,body});
    if(action==='StartVoiceChat') throw new Error('lost reply');
    return {ResponseMetadata:{Action:action}};
  });
  await assert.rejects(probe.start('test',[]));
  assert.equal(probe.needsStop,true);
  await assert.rejects(probe.start('test',[]),/cleanup/);
  assert.equal((await probe.stop()).ok,true);
  assert.equal(calls[0].body.TaskId,calls[1].body.TaskId);
});

test('cleanup failure remains retryable and never claims media completion',async()=>{
  let stops=0;
  const probe=new Probe(credentials,async action=>({ResponseMetadata:{Action:action,
    ...(action==='StopVoiceChat' && stops++===0?{Error:{Code:'Temporary'}}:{})}}));
  const start=await probe.start('test',[]);
  assert.equal(start.media_verified,false);
  assert.equal((await probe.stop()).ok,false);
  assert.equal(probe.needsStop,true);
  assert.equal((await probe.stop()).ok,true);
  assert.equal(probe.needsStop,false);
});
