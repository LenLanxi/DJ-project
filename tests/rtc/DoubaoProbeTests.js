'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {Probe}=require('../../web/DoubaoProbe');
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

test('probe endpoint override is explicit and isolated from default starts',async()=>{
  const bodies=[];
  const invoke=async(action,body)=>{bodies.push(body);return {ResponseMetadata:{Action:action}};};
  const tuned=new Probe(credentials,invoke);
  await tuned.start('test',[],1200);
  assert.deepEqual(bodies[0].Config.S2SConfig.ProviderParams.asr,
    {extra:{end_smooth_window_ms:1200}});
  await tuned.stop();
  const defaults=new Probe(credentials,invoke);
  await defaults.start('test',[]);
  assert.equal(bodies[2].Config.S2SConfig.ProviderParams.asr,undefined);
  await defaults.stop();
});

test('invalid endpoint cannot open a paid task',async()=>{
  const probe=new Probe(credentials,async()=>assert.fail('must not invoke'));
  for (const value of [true,0,2001,NaN,'1200']) {
    await assert.rejects(probe.start('test',[],value),/invalid_probe_endpoint/);
    assert.equal(probe.needsStop,false);
  }
});

test('diagnostic start requires an explicit tool selection and preserves an empty selection',async()=>{
  const bodies=[];
  const probe=new Probe(credentials,async(action,body)=>{
    bodies.push(body); return {ResponseMetadata:{Action:action}};
  });
  for(const tools of [undefined,null,false,{}]) {
    await assert.rejects(probe.start('diagnostic instructions',tools),/invalid_tools/);
    assert.equal(probe.needsStop,false);
  }
  for(const instructions of [undefined,null,false,'','x'.repeat(20001)]) {
    await assert.rejects(probe.start(instructions,[]),/invalid_instructions/);
    assert.equal(probe.needsStop,false);
  }
  assert.equal(bodies.length,0);
  await probe.start('diagnostic instructions',[]);
  assert.deepEqual(bodies[0].Config.LLMConfig.Tools,[]);
  assert.equal(bodies[0].Config.LLMConfig.SystemMessages[0],'diagnostic instructions');
  await probe.stop();
});

test('deferred diagnostic settings do not weaken credential validation',()=>{
  for(const [section,field] of [['RTC','AppId'],['RTC','AppKey'],['OpenAPI','AccessKeyId'],
      ['OpenAPI','SecretAccessKey'],['Speech','AppId'],['Speech','AccessToken'],['Ark','ModelName']]) {
    const incomplete=structuredClone(credentials);
    incomplete[section][field]='';
    assert.throws(()=>new Probe(incomplete,()=>assert.fail('must not invoke')),/missing_credentials/);
  }
});
