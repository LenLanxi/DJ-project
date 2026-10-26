/**
 * Local single-session RTC/VoiceChat control plane.
 * Signing and RTC token format adapted from the official rtc-aigc-demo.
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd.
 * SPDX-License-Identifier: BSD-3-Clause. See License.txt and Provenance.txt.
 * Long-term credentials stay in the environment and are never returned to JS.
 */
'use strict';
const http = require('node:http');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { createToken } = require('./RTCToken');

const VERSION = '2025-06-01';
const ENDPOINT = 'https://rtc.volcengineapi.com';
const REGION = 'cn-north-1';
const ACTIONS = new Set(['StartVoiceChat', 'UpdateVoiceChat', 'StopVoiceChat']);
const DEFAULT_INSTRUCTIONS = '你是无人机助手。仅通过工具请求动作，必须如实报告结果；不能把视觉大小当真实距离或通行证明。需要视觉或调用工具时不得输出<NOVISION>；不需要视觉且不调用工具时只输出<NOVISION>。';
const DEFAULT_TOOLS = [
  { type: 'function', function: { name: 'run_program', description: '在隔离执行器运行完整Python无人机程序。', parameters: { type: 'object', properties: { code: { type: 'string' } }, required: ['code'], additionalProperties: false } } },
  { type: 'function', function: { name: 'interrupt', description: '停止或降落，不依赖当前模型程序。', parameters: { type: 'object', properties: { land: { type: 'boolean' } }, required: ['land'], additionalProperties: false } } },
];

function safeCode(value) { return String(value || 'unknown').replace(/[^A-Za-z0-9_.-]/g, '').slice(0, 100); }
function loadConfig(env = process.env) {
  const port = Number(env.RTC_PORT || 0);
  const sessionSeconds = Number(env.RTC_SESSION_SECONDS || 180);
  const runtimeDeadline = env.RTC_RUNTIME_DEADLINE_MS ? Number(env.RTC_RUNTIME_DEADLINE_MS) : null;
  if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error('invalid_RTC_PORT');
  if (!Number.isFinite(sessionSeconds) || sessionSeconds <= 0 || sessionSeconds > 180) throw new Error('invalid_RTC_SESSION_SECONDS');
  if (runtimeDeadline !== null && (!Number.isSafeInteger(runtimeDeadline) || runtimeDeadline <= 0))
    throw new Error('invalid_RTC_RUNTIME_DEADLINE_MS');
  let tools = DEFAULT_TOOLS;
  if (env.RTC_TOOLS_JSON) {
    tools = JSON.parse(env.RTC_TOOLS_JSON);
    if (!Array.isArray(tools) || !tools.length || tools.length > 10) throw new Error('invalid_RTC_TOOLS_JSON');
    tools = tools.map(item => item.function ? item : { type: 'function', function: { name: item.name, description: item.description, parameters: item.parameters } });
  }
  const config = {
    appId: env.RTC_APP_ID || '', appKey: env.RTC_APP_KEY || '',
    credential: { accessKeyId: env.VOLCENGINE_ACCESS_KEY_ID || '', secretKey: env.VOLCENGINE_SECRET_ACCESS_KEY || '' },
    s2sAppId: env.VOLC_S2S_APP_ID || '', s2sToken: env.VOLC_S2S_TOKEN || '',
    model: env.RTC_VLM_MODEL || '', modelKey: env.RTC_VLM_API_KEY || '',
    instructions: env.RTC_INSTRUCTIONS || DEFAULT_INSTRUCTIONS, tools,
    bridgeUrl: env.RTC_BRIDGE_URL || '', bridgeToken: env.RTC_BRIDGE_TOKEN || '',
    businessId: env.RTC_BUSINESS_ID || undefined, voice: env.VOLC_VOICE || 'zh_female_vv_jupiter_bigtts',
    port, sessionMs: sessionSeconds * 1000, runtimeDeadline, heartbeatMs: 8000,
    apiTimeoutMs: 8000, retryMs: 2000, staticRoot: __dirname,
  };
  config.missing = Object.entries({ RTC_APP_ID: config.appId, RTC_APP_KEY: config.appKey,
    VOLCENGINE_ACCESS_KEY_ID: config.credential.accessKeyId,
    VOLCENGINE_SECRET_ACCESS_KEY: config.credential.secretKey,
    VOLC_S2S_APP_ID: config.s2sAppId, VOLC_S2S_TOKEN: config.s2sToken,
    RTC_VLM_MODEL: config.model }).filter(([, value]) => !value).map(([key]) => key);
  return config;
}

function startBody(session, config) {
  const llm = { Mode: 'ArkV3', ModelName: config.model,
    SystemMessages: [config.instructions,
      '混合编排回答选择规则：不需要当前画面且不调用工具时，只输出完整的<NOVISION>；需要视觉、图片、工具调用或无法确认画面时，不得输出<NOVISION>，应调用工具、据实回答或说明具体限制。'], Tools: config.tools,
    EnableParallelToolCalls: false, EnableToolCallHistory: true,
    VisionConfig: { Enable: true, SnapshotConfig: { StreamType: 0, Interval: 500, ImagesLimit: 1 } } };
  if (config.modelKey) llm.APIKey = config.modelKey;
  return { AppId: config.appId, RoomId: session.roomId, TaskId: session.taskId,
    ...(config.businessId ? { BusinessId: config.businessId } : {}),
    AgentConfig: { UserId: session.botId, TargetUserId: [session.userId],
      EnableConversationStateCallback: true, IdleTimeout: 8 },
    Config: { S2SConfig: { Provider: 'volcano_duplex', OutputMode: 1,
      ProviderParams: { app: { appid: config.s2sAppId, token: config.s2sToken },
        session: { audio: { output: { voice: config.voice } } } } },
      LLMConfig: llm, SubtitleConfig: { SubtitleMode: 1 }, InterruptMode: 0 } };
}

function taskBody(session, config) {
  return { AppId: config.appId, RoomId: session.roomId, TaskId: session.taskId };
}

function createOpenApiInvoker(config, fetchImpl = globalThis.fetch, SignerClass) {
  const Signer = SignerClass || require('@volcengine/openapi').Signer;
  return async (action, body) => {
    if (!ACTIONS.has(action)) throw new Error('unsupported_openapi_action');
    const request = { region: REGION, method: 'POST',
      params: { Action: action, Version: VERSION },
      headers: { Host: 'rtc.volcengineapi.com', 'Content-type': 'application/json' }, body };
    new Signer(request, 'rtc').addAuthorization(config.credential);
    const url = new URL(ENDPOINT);
    url.searchParams.set('Action', action); url.searchParams.set('Version', VERSION);
    const response = await fetchImpl(url.toString(), { method: 'POST', headers: request.headers,
      body: JSON.stringify(body), signal: AbortSignal.timeout(config.apiTimeoutMs), redirect: 'error' });
    const raw = await response.text();
    if (Buffer.byteLength(raw) > 65536) throw new Error('openapi_response_too_large');
    let result;
    try { result = JSON.parse(raw); } catch { throw new Error('openapi_invalid_json'); }
    if (!response.ok && !result?.ResponseMetadata?.Error) throw new Error('openapi_http_' + response.status);
    if (!result?.ResponseMetadata || typeof result.ResponseMetadata !== 'object') throw new Error('openapi_missing_metadata');
    return result;
  };
}

function cloudResult(response, action) {
  const metadata = response?.ResponseMetadata;
  if (!metadata || typeof metadata !== 'object' || (metadata.Action && metadata.Action !== action))
    return { ok: false, error: 'invalid_openapi_response' };
  return metadata.Error
    ? { ok: false, error: safeCode(metadata.Error.Code), requestId: metadata.RequestId }
    : { ok: true, requestId: metadata.RequestId };
}

class SessionController {
  constructor(config, invoke, now = Date.now, audit = null) {
    this.config = config; this.invoke = invoke; this.now = now; this.session = null;
    this.auditLogger = audit; this.auditFailures = 0;
  }
  audit(event, session, details = {}) {
    if (!this.auditLogger) return;
    // An allowlist prevents token-bearing config, URLs, provider bodies or error
    // messages from being added to a billing-evidence log by accident.
    const entry = { schema_version: 1, event, timestamp: new Date(this.now()).toISOString(),
      sessionId: session.id, roomId: session.roomId, taskId: session.taskId,
      botId: session.botId, userId: session.userId, status: session.status,
      expiresAt: session.expiresAt, cloudTaskMayBeRunning: Boolean(session.needsStop),
      cloudStopConfirmed: Boolean(session.cloudStopConfirmed),
      billingStoppedVerified: false, hardwareVerified: false,
      reason: details.reason ? safeCode(details.reason) : null };
    try { this.auditLogger(entry); } catch { this.auditFailures++; }
  }
  status() {
    const s = this.session;
    return { ok: true, configured: !this.config.missing.length, missing: this.config.missing,
      status: s?.status || 'idle', sessionId: s?.id || null, cleanupPending: Boolean(s?.needsStop),
      acceptTools: Boolean(s && s.status === 'submitted' && !s.stopRequested && this.now() < s.expiresAt),
      expiresAt: s?.expiresAt || null, stopReason: s?.stopReason || null,
      error: s?.lastError || null, hardwareVerified: false,
      cloudStopConfirmed: Boolean(s?.cloudStopConfirmed), billingStoppedVerified: false,
      auditEnabled: Boolean(this.auditLogger), auditComplete: Boolean(this.auditLogger) && !this.auditFailures,
      auditFailures: this.auditFailures };
  }
  create() {
    if (this.config.missing.length) throw Object.assign(new Error('missing_credentials'), { statusCode: 503 });
    if (this.config.runtimeDeadline !== null && this.now() >= this.config.runtimeDeadline)
      throw Object.assign(new Error('runtime_deadline_exceeded'), { statusCode: 409 });
    if (this.session?.needsStop && (this.session.stopRequested || this.session.status === 'cleanup_pending'))
      throw Object.assign(new Error('previous_task_cleanup_pending'), { statusCode: 409 });
    if (this.session && this.session.status !== 'stopped' && this.now() < this.session.expiresAt)
      return this.publicSession();
    if (this.session?.needsStop) throw Object.assign(new Error('previous_task_cleanup_pending'), { statusCode: 409 });
    const now = this.now();
    this.session = { id: crypto.randomUUID(), roomId: crypto.randomUUID(), userId: crypto.randomUUID(),
      taskId: crypto.randomUUID(), botId: 'CompanionBot' + crypto.randomBytes(8).toString('hex'),
      status: 'idle', createdAt: now,
      expiresAt: Math.min(now + this.config.sessionMs, this.config.runtimeDeadline ?? Infinity),
      lastSeen: now, pending: null, needsStop: false, stopRequested: false,
      lastAttempt: 0, stopReason: null, lastError: null, cloudStopConfirmed: false,
      stopRequestAudited: false };
    this.audit('rtc_session_created', this.session);
    return this.publicSession();
  }
  publicSession() {
    const s = this.session;
    const tokenExpiresAt = Math.floor(s.expiresAt / 1000) + 30;
    return { ok: true, sessionId: s.id, appId: this.config.appId,
      token: createToken(this.config.appId, this.config.appKey, s.roomId, s.userId,
                         tokenExpiresAt, { now: Math.floor(this.now() / 1000) }),
      roomId: s.roomId, userId: s.userId, botId: s.botId, taskId: s.taskId,
      businessId: this.config.businessId, expiresAt: s.expiresAt, tokenExpiresAt,
      bridgeUrl: this.config.bridgeUrl, bridgeToken: this.config.bridgeToken,
      status: s.status, hardwareVerified: false };
  }
  require(id) {
    if (!this.session || typeof id !== 'string' || id !== this.session.id)
      throw Object.assign(new Error('unknown_session'), { statusCode: 404 });
    return this.session;
  }
  heartbeat(id) {
    const s = this.require(id);
    if (s.stopRequested || this.now() >= s.expiresAt || s.status === 'stopped')
      return { ok: false, status: s.status, reason: s.stopReason || 'session_expired' };
    s.lastSeen = this.now();
    return { ok: true, expiresAt: s.expiresAt, status: s.status };
  }
  async start(id) {
    const s = this.require(id);
    if (s.stopRequested || this.now() >= s.expiresAt) return { ok: false, status: s.status, error: 'session_expired' };
    if (s.status === 'submitted') return { ok: true, status: 'submitted', alreadySubmitted: true };
    if (s.pending) return s.pending;
    if (s.needsStop || s.status === 'stopped') return { ok: false, status: s.status, error: 'cleanup_or_restart_required' };
    s.status = 'starting'; s.needsStop = true;
    this.audit('rtc_start_requested', s);
    s.pending = (async () => {
      let result;
      try { result = cloudResult(await this.invoke('StartVoiceChat', startBody(s, this.config)), 'StartVoiceChat'); }
      catch { result = { ok: false, error: 'start_result_unknown' }; }
      s.status = result.ok ? 'submitted' : 'cleanup_pending'; s.lastError = result.error || null;
      this.audit(result.ok ? 'rtc_start_submitted' : 'rtc_start_unconfirmed', s);
      return { ...result, status: s.status, ready: false, note: 'Wait for RTC bot/conversation callbacks; HTTP acceptance is not readiness.' };
    })();
    try { return await s.pending; } finally { s.pending = null; }
  }
  async update(id, command, message) {
    const s = this.require(id);
    if (s.status !== 'submitted' || s.stopRequested || this.now() >= s.expiresAt)
      return { ok: false, error: 'task_not_accepting_commands', status: s.status };
    if (!['interrupt', 'function'].includes(command)) throw new Error('unsupported_update_command');
    const body = { ...taskBody(s, this.config), Command: command };
    if (command === 'function') {
      if (typeof message !== 'string' || Buffer.byteLength(message) > 48000) throw new Error('invalid_function_result');
      let value; try { value = JSON.parse(message); } catch { throw new Error('invalid_function_result_json'); }
      if (typeof value.ToolCallID !== 'string' || !value.ToolCallID || typeof value.Content !== 'string')
        throw new Error('invalid_function_result_fields');
      body.Message = message;
    }
    try { return cloudResult(await this.invoke('UpdateVoiceChat', body), 'UpdateVoiceChat'); }
    catch { return { ok: false, error: 'update_result_unknown' }; }
  }
  async stop(id, reason = 'requested') {
    const s = this.require(id);
    s.stopRequested = true; s.stopReason = reason;
    if (!s.stopRequestAudited) {
      s.stopRequestAudited = true; this.audit('rtc_stop_requested', s, { reason });
    }
    if (s.pending) { await s.pending; return this.stop(id, reason); }
    if (!s.needsStop) {
      const changed = s.status !== 'stopped'; s.status = 'stopped';
      if (changed) this.audit('rtc_session_closed_without_cloud_task', s, { reason });
      return { ok: true, status: 'stopped' };
    }
    s.status = 'stopping'; s.lastAttempt = this.now();
    this.audit('rtc_cloud_stop_attempt', s, { reason });
    s.pending = (async () => {
      let result;
      try { result = cloudResult(await this.invoke('StopVoiceChat', taskBody(s, this.config)), 'StopVoiceChat'); }
      catch { result = { ok: false, error: 'stop_result_unknown' }; }
      s.needsStop = !result.ok; s.status = result.ok ? 'stopped' : 'cleanup_pending';
      s.cloudStopConfirmed = result.ok;
      s.lastError = result.error || null;
      this.audit(result.ok ? 'rtc_stop_confirmed' : 'rtc_stop_failed', s, { reason });
      return { ...result, status: s.status, retryable: !result.ok };
    })();
    try { return await s.pending; } finally { s.pending = null; }
  }
  async tick() {
    const s = this.session;
    if (!s || s.status === 'stopped') return;
    const reason = this.now() >= s.expiresAt ? 'session_deadline'
      : this.now() - s.lastSeen >= this.config.heartbeatMs ? 'client_heartbeat_lost' : null;
    if (reason) {
      if (reason !== s.stopReason) this.audit(reason === 'session_deadline'
        ? 'rtc_session_deadline' : 'rtc_client_disconnected', s, { reason });
      s.stopRequested = true; s.stopReason = reason;
      if (!s.stopRequestAudited) {
        s.stopRequestAudited = true; this.audit('rtc_stop_requested', s, { reason });
      }
    }
    if ((s.stopRequested || s.status === 'cleanup_pending') && !s.pending
        && this.now() - s.lastAttempt >= this.config.retryMs)
      await this.stop(s.id, s.stopReason || 'cleanup_retry');
  }
}

function json(res, status, value) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY' });
  res.end(JSON.stringify(value));
}
async function body(req) {
  if (!String(req.headers['content-type'] || '').startsWith('application/json')) throw new Error('application_json_required');
  const chunks = []; let size = 0;
  for await (const chunk of req) { size += chunk.length; if (size > 65536) throw new Error('request_too_large'); chunks.push(chunk); }
  const parsed = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('request_object_required');
  return parsed;
}

function sdkFile() {
  let directory = path.dirname(require.resolve('@volcengine/rtc'));
  for (let depth = 0; depth < 4; depth++, directory = path.dirname(directory)) {
    for (const candidate of ['index.min.js', 'dist/index.min.js']) {
      const file = path.join(directory, candidate);
      if (fs.existsSync(file)) return file;
    }
  }
  throw new Error('rtc_browser_sdk_missing');
}

function createService(options = {}) {
  const config = options.config || loadConfig(options.env);
  // No credentials means no cloud invoker is constructed and no cloud request.
  const invoke = options.invoke || (config.missing.length ? async () => { throw new Error('missing_credentials'); }
    : createOpenApiInvoker(config));
  const controller = new SessionController(config, invoke, options.now, options.audit);
  let shutdownRequested = false;
  let finalized;
  const server = http.createServer(async (req, res) => {
    const address = server.address();
    const host = '127.0.0.1:' + address.port;
    if (req.headers.host !== host) return json(res, 403, { ok: false, error: 'invalid_local_host' });
    if ((req.headers.origin && req.headers.origin !== 'http://' + host)
        || req.headers['sec-fetch-site'] === 'cross-site')
      return json(res, 403, { ok: false, error: 'same_origin_required' });
    try {
      const route = new URL(req.url, 'http://' + host).pathname;
      if (req.method === 'GET' && route === '/api/status')
        return json(res, 200, { ...controller.status(), shutdownRequested });
      if (req.method === 'GET' && route === '/api/session') {
        if (shutdownRequested) return json(res, 409, { ok: false, error: 'service_shutting_down' });
        return json(res, 200, controller.create());
      }
      if (req.method === 'POST' && route.startsWith('/api/')) {
        const value = await body(req);
        let result;
        if (route === '/api/shutdown') {
          if (value.sessionId && value.sessionId !== controller.session?.id) throw new Error('unknown_session');
          result = await requestShutdown();
          if (result.ok) res.once('finish', () => { finalize().catch(() => {}); });
        }
        else if (shutdownRequested && route !== '/api/stop')
          result = { ok: false, error: 'service_shutting_down' };
        else if (route === '/api/start') result = await controller.start(value.sessionId);
        else if (route === '/api/update') result = await controller.update(value.sessionId, value.command, value.message);
        else if (route === '/api/stop') result = await controller.stop(value.sessionId);
        else if (route === '/api/heartbeat') result = controller.heartbeat(value.sessionId);
        else return json(res, 404, { ok: false, error: 'not_found' });
        return json(res, result.ok ? 200 : 409, result);
      }
      const files = { '/': ['index.html', 'text/html'], '/index.html': ['index.html', 'text/html'],
        '/RTCClient.js': ['RTCClient.js', 'text/javascript'] };
      let file, mime;
      if (req.method === 'GET' && files[route]) { [file, mime] = files[route]; file = path.join(config.staticRoot, file); }
      else if (req.method === 'GET' && route === '/sdk/rtc.js') { file = sdkFile(); mime = 'text/javascript'; }
      else return json(res, 404, { ok: false, error: 'not_found' });
      const content = await fs.promises.readFile(file);
      res.writeHead(200, { 'Content-Type': mime + '; charset=utf-8', 'Cache-Control': 'no-store',
        'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY' });
      res.end(content);
    } catch (error) {
      const code = /^[A-Za-z0-9_]{1,80}$/.test(error.message || '') ? error.message : 'invalid_request';
      if (!res.headersSent) json(res, error.statusCode || 400, { ok: false, error: code });
    }
  });
  const finalize = () => finalized || (finalized = (async () => {
    clearInterval(timer);
    server.closeIdleConnections();
    await new Promise(resolve => server.close(resolve));
    if (options.onClosed) options.onClosed();
  })());
  const requestShutdown = async () => {
    shutdownRequested = true;
    const result = controller.session
      ? await controller.stop(controller.session.id, 'service_shutdown')
      : { ok: true, status: 'stopped' };
    return { ...result, shutdownRequested: true };
  };
  const housekeeping = async () => {
    await controller.tick();
    if (shutdownRequested && (!controller.session || controller.session.status === 'stopped')) await finalize();
  };
  const timer = setInterval(() => { housekeeping().catch(() => {}); }, 500);
  timer.unref();
  const close = async () => {
    const result = await requestShutdown();
    // Failed cloud cleanup keeps HTTP/status and retries alive. Never forget a
    // possibly billable task merely because its parent is shutting down.
    if (result.ok) await finalize();
    return result;
  };
  return { server, controller, config, close, housekeeping };
}

if (require.main === module) {
  const service = createService({ audit: event => console.log(JSON.stringify(event)) });
  service.server.listen(service.config.port, '127.0.0.1', () => {
    const port = service.server.address().port;
    console.log(JSON.stringify({ schema_version: 1, event: 'rtc_listening', timestamp: new Date().toISOString(),
      url: 'http://127.0.0.1:' + port, port, configured: !service.config.missing.length,
      missing: service.config.missing, cloudVerified: false, mediaVerified: false,
      billingStoppedVerified: false, hardwareVerified: false }));
  });
  for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => {
    service.close().then(result => {
      console.log(JSON.stringify({ event: result.ok ? 'rtc_closed' : 'rtc_cleanup_pending', ...result }));
      if (result.ok) process.exit(0);
    });
  });
}

module.exports = { loadConfig, startBody, taskBody, createOpenApiInvoker, cloudResult,
  SessionController, createService, VERSION, ENDPOINT, REGION };
