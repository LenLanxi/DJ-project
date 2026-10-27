/* RTC SDK 4.69.3 integration. The TLV envelope follows volcengine/rtc-aigc-demo
 * web/src/utils/utils.ts and handler.ts (BSD-3-Clause; see License.txt and Provenance.txt).
 * No camera capture, canned intent mapping or host-side model-code execution.
 */
(function (global) {
  'use strict';
  const encoder = new TextEncoder();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  const MAX_MESSAGE = 65536;
  const FRAME_AGE_MS = 500;

  function encodeTLV(type, payload) {
    if (!/^[a-z]{4}$/.test(type)) throw new Error('Invalid RTC message type');
    const value = encoder.encode(JSON.stringify(payload));
    if (value.length + 8 > MAX_MESSAGE) throw new Error('RTC message exceeds 64KiB');
    const output = new Uint8Array(8 + value.length);
    output.set(encoder.encode(type), 0);
    new DataView(output.buffer).setUint32(4, value.length, false);
    output.set(value, 8);
    return output.buffer;
  }

  function decodeTLV(buffer) {
    const bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer);
    if (bytes.length < 8 || bytes.length > MAX_MESSAGE) throw new Error('Invalid RTC envelope size');
    const length = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(4, false);
    if (length !== bytes.length - 8) throw new Error('RTC envelope length mismatch');
    const type = decoder.decode(bytes.subarray(0, 4));
    if (!/^[a-z]{4}$/.test(type)) throw new Error('Invalid RTC envelope type');
    const value = JSON.parse(decoder.decode(bytes.subarray(8)));
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('RTC payload must be an object');
    return { type, value };
  }

  function explicitDevice(device) {
    return device && device.deviceId && !['default', 'communications'].includes(device.deviceId)
      && !/^(default|communications|默认|通信设备)(\s|\s*-|$)/i.test(device.label || '');
  }

  function safeResult(result) {
    const compact = {
      ok: result.ok === true, status: String(result.status || 'failed'),
      reason: String(result.reason || '').slice(0, 120)
    };
    if (typeof result.simulated === 'boolean') compact.simulated = result.simulated;
    if (result.follow_cleanup?.status === 'follow_resumed') {
      compact.follow_resumed = true;
      if (typeof result.follow_cleanup.reference_adjusted === 'string')
        compact.follow_reference_adjusted = result.follow_cleanup.reference_adjusted;
    }
    if (typeof result.debug === 'string' && result.debug.trim()) {
      compact.program_output = result.debug.trim().slice(0, 180);
      if (result.debug.trim().length > 180) compact.truncated = true;
    }
    const journal = Array.isArray(result.journal) ? result.journal : [];
    const last = journal[journal.length - 1];
    if (last) {
      compact.last_method = last.method;
      const value = last.result || {};
      for (const field of ['battery', 'height_cm', 'flight_state', 'connected', 'visible', 'center_x', 'width_fraction', 'age_seconds']) {
        if (['string', 'number', 'boolean'].includes(typeof value[field]) || value[field] === null) compact[field] = value[field];
      }
      if (typeof value.answer === 'string') compact.answer = value.answer.slice(0, 100);
      if (typeof value.description === 'string') compact.description = value.description.slice(0, 80);
      if (typeof value.status === 'string') compact.last_status = value.status;
      if (typeof value.ok === 'boolean') compact.last_ok = value.ok;
      if (last.error) compact.last_error = String(last.error).slice(0, 100);
    }
    if (journal.length) compact.interface_calls = journal.length;
    return compact;
  }

  function toolReply(id, result) {
    const content = safeResult(result || {});
    let payload = { ToolCallID: id, Content: JSON.stringify(content) };
    // Default RTS is 1KiB. Do not require paid advanced messaging just to return
    // a full local journal; the complete evidence remains in the host log.
    while (encodeTLV('func', payload).byteLength > 1000) {
      const strings = ['reason', 'last_error', 'answer', 'description', 'program_output'].filter(key =>
        typeof content[key] === 'string' && content[key].length > (key === 'program_output' ? 24 : 12));
      if (!strings.length) throw new Error('RTC tool result cannot fit the basic message channel');
      strings.sort((a, b) => encoder.encode(content[b]).length - encoder.encode(content[a]).length);
      const key = strings[0];
      content[key] = content[key].slice(0, Math.max(key === 'program_output' ? 24 : 12, Math.floor(content[key].length/2)));
      content.truncated = true;
      payload = { ToolCallID: id, Content: JSON.stringify(content) };
    }
    if (encodeTLV('func', payload).byteLength > 1000) throw new Error('RTC tool reply identifier too large');
    return payload;
  }

  class Client {
    constructor(options) {
      this.sdk = options.sdk;
      this.api = this.sdk && (this.sdk.createEngine ? this.sdk : this.sdk.default);
      this.fetch = options.fetch || global.fetch.bind(global);
      this.Socket = options.WebSocket || global.WebSocket;
      this.canvas = options.canvas;
      this.onState = options.onState || (() => {});
      this.onMessage = options.onMessage || (() => {});
      this.onFrame = options.onFrame || (() => {});
      this.onAutoplay = options.onAutoplay || (() => {});
      this.engine = this.socket = this.session = null;
      this.stream = this.track = null;
      this.epoch = null;
      this.bound = false;
      this.active = false;
      this.callIds = new Set();
      this.callSignatures = new Map();
      this.pendingTools = new Set();
      this.paragraphs = new Set();
      this.pendingCalls = [];
      this.pendingTurn = null;
      this.turnOpen = false;
      this.lastFrame = 0;
      this.lastFrameId = null;
      this.frameGeneration = 0;
      this.timers = [];
      this.ending = null;
      this.readyWaiters = [];
      this.requestTimeoutMs = options.requestTimeoutMs || 10000;
      this.connectionGeneration = 0;
    }

    async request(path, body) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.requestTimeoutMs);
      try {
        const response = await this.fetch(path, { method: body === undefined ? 'GET' : 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store', signal: controller.signal });
        const value = await response.json();
        if (!response.ok || value.ok === false) throw new Error(value.reason || value.error || 'Local RTC service rejected request');
        return value;
      } finally { clearTimeout(timer); }
    }

    async post(path, body) { return this.request(path, body); }

    async devices() {
      if (!this.api) throw new Error('RTC SDK 未加载，请先安装项目 Node 依赖');
      const permission = await this.api.enableDevices({ audio: true, video: false });
      if (!permission.audio) throw new Error('麦克风权限未授予');
      const inputs = await this.api.enumerateAudioCaptureDevices();
      const outputs = await this.api.enumerateAudioPlaybackDevices();
      return { inputs: inputs.filter(explicitDevice), outputs: outputs.filter(explicitDevice) };
    }

    send(message) {
      if (!this.socket || this.socket.readyState !== 1) throw new Error('本机控制连接已断开');
      this.socket.send(JSON.stringify(message));
    }

    waitFor(predicate, timeout, label) {
      if (predicate()) return Promise.resolve();
      return new Promise((resolve, reject) => {
        const item = { predicate, resolve, reject, timer: null };
        item.timer = setTimeout(() => {
          this.readyWaiters = this.readyWaiters.filter(entry => entry !== item);
          reject(new Error(label));
        }, timeout);
        this.readyWaiters.push(item);
      });
    }

    notify() {
      for (const item of [...this.readyWaiters]) {
        if (item.predicate()) {
          clearTimeout(item.timer);
          this.readyWaiters = this.readyWaiters.filter(entry => entry !== item);
          item.resolve();
        }
      }
    }

    async connect(inputId, outputId) {
      // A canceled attempt can still be awaiting an HTTP/SDK promise. It must
      // never create media, reuse a later session, or stop that later session.
      if (this.ending) await this.ending;
      if (this.active || this.session) throw new Error('请先结束当前连接');
      if (!inputId || !outputId || ['default', 'communications'].includes(inputId)
        || ['default', 'communications'].includes(outputId)) throw new Error('请明确选择麦克风和耳机输出');
      this.ending = null;
      this.active = true;
      const generation = ++this.connectionGeneration;
      const current = () => {
        if (!this.active || generation !== this.connectionGeneration) throw new Error('连接已取消');
      };
      this.onState('正在连接本机控制与RTC配置…');
      try {
        const session = await this.request('/api/session');
        current();
        for (const name of ['sessionId', 'appId', 'token', 'roomId', 'userId', 'botId', 'taskId', 'bridgeUrl']) {
          if (typeof session[name] !== 'string' || !session[name]) throw new Error('缺少会话字段：' + name);
        }
        if (!Number.isFinite(session.expiresAt) || session.expiresAt <= Date.now()) throw new Error('会话已过期');
        const url = new URL(session.bridgeUrl);
        if (url.protocol !== 'ws:' || url.hostname !== '127.0.0.1') throw new Error('控制连接必须是本机WebSocket');
        this.session = session;
        this.epoch = null;
        this.bound = false;
        this.callIds.clear(); this.callSignatures.clear(); this.pendingTools.clear(); this.paragraphs.clear(); this.pendingCalls = [];
        this.pendingTurn = null; this.turnOpen = false;
        this.lastFrame = 0; this.lastFrameId = null;
        const socket = new this.Socket(session.bridgeUrl);
        this.socket = socket;
        const live = () => this.active && generation === this.connectionGeneration;
        socket.onopen = () => {
          if (!live()) { socket.close(); return; }
          this.send({ type: 'bind', sessionId: session.sessionId, roomId: session.roomId,
            taskId: session.taskId, botId: session.botId, userId: session.userId });
        };
        socket.onmessage = event => {
          if (!live()) return;
          try {
            const message = JSON.parse(event.data);
            Promise.resolve(this.bridgeMessage(message)).catch(error => { if (live()) this.fail(error); });
          } catch (error) { if (live()) this.fail(error); }
        };
        socket.onerror = () => { if (live()) this.fail(new Error('本机控制连接失败')); };
        socket.onclose = () => { if (live()) this.fail(new Error('本机控制连接已断开，任务已停止')); };
        this.timers.push(setInterval(() => {
          if (live()) this.heartbeat().catch(error => { if (live()) this.fail(error); });
        }, 2000));
        this.timers.push(setTimeout(() => { if (live()) this.fail(new Error('达到会话时限，已结束连接')); }, session.expiresAt-Date.now()));
        await this.waitFor(() => this.bound, 8000, '本机控制授权超时');
        current();
        await this.waitFor(() => this.lastFrame && Date.now()-this.lastFrame <= FRAME_AGE_MS, 10000, '没有新鲜无人机图像');
        current();
        this.onState('正在接入语音房间…');
        this.engine = this.api.createEngine(session.appId);
        this.installEvents(generation);
        await this.engine.setAudioPlaybackDevice(outputId);
        current();
        await this.engine.startAudioCapture(inputId);
        current();
        const mainStream = this.sdk.StreamIndex ? this.sdk.StreamIndex.STREAM_INDEX_MAIN : 0;
        const external = this.sdk.VideoSourceType ? this.sdk.VideoSourceType.VIDEO_SOURCE_TYPE_EXTERNAL : 0;
        await this.engine.setVideoSourceType(mainStream, external);
        current();
        this.stream = this.canvas.captureStream(0);
        this.track = this.stream.getVideoTracks()[0];
        if (!this.track || typeof this.track.requestFrame !== 'function') throw new Error('浏览器不支持按新帧发布画面，请使用当前Edge/Chrome');
        await this.engine.setExternalVideoTrack(mainStream, this.track);
        current();
        await this.engine.joinRoom(session.token, session.roomId, { userId: session.userId }, {
          isAutoPublish: false, isAutoSubscribeAudio: true, isAutoSubscribeVideo: false,
          roomProfileType: this.sdk.RoomProfileType ? this.sdk.RoomProfileType.chat : 5
        });
        current();
        await this.engine.publishStream(this.sdk.MediaType ? this.sdk.MediaType.AUDIO_AND_VIDEO : 3);
        current();
        this.track.requestFrame();
        await this.post('/api/start', { sessionId: session.sessionId });
        current();
        this.onState('已提交连接，等待模型就绪。可以查看图传状态。');
        this.timers.push(setInterval(() => {
          if (live() && this.lastFrame && Date.now()-this.lastFrame > FRAME_AGE_MS) {
            this.clearFrame();
            this.fail(new Error('图传超过0.5秒未更新，当前会话已停止'));
          }
        }, 100));
      } catch (error) {
        if (generation === this.connectionGeneration) await this.stop(String(error.message || error));
        throw error;
      }
    }

    installEvents(generation = this.connectionGeneration) {
      const events = this.api.events || this.sdk.events || {};
      const engine = this.engine;
      const on = (name, handler) => engine.on(events[name] || name, event => {
        if (this.active && generation === this.connectionGeneration && this.engine === engine) handler(event);
      });
      const receive = event => {
        if (!this.active || !this.session || event.userId !== this.session.botId) return;
        try { this.rtcMessage(decodeTLV(event.message)); }
        catch (error) { this.fail(error); }
      };
      on('onRoomBinaryMessageReceived', receive);
      on('onUserBinaryMessageReceived', receive);
      on('onError', error => this.fail(new Error('RTC错误：' + String(error.errorCode || error.code || 'unknown'))));
      on('onTokenWillExpire', () => this.fail(new Error('RTC授权即将过期，已结束当前会话')));
      on('onUserLeave', event => {
        const user = event.userInfo && event.userInfo.userId || event.userId;
        if (this.session && user === this.session.botId) this.fail(new Error('模型已离开房间，停止动作并结束会话'));
      });
      on('onAutoplayFailed', () => this.onAutoplay(true));
      on('onTrackEnded', event => { if (event.kind === 'audio') this.fail(new Error('耳机麦克风已断开')); });
      on('onConnectionStateChanged', event => {
        if ([1, 4, 6].includes(Number(event.state))) this.fail(new Error('RTC连接中断，停止动作并结束会话'));
      });
      on('onAudioDeviceStateChanged', event => {
        if (event.deviceState === 'inactive') this.fail(new Error('音频设备不可用，未自动切换设备'));
      });
    }

    rtcMessage(message) {
      if (!this.active || !this.session) return;
      if (message.type === 'conv') {
        const stage = Number(message.value.Stage && message.value.Stage.Code);
        const states = { 1: '模型正在听', 2: '模型正在理解', 3: '模型正在回答', 4: '模型已中断', 5: '本轮回复结束' };
        if (states[stage]) this.onState(states[stage]);
        if (stage === 4 && this.bound) {
          this.interrupt(false).catch(error => this.fail(error));
          return;
        }
        if (stage === 5 && !this.pendingTools.size && !this.pendingTurn && !this.pendingCalls.length) this.turnOpen = false;
      } else if (message.type === 'subv') {
        for (const item of Array.isArray(message.value.data) ? message.value.data : []) {
          if (item.userId === this.session.userId) {
            const paragraph = item.paragraph === null || item.paragraph === undefined ? null : String(item.paragraph);
            if ((this.pendingTools.size || this.turnOpen) && item.text && !this.paragraphs.has(paragraph)) {
              this.interrupt(false).catch(error => this.fail(error));
              return;
            }
            if (item.definite === true && item.text && paragraph !== null && !this.paragraphs.has(paragraph)) {
              const id = paragraph;
              this.paragraphs.add(id);
              this.epoch = null;
              this.pendingTurn = id;
              this.turnOpen = true;
              this.onMessage('你：' + item.text);
              this.send({ type: 'user_turn', sessionId: this.session.sessionId, roomId: this.session.roomId,
                taskId: this.session.taskId, userId: item.userId, id, text: item.text, completed: true });
            }
          } else if (item.userId === this.session.botId && item.definite === true) this.onMessage('模型：' + item.text);
        }
      } else if (message.type === 'tool') {
        const calls = message.value.tool_calls;
        if (!Array.isArray(calls) || !calls.length) throw new Error('模型工具格式无效');
        if (this.epoch === null) {
          // A final subtitle and its host acknowledgement may arrive shortly
          // before a tool event. Never label an unrelated old call with a new turn.
          if (this.pendingTurn === null || this.pendingCalls.length) throw new Error('模型工具缺少有效用户轮次');
          this.pendingCalls.push(calls);
          return;
        }
        this.forwardCalls(calls);
      }
    }

    forwardCalls(calls) {
      if (this.epoch === null || !this.bound || !this.active) throw new Error('缺少有效控制轮次');
      const fresh = [];
      for (const call of calls) {
        if (!call || typeof call.id !== 'string' || call.id.length > 128) throw new Error('无效工具编号');
        const signature = JSON.stringify(call.function);
        if (call.function && call.function.name === 'run_program') {
          let args;
          try { args = typeof call.function.arguments === 'string' ? JSON.parse(call.function.arguments) : call.function.arguments; }
          catch (_) { throw new Error('模型程序参数不是JSON'); }
          if (!args || typeof args.code !== 'string' || encoder.encode(args.code).length > 600) throw new Error('RTC模型程序超过600字节或格式无效');
        }
        if (this.callIds.has(call.id)) {
          if (this.callSignatures.get(call.id) !== signature) throw new Error('工具编号被用于不同请求');
          continue;
        }
        this.callIds.add(call.id); this.pendingTools.add(call.id);
        this.callSignatures.set(call.id, signature);
        fresh.push(call);
      }
      if (fresh.length) this.send({ type: 'tool', sessionId: this.session.sessionId, roomId: this.session.roomId,
        taskId: this.session.taskId, botId: this.session.botId, epoch: this.epoch, calls: fresh });
    }

    async bridgeMessage(message) {
      if (!message || !this.active) return;
      if (message.type === 'bound') {
        this.bound = true;
        this.notify();
      } else if (message.type === 'turn') {
        if (!Number.isInteger(message.epoch)) throw new Error('本机返回无效轮次');
        if (this.pendingTurn === null || String(message.id) !== this.pendingTurn) throw new Error('本机用户轮次不匹配');
        this.epoch = message.epoch;
        this.pendingTurn = null;
        const waiting = this.pendingCalls.splice(0);
        for (const calls of waiting) this.forwardCalls(calls);
      } else if (message.type === 'tool_result') {
        if (message.epoch !== this.epoch || !this.pendingTools.has(message.id) || !this.engine || !this.session) return;
        this.pendingTools.delete(message.id);
        const reply = toolReply(message.id, message.result);
        await this.engine.sendUserBinaryMessage(this.session.botId, encodeTLV('func', reply));
        this.onMessage('执行：' + JSON.stringify(safeResult(message.result || {})));
      } else if (message.type === 'frame') {
        await this.renderFrame(message);
      } else if (message.type === 'invalid_frame') {
        this.clearFrame();
        await this.stop('图传不可用，当前会话已停止');
      } else if (message.type === 'session_reset_required') {
        await this.stop('任务已取消。请重新连接，再说一次要求；旧会话不会恢复执行。');
      } else if (message.type === 'error') {
        throw new Error(message.reason || '本机控制拒绝请求');
      }
    }

    async renderFrame(message) {
      if (typeof message.jpeg !== 'string' || message.jpeg.length > 4000000
        || !Number.isFinite(message.timestamp) || !Number.isInteger(message.id)) throw new Error('无效图传帧');
      if (Date.now()-message.timestamp*1000 < -50 || Date.now()-message.timestamp*1000 > FRAME_AGE_MS) return;
      if (this.lastFrameId !== null && message.id <= this.lastFrameId) return;
      this.lastFrameId = message.id;
      const generation = ++this.frameGeneration;
      const image = new Image();
      await new Promise((resolve, reject) => {
        image.onload = resolve; image.onerror = () => reject(new Error('无人机图像解码失败'));
        image.src = 'data:image/jpeg;base64,' + message.jpeg;
      });
      if (!this.active || generation !== this.frameGeneration || Date.now()-message.timestamp*1000 > FRAME_AGE_MS) return;
      if (!image.naturalWidth || image.naturalWidth > 1920 || image.naturalHeight > 1920) throw new Error('无人机图像尺寸无效');
      if (this.canvas.width !== image.naturalWidth) this.canvas.width = image.naturalWidth;
      if (this.canvas.height !== image.naturalHeight) this.canvas.height = image.naturalHeight;
      this.canvas.getContext('2d').drawImage(image, 0, 0);
      this.lastFrame = message.timestamp*1000;
      if (this.track) this.track.requestFrame();
      this.onFrame({ id: message.id, ageMs: Date.now()-this.lastFrame });
      this.notify();
    }

    clearFrame() {
      this.frameGeneration += 1;
      this.lastFrame = 0;
      const context = this.canvas.getContext('2d');
      context.fillStyle = '#101820'; context.fillRect(0, 0, this.canvas.width, this.canvas.height);
      this.onFrame(null);
    }

    async heartbeat() {
      if (!this.active || !this.session) return;
      this.send({ type: 'heartbeat' });
      await this.post('/api/heartbeat', { sessionId: this.session.sessionId });
    }

    async interrupt(land) {
      if (!this.active || !this.bound) throw new Error('本机控制尚未连接，请使用物理急停键');
      this.epoch = null;
      this.pendingCalls = [];
      this.pendingTurn = null;
      this.turnOpen = false;
      this.pendingTools.clear();
      this.send({ type: 'interrupt', land: Boolean(land) });
      this.onState(land ? '已请求降落，请核对实际状态' : '已请求停止，请核对实际状态');
    }

    async resumeAudio() {
      if (!this.engine || !this.session) return;
      await this.engine.play(this.session.botId, this.sdk.MediaType ? this.sdk.MediaType.AUDIO : 1);
      this.onAutoplay(false);
    }

    fail(error) {
      if (!this.active) return;
      this.stop(String(error && error.message || error)).catch(() => {});
    }

    async stop(reason = '语音连接已结束，已请求停止；如需降落请使用物理降落键或在终端结束程序') {
      if (this.ending) return this.ending;
      this.connectionGeneration += 1;
      this.active = false;
      this.epoch = null;
      this.pendingCalls = [];
      this.pendingTurn = null;
      this.turnOpen = false;
      this.pendingTools.clear();
      this.ending = (async () => {
        for (const timer of this.timers.splice(0)) clearTimeout(timer);
        for (const waiter of this.readyWaiters.splice(0)) { clearTimeout(waiter.timer); waiter.reject(new Error('连接已结束')); }
        if (this.socket && this.socket.readyState === 1) {
          try { this.socket.send(JSON.stringify({ type: 'interrupt', land: false })); } catch (_) {}
        }
        const session = this.session;
        this.session = null;
        const engine = this.engine;
        this.engine = null;
        const work = [];
        const bounded = promise => new Promise(resolve => {
          const timer = setTimeout(resolve, 3000);
          Promise.resolve(promise).then(() => { clearTimeout(timer); resolve(); }, () => { clearTimeout(timer); resolve(); });
        });
        // Local capture/room teardown happens before waiting on any cloud HTTP.
        // A slow StopVoiceChat must not keep a microphone or RTC room alive.
        if (this.stream) for (const track of this.stream.getTracks()) { try { track.stop(); } catch (_) {} }
        this.stream = this.track = null;
        if (engine) {
          try { work.push(bounded(engine.leaveRoom())); } catch (_) {}
          try { work.push(bounded(this.api.destroyEngine(engine))); } catch (_) {}
        }
        if (this.socket) this.socket.close();
        this.socket = null;
        this.bound = false;
        this.pendingTools.clear(); this.callIds.clear(); this.callSignatures.clear(); this.paragraphs.clear();
        this.clearFrame();
        if (session) work.push(this.post('/api/stop', { sessionId: session.sessionId }).catch(error => {
          this.onMessage('云端停止未确认：' + String(error.message || error));
        }));
        await Promise.allSettled(work);
        this.onState(reason);
      })();
      return this.ending;
    }
  }

  const exported = { Client, encodeTLV, decodeTLV, toolReply, safeResult, explicitDevice };
  global.CompanionRTC = exported;
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
  if (typeof document === 'undefined') return;

  function mount() {
    const byId = id => document.getElementById(id);
    const messages = byId('messages');
    function add(text) {
      const line = document.createElement('div');
      line.textContent = new Date().toLocaleTimeString() + ' ' + text;
      messages.append(line);
      while (messages.childNodes.length > 200) messages.firstChild.remove();
      messages.scrollTop = messages.scrollHeight;
    }
    function state(text) {
      byId('status').textContent = text;
      const active = client.active;
      byId('connect').disabled = active || !byId('microphone').value || !byId('speaker').value;
      for (const id of ['disconnect', 'stop', 'land']) byId(id).disabled = !active;
      for (const id of ['devices', 'microphone', 'speaker']) byId(id).disabled = active;
    }
    const client = new Client({ sdk: global.VERTC, canvas: byId('camera'), onState: state, onMessage: add,
      onFrame: frame => { byId('cameraStatus').textContent = frame ?
        '无人机新帧 #' + frame.id + ' · 图像年龄 ' + Math.round(frame.ageMs) + 'ms' : '无有效新帧；画面已清空'; },
      onAutoplay: failed => { byId('resumeAudio').hidden = !failed; if (failed) add('浏览器阻止播放，请点击恢复耳机播放。'); } });
    byId('devices').onclick = async () => {
      try {
        const devices = await client.devices();
        for (const [id, list] of [['microphone', devices.inputs], ['speaker', devices.outputs]]) {
          const select = byId(id); select.replaceChildren(new Option('请选择具体设备', ''));
          for (const device of list) select.add(new Option(device.label || device.deviceId, device.deviceId));
        }
        state('设备已列出，请分别选择输入与输出。');
      } catch (error) { state(String(error.message || error)); }
    };
    byId('microphone').onchange = byId('speaker').onchange = () => state('设备选择已更新。');
    byId('connect').onclick = async () => {
      state('正在建立连接…');
      try { await client.connect(byId('microphone').value, byId('speaker').value); }
      catch (error) { state(String(error.message || error)); }
    };
    byId('disconnect').onclick = () => client.stop();
    byId('stop').onclick = () => client.interrupt(false).catch(error => state(error.message));
    byId('land').onclick = () => client.interrupt(true).catch(error => state(error.message));
    byId('resumeAudio').onclick = () => client.resumeAudio().catch(error => state(error.message));
    global.addEventListener('pagehide', () => {
      if (client.session) navigator.sendBeacon('/api/stop', new Blob([JSON.stringify({ sessionId: client.session.sessionId })], { type: 'application/json' }));
      client.stop('页面已关闭').catch(() => {});
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount, { once: true });
  else mount();
})(typeof globalThis !== 'undefined' ? globalThis : this);
