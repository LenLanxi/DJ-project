'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const http = require('node:http');
const { once } = require('node:events');
const { createToken } = require('./RTCToken');
const { loadConfig, startBody, SessionController, createOpenApiInvoker, cloudResult, createService } = require('./RTCService');

const ENV = { RTC_APP_ID: '123456789012345678901234', RTC_APP_KEY: 'fake-app-secret',
  VOLCENGINE_ACCESS_KEY_ID: 'fake-access-id', VOLCENGINE_SECRET_ACCESS_KEY: 'fake-account-secret',
  VOLC_S2S_APP_ID: 'fake-speech-id', VOLC_S2S_TOKEN: 'fake-speech-secret',
  RTC_VLM_MODEL: 'fake-vision-model', RTC_VLM_API_KEY: 'fake-vision-secret',
  RTC_BRIDGE_URL: 'ws://127.0.0.1:12345', RTC_BRIDGE_TOKEN: 'temporary-bridge-token' };
function success(action) { return { ResponseMetadata: { Action: action, RequestId: 'test-request-id' } }; }
function setup(invoke) {
  let now = Date.now();
  const calls = [];
  const config = loadConfig(ENV);
  const controller = new SessionController(config, async (action, value) => {
    calls.push({ action, value }); return invoke ? invoke(action, value) : success(action);
  }, () => now);
  const session = controller.create();
  return { controller, config, calls, session, advance: ms => { now += ms; } };
}

test('RTC token matches official little-endian message and HMAC format', () => {
  const token = createToken(ENV.RTC_APP_ID, ENV.RTC_APP_KEY, 'room', 'user', 1200, { now: 1000, nonce: 42 });
  assert.ok(token.startsWith('001' + ENV.RTC_APP_ID));
  const packed = Buffer.from(token.slice(3 + ENV.RTC_APP_ID.length), 'base64');
  const length = packed.readUInt16LE(); const message = packed.subarray(2, 2 + length);
  assert.equal(message.readUInt32LE(0), 42);
  assert.equal(message.readUInt32LE(4), 1000);
  assert.equal(message.readUInt32LE(8), 1200);
  const signature = packed.subarray(2 + length + 2);
  assert.equal(signature.length, 32);
  assert.deepEqual(signature, crypto.createHmac('sha256', ENV.RTC_APP_KEY).update(message).digest());
  assert.notEqual(createToken(ENV.RTC_APP_ID, ENV.RTC_APP_KEY, 'room', 'user', 1200, { now: 1000 }), token);
});

test('tokens cannot be made immortal or long-lived', () => {
  for (const expiry of [0, 999, 1000, 99999])
    assert.throws(() => createToken('app', 'key', 'room', 'user', expiry, { now: 1000 }));
});

test('long sessions use renewable one-hour tokens without extending session authority', async () => {
  const { controller, session, advance, calls } = setup();
  assert.equal(session.expiresAt - controller.session.createdAt, 30000000);
  assert.ok(session.tokenExpiresAt <= Math.floor(controller.session.createdAt / 1000) + 3600);
  advance(3500000);
  const renewed = controller.renewToken(session.sessionId);
  assert.equal(renewed.expiresAt, session.expiresAt);
  assert.equal(renewed.sessionId, session.sessionId);
  assert.equal(renewed.tokenExpiresAt, session.tokenExpiresAt + 3500);
  assert.notEqual(renewed.token, session.token);
  assert.equal(calls.length, 0);
  assert.throws(() => controller.renewToken('obsolete'), /unknown_session/);
  await controller.stop(session.sessionId);
  assert.throws(() => controller.renewToken(session.sessionId), /session_expired/);
});

test('token renewal respects the final session deadline', () => {
  const { controller, session, advance } = setup();
  advance(29990000);
  assert.equal(controller.renewToken(session.sessionId).tokenExpiresAt,
    Math.floor(session.expiresAt / 1000) + 30);
  advance(10000);
  assert.throws(() => controller.renewToken(session.sessionId), /session_expired/);
});

test('missing credentials remain explicit and do not generate fake RTC sessions', () => {
  const config = loadConfig({});
  const controller = new SessionController(config, () => assert.fail('no API call'));
  assert.equal(controller.status().configured, false);
  assert.ok(controller.status().missing.includes('RTC_APP_KEY'));
  assert.throws(() => controller.create(), /missing_credentials/);
});

test('session response includes only temporary credentials and server-generated identifiers', () => {
  const { controller, session } = setup();
  const serialized = JSON.stringify(session);
  for (const value of [ENV.RTC_APP_KEY, ENV.VOLCENGINE_SECRET_ACCESS_KEY, ENV.VOLC_S2S_TOKEN, ENV.RTC_VLM_API_KEY])
    assert.ok(!serialized.includes(value));
  assert.equal(session.bridgeToken, ENV.RTC_BRIDGE_TOKEN);
  assert.equal(controller.create().sessionId, session.sessionId); // One session, never parallel rooms.
  assert.ok(session.tokenExpiresAt * 1000 <= session.expiresAt + 31000);
});

test('mixed orchestration uses external vision model and real stream snapshots', () => {
  const { controller, config, session } = setup();
  const request = startBody(controller.session, config);
  assert.equal(request.AppId, config.appId);
  assert.deepEqual(request.AgentConfig.TargetUserId, [session.userId]);
  assert.equal(request.AgentConfig.EnableConversationStateCallback, true);
  assert.equal(request.Config.S2SConfig.Provider, 'volcano_duplex');
  assert.equal(request.Config.S2SConfig.OutputMode, 1);
  assert.equal(request.Config.LLMConfig.Mode, 'ArkV3');
  // A vision result can be rejected as stale while the model is thinking;
  // request the documented real-time mode rather than its model default.
  assert.equal(request.Config.LLMConfig.ThinkingType, 'disabled');
  assert.equal(request.Config.LLMConfig.ReasoningEffort, undefined);
  assert.equal(request.Config.LLMConfig.VisionConfig.Enable, true);
  assert.equal(request.Config.LLMConfig.VisionConfig.SnapshotConfig.StreamType, 0);
  assert.equal(request.Config.LLMConfig.Tools[0].function.name, 'run_program');
  assert.ok(request.Config.LLMConfig.SystemMessages.join('\n').includes('<NOVISION>'));
  assert.equal(request.Config.SubtitleConfig.SubtitleMode, 1);
  assert.equal(request.Config.ASRConfig, undefined);
  assert.equal(request.Config.TTSConfig, undefined);
});

test('public model resource omits APIKey, custom model includes it', () => {
  const first = setup();
  delete first.config.modelKey;
  assert.equal(startBody(first.controller.session, first.config).Config.LLMConfig.APIKey, undefined);
  assert.equal(startBody(first.controller.session, loadConfig(ENV)).Config.LLMConfig.APIKey, ENV.RTC_VLM_API_KEY);
});

test('successful start means submitted, not bot ready or verified hardware', async () => {
  const { controller, session, calls } = setup();
  const result = await controller.start(session.sessionId);
  assert.equal(result.ok, true); assert.equal(result.status, 'submitted'); assert.equal(result.ready, false);
  assert.equal(controller.status().hardwareVerified, false);
  await controller.start(session.sessionId);
  assert.equal(calls.filter(x => x.action === 'StartVoiceChat').length, 1);
});

test('empty or mismatched cloud response cannot be called success', () => {
  for (const response of [{}, null, { ResponseMetadata: { Action: 'Wrong' } }])
    assert.equal(cloudResult(response, 'StartVoiceChat').ok, false);
});

test('lost start response keeps task identifiers for cleanup', async () => {
  const { controller, session, calls } = setup(action => {
    if (action === 'StartVoiceChat') throw new Error('network timeout SECRET');
    return success(action);
  });
  assert.equal((await controller.start(session.sessionId)).status, 'cleanup_pending');
  assert.equal(controller.status().cleanupPending, true);
  await controller.stop(session.sessionId);
  assert.equal(calls[0].value.TaskId, calls[1].value.TaskId);
  assert.equal(controller.status().status, 'stopped');
});

test('failed stop remains retryable and does not discard old session', async () => {
  let stops = 0;
  const { controller, session } = setup(action => action === 'StopVoiceChat' && stops++ === 0
    ? { ResponseMetadata: { Error: { Code: 'Busy' } } } : success(action));
  await controller.start(session.sessionId);
  const result = await controller.stop(session.sessionId);
  assert.equal(result.ok, false); assert.equal(result.retryable, true);
  assert.equal(controller.status().cleanupPending, true);
  assert.throws(() => controller.create(), /previous_task_cleanup_pending/);
  assert.equal((await controller.stop(session.sessionId)).ok, true);
  assert.equal(controller.status().cleanupPending, false);
  assert.notEqual(controller.create().sessionId, session.sessionId);
});

test('missing heartbeat revokes tools and stops bot without extending deadline', async () => {
  const { controller, session, advance, calls } = setup();
  await controller.start(session.sessionId);
  advance(8001); await controller.tick();
  assert.equal(controller.status().status, 'stopped');
  assert.equal(controller.status().stopReason, 'client_heartbeat_lost');
  assert.equal(controller.status().acceptTools, false);
  assert.equal(calls.at(-1).action, 'StopVoiceChat');
  assert.equal(controller.heartbeat(session.sessionId).ok, false);
});

test('heartbeat cannot extend the configured session deadline', async () => {
  const { controller, session, advance, config } = setup();
  await controller.start(session.sessionId);
  for (let elapsed = 0; elapsed < config.sessionMs; elapsed += 2000) {
    advance(2000); controller.heartbeat(session.sessionId);
  }
  await controller.tick();
  assert.equal(controller.status().status, 'stopped');
  assert.equal(controller.status().stopReason, 'session_deadline');
});

test('automatic stop retries after unknown result instead of pretending success', async () => {
  let stops = 0;
  const { controller, session, advance } = setup(action => {
    if (action === 'StopVoiceChat' && stops++ === 0) throw new Error('lost response');
    return success(action);
  });
  await controller.start(session.sessionId);
  advance(8001); await controller.tick();
  assert.equal(controller.status().status, 'cleanup_pending');
  advance(2001); await controller.tick();
  assert.equal(controller.status().status, 'stopped');
  assert.equal(stops, 2);
});

test('stop during pending start waits for known task then stops it', async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const { controller, session, calls } = setup(async action => {
    if (action === 'StartVoiceChat') await gate;
    return success(action);
  });
  const start = controller.start(session.sessionId);
  const stop = controller.stop(session.sessionId);
  assert.equal(controller.status().acceptTools, false);
  release(); await Promise.all([start, stop]);
  assert.deepEqual(calls.map(x => x.action), ['StartVoiceChat', 'StopVoiceChat']);
  assert.equal(controller.status().status, 'stopped');
});

test('function results are matched by ToolCallID with server-owned task identity', async () => {
  const { controller, session, calls } = setup();
  await controller.start(session.sessionId);
  await controller.update(session.sessionId, 'function', JSON.stringify({ ToolCallID: 'call1', Content: '{"ok":false}' }));
  assert.equal(calls.at(-1).value.TaskId, session.taskId);
  assert.equal(JSON.parse(calls.at(-1).value.Message).ToolCallID, 'call1');
  await assert.rejects(controller.update(session.sessionId, 'ExternalTextToLLM', 'unsafe'), /unsupported/);
  await assert.rejects(controller.update(session.sessionId, 'function', '{}'), /fields/);
});

test('official signer receives exact 2025-06-01 request without automatic retries', async () => {
  const config = loadConfig(ENV); const captured = [];
  class Signer {
    constructor(request, service) { captured.push({ request, service }); this.request = request; }
    addAuthorization(credential) { assert.deepEqual(credential, config.credential); this.request.headers.Authorization = 'test-signature'; }
  }
  let fetchCount = 0;
  const invoke = createOpenApiInvoker(config, async (url, request) => {
    fetchCount++; assert.equal(request.redirect, 'error'); assert.equal(request.headers.Authorization, 'test-signature');
    assert.equal(new URL(url).searchParams.get('Version'), '2025-06-01');
    return { ok: true, text: async () => JSON.stringify(success('StartVoiceChat')) };
  }, Signer);
  await invoke('StartVoiceChat', { AppId: config.appId });
  assert.equal(fetchCount, 1); assert.equal(captured[0].service, 'rtc');
  assert.equal(captured[0].request.region, 'cn-north-1');
  await assert.rejects(invoke('DeleteApplication', {}), /unsupported/);
});

test('malformed upstream replies do not masquerade as success', async () => {
  class Signer { addAuthorization() {} }
  for (const text of ['{}', 'not json', 'x'.repeat(65537)]) {
    const invoke = createOpenApiInvoker(loadConfig(ENV), async () => ({ ok: true, text: async () => text }), Signer);
    await assert.rejects(invoke('StopVoiceChat', {}));
  }
});

test('installed official SDK signs requests without a network call', async () => {
  const invoke = createOpenApiInvoker(loadConfig(ENV), async (url, request) => {
    assert.equal(new URL(url).hostname, 'rtc.volcengineapi.com');
    assert.match(request.headers.Authorization, /HMAC-SHA256/);
    assert.ok(request.headers.Authorization.includes('fake-access-id'));
    assert.ok(request.headers.Authorization.includes('/cn-north-1/rtc/request'));
    assert.equal(JSON.parse(request.body).TaskId, 'local-test-task');
    return { ok: true, text: async () => JSON.stringify(success('StopVoiceChat')) };
  });
  const response = await invoke('StopVoiceChat', { TaskId: 'local-test-task' });
  assert.equal(cloudResult(response, 'StopVoiceChat').ok, true);
});

test('loopback HTTP rejects foreign origins, secret-bearing bad JSON and arbitrary hosts', async () => {
  const calls = [];
  const service = createService({ config: loadConfig(ENV), invoke: async (action, data) => { calls.push(data); return success(action); } });
  service.server.listen(0, '127.0.0.1'); await once(service.server, 'listening');
  const base = 'http://127.0.0.1:' + service.server.address().port;
  try {
    let response = await fetch(base + '/api/session', { headers: { Origin: 'http://evil.example' } });
    assert.equal(response.status, 403);
    const hostStatus = await new Promise((resolve, reject) => {
      http.get(base + '/api/session', { headers: { Host: 'evil.example' } }, reply => {
        reply.resume(); resolve(reply.statusCode);
      }).on('error', reject);
    });
    assert.equal(hostStatus, 403);
    response = await fetch(base + '/api/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: 'fake-super-secret-invalid-json' });
    assert.ok(!(await response.text()).includes('fake-super-secret'));
    assert.equal(calls.length, 0);
    response = await fetch(base + '/api/session');
    const session = await response.json();
    response = await fetch(base + '/api/token', { method: 'POST',
      headers: { 'Content-Type': 'application/json', Origin: base },
      body: JSON.stringify({ sessionId: session.sessionId }) });
    const renewal = await response.json();
    assert.equal(response.status, 200);
    assert.equal(renewal.sessionId, session.sessionId);
    assert.equal(renewal.expiresAt, session.expiresAt);
    assert.ok(renewal.tokenExpiresAt <= Math.floor(Date.now() / 1000) + 3600);
    assert.equal(calls.length, 0);
    response = await fetch(base + '/api/start', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: base }, body: JSON.stringify({ sessionId: session.sessionId }) });
    assert.equal((await response.json()).status, 'submitted');
    assert.equal(calls.length, 1);
  } finally { await service.close(); }
});

test('HTTP shutdown returns only after stop and closes a clean service', async () => {
  const service = createService({ config: loadConfig(ENV), invoke: async action => success(action) });
  service.server.listen(0, '127.0.0.1'); await once(service.server, 'listening');
  const base = 'http://127.0.0.1:' + service.server.address().port;
  const session = await (await fetch(base + '/api/session')).json();
  await fetch(base + '/api/start', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessionId: session.sessionId }) });
  const closed = once(service.server, 'close');
  const response = await fetch(base + '/api/shutdown', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: '{}' });
  const result = await response.json();
  assert.equal(result.ok, true); assert.equal(result.shutdownRequested, true);
  await closed;
  assert.equal(service.controller.status().status, 'stopped');
});

test('failed shutdown retains hidden cleanup service, rejects new work and retries', async () => {
  let now = Date.now(); let stops = 0;
  const service = createService({ config: loadConfig(ENV), now: () => now,
    invoke: async action => action === 'StopVoiceChat' && stops++ === 0
      ? { ResponseMetadata: { Error: { Code: 'TemporaryUnavailable' } } } : success(action) });
  service.server.listen(0, '127.0.0.1'); await once(service.server, 'listening');
  const base = 'http://127.0.0.1:' + service.server.address().port;
  const session = await (await fetch(base + '/api/session')).json();
  await service.controller.start(session.sessionId);
  let response = await fetch(base + '/api/shutdown', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: '{}' });
  assert.equal(response.status, 409);
  assert.equal((await response.json()).status, 'cleanup_pending');
  response = await fetch(base + '/api/status');
  const pendingStatus = await response.json();
  assert.equal(pendingStatus.shutdownRequested, true);
  assert.equal(pendingStatus.cloudStopConfirmed, false);
  assert.equal(pendingStatus.billingStoppedVerified, false);
  response = await fetch(base + '/api/session'); assert.equal(response.status, 409); await response.text();
  response = await fetch(base + '/api/start', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessionId: session.sessionId }) });
  assert.equal((await response.json()).error, 'service_shutting_down');
  now += 2001;
  await service.housekeeping();
  assert.equal(service.controller.status().status, 'stopped');
  assert.equal(service.server.listening, false);
});

test('installed browser SDK is served locally without cloud access', async () => {
  const service = createService({ env: {} });
  service.server.listen(0, '127.0.0.1'); await once(service.server, 'listening');
  const base = 'http://127.0.0.1:' + service.server.address().port;
  try {
    const response = await fetch(base + '/sdk/rtc.js');
    assert.equal(response.status, 200);
    assert.match(response.headers.get('content-type'), /javascript/);
    assert.ok((await response.text()).includes('VERTC'));
  } finally { await service.close(); }
});

test('audit events correlate cloud cleanup without recording any credentials or bodies', async () => {
  let now = Date.now();
  let stops = 0;
  const entries = [];
  const controller = new SessionController(loadConfig(ENV), async action => {
    if (action === 'StopVoiceChat' && stops++ === 0)
      throw new Error('SECRET ' + ENV.VOLC_S2S_TOKEN + ' ' + ENV.RTC_APP_KEY);
    return success(action);
  }, () => now, entry => entries.push(entry));
  const session = controller.create();
  await controller.start(session.sessionId);
  await controller.stop(session.sessionId);
  assert.equal(controller.status().cleanupPending, true);
  assert.equal(controller.status().cloudStopConfirmed, false);
  assert.equal(controller.status().billingStoppedVerified, false);
  now += 2001;
  await controller.tick();
  assert.equal(controller.status().cloudStopConfirmed, true);
  // StopVoiceChat confirms only the bot task; it does not prove that the human
  // RTC client has left, that provider billing has settled, or hardware flown.
  assert.equal(controller.status().billingStoppedVerified, false);
  assert.deepEqual(entries.map(value => value.event), [
    'rtc_session_created', 'rtc_start_requested', 'rtc_start_submitted',
    'rtc_stop_requested', 'rtc_cloud_stop_attempt', 'rtc_stop_failed',
    'rtc_cloud_stop_attempt', 'rtc_stop_confirmed']);
  for (const entry of entries) {
    assert.equal(entry.sessionId, session.sessionId);
    assert.equal(entry.roomId, session.roomId);
    assert.equal(entry.taskId, session.taskId);
    assert.equal(entry.botId, session.botId);
    assert.match(entry.timestamp, /^\d{4}-\d\d-\d\dT.*Z$/);
    assert.equal(entry.schema_version, 1);
    assert.equal(entry.billingStoppedVerified, false);
    assert.equal(entry.hardwareVerified, false);
  }
  const encoded = JSON.stringify(entries);
  for (const key of ['token', 'bridgeUrl', 'bridgeToken', 'config', 'body', 'authorization'])
    assert.ok(!encoded.toLowerCase().includes('"' + key.toLowerCase() + '"'));
  for (const value of [session.token, ENV.RTC_APP_KEY, ENV.VOLCENGINE_ACCESS_KEY_ID,
    ENV.VOLCENGINE_SECRET_ACCESS_KEY, ENV.VOLC_S2S_TOKEN, ENV.RTC_VLM_API_KEY,
    ENV.RTC_BRIDGE_URL, ENV.RTC_BRIDGE_TOKEN]) assert.ok(!encoded.includes(value));
});

test('audit distinguishes heartbeat disconnection from an absolute session deadline', async () => {
  for (const trigger of ['disconnect', 'deadline']) {
    let now = Date.now(); const entries = [];
    const controller = new SessionController(loadConfig(ENV), async action => success(action),
      () => now, entry => entries.push(entry));
    const session = controller.create(); await controller.start(session.sessionId);
    now = trigger === 'disconnect' ? now + 8001 : session.expiresAt + 1;
    await controller.tick(); await controller.tick();
    const expected = trigger === 'disconnect' ? 'rtc_client_disconnected' : 'rtc_session_deadline';
    assert.equal(entries.filter(entry => entry.event === expected).length, 1);
    assert.equal(entries.filter(entry => entry.event === 'rtc_stop_requested').length, 1);
    assert.equal(entries.at(-1).event, 'rtc_stop_confirmed');
  }
});

test('closing an unstarted session does not fabricate a confirmed cloud StopVoiceChat', async () => {
  const entries = [];
  const controller = new SessionController(loadConfig(ENV), async () => assert.fail('no cloud task'),
    Date.now, entry => entries.push(entry));
  const session = controller.create(); await controller.stop(session.sessionId);
  assert.equal(controller.status().cloudStopConfirmed, false);
  assert.equal(controller.status().billingStoppedVerified, false);
  assert.equal(entries.at(-1).event, 'rtc_session_closed_without_cloud_task');
  assert.ok(!entries.some(entry => entry.event === 'rtc_stop_confirmed'));
});

test('audit writer failure is explicit and does not prevent priority cloud cleanup', async () => {
  const controller = new SessionController(loadConfig(ENV), async action => success(action),
    Date.now, () => { throw new Error('disk full'); });
  const session = controller.create(); await controller.start(session.sessionId);
  await controller.stop(session.sessionId);
  const status = controller.status();
  assert.equal(status.status, 'stopped'); assert.equal(status.cloudStopConfirmed, true);
  assert.equal(status.auditEnabled, true); assert.equal(status.auditComplete, false);
  assert.ok(status.auditFailures > 0);
});

test('a late client receives only the remaining runtime deadline and cannot restart beyond it', () => {
  let now = Date.now();
  const config = loadConfig({ ...ENV, RTC_RUNTIME_DEADLINE_MS: String(now + 20000) });
  const controller = new SessionController(config, async action => success(action), () => now);
  const session = controller.create();
  assert.equal(session.expiresAt, now + 20000);
  now += 20001;
  assert.throws(() => controller.create(), /runtime_deadline_exceeded/);
  assert.throws(() => loadConfig({ ...ENV, RTC_RUNTIME_DEADLINE_MS: 'NaN' }), /invalid_RTC_RUNTIME/);
});
