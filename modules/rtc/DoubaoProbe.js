/** Native media benchmark control plane. Never captures devices or runs aircraft code. */
'use strict';
const fs = require('node:fs');
const readline = require('node:readline');
const crypto = require('node:crypto');
const {loadConfig, startBody, taskBody, createOpenApiInvoker, cloudResult} = require('./RTCService');
const {createToken} = require('./RTCToken');

class Probe {
  constructor(credentials, invoke) {
    this.config = loadConfig({RTC_APP_ID: credentials.RTC.AppId, RTC_APP_KEY: credentials.RTC.AppKey,
      VOLCENGINE_ACCESS_KEY_ID: credentials.OpenAPI.AccessKeyId,
      VOLCENGINE_SECRET_ACCESS_KEY: credentials.OpenAPI.SecretAccessKey,
      VOLC_S2S_APP_ID: credentials.Speech.AppId, VOLC_S2S_TOKEN: credentials.Speech.AccessToken,
      RTC_VLM_MODEL: credentials.Ark.ModelName, RTC_VLM_API_KEY: credentials.Ark.APIKey});
    if (this.config.missing.length) throw new Error('missing_credentials');
    this.invoke = invoke || createOpenApiInvoker(this.config);
    const suffix = crypto.randomBytes(6).toString('hex');
    this.session = {roomId:'Bench'+suffix, taskId:'Task'+suffix, userId:'User'+suffix, botId:'Bot'+suffix};
    this.needsStop = false;
  }
  identity() {
    const s = this.session;
    return {...s, appId:this.config.appId,
      token:createToken(this.config.appId,this.config.appKey,s.roomId,s.userId,Math.floor(Date.now()/1000)+180)};
  }
  async start(instructions, tools, endpointMs) {
    if (this.needsStop) throw new Error('previous_task_needs_cleanup');
    if (typeof instructions !== 'string' || !instructions || instructions.length>20000) throw new Error('invalid_instructions');
    this.config.instructions = instructions;
    if (tools) {
      if (!Array.isArray(tools) || tools.length>10) throw new Error('invalid_tools');
      this.config.tools = tools.map(x=> x.function ? x : {type:'function',function:{name:x.name,description:x.description,parameters:x.parameters}});
    }
    if (endpointMs !== undefined && (!Number.isInteger(endpointMs) || endpointMs < 500 || endpointMs > 2000))
      throw new Error('invalid_probe_endpoint');
    const body = startBody(this.session,this.config);
    if (endpointMs !== undefined) {
      // https://docs.volcengine.com/docs/real-time-communication/integrate-end-to-end-real-time-speech-llm?lang=en
      // Official hybrid anti-interruption knob. Probe-only opt-in; production
      // defaults remain unchanged until empirical turn-boundary verification.
      body.Config.S2SConfig.ProviderParams.asr = {extra:{end_smooth_window_ms:endpointMs}};
    }
    // A timeout after sending may still create a billable bot. Retain identity.
    this.needsStop = true;
    const start = performance.now();
    const result = cloudResult(await this.invoke('StartVoiceChat',body),'StartVoiceChat');
    return {...result,control_ms:performance.now()-start,media_verified:false};
  }
  async stop() {
    if (!this.needsStop) return {ok:true,already_stopped:true};
    const start = performance.now();
    const result = cloudResult(await this.invoke('StopVoiceChat',taskBody(this.session,this.config)),'StopVoiceChat');
    if (result.ok) this.needsStop=false;
    return {...result,control_ms:performance.now()-start,billing_stopped_verified:false};
  }
}

async function main() {
  const credentials = JSON.parse(fs.readFileSync(process.env.COMPANION_DOU_CREDS,'utf8').replace(/^\uFEFF/,''));
  const probe = new Probe(credentials);
  let chain = Promise.resolve();
  const output = x=>process.stdout.write(JSON.stringify(x)+'\n');
  const lines=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
  let timer=setTimeout(()=>{lines.close();},120000);
  lines.on('line',line=>{chain=chain.then(async()=>{
    let request;
    try {
      request=JSON.parse(line);
      const result=request.command==='identity'?probe.identity():request.command==='start'?
        await probe.start(request.instructions,request.tools,request.endpointMs):request.command==='stop'?await probe.stop():
        {ok:false,error:'unsupported_command'};
      output({id:request.id,result});
    } catch {output({id:request?.id,result:{ok:false,error:'control_request_failed'}});}
  });});
  await new Promise(resolve=>lines.on('close',resolve));
  clearTimeout(timer);
  await chain;
  for (let i=0;probe.needsStop && i<3;i++) {
    try {const result=await probe.stop();output({event:'final_cleanup',...result});} catch {output({event:'final_cleanup',ok:false});}
  }
  if (probe.needsStop) process.exitCode=2;
}
if(require.main===module) main().catch(()=>{process.stderr.write('probe_failed\n');process.exitCode=1;});
module.exports={Probe};
