'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { Client, decodeTLV, toolReply, explicitDevice } = require('../../web/RTCClient');
function encodeTLV(type, payload) {
  const data = Buffer.from(JSON.stringify(payload)), raw = Buffer.alloc(8 + data.length);
  raw.write(type, 0, 4); raw.writeUInt32BE(data.length, 4); data.copy(raw, 8);
  return raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.length);
}

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

test('RTC expiry callbacks share a renewal and update the current engine', async () => {
  const { value } = client();
  value.session.expiresAt = Date.now() + 30000000;
  const handlers = {}, updates = [];
  value.engine = { on(name, fn) { handlers[name] = fn; }, async updateToken(token) { updates.push(token); } };
  let requests = 0, release;
  value.post = async (path, body) => {
    requests++;
    assert.equal(path, '/api/token'); assert.equal(body.sessionId, identity.sessionId);
    await new Promise(resolve => { release = resolve; });
    return { sessionId: identity.sessionId, token: 'new-token',
      tokenExpiresAt: Math.floor(Date.now() / 1000) + 3600, expiresAt: value.session.expiresAt };
  };
  value.installEvents();
  handlers.onTokenWillExpire();
  handlers.onTokenPublishPrivilegeWillExpire();
  handlers.onTokenSubscribePrivilegeWillExpire();
  assert.equal(requests, 1);
  const pending = value.tokenRenewal.promise;
  release(); await pending;
  assert.deepEqual(updates, ['new-token']);
  assert.equal(value.session.token, 'new-token');
});

test('late token renewal cannot affect a replaced session or engine', async () => {
  const { value } = client();
  let release;
  value.engine = { updateToken() { assert.fail('old engine must not be updated'); } };
  value.post = () => new Promise(resolve => { release = resolve; });
  const pending = value.renewToken();
  value.connectionGeneration++;
  value.session = { sessionId: 'new-session' };
  value.engine = { updateToken() { assert.fail('new engine must not receive old token'); } };
  release({ token: 'old-token' });
  await pending;
  assert.equal(value.session.token, undefined);
});

test('token renewal rejects extended deadlines and propagates request failure', async () => {
  const { value } = client();
  value.session.expiresAt = Date.now() + 30000000;
  value.engine = { updateToken() { assert.fail('invalid token must not reach SDK'); } };
  value.post = async () => ({ sessionId: identity.sessionId, token: 'token',
    tokenExpiresAt: Math.floor(Date.now() / 1000) + 3600, expiresAt: value.session.expiresAt + 1000 });
  await assert.rejects(value.renewToken(), /invalid_renewed_token/);
  value.post = async () => { throw new Error('session_expired'); };
  await assert.rejects(value.renewToken(), /session_expired/);
  assert.equal(value.tokenRenewal, null);
});
// Minimal anonymized shape from DoubaoMedia.json development capture. User IDs
// are replaced by the local fixture; sequence/roundId/boolean flags preserve
// the real wire types. Official reference:
// https://docs.volcengine.com/docs/real_time_communication/Real-timesubtitlesconversationrecords?lang=zh
const capturedUserSubtitles = [
  { text: '请', sequence: 0, definite: false, paragraph: false, roundId: 1 },
  { text: '请只回答一句话，测试收到。', sequence: 11, definite: true, paragraph: true, roundId: 1 }
];

test('reference adjustment feedback requires confirmed follow cleanup', () => {
  const accepted = toolReply('id', { ok: true, status: 'completed',
    journal: [{ method: 'adjust_follow_reference', result: { ok: true, status: 'accepted' } }] });
  assert.equal(JSON.parse(accepted.Content).follow_reference_adjusted, undefined);
  const applied = toolReply('id', { ok: true, status: 'completed',
    follow_cleanup: { status: 'follow_resumed', reference_adjusted: 'depth' } });
  assert.equal(JSON.parse(applied.Content).follow_cleanup.reference_adjusted, 'depth');
});

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

test('only selected audio device loss pauses media and recovery reuses the exact endpoints',async()=>{
  const {value}=client(),handlers={},calls=[];
  value.inputId='chosenMic';value.outputId='chosenSpeaker';
  value.engine={on(name,fn){handlers[name]=fn;},
    stopAudioCapture(){calls.push(['pauseInput']);return Promise.resolve();},
    stop(){calls.push(['pauseOutput']);},
    async startAudioCapture(id){calls.push(['input',id]);},
    async setAudioPlaybackDevice(id){calls.push(['output',id]);},async play(){calls.push(['play']);}};
  value.installEvents();
  handlers.onAudioDeviceStateChanged({deviceState:'inactive',mediaDeviceInfo:{deviceId:'otherDevice'}});
  assert.deepEqual(calls,[]);
  for(const deviceId of ['chosenMic','chosenSpeaker'])
    handlers.onAudioDeviceStateChanged({deviceState:'inactive',mediaDeviceInfo:{deviceId}});
  assert.equal(value.active,true);assert.equal(value.capturePaused,true);
  assert.deepEqual(calls,[['pauseInput'],['pauseOutput']]);
  await value.resumeAudio();assert.equal(value.capturePaused,false);
  assert.deepEqual(calls.slice(2),[['input','chosenMic'],['output','chosenSpeaker'],['play']]);
});

test('unavailable video disables old publication and fresh frames restore it without closing voice',async()=>{
  const {value,sent}=client();value.epoch=2;
  value.track={enabled:true,requestFrame(){this.frames=(this.frames||0)+1;}};
  const previous=globalThis.Image;
  globalThis.Image=class{constructor(){this.naturalWidth=this.naturalHeight=16;}
    set src(_){queueMicrotask(()=>this.onload());}};
  value.canvas.getContext=()=>({fillRect(){},drawImage(){}});
  try{
    await value.bridgeMessage({type:'invalid_frame'});
    assert.equal(value.active,true);assert.equal(value.track.enabled,false);
    value.forwardCalls([call()]);assert.equal(sent.at(-1).type,'tool');
    await value.renderFrame({id:1,timestamp:Date.now()/1000,jpeg:'fixture'});
    assert.equal(value.track.enabled,true);assert.equal(value.track.frames,1);
    value.clearFrame();assert.equal(value.track.enabled,false);
  }finally{globalThis.Image=previous;}
});
test('tool without completed user turn is rejected rather than queued for later', () => {
  const { value, sent } = client();
  assert.throws(() => value.rtcMessage({ type: 'tool', value: { tool_calls: [call()] } }), /有效用户轮次/);
  assert.equal(value.pendingCalls.length, 0);
  assert.equal(sent.length, 0);
});
test('partial speech does not authorize a program', () => {
  const { value, sent } = client();
  value.rtcMessage(subtitle({ userId: identity.userId, roundId: 1, paragraph: false, text: '起', definite: false }));
  assert.equal(sent.length, 0);
  assert.equal(value.epoch, null);
});
test('completed human subtitle sends identity and waits for the exact host turn acknowledgement', async () => {
  const { value, sent } = client();
  value.rtcMessage(subtitle({ userId: identity.userId, roundId: 1, paragraph: true, text: '起飞', definite: true }));
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
test('duplicate tools before turn acknowledgement wait for the same exact grant', async () => {
  const { value, sent } = client();
  value.rtcMessage(subtitle({ userId: identity.userId, roundId: 1, paragraph: true, text: '查询状态', definite: true }));
  const event = { type: 'tool', value: { tool_calls: [call()] } };
  value.rtcMessage(event); value.rtcMessage(event);
  assert.equal(sent.filter(message => message.type === 'tool').length, 0);
  await value.bridgeMessage({ type: 'turn', epoch: 4, id: '1' });
  const tools = sent.filter(message => message.type === 'tool');
  assert.equal(tools.length, 2);
  assert.equal(tools.every(message => message.epoch === 4), true);
  assert.equal(value.pendingTools.size, 1);
  assert.equal(value.pendingCalls.length, 0);
});

test('duplicate tool events reach the host and share one pending reply', async () => {
  const { value, sent } = client(); value.epoch = 2;
  const replies = [];
  value.engine = {};
  value.post = async (_path, body) => replies.push(JSON.parse(body.message));
  value.forwardCalls([call()]); value.forwardCalls([call()]);
  assert.equal(sent.length, 2, 'the host decides whether this call can execute');
  assert.equal(value.pendingTools.size, 1);
  const result = { type: 'tool_result', id: 'call1', epoch: 2,
    result: { ok: true, status: 'completed' } };
  await value.bridgeMessage(result);
  await value.bridgeMessage(result);
  assert.equal(replies.length, 1, 'one pending cloud call receives one result');
  assert.equal(value.pendingTools.size, 0);
});

test('completed call replay is forwarded and consumes the host cached result', async () => {
  const { value, sent } = client(); value.epoch = 2;
  const replies = [];
  value.engine = {};
  value.post = async (_path, body) => replies.push(JSON.parse(body.message));
  const result = { type: 'tool_result', id: 'call1', epoch: 2,
    result: { ok: true, status: 'completed' } };
  value.forwardCalls([call()]);
  await value.bridgeMessage(result);
  value.forwardCalls([call()]);
  assert.equal(sent.length, 2);
  assert.equal(value.pendingTools.size, 1);
  await value.bridgeMessage(result);
  assert.equal(value.pendingTools.size, 0, 'a completed replay must not leave an unanswered pending call');
  assert.equal(replies.length, 2);
  assert.deepEqual(replies[1], replies[0]);
});

test('changed tool payload reaches host rejection and session cleanup clears pending replies', async () => {
  const { value, sent } = client(); value.epoch = 2;
  value.forwardCalls([call()]);
  value.forwardCalls([call('call1', 'drone.takeoff()')]);
  assert.equal(sent.length, 2);
  assert.equal(value.pendingTools.size, 1);
  assert.equal(JSON.parse(sent[1].calls[0].function.arguments).code, 'drone.takeoff()');
  await assert.rejects(value.bridgeMessage({ type: 'error', reason: 'tool_id_reused_with_different_payload' }),
    /tool_id_reused_with_different_payload/);
  await value.stop('host rejected changed payload');
  assert.equal(value.pendingTools.size, 0);
});
test('new speech while the old model is still planning interrupts before any tool arrives', () => {
  const { value, sent } = client();
  value.turnOpen = true; value.completedRounds.add('1'); value.epoch = 2;
  value.rtcMessage(subtitle({ userId: identity.userId, roundId: 2, paragraph: false, text: '停', definite: false }));
  assert.equal(sent[0].type, 'interrupt');
  assert.equal(value.epoch, null);
});

test('real cloud boolean paragraph ends a round instead of becoming its identity', () => {
  const { value, sent } = client();
  for (const item of capturedUserSubtitles) value.rtcMessage(subtitle({ ...item, userId: identity.userId }));
  assert.equal(sent.length, 1);
  assert.equal(sent[0].type, 'user_turn');
  assert.equal(sent[0].id, '1');
  assert.equal(sent[0].text, '请只回答一句话，测试收到。');
});

test('a completed clause with paragraph false does not authorize a program', () => {
  const { value, sent } = client();
  value.rtcMessage(subtitle({ userId: identity.userId, roundId: 1, definite: true,
    paragraph: false, text: '起飞以后，' }));
  assert.equal(sent.length, 0);
  assert.equal(value.pendingTurn, null);
  assert.equal(value.epoch, null);
  assert.throws(() => value.rtcMessage({ type: 'tool', value: { tool_calls: [call()] } }), /有效用户轮次/);
});

test('two different completed rounds both using paragraph true are submitted once each', async () => {
  const { value, sent } = client();
  const final = roundId => subtitle({ userId: identity.userId, roundId, paragraph: true,
    definite: true, text: '查询状态' });
  value.rtcMessage(final(1)); value.rtcMessage(final(1));
  await value.bridgeMessage({ type: 'turn', epoch: 4, id: '1' });
  value.rtcMessage({ type: 'conv', value: { Stage: { Code: 5 } } });
  value.rtcMessage(final(2)); value.rtcMessage(final(2));
  assert.deepEqual(sent.filter(item => item.type === 'user_turn').map(item => item.id), ['1', '2']);
  assert.equal(value.pendingTurn, '2');
  assert.equal(value.epoch, null);
});

test('empty quick-subtitle ending uses the same-round cumulative text and only grants once', () => {
  const { value, sent } = client();
  value.rtcMessage(subtitle({ userId: identity.userId, roundId: 8, paragraph: false,
    definite: false, text: '查询电量' }));
  value.rtcMessage(subtitle({ userId: identity.userId, roundId: 8, paragraph: false,
    definite: true, text: '查询当前电量' }));
  const ending = subtitle({ userId: identity.userId, roundId: 8, paragraph: true, definite: true, text: '' });
  value.rtcMessage(ending); value.rtcMessage(ending);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].id, '8');
  assert.equal(sent[0].text, '查询当前电量');
});

test('missing negative boolean string and fractional round IDs never authorize', () => {
  for (const roundId of [undefined, null, -1, true, false, '1', 1.5, Infinity]) {
    const { value, sent } = client();
    value.rtcMessage(subtitle({ userId: identity.userId, roundId, paragraph: true,
      definite: true, text: '起飞' }));
    assert.equal(sent.length, 0);
    assert.equal(value.pendingTurn, null);
  }
});

test('numeric paragraph fixture cannot silently regain the old invented identity semantics', () => {
  const { value, sent } = client();
  value.rtcMessage(subtitle({ userId: identity.userId, roundId: 1, paragraph: 1,
    definite: true, text: '起飞' }));
  assert.equal(sent.length, 0);
});

test('a new round empty ending cannot borrow the prior round text', () => {
  const { value, sent } = client();
  value.rtcMessage(subtitle({ userId: identity.userId, roundId: 1, paragraph: false,
    definite: false, text: '起飞' }));
  value.rtcMessage(subtitle({ userId: identity.userId, roundId: 2, paragraph: true,
    definite: true, text: '' }));
  value.rtcMessage(subtitle({ userId: identity.userId, roundId: 1, paragraph: true,
    definite: true, text: '起飞' }));
  assert.equal(sent.length, 0);
});

test('new round partial revokes immediately and its final cannot restore the old session grant', () => {
  const { value, sent } = client();
  value.turnOpen = true; value.completedRounds.add('1'); value.epoch = 2;
  value.rtcMessage(subtitle({ userId: identity.userId, roundId: 2, paragraph: false,
    definite: false, text: '停' }));
  value.rtcMessage(subtitle({ userId: identity.userId, roundId: 2, paragraph: true,
    definite: true, text: '停止' }));
  assert.deepEqual(sent, [{ type: 'interrupt', land: false, source: 'user_speech_during_turn' }]);
  assert.equal(value.epoch, null);
  assert.equal(value.pendingTurn, null);
});
test('cloud interruption state reaches local cancellation before final subtitles', () => {
  const { value, sent } = client(); value.epoch = 2;
  value.rtcMessage({ type: 'conv', value: { Stage: { Code: 4 } } });
  assert.equal(sent[0].type, 'interrupt');
  assert.equal(value.epoch, null);
});
test('oversized and malformed programs reach host validation without disconnecting voice', async () => {
  const { value, sent } = client(); value.epoch = 2;
  value.forwardCalls([call('short', 'for _ in range(2):\n print(drone.get_status())')]);
  assert.equal(sent.length, 1);
  value.forwardCalls([call('long', '中'.repeat(201))]);
  const malformed = call('malformed'); malformed.function.arguments = '{"code":';
  value.forwardCalls([malformed]);
  assert.equal(sent.length, 3);
  const replies = [];
  value.engine = {};
  value.post = async (_path, body) => replies.push(JSON.parse(body.message));
  await value.bridgeMessage({ type: 'tool_result', id: 'long', epoch: 2,
    result: { ok: false, status: 'failed', reason: 'invalid_program_arguments: max=60000' } });
  assert.equal(JSON.parse(replies[0].Content).ok, false);
  assert.equal(value.active, true);
  value.forwardCalls([call('corrected', 'print(drone.get_status())')]);
  assert.equal(sent.at(-1).calls[0].id, 'corrected');
});
test('server tool reply preserves observation and completion evidence beyond one KiB', () => {
  const reply = toolReply('tool1', { ok: true, status: 'completed', reason: '说明'.repeat(200), journal: [
    { method: 'describe', result: { ok: true, status: 'completed', visible: true, center_x: .4,
      width_fraction: .2, age_seconds: 1.5, answer: '一顶红色帐篷'.repeat(100), description: '看见红色'.repeat(100) } }
  ] });
  assert.ok(Buffer.byteLength(JSON.stringify(reply)) > 1000);
  const result = JSON.parse(reply.Content);
  assert.equal(result.ok, true);
  assert.equal(result.journal[0].result.status, 'completed');
  assert.equal(result.journal[0].result.visible, true);
  assert.ok(result.journal[0].result.answer.length > 0);
  assert.equal(result.journal[0].result.age_seconds, 1.5);
});
test('interrupt invalidates local turn synchronously before network acknowledgement', async () => {
  const { value, sent } = client(); value.epoch = 8; value.pendingTurn = 'pending';
  await value.interrupt(true);
  assert.equal(value.epoch, null);
  assert.equal(value.pendingTurn, null);
  assert.deepEqual(sent[0], { type: 'interrupt', land: true, source: 'operator' });
});

test('client failure preserves its reason before closing the control socket', async () => {
  const { value, sent } = client();
  await value.stop('RTC错误：fixture_error');
  assert.equal(sent[0].source, 'client_stop');
  assert.equal(sent[0].reason, 'RTC错误：fixture_error');
  assert.equal(sent[0].land, false);
});
test('late tool result cannot be returned to the cloud after local interruption', async () => {
  const { value } = client();
  value.epoch = 3; value.pendingTools.add('call1');
  let replies = 0;
  value.engine = {};
  value.post = async () => { replies += 1; };
  await value.interrupt(false);
  await value.bridgeMessage({ type: 'tool_result', id: 'call1', epoch: 3, result: { ok: true } });
  assert.equal(replies, 0);
  assert.equal(value.pendingTools.size, 0);
});
test('pending identifier alone cannot authorize a mismatched epoch result', async () => {
  const { value } = client();
  value.epoch = 4; value.pendingTools.add('call1');
  let replies = 0;
  value.engine = {};
  value.post = async () => { replies += 1; };
  await value.bridgeMessage({ type: 'tool_result', id: 'call1', epoch: 3, result: { ok: true } });
  assert.equal(replies, 0);
});
test('settled old tool reply cannot report execution into a newer connection', async () => {
  const { value } = client(); value.epoch = 2;
  const messages = [];
  value.onMessage = message => messages.push(message);
  let release;
  value.engine = {};
  value.post = path => path === '/api/update' ? new Promise(resolve => { release = resolve; }) : Promise.resolve({ok:true});
  value.forwardCalls([call()]);
  const replying = value.bridgeMessage({ type: 'tool_result', id: 'call1', epoch: 2,
    result: { ok: true, status: 'completed' } });
  await value.stop('replace connection');
  messages.length = 0;
  value.active = true;
  value.session = { ...identity, sessionId: 'session2' };
  value.engine = {};
  value.epoch = 2;
  value.pendingTools.add('call1');
  release(); await replying;
  assert.deepEqual(messages, []);
  assert.equal(value.pendingTools.size, 1, 'the new connection keeps its own pending call');
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
test('Chinese program branch evidence survives the server reply without another truncation', () => {
  const reply = toolReply('tool1', { ok: true, status: 'completed', simulated: true,
    debug: 'stage_status=failed：没有找到红色帐篷，已停止，旋转与恢复跟随未执行。'.repeat(20),
    journal: [{ method: 'stop_motion', result: { ok: true, status: 'completed' } }] });
  const result = JSON.parse(reply.Content);
  assert.equal(result.ok, true, 'the program did exit normally; do not fabricate its exit status');
  assert.equal(result.journal[0].result.status, 'completed');
  assert.match(result.debug, /stage_status=failed/);
  assert.equal(result.truncated, undefined);
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

test('late canceled session request cannot create a socket or stop a newer connection', async () => {
  const requests = [], sockets = [], stopped = [];
  class Socket {
    constructor(url) { this.url = url; this.readyState = 0; this.closed = false; sockets.push(this); }
    close() { this.closed = true; }
  }
  const value = new Client({ sdk: {}, WebSocket: Socket,
    canvas: { width: 16, height: 16, getContext() { return { fillRect() {} }; } },
    fetch: (path, options) => {
      if (path === '/api/session') return new Promise(resolve => requests.push(resolve));
      if (path === '/api/stop') stopped.push(JSON.parse(options.body).sessionId);
      return Promise.resolve({ ok: true, json: async () => ({ ok: true }) });
    } });
  const response = suffix => ({ ok: true, json: async () => ({ ok: true, ...identity,
    sessionId: 'session' + suffix, appId: 'app', token: 'fixture',
    bridgeUrl: 'ws://127.0.0.1:1234/?token=' + suffix, expiresAt: Date.now()+180000 }) });
  const oldConnect = value.connect('mic', 'speaker');
  const oldRejected = assert.rejects(oldConnect, /连接已取消/);
  await value.stop('cancel initial HTTP');
  const newConnect = value.connect('mic', 'speaker');
  const newRejected = assert.rejects(newConnect, /连接已结束/);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 2);
  requests[0](response('Old'));
  await oldRejected;
  assert.equal(value.active, true, 'the old catch must not stop the new connection');
  assert.equal(value.session, null);
  assert.equal(sockets.length, 0, 'late canceled HTTP must not create a bridge connection');
  requests[1](response('New'));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(value.session.sessionId, 'sessionNew');
  assert.equal(sockets.length, 1);
  await value.stop('finish regression');
  await newRejected;
  assert.deepEqual(stopped, ['sessionNew'], 'old cleanup must not target a later session');
  assert.equal(sockets[0].closed, true);
  assert.equal(value.session, null);
});

test('canceling an awaited SDK setup cannot resume microphone capture or publication', async () => {
  let releasePlayback, enteredPlayback;
  const playback = new Promise(resolve => { releasePlayback = resolve; });
  const entered = new Promise(resolve => { enteredPlayback = resolve; });
  const steps = [];
  const engine = { on() {},
    async setAudioPlaybackDevice() { enteredPlayback(); await playback; },
    async startAudioCapture() { steps.push('capture'); },
    async publishStream() { steps.push('publish'); },
    async leaveRoom() { steps.push('leave'); } };
  class Socket {
    constructor() { this.readyState = 0; queueMicrotask(() => { this.readyState = 1; this.onopen(); }); }
    send(raw) { if (JSON.parse(raw).type === 'bind') queueMicrotask(() => {
      this.onmessage({ data: JSON.stringify({ type: 'bound', epoch: 1 }) });
      this.onmessage({ data: JSON.stringify({ type: 'frame', id: 1, timestamp: Date.now()/1000, jpeg: 'fixture' }) });
    }); }
    close() { this.readyState = 3; }
  }
  const previous = globalThis.Image;
  globalThis.Image = class { constructor() { this.naturalWidth = 16; this.naturalHeight = 16; }
    set src(_) { queueMicrotask(() => this.onload()); } };
  const value = new Client({ sdk: { createEngine() { return engine; }, destroyEngine() { steps.push('destroy'); } },
    WebSocket: Socket, canvas: { width: 16, height: 16,
      getContext() { return { fillRect() {}, drawImage() {} }; } },
    fetch: async path => ({ ok: true, json: async () => path === '/api/session' ?
      { ok: true, ...identity, appId: 'app', token: 'fixture',
        bridgeUrl: 'ws://127.0.0.1:1234/?token=fixture', expiresAt: Date.now()+180000 } : { ok: true } }) });
  try {
    const connecting = value.connect('mic', 'speaker');
    const rejected = assert.rejects(connecting, /连接已取消/);
    await entered;
    await value.stop('cancel SDK setup');
    releasePlayback();
    await rejected;
    assert.deepEqual(steps, ['leave', 'destroy']);
    assert.equal(value.engine, null);
    assert.equal(value.session, null);
  } finally { releasePlayback(); await value.stop(); globalThis.Image = previous; }
});

test('late callbacks from a closed bridge or destroyed RTC engine cannot end a newer connection', async () => {
  const sockets = [];
  class Socket {
    constructor() { this.readyState = 0; sockets.push(this); }
    close() { this.readyState = 3; }
  }
  let count = 0;
  const value = new Client({ sdk: { createEngine() {}, destroyEngine() {} }, WebSocket: Socket,
    canvas: { width: 16, height: 16, getContext() { return { fillRect() {} }; } },
    fetch: async path => ({ ok: true, json: async () => path === '/api/session' ?
      { ok: true, ...identity, sessionId: 'session' + ++count, appId: 'app', token: 'fixture',
        bridgeUrl: 'ws://127.0.0.1:1234/?token=fixture', expiresAt: Date.now()+180000 } : { ok: true } }) });
  const first = value.connect('mic', 'speaker');
  const firstRejected = assert.rejects(first, /连接已结束/);
  await new Promise(resolve => setImmediate(resolve));
  const oldHandlers = {};
  value.engine = { on(name, handler) { oldHandlers[name] = handler; }, leaveRoom() {} };
  value.api.destroyEngine = () => {};
  value.installEvents();
  await value.stop('first stopped');
  await firstRejected;
  const second = value.connect('mic', 'speaker');
  const secondRejected = assert.rejects(second, /连接已结束/);
  await new Promise(resolve => setImmediate(resolve));
  sockets[0].onclose();
  sockets[0].onerror();
  sockets[0].onmessage({ data: JSON.stringify({ type: 'invalid_frame' }) });
  oldHandlers.onError({ errorCode: 'old engine' });
  assert.equal(value.active, true);
  assert.equal(value.session.sessionId, 'session2');
  assert.equal(sockets[1].readyState, 0);
  await value.stop('finish regression');
  await secondRejected;
});


test('only the expected bot join clears waiting and late HTTP cannot overwrite conversation', () => {
  const { value } = client();
  const handlers = {}, states = [];
  value.engine = { on(name, callback) { handlers[name] = callback; } };
  value.onState = state => states.push(state);
  value.installEvents();
  value.showSubmittedState();
  assert.match(states.at(-1), /最多30秒/);
  handlers.onUserJoined({ userInfo: { userId: 'unrelated' } });
  assert.equal(value.botJoined, false);
  handlers.onUserJoined({ userInfo: { userId: identity.botId } });
  assert.equal(value.botJoined, true);
  assert.match(states.at(-1), /模型已入房/);
  handlers.onRoomBinaryMessageReceived({ userId: identity.botId,
    message: encodeTLV('conv', { Stage: { Code: 3 } }) });
  assert.equal(states.at(-1), '模型正在回答');
  value.showSubmittedState();
  assert.equal(states.at(-1), '模型正在回答');
  value.connectionGeneration++;
  value.botJoined = false;
  handlers.onUserJoined({ userInfo: { userId: identity.botId } });
  assert.equal(value.botJoined, false);
});
