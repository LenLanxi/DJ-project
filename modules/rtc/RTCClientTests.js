'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { Client, encodeTLV, decodeTLV, toolReply, explicitDevice } = require('./RTCClient');

const identity = { sessionId: 'session1', roomId: 'room1', userId: 'person1', botId: 'bot1', taskId: 'task1' };
const call = (id = 'call1', code = 'print(drone.get_status())') =>
  ({ id, function: { name: 'run_program', arguments: JSON.stringify({ code }) } });
function client() {
  const sent = [];
  const value = new Client({ sdk: { createEngine() {} }, canvas: {
    width: 640, height: 480, getContext() { return { fillRect() {} }; }
  }, fetch: async () => ({ ok: true, json: async () => ({ ok: true }) }) });
  value.active = true; value.bound = true; value.session = { ...identity };
  value.socket = { readyState: 1, send(raw) { sent.push(JSON.parse(raw)); }, close() {} };
  return { value, sent };
}
const subtitle = item => ({ type: 'subv', value: { data: [item] } });

test('TLV Unicode round trip uses exact big endian length', () => {
  const raw = encodeTLV('tool', { text: '前进然后旋转', tool_calls: [call()] });
  assert.equal(new DataView(raw).getUint32(4, false), raw.byteLength - 8);
  assert.equal(decodeTLV(raw).value.text, '前进然后旋转');
});
test('malformed TLV cannot reach tool dispatch', () => {
  const raw = encodeTLV('tool', { tool_calls: [call()] });
  new DataView(raw).setUint32(4, raw.byteLength + 1, false);
  assert.throws(() => decodeTLV(raw), /length/);
  assert.throws(() => decodeTLV(new ArrayBuffer(3)), /size/);
});
test('default audio devices are excluded instead of silently selected', () => {
  assert.equal(Boolean(explicitDevice({ deviceId: 'default', label: 'Default headset' })), false);
  assert.equal(Boolean(explicitDevice({ deviceId: 'communications', label: 'Headset' })), false);
  assert.equal(Boolean(explicitDevice({ deviceId: 'id1', label: 'Bluetooth headset' })), true);
});
test('tool without completed user turn is rejected rather than queued for later', () => {
  const { value, sent } = client();
  assert.throws(() => value.rtcMessage({ type: 'tool', value: { tool_calls: [call()] } }), /有效用户轮次/);
  assert.equal(value.pendingCalls.length, 0);
  assert.equal(sent.length, 0);
});
test('partial speech does not authorize a program', () => {
  const { value, sent } = client();
  value.rtcMessage(subtitle({ userId: identity.userId, paragraph: 1, text: '起', definite: false }));
  assert.equal(sent.length, 0);
  assert.equal(value.epoch, null);
});
test('completed human subtitle sends identity and waits for the exact host turn acknowledgement', async () => {
  const { value, sent } = client();
  value.rtcMessage(subtitle({ userId: identity.userId, paragraph: 1, text: '起飞', definite: true }));
  assert.equal(sent[0].type, 'user_turn');
  assert.equal(sent[0].completed, true);
  assert.equal(sent[0].taskId, identity.taskId);
  assert.equal(value.epoch, null);
  value.rtcMessage({ type: 'tool', value: { tool_calls: [call()] } });
  assert.equal(sent.length, 1);
  await value.bridgeMessage({ type: 'turn', epoch: 4, id: '1' });
  assert.equal(sent[1].type, 'tool');
  assert.equal(sent[1].epoch, 4);
  assert.equal(sent[1].botId, identity.botId);
});
test('mismatched turn acknowledgement never relabels a queued tool', async () => {
  const { value, sent } = client();
  value.pendingTurn = 'expected';
  value.pendingCalls.push([call()]);
  await assert.rejects(value.bridgeMessage({ type: 'turn', epoch: 5, id: 'old' }), /不匹配/);
  assert.equal(sent.length, 0);
});
test('identical duplicate tool is not forwarded twice; changed payload is rejected', () => {
  const { value, sent } = client(); value.epoch = 2;
  value.forwardCalls([call()]); value.forwardCalls([call()]);
  assert.equal(sent.length, 1);
  assert.throws(() => value.forwardCalls([call('call1', 'drone.takeoff()')]), /不同请求/);
});
test('new speech while the old model is still planning interrupts before any tool arrives', () => {
  const { value, sent } = client();
  value.turnOpen = true; value.paragraphs.add('old'); value.epoch = 2;
  value.rtcMessage(subtitle({ userId: identity.userId, paragraph: 'new', text: '停', definite: false }));
  assert.equal(sent[0].type, 'interrupt');
  assert.equal(value.epoch, null);
});
test('cloud interruption state reaches local cancellation before final subtitles', () => {
  const { value, sent } = client(); value.epoch = 2;
  value.rtcMessage({ type: 'conv', value: { Stage: { Code: 4 } } });
  assert.equal(sent[0].type, 'interrupt');
  assert.equal(value.epoch, null);
});
test('RTC program is bounded by UTF-8 bytes, while short loops remain possible', () => {
  const { value, sent } = client(); value.epoch = 2;
  value.forwardCalls([call('short', 'for _ in range(2):\n print(drone.get_status())')]);
  assert.equal(sent.length, 1);
  assert.throws(() => value.forwardCalls([call('long', '中'.repeat(201))]), /600/);
});
test('basic RTS reply remains below one KiB and retains observation and actual completion status', () => {
  const reply = toolReply('tool1', { ok: true, status: 'completed', reason: '说明'.repeat(200), journal: [
    { method: 'describe', result: { ok: true, status: 'completed', visible: true, center_x: .4,
      width_fraction: .2, age_seconds: 1.5, answer: '一顶红色帐篷'.repeat(100), description: '看见红色'.repeat(100) } }
  ] });
  assert.ok(encodeTLV('func', reply).byteLength <= 1000);
  const result = JSON.parse(reply.Content);
  assert.equal(result.ok, true);
  assert.equal(result.last_status, 'completed');
  assert.equal(result.visible, true);
  assert.ok(result.answer.length > 0);
  assert.equal(result.age_seconds, 1.5);
});
test('interrupt invalidates local turn synchronously before network acknowledgement', async () => {
  const { value, sent } = client(); value.epoch = 8; value.pendingTurn = 'pending';
  await value.interrupt(true);
  assert.equal(value.epoch, null);
  assert.equal(value.pendingTurn, null);
  assert.deepEqual(sent[0], { type: 'interrupt', land: true });
});
test('late tool result cannot be returned to the cloud after local interruption', async () => {
  const { value } = client();
  value.epoch = 3; value.pendingTools.add('call1');
  let replies = 0;
  value.engine = { async sendUserBinaryMessage() { replies += 1; } };
  await value.interrupt(false);
  await value.bridgeMessage({ type: 'tool_result', id: 'call1', epoch: 3, result: { ok: true } });
  assert.equal(replies, 0);
  assert.equal(value.pendingTools.size, 0);
});
test('pending identifier alone cannot authorize a mismatched epoch result', async () => {
  const { value } = client();
  value.epoch = 4; value.pendingTools.add('call1');
  let replies = 0;
  value.engine = { async sendUserBinaryMessage() { replies += 1; } };
  await value.bridgeMessage({ type: 'tool_result', id: 'call1', epoch: 3, result: { ok: true } });
  assert.equal(replies, 0);
});
test('slow cloud stop cannot keep local capture and RTC room alive', async () => {
  const { value } = client();
  value.requestTimeoutMs = 20;
  const steps = [];
  value.api.destroyEngine = () => { steps.push('destroy'); };
  value.engine = { async leaveRoom() { steps.push('leave'); } };
  value.stream = { getTracks() { return [{ stop() { steps.push('captureStop'); } }]; } };
  value.fetch = (_path, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('cloud stop timeout')));
  });
  const stopping = value.stop();
  assert.deepEqual(steps, ['captureStop', 'leave', 'destroy']);
  assert.equal(value.engine, null);
  assert.equal(value.socket, null);
  await stopping;
  assert.equal(value.active, false);
});
test('simulated source flag survives compact tool feedback', () => {
  const value = JSON.parse(toolReply('id', { ok: true, status: 'completed', simulated: true }).Content);
  assert.equal(value.simulated, true);
});
test('Chinese program branch evidence survives the one KiB reply without claiming task success', () => {
  const reply = toolReply('tool1', { ok: true, status: 'completed', simulated: true,
    debug: 'stage_status=failed：没有找到红色帐篷，已停止，旋转与恢复跟随未执行。'.repeat(20),
    journal: [{ method: 'stop_motion', result: { ok: true, status: 'completed' } }] });
  assert.ok(encodeTLV('func', reply).byteLength <= 1000);
  const result = JSON.parse(reply.Content);
  assert.equal(result.ok, true, 'the program did exit normally; do not fabricate its exit status');
  assert.equal(result.last_status, 'completed');
  assert.match(result.program_output, /stage_status=failed/);
  assert.equal(result.truncated, true);
});
test('joining publishes only the external drone canvas and explicit headset devices', async () => {
  const calls = [];
  const track = { requestFrame() { calls.push('requestFrame'); }, stop() { calls.push('trackStop'); } };
  const engine = {};
  for (const name of ['setAudioPlaybackDevice', 'startAudioCapture', 'setVideoSourceType',
    'setExternalVideoTrack', 'joinRoom', 'publishStream', 'leaveRoom']) {
    engine[name] = async (...args) => { calls.push([name, args]); };
  }
  engine.on = () => {};
  engine.startVideoCapture = () => { throw new Error('PC camera must never open'); };
  const sdk = { createEngine() { return engine; }, destroyEngine() { calls.push('destroy'); } };
  class Socket {
    constructor() { this.readyState = 0; queueMicrotask(() => { this.readyState = 1; this.onopen(); }); }
    send(raw) {
      const message = JSON.parse(raw);
      if (message.type === 'bind') queueMicrotask(() => {
        this.onmessage({ data: JSON.stringify({ type: 'bound', epoch: 1 }) });
        this.onmessage({ data: JSON.stringify({ type: 'frame', id: 1, timestamp: Date.now()/1000, jpeg: 'abc' }) });
      });
    }
    close() { this.readyState = 3; }
  }
  const previous = globalThis.Image;
  globalThis.Image = class { constructor() { this.naturalWidth = 640; this.naturalHeight = 480; }
    set src(_value) { queueMicrotask(() => this.onload()); } };
  const canvas = { width: 640, height: 480, getContext() { return { drawImage() {}, fillRect() {} }; },
    captureStream(rate) { assert.equal(rate, 0); return { getVideoTracks() { return [track]; }, getTracks() { return [track]; } }; } };
  const fetch = async path => ({ ok: true, json: async () => path === '/api/session' ?
    { ok: true, ...identity, appId: 'app1', token: 'shortlived', bridgeUrl: 'ws://127.0.0.1:1234/?token=nonce', expiresAt: Date.now()+180000 }
    : { ok: true } });
  const value = new Client({ sdk, canvas, fetch, WebSocket: Socket });
  try {
    await value.connect('bluetoothMic', 'bluetoothOutput');
    assert.deepEqual(calls.find(item => Array.isArray(item) && item[0] === 'startAudioCapture')[1], ['bluetoothMic']);
    assert.deepEqual(calls.find(item => Array.isArray(item) && item[0] === 'setAudioPlaybackDevice')[1], ['bluetoothOutput']);
    assert.ok(calls.some(item => Array.isArray(item) && item[0] === 'setExternalVideoTrack'));
    assert.equal(calls.some(item => Array.isArray(item) && item[0] === 'startVideoCapture'), false);
  } finally { await value.stop(); globalThis.Image = previous; }
});
