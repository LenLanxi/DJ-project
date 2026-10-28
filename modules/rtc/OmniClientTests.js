'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { Client, explicitDevice, encodePCM, decodePCM, MAX_UPSTREAM, MAX_BUFFERED_AUDIO_SECONDS, PLAYBACK_STALL_MS } = require('./OmniClient');
const { StreamingResampler, PCMFramer } = require('./OmniAudio');

function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
async function until(predicate) { for (let i = 0; i < 30 && !predicate(); i++) await new Promise(resolve => setImmediate(resolve)); assert.ok(predicate(), 'expected phase was not reached'); }
class Events {
  constructor() { this.listeners = new Map(); }
  addEventListener(name, handler) { if (!this.listeners.has(name)) this.listeners.set(name, new Set()); this.listeners.get(name).add(handler); }
  removeEventListener(name, handler) { this.listeners.get(name)?.delete(handler); }
  emit(name) { for (const callback of this.listeners.get(name) || []) callback(); }
}
class Track extends Events {
  constructor(id = 'mic1') { super(); this.id = id; this.stopped = 0; }
  getSettings() { return { deviceId: this.id }; }
  stop() { this.stopped++; }
}
function stream(id = 'mic1') { const track = new Track(id); return { track, getTracks: () => [track], getAudioTracks: () => [track] }; }
function harness(options = {}) {
  const contexts = [], sockets = [], captures = [], calls = [], images = [], audios = [], timers = new Set(), states = [], messages = [];
  let clock = 1700000000000;
  const media = new Events();
  media.devices = [{ kind: 'audioinput', deviceId: 'mic1' }, { kind: 'audiooutput', deviceId: 'headset1' }];
  media.streams = [];
  media.getUserMedia = async constraints => { calls.push(constraints); const result = options.capture ? await options.capture(constraints) : stream(); media.streams.push(result); return result; };
  media.enumerateDevices = async () => media.devices;
  class Context extends Events {
    constructor() {
      super(); this.sampleRate = 48000; this.currentTime = 0; this.state = 'suspended'; this.destination = { name: 'direct' };
      this.buffers = []; this.sources = []; this.closed = 0; contexts.push(this);
      this.audioWorklet = { addModule: async path => { assert.equal(path, '/OmniAudio.js'); if (options.module) await options.module.promise; } };
      if (options.fallback) this.setSinkId = undefined;
    }
    async resume() { this.state = 'running'; }
    async setSinkId(id) { this.sinkId = id; if (options.sink) await options.sink.promise; }
    async close() { this.closed++; this.state = 'closed'; }
    createMediaStreamDestination() { this.fallbackDestination = { name: 'media', stream: {} }; return this.fallbackDestination; }
    createMediaStreamSource() { return node(); }
    createGain() { return { ...node(), gain: { value: 1 } }; }
    createBuffer(channels, length, rate) { const value = { channels, length, rate, data: new Float32Array(length), getChannelData() { return this.data; } }; this.buffers.push(value); return value; }
    createBufferSource() { const value = { ...node(), stopped: 0, start(time) { this.startTime = time; }, stop() { this.stopped++; } }; this.sources.push(value); return value; }
  }
  function node() { return { disconnected: 0, connect(output) { this.output = output; }, disconnect() { this.disconnected++; } }; }
  class Worklet {
    constructor(context, name) { assert.equal(name, 'companioncapture'); this.context = context; this.port = { closed: false, close() { this.closed = true; } }; Object.assign(this, node()); captures.push(this); }
  }
  class Socket {
    constructor(url) { this.url = url; this.readyState = 0; this.bufferedAmount = 0; this.sent = []; this.closed = 0; sockets.push(this); }
    send(raw) { this.sent.push(JSON.parse(raw)); }
    open() { this.readyState = 1; this.onopen?.(); }
    receive(value) { this.onmessage?.({ data: typeof value === 'string' ? value : JSON.stringify(value) }); }
    close() { this.closed++; this.readyState = 3; }
  }
  const canvas = { width: 640, height: 480, draws: 0, cleared: 0, getContext() { return { drawImage: () => { canvas.draws++; }, fillRect: () => { canvas.cleared++; } }; } };
  const client = new Client({ WebSocket: Socket, AudioContext: Context, AudioWorkletNode: Worklet, mediaDevices: media, canvas,
    fetch: options.fetch || (async () => ({ ok: true, json: async () => ({ ok: true, url: 'ws://127.0.0.1:12345/?token=shortlived', expiresAt: clock + 180000, provider: 'alibaba' }) })),
    now: () => clock, phaseTimeoutMs: 1000,
    setInterval: (callback, ms) => { const timer = { callback, ms }; timers.add(timer); return timer; }, clearInterval: timer => timers.delete(timer),
    imageFactory: () => { const image = {}; images.push(image); return image; },
    audioFactory: () => { const audio = { async setSinkId(id) { this.sinkId = id; }, async play() { this.played = true; }, pause() { this.paused = true; }, removeAttribute() {} }; audios.push(audio); return audio; },
    onState: text => states.push(text), onMessage: (who, text) => messages.push({ who, text }) });
  return { client, contexts, sockets, captures, calls, media, canvas, images, audios, timers, states, messages,
    tick(ms) { clock += ms; for (const timer of [...timers]) timer.callback(); }, now: () => clock };
}
async function connect(value) {
  const pending = value.client.connect('mic1', 'headset1');
  await until(() => value.sockets.length);
  const socket = value.sockets.at(-1); socket.open(); socket.receive({ type: 'ready' });
  await pending; return socket;
}
const audio = seconds => ({ type: 'audio', audio: encodePCM(new Uint8Array(Math.round(48000 * seconds))) });

test('streaming conversion produces 16k samples per second from actual 44.1/48/96k rates', () => {
  for (const rate of [16000, 44100, 48000, 96000]) {
    const resampler = new StreamingResampler(rate), framer = new PCMFramer(); let count = 0, frames = 0;
    for (let offset = 0; offset < rate; offset += 127) {
      const converted = resampler.push(new Float32Array(Math.min(127, rate - offset)).fill(.25));
      count += converted.length; frames += framer.push(converted).length;
    }
    assert.equal(count, 16000, `rate ${rate}`); assert.equal(frames, 50);
  }
});
test('resampler phase and filter history do not reset between arbitrary callbacks', () => {
  const signal = Float32Array.from({ length: 44100 }, (_, i) => Math.sin(2 * Math.PI * i * 1200 / 44100));
  const whole = new StreamingResampler(44100).push(signal);
  const split = new StreamingResampler(44100); const result = [];
  for (let i = 0; i < signal.length; i += 113) result.push(...split.push(signal.subarray(i, i + 113)));
  assert.deepEqual(Float32Array.from(result), whole);
});
test('downsampling suppresses frequencies above 16k Nyquist rather than aliasing them', () => {
  function rms(hz) { const input = Float32Array.from({ length: 48000 }, (_, i) => Math.sin(2 * Math.PI * i * hz / 48000)); const result = new StreamingResampler(48000).push(input).subarray(1000); return Math.sqrt(result.reduce((sum, n) => sum + n * n, 0) / result.length); }
  assert.ok(rms(1000) > .65); assert.ok(rms(12000) < .02);
});
test('PCM frame packaging is 320 signed little-endian samples and clamps safely', () => {
  const framer = new PCMFramer(); const values = new Float32Array(320); values.set([-2, -1, 0, 1, 2]);
  const [frame] = framer.push(values); assert.equal(frame.length, 640);
  const view = new DataView(frame.buffer); assert.deepEqual([0, 1, 2, 3, 4].map(i => view.getInt16(i * 2, true)), [-32768, -32768, 0, 32767, 32767]);
  assert.equal(framer.push(new Float32Array(319)).length, 0); assert.equal(framer.push(new Float32Array(1)).length, 1);
  assert.throws(() => new StreamingResampler(48000).push([NaN]));
});
test('production AudioWorklet mixes channels, emits exact PCM frames, and outputs silence', () => {
  let Processor; const posted = [];
  const context = { sampleRate: 48000, Float32Array, Float64Array, Uint8Array, Math, Number,
    AudioWorkletProcessor: class { constructor() { this.port = { postMessage(value) { posted.push(value); } }; } },
    registerProcessor(name, type) { assert.equal(name, 'companioncapture'); Processor = type; } };
  vm.runInNewContext(fs.readFileSync(require.resolve('./OmniAudio'), 'utf8'), context);
  const processor = new Processor();
  for (let i = 0; i < 8; i++) { const output = new Float32Array(128).fill(1); assert.equal(processor.process([[new Float32Array(128).fill(.5), new Float32Array(128).fill(-.5)]], [[output]]), true); assert.ok(output.every(value => value === 0)); }
  assert.equal(posted.length, 1); assert.equal(posted[0].pcm.byteLength, 640); assert.ok(new Uint8Array(posted[0].pcm).every(value => value === 0));
});
test('explicit device IDs are required before opening any capture or connection', async () => {
  const value = harness();
  for (const id of ['', 'default', 'communications']) { assert.equal(Boolean(explicitDevice(id)), false); await assert.rejects(value.client.connect(id, 'headset1')); await assert.rejects(value.client.connect('mic1', id)); }
  assert.equal(value.calls.length, 0); assert.equal(value.contexts.length, 0); assert.equal(value.sockets.length, 0);
});
test('connection uses selected microphone only and routes output before ready', async t => {
  const value = harness(); t.after(() => value.client.stop()); const socket = await connect(value);
  assert.equal(value.calls[0].video, false); assert.deepEqual(value.calls[0].audio.deviceId, { exact: 'mic1' });
  assert.equal(value.contexts[0].sinkId, 'headset1'); assert.deepEqual(socket.sent, [{ type: 'start' }]);
  value.captures[0].port.onmessage({ data: { pcm: new Uint8Array(640).buffer } });
  assert.equal(socket.sent[1].type, 'audio'); assert.equal(atob(socket.sent[1].audio).length, 640);
  socket.receive(audio(.1)); assert.equal(value.contexts[0].buffers[0].rate, 24000); assert.equal(value.contexts[0].sources[0].output, value.contexts[0].destination);
});
test('media-element fallback sets exact sink before playing its media destination', async t => {
  const value = harness({ fallback: true }); t.after(() => value.client.stop()); const socket = await connect(value);
  assert.equal(value.audios[0].sinkId, 'headset1'); assert.equal(value.audios[0].played, true);
  socket.receive(audio(.1)); assert.equal(value.contexts[0].sources[0].output, value.contexts[0].fallbackDestination);
  await value.client.stop(); assert.equal(value.audios[0].paused, true); assert.equal(value.audios[0].srcObject, null);
});
test('mismatched actual microphone identity fails without opening a session', async () => {
  const value = harness({ capture: async () => stream('someOtherMicrophone') });
  await assert.rejects(value.client.connect('mic1', 'headset1'), /端点/); assert.equal(value.sockets.length, 0); assert.ok(value.media.streams[0].track.stopped); assert.equal(value.contexts[0].state, 'closed');
});
test('late getUserMedia after cancellation stops its own tracks without stopping new connection', async t => {
  const late = deferred(); let calls = 0;
  const value = harness({ capture: () => ++calls === 1 ? late.promise : Promise.resolve(stream()) }); t.after(() => value.client.stop());
  const old = value.client.connect('mic1', 'headset1'); const rejected = assert.rejects(old, /connection_cancelled/);
  await until(() => value.calls.length === 1); await value.client.stop();
  const socket = await connect(value); const oldStream = stream(); late.resolve(oldStream); await rejected;
  assert.ok(oldStream.track.stopped); assert.equal(value.client.active, true); assert.equal(socket.closed, 0); assert.equal(value.contexts[0].state, 'closed'); assert.equal(value.contexts[1].state, 'running');
});
test('late session fetch cannot create websocket or let its catch stop a newer connection', async t => {
  const late = deferred(); let calls = 0;
  const session = () => ({ ok: true, json: async () => ({ ok: true, url: 'ws://127.0.0.1:12345/?token=temporary', expiresAt: 1700000180000 }) });
  const value = harness({ fetch: () => ++calls === 1 ? late.promise : Promise.resolve(session()) }); t.after(() => value.client.stop());
  const old = value.client.connect('mic1', 'headset1'); const rejected = assert.rejects(old, /connection_cancelled/);
  await until(() => calls === 1); await value.client.stop(); const socket = await connect(value);
  late.resolve(session()); await rejected; assert.equal(value.sockets.length, 1); assert.equal(socket.closed, 0); assert.equal(value.client.active, true);
});
test('late output sink completion closes old context and never creates a session', async () => {
  const sink = deferred(), value = harness({ sink });
  const old = value.client.connect('mic1', 'headset1'); const rejected = assert.rejects(old, /connection_cancelled/);
  await until(() => value.contexts[0]?.sinkId); await value.client.stop(); sink.resolve(); await rejected;
  assert.equal(value.contexts[0].state, 'closed'); assert.equal(value.sockets.length, 0); assert.ok(value.media.streams[0].track.stopped);
});
test('late worklet module load cannot reopen capture or websocket after stop', async () => {
  const module = deferred(), value = harness({ module });
  const old = value.client.connect('mic1', 'headset1'); const rejected = assert.rejects(old, /connection_cancelled/);
  await until(() => value.contexts[0]?.sinkId); await value.client.stop(); module.resolve(); await rejected;
  assert.equal(value.captures.length, 0); assert.equal(value.sockets.length, 0); assert.equal(value.contexts[0].state, 'closed');
});
test('stop synchronously releases capture, playback, websocket, timers and worklet', async () => {
  const value = harness(); const socket = await connect(value); socket.receive(audio(1)); const source = value.contexts[0].sources[0];
  const stopping = value.client.stop();
  assert.equal(value.client.active, false); assert.ok(value.media.streams[0].track.stopped); assert.ok(source.stopped); assert.equal(socket.closed, 1); assert.equal(value.timers.size, 0); assert.equal(value.captures[0].port.closed, true);
  await stopping; assert.equal(value.contexts[0].state, 'closed'); assert.equal(socket.sent.at(-1).type, 'stop');
});
test('upstream backpressure immediately stops instead of accumulating old speech', async () => {
  const value = harness(); const socket = await connect(value); socket.bufferedAmount = MAX_UPSTREAM + 1;
  value.captures[0].port.onmessage({ data: { pcm: new Uint8Array(640).buffer } });
  assert.equal(value.client.active, false); assert.ok(value.media.streams[0].track.stopped); assert.equal(socket.closed, 1); assert.equal(socket.sent.filter(value => value.type === 'audio').length, 0);
});
test('normal faster-than-realtime speech longer than six seconds queues and drains in order', async t => {
  const value = harness(); t.after(() => value.client.stop()); const socket = await connect(value);
  for (let i = 0; i < 10; i++) socket.receive(audio(1));
  assert.equal(value.client.active, true);
  assert.ok(value.client.connection.pendingAudioSeconds >= 4);
  assert.ok(value.contexts[0].sources.length < 10);
  for (let i = 0; i < 12; i++) {
    value.contexts[0].currentTime += .5; value.tick(500);
  }
  assert.equal(value.client.active, true);
  assert.equal(value.client.connection.pendingAudioSeconds, 0);
  assert.equal(value.contexts[0].buffers.reduce((sum, b) => sum + b.length, 0), 240000);
  for (const buffer of value.contexts[0].buffers) assert.equal(buffer.rate, 24000);
  const sources=value.contexts[0].sources;
  for (let i=1;i<sources.length;i++) assert.equal(sources[i].startTime,sources[i-1].startTime+1);
});
test('total pending and scheduled speech remains bounded and reports why it stopped', async () => {
  const value = harness(); const socket = await connect(value);
  for (let i = 0; i < MAX_BUFFERED_AUDIO_SECONDS + 1 && value.client.active; i++) socket.receive(audio(1));
  assert.equal(value.client.active, false); assert.ok(value.contexts[0].sources.every(source => source.stopped));
  const report=socket.sent.at(-1);
  assert.equal(report.type,'stop'); assert.equal(report.source,'client_error'); assert.match(report.reason,/30秒/);
  assert.ok(report.diagnostics.pending_audio_seconds>0);
  assert.ok(report.diagnostics.pending_audio_seconds+report.diagnostics.scheduled_audio_seconds<=MAX_BUFFERED_AUDIO_SECONDS);
});
test('a genuinely stalled playback clock stops without treating fast generation as a stall', async () => {
  const value = harness(); const socket = await connect(value); socket.receive(audio(1));
  value.tick(PLAYBACK_STALL_MS+1);
  assert.equal(value.client.active,false);
  assert.match(socket.sent.at(-1).reason,/播放时钟/);
  assert.equal(socket.sent.at(-1).diagnostics.audio_state,'running');
});
test('new user input and interrupt discard all scheduled old audio immediately', async t => {
  const value = harness(); t.after(() => value.client.stop()); const socket = await connect(value);
  for (let i=0;i<10;i++) socket.receive(audio(1));
  assert.ok(value.client.connection.pendingAudioSeconds>0);
  socket.receive({ type: 'user_text', text: '停一下' });
  assert.ok(value.contexts[0].sources.every(source => source.stopped)); assert.equal(value.client.connection.sources.size, 0);
  assert.equal(value.client.connection.pendingAudio.length,0);
  assert.equal(value.client.connection.pendingAudioSeconds,0);
  socket.receive(audio(.1)); value.client.interrupt(true); assert.ok(value.contexts[0].sources.at(-1).stopped); assert.deepEqual(socket.sent.at(-1), { type: 'interrupt', land: true });
  socket.receive(audio(.1)); socket.receive({ type: 'interrupted' }); assert.ok(value.contexts[0].sources.at(-1).stopped);
});
test('invalid PCM and output sample rates fail closed', async () => {
  for (const message of [{ type: 'audio' }, { type: 'audio', audio: '' }, { type: 'audio', audio: 'AA==' }, { type: 'audio', audio: 'not base64' }, { ...audio(.01), sampleRate: 24000.5 }, { ...audio(.01), sample_rate: '24000' }]) {
    const value = harness(); const socket = await connect(value); socket.receive(message); assert.equal(value.client.active, false); assert.equal(value.contexts[0].sources.length, 0);
  }
  assert.throws(() => decodePCM('AA==')); assert.equal(decodePCM(encodePCM(new Uint8Array([0, 128, 255, 127])))[0], -1);
});
test('oversize messages close the connection without allocating playback', async () => {
  const value = harness(); const socket = await connect(value); socket.receive(' '.repeat(256 * 1024 + 1)); assert.equal(value.client.active, false); assert.equal(value.contexts[0].sources.length, 0);
});
test('device removal and ended tracks stop without choosing a replacement', async () => {
  for (const method of ['devicechange', 'ended']) {
    const value = harness(); const socket = await connect(value);
    if (method === 'devicechange') { value.media.devices = value.media.devices.filter(device => device.kind !== 'audiooutput'); value.media.emit('devicechange'); await until(() => !value.client.active); }
    else value.media.streams[0].track.emit('ended');
    assert.equal(value.client.active, false); assert.equal(socket.closed, 1); assert.equal(value.calls.length, 1);
  }
});
test('unexpected sink reroute and audio-context suspension revoke active media', async () => {
  for (const event of ['sinkchange', 'statechange', 'error']) {
    const value = harness(); const socket = await connect(value);
    if (event === 'sinkchange') value.contexts[0].sinkId = 'default';
    if (event === 'statechange') value.contexts[0].state = 'suspended';
    value.contexts[0].emit(event);
    assert.equal(value.client.active, false); assert.equal(socket.closed, 1); assert.ok(value.media.streams[0].track.stopped);
  }
});
test('websocket loss clears audio and capture, without reconnect or replay', async () => {
  const value = harness(); const socket = await connect(value); socket.receive(audio(1)); const late = socket.onmessage;
  socket.onclose(); assert.equal(value.client.active, false); assert.ok(value.contexts[0].sources[0].stopped); assert.ok(value.media.streams[0].track.stopped);
  late({ data: JSON.stringify(audio(.1)) }); assert.equal(value.contexts[0].sources.length, 1); assert.equal(value.sockets.length, 1);
  await assert.rejects(value.client.connect('mic1', 'headset1'), /重新启动入口/);
  assert.equal(value.sockets.length, 1); assert.equal(value.contexts.length, 1);
});
test('heartbeat is two seconds while frame freshness is checked more frequently', async t => {
  const value = harness(); t.after(() => value.client.stop()); const socket = await connect(value);
  value.tick(100); value.tick(100); value.tick(100); assert.equal(socket.sent.filter(message => message.type === 'heartbeat').length, 1);
  value.tick(1900); assert.equal(socket.sent.filter(message => message.type === 'heartbeat').length, 2);
});
test('duplicate frames cannot refresh source timestamps or delay stale-frame shutdown', async () => {
  const value = harness(); const socket = await connect(value); const timestamp = value.now() / 1000;
  socket.receive({ type: 'frame', id: 1, timestamp, jpeg: '/9j/2Q==' }); value.images[0].onload(); assert.equal(value.canvas.draws, 1);
  value.tick(900); socket.receive({ type: 'frame', id: 1, timestamp: value.now() / 1000, jpeg: '/9j/2Q==' });
  assert.equal(value.client.connection.lastFrameTimestamp, timestamp); value.tick(101); assert.equal(value.client.active, false); assert.ok(value.canvas.cleared);
});
test('future and stale frame timestamps fail closed, and late image decoding cannot repaint', async () => {
  for (const offset of [-1001, 1]) {
    const value = harness(); const socket = await connect(value); socket.receive({ type: 'frame', id: 1, timestamp: (value.now() + offset) / 1000, jpeg: '/9j/2Q==' }); assert.equal(value.client.active, false);
  }
  const value = harness(); const socket = await connect(value); socket.receive({ type: 'frame', id: 1, timestamp: value.now() / 1000, jpeg: '/9j/2Q==' }); const late = value.images[0].onload;
  await value.client.stop(); late(); assert.equal(value.canvas.draws, 0);
});
test('webpage uses the agreed one-second frame freshness window', async t => {
  const value=harness(); t.after(()=>value.client.stop()); const socket=await connect(value);
  socket.receive({type:'frame',id:1,timestamp:(value.now()-800)/1000,jpeg:'/9j/2Q=='});
  assert.equal(value.client.active,true);
  value.images[0].onload(); assert.equal(value.canvas.draws,1);
  value.tick(201); assert.equal(value.client.active,false);
  assert.match(socket.sent.at(-1).reason,/画面已过期/);
});
test('session wall-clock deadline stops even with an otherwise healthy socket', async () => {
  const value = harness(); const socket = await connect(value); value.tick(180001); assert.equal(value.client.active, false); assert.equal(socket.closed, 1); assert.ok(value.media.streams[0].track.stopped);
});
