'use strict';
// Replay recorded cloud chunk timing through the production playback queue.
// AudioContext/time/output are fixtures: this never opens audio devices.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const { Client, MAX_PLAYBACK_SECONDS, MAX_BUFFERED_AUDIO_SECONDS } = require('../../web/OmniClient');
const receipt = JSON.parse(fs.readFileSync(process.argv[2], 'utf8').replace(/^\uFEFF/, ''));
const chunks = receipt.events.filter(event => event.event === 'client_audio_chunk');
assert.ok(chunks.length, 'receipt has no audio chunk timing');
const results = [];
for (const task of [...new Set(chunks.map(event => event.task))]) {
  const stream = chunks.filter(event => event.task === task);
  let clock = 0, scheduledSamples = 0, previousEnd = null, received = 0, maxBacklog = 0;
  let previousPolicyEnd = 0, previousPolicyRejected = false;
  const context = { currentTime: 0, state: 'running',
    createBuffer(_channels,length,rate) {
      assert.equal(rate,24000); scheduledSamples += length;
      return {getChannelData(){return new Float32Array(length);}};
    },
    createBufferSource() { return {connect(){},disconnect(){},stop(){},
      start(at) {
        assert.ok(previousEnd === null || at >= previousEnd - 1e-8);
        previousEnd = at + this.buffer.getChannelData(0).length / 24000;
      }};
    }
  };
  const client = new Client({now:()=>clock});
  const bundle = {context,ready:true,output:{},sources:new Set(),nextAudioAt:0,
    pendingAudio:[],pendingAudioSeconds:0};
  const origin = stream[0].monotonic_ms;
  function advance(to) {
    while(clock < to) {
      clock=Math.min(to,clock+100); context.currentTime=clock/1000;
      client.pumpPlayback(bundle);
    }
  }
  for (const event of stream) {
    assert.equal(event.sample_rate,24000);
    assert.ok(Number.isInteger(event.samples) && event.samples > 0 && event.samples <= 48000);
    advance(event.monotonic_ms-origin);
    if (!previousPolicyRejected) {
      const end=Math.max(context.currentTime+.015,previousPolicyEnd)+event.samples/24000;
      if (end-context.currentTime>6) previousPolicyRejected=true;
      else previousPolicyEnd=end;
    }
    received += event.samples;
    maxBacklog=Math.max(maxBacklog,received/24000-clock/1000);
    client.play(bundle,new Float32Array(event.samples));
    assert.ok(bundle.nextAudioAt-context.currentTime <= MAX_PLAYBACK_SECONDS+1e-8);
    assert.ok(bundle.pendingAudioSeconds+Math.max(0,bundle.nextAudioAt-context.currentTime) <= MAX_BUFFERED_AUDIO_SECONDS+1e-8);
  }
  advance(clock+Math.ceil(MAX_BUFFERED_AUDIO_SECONDS*1000));
  assert.equal(bundle.pendingAudio.length,0);
  assert.equal(scheduledSamples,received);
  client.clearPlayback(bundle);
  results.push({task,ok:true,chunks:stream.length,audio_seconds:received/24000,
    peak_generated_backlog_seconds:maxBacklog,
    previous_policy_rejected:previousPolicyRejected,all_samples_scheduled:true});
}
console.log(JSON.stringify({ok:true,physical_audio_devices:false,results}));
