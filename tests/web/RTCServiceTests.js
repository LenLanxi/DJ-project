'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { createToken } = require('../../web/RTCToken');
const { loadConfig, startBody, SessionController, createOpenApiInvoker } = require('../../web/RTCService');

const ENV = { RTC_APP_ID: '123456789012345678901234', RTC_APP_KEY: 'test-app-secret',
  VOLCENGINE_ACCESS_KEY_ID: 'test-access-id', VOLCENGINE_SECRET_ACCESS_KEY: 'test-access-secret',
  VOLC_S2S_APP_ID: 'test-speech-id', VOLC_S2S_TOKEN: 'test-speech-secret',
  RTC_VLM_MODEL: 'test-model', RTC_VLM_API_KEY: 'test-model-secret',
  RTC_INSTRUCTIONS: 'Run complete Python and report actual results.',
  RTC_TOOLS_JSON: JSON.stringify([{ type: 'function', name: 'run_program',
    parameters: { type: 'object', properties: { code: { type: 'string', maxLength: 60000 } }, required: ['code'] } }]) };
const success = action => ({ ResponseMetadata: { Action: action, RequestId: 'test-request' } });

test('cloud request config retains duplex audio, video and complete-program tools', () => {
  const config = loadConfig(ENV);
  const body = startBody({ roomId: 'room', taskId: 'task', botId: 'bot', userId: 'user' }, config);
  assert.equal(body.Config.S2SConfig.Provider, 'volcano_duplex');
  assert.equal(body.Config.LLMConfig.Mode, 'ArkV3');
  assert.equal(body.Config.LLMConfig.VisionConfig.Enable, true);
  assert.equal(body.Config.LLMConfig.Tools[0].function.parameters.properties.code.maxLength, 60000);
  assert.match(body.Config.LLMConfig.SystemMessages.join('\n'), /NOVISION/);
  assert.equal(body.Config.LLMConfig.APIKey, ENV.RTC_VLM_API_KEY);
});

test('token uses the official signed wire layout and renews without extending authority', () => {
  const token = createToken(ENV.RTC_APP_ID, ENV.RTC_APP_KEY, 'room', 'user', 1200, { now: 1000, nonce: 42 });
  const packed = Buffer.from(token.slice(3 + ENV.RTC_APP_ID.length), 'base64');
  const length = packed.readUInt16LE();
  const message = packed.subarray(2, 2 + length);
  assert.equal(message.readUInt32LE(8), 1200);
  assert.deepEqual(packed.subarray(2 + length + 2), crypto.createHmac('sha256', ENV.RTC_APP_KEY).update(message).digest());
  let now = Date.now();
  const controller = new SessionController(loadConfig(ENV), async action => success(action), () => now);
  const session = controller.create(); now += 3500000;
  const renewed = controller.renewToken(session.sessionId);
  assert.equal(renewed.expiresAt, session.expiresAt);
  assert.notEqual(renewed.token, session.token);
});

test('start timeout retains cloud cleanup ownership until StopVoiceChat is confirmed', async () => {
  let stopAttempts = 0;
  const controller = new SessionController(loadConfig(ENV), async action => {
    if (action === 'StartVoiceChat') throw new Error('request timeout');
    if (++stopAttempts === 1) throw new Error('stop timeout');
    return success(action);
  });
  const session = controller.create();
  assert.equal((await controller.start(session.sessionId)).ok, false);
  assert.equal((await controller.stop(session.sessionId)).ok, false);
  assert.equal(controller.status().cleanupPending, true);
  assert.throws(() => controller.create(), /cleanup_pending/);
  assert.equal((await controller.stop(session.sessionId)).ok, true);
  assert.equal(controller.status().cloudStopConfirmed, true);
});

test('tool feedback preserves call identity and detailed execution outcome', async () => {
  const calls = [];
  const controller = new SessionController(loadConfig(ENV), async (action, body) => {
    calls.push({ action, body }); return success(action);
  });
  const session = controller.create(); await controller.start(session.sessionId);
  const message = JSON.stringify({ ToolCallID: 'call-123', Content: JSON.stringify({ ok: false,
    reason: 'motion_response_timeout', details: { wire_sent: true, outcome_unknown: true } }) });
  assert.equal((await controller.update(session.sessionId, 'function', message)).ok, true);
  assert.equal(calls.at(-1).body.Message, message);
  assert.equal(calls.at(-1).body.Command, 'function');
  await controller.stop(session.sessionId);
  assert.equal((await controller.update(session.sessionId, 'function', message)).ok, false);
});

test('official signer signs the actual JSON request and never exposes credential fields', async () => {
  const config = loadConfig(ENV);
  let request;
  const invoke = createOpenApiInvoker(config, async (url, options) => {
    request = { url, ...options };
    return { ok: true, text: async () => JSON.stringify(success('StopVoiceChat')) };
  });
  await invoke('StopVoiceChat', { AppId: config.appId, RoomId: 'room', TaskId: 'task' });
  assert.match(request.headers.Authorization, /HMAC-SHA256/);
  assert.deepEqual(JSON.parse(request.body), { AppId: config.appId, RoomId: 'room', TaskId: 'task' });
  assert.ok(!request.body.includes(ENV.VOLCENGINE_SECRET_ACCESS_KEY));
  assert.match(request.url, /Action=StopVoiceChat/);
});
