'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { Client, decodeTLV } = require('../../web/RTCClient');

const identity = { sessionId: 'session', roomId: 'room', userId: 'user', botId: 'bot', taskId: 'task' };
function envelope(type, value) {
  const data = Buffer.from(JSON.stringify(value)), raw = Buffer.alloc(8 + data.length);
  raw.write(type); raw.writeUInt32BE(data.length, 4); data.copy(raw, 8);
  return raw;
}
function fixture() {
  const sent = [], states = [];
  const client = new Client({ sdk: { createEngine() {} }, canvas: {
    width: 16, height: 16, getContext: () => ({ fillRect() {}, drawImage() {} })
  }, fetch: async () => ({ ok: true, json: async () => ({ ok: true }) }), onState: text => states.push(text) });
  Object.assign(client, { active: true, bound: true, session: { ...identity },
    socket: { readyState: 1, send: raw => sent.push(JSON.parse(raw)), close() {} } });
  return { client, sent, states };
}

test('TLV preserves Unicode and large escaped tool arguments without a 64KB local cutoff', () => {
  const value = { tool_calls: [{ id: 'call', function: { name: 'run_program',
    arguments: JSON.stringify({ code: '# 中文引号"'.repeat(7000) }) } }] };
  const raw = envelope('tool', value);
  assert.ok(raw.length > 65536);
  assert.deepEqual(decodeTLV(raw), { type: 'tool', value });
  raw.writeUInt32BE(1, 4);
  assert.throws(() => decodeTLV(raw), /length/);
});

test('tools before subtitles and ordinary interruptions forward without stopping the session', () => {
  const { client, sent } = fixture();
  const events = [{ type: 'tool', value: { tool_calls: [] } },
    { type: 'conv', value: { Stage: { Code: 4 } } },
    { type: 'subv', value: { data: [{ userId: identity.userId, text: '等等', definite: false }] } }];
  for (const event of events) client.rtcMessage(event);
  assert.deepEqual(sent.map(message => message.event), events);
  assert.equal(client.active, true);
});

test('recoverable SDK events preserve control and explicit device recovery', async () => {
  const { client } = fixture(), handlers = {}, calls = [];
  client.inputId = 'mic'; client.outputId = 'speaker';
  client.engine = { on(name, handler) { handlers[name] = handler; },
    stopAudioCapture() { calls.push('pause'); }, stop() {},
    async startAudioCapture(id) { calls.push(id); }, async setAudioPlaybackDevice(id) { calls.push(id); }, async play() {} };
  client.installEvents();
  handlers.onConnectionStateChanged({ state: 4 }); handlers.onError({ code: 1 });
  handlers.onUserLeave({ userId: identity.botId });
  handlers.onRoomBinaryMessageReceived({ userId: identity.botId, message: new ArrayBuffer(1) });
  handlers.onAudioDeviceStateChanged({ deviceState: 'inactive', mediaDeviceInfo: { deviceId: 'mic' } });
  assert.equal(client.active, true);
  await client.resumeAudio();
  assert.deepEqual(calls, ['pause', 'mic', 'speaker']);
});

test('video can recover after invalid frames without stopping voice or publishing a stale canvas', async () => {
  const { client } = fixture(), original = globalThis.Image;
  client.track = { enabled: true, requestFrame() { this.published = true; } };
  globalThis.Image = class { constructor() { this.naturalWidth = this.naturalHeight = 16; }
    set src(_) { queueMicrotask(() => this.onload()); } };
  try {
    await client.bridgeMessage({ type: 'invalid_frame' });
    assert.equal(client.track.enabled, false); assert.equal(client.active, true);
    await client.renderFrame({ id: 1, timestamp: Date.now() / 1000, jpeg: 'fixture' });
    assert.equal(client.track.enabled, true); assert.equal(client.track.published, true);
  } finally { globalThis.Image = original; }
});

test('tool results retain detailed errors and never trigger a duplicate browser cloud update', async () => {
  const { client } = fixture(); let displayed;
  client.post = () => assert.fail('results belong to the server transport');
  client.onMessage = text => { displayed = text; };
  await client.bridgeMessage({ type: 'tool_result', result: { ok: false,
    action_error: { details: { wire_sent: true, reason: 'response_timeout', outcome_unknown: true } } } });
  assert.match(displayed, /wire_sent/); assert.match(displayed, /outcome_unknown/);
});

test('operator interrupt is sent locally before any cloud request', async () => {
  const { client, sent } = fixture(); client.post = () => assert.fail('must not wait for cloud');
  await client.interrupt(true);
  assert.equal(sent[0].type, 'interrupt'); assert.equal(sent[0].land, true);
});

test('stop releases capture and room before waiting for a slow cloud stop', async () => {
  const { client } = fixture(), steps = [];
  let finish;
  client.stream = { getTracks: () => [{ stop: () => steps.push('capture') }] };
  client.engine = { leaveRoom() { steps.push('room'); } };
  client.api.destroyEngine = () => steps.push('engine');
  client.post = () => new Promise(resolve => { finish = resolve; });
  const stopping = client.stop();
  assert.deepEqual(steps, ['capture', 'room', 'engine']);
  assert.equal(client.socket, null); assert.equal(client.active, false);
  finish(); await stopping;
});

test('connection publishes the drone canvas and explicit audio devices without PC camera capture', async () => {
  const calls = [], handlers = {};
  const track = { requestFrame() {}, stop() {} };
  const engine = { on(name, handler) { handlers[name] = handler; } };
  for (const name of ['setAudioPlaybackDevice', 'startAudioCapture', 'setVideoSourceType',
    'setExternalVideoTrack', 'joinRoom', 'publishStream', 'leaveRoom']) {
    engine[name] = async (...args) => calls.push([name, args]);
  }
  engine.startVideoCapture = () => assert.fail('must not use PC camera');
  class Socket {
    constructor() { this.readyState = 0; queueMicrotask(() => { this.readyState = 1; this.onopen(); }); }
    send(raw) { if (JSON.parse(raw).type === 'bind') queueMicrotask(() => this.onmessage({ data: '{"type":"bound"}' })); }
    close() { this.readyState = 3; }
  }
  const client = new Client({ sdk: { createEngine: () => engine, destroyEngine() {} }, WebSocket: Socket,
    canvas: { width: 16, height: 16, getContext: () => ({ fillRect() {} }),
      captureStream(rate) { assert.equal(rate, 0); return { getVideoTracks: () => [track], getTracks: () => [track] }; } },
    fetch: async path => ({ ok: true, json: async () => path === '/api/session' ? { ok: true, ...identity,
      appId: 'app', token: 'temporary', bridgeUrl: 'ws://127.0.0.1:1234', expiresAt: Date.now() + 60000 } : { ok: true } }) });
  try {
    await client.connect('headsetMic', 'headsetOutput');
    assert.deepEqual(calls.find(([name]) => name === 'startAudioCapture')[1], ['headsetMic']);
    assert.deepEqual(calls.find(([name]) => name === 'setAudioPlaybackDevice')[1], ['headsetOutput']);
    assert.equal(calls.find(([name]) => name === 'setExternalVideoTrack')[1][1], track);
    assert.ok(calls.some(([name]) => name === 'publishStream'));
  } finally { await client.stop(); }
});
