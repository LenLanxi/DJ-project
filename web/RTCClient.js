/* RTC SDK 4.69.3 integration. The TLV envelope follows volcengine/rtc-aigc-demo
 * web/src/utils/utils.ts and handler.ts (BSD-3-Clause; see License.txt and Provenance.txt).
 * No camera capture, canned intent mapping or host-side model-code execution.
 */
(function (global) {
  'use strict';
  const decoder = new TextDecoder('utf-8', { fatal: true });
  const MAX_MESSAGE = 256 * 1024;
  const FRAME_AGE_MS = 1000;

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

  function finishSpeechMessage() {
    const data = new TextEncoder().encode(JSON.stringify({ Command: 'FinishSpeechRecognition' }));
    const bytes = new Uint8Array(8 + data.length);
    bytes.set(new TextEncoder().encode('ctrl'));
    new DataView(bytes.buffer).setUint32(4, data.length, false);
    bytes.set(data, 8);
    return bytes.buffer;
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
      this.onRecordingStatus = options.onRecordingStatus || (() => {});
      this.onMicrophone = options.onMicrophone || (() => {});
      this.microphoneMuted = true;
      this.microphoneChanging = false;
      this.onFrame = options.onFrame || (() => {});
      this.onAutoplay = options.onAutoplay || (() => {});
      this.engine = this.socket = this.session = null;
      this.stream = this.track = null;
      this.bound = false;
      this.active = false;
      this.lastFrame = 0;
      this.lastFrameId = null;
      this.frameGeneration = 0;
      this.frameRequest = null;
      this.timers = [];
      this.ending = null;
      this.readyWaiters = [];
      this.requestTimeoutMs = options.requestTimeoutMs || 10000;
      this.connectionGeneration = 0;
      this.recording = null;
      this.tokenRenewal = null;
      this.botJoined = false;
      this.cloudMessages = false;
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
      if (this.recording && ['interrupt', 'frame_published'].includes(message.type)) this.recording.event('browser_send', message);
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
      this.microphoneMuted = true;
      this.microphoneChanging = false;
      this.onMicrophone(true, false);
      this.inputId = inputId; this.outputId = outputId; this.capturePaused = false;
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
        this.botJoined = false; this.cloudMessages = false;
        this.bound = false;
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
        this.timers.push(setTimeout(() => { if (live()) this.fail(new Error('达到会话时限，已结束连接')); }, session.expiresAt-Date.now()));
        await this.waitFor(() => this.bound, 8000, '本机控制授权超时');
        current();
        this.onState('正在接入语音房间…');
        this.engine = this.api.createEngine(session.appId);
        this.installEvents(generation);
        if (session.recording) {
          this.recording = new global.ReplayRecorder(session, this.fetch, text => { this.onMessage(text); this.onRecordingStatus(text); });
          this.recording.setMicrophoneMuted(true);
          this.recording.start(this.engine, this.sdk.StreamIndex ? this.sdk.StreamIndex.STREAM_INDEX_MAIN : 0);
        }
        await this.engine.setAudioPlaybackDevice(outputId);
        current();
        const mainStream = this.sdk.StreamIndex ? this.sdk.StreamIndex.STREAM_INDEX_MAIN : 0;
        const external = this.sdk.VideoSourceType ? this.sdk.VideoSourceType.VIDEO_SOURCE_TYPE_EXTERNAL : 0;
        await this.engine.setVideoSourceType(mainStream, external);
        current();
        this.stream = this.canvas.captureStream(0);
        this.track = this.stream.getVideoTracks()[0];
        if (!this.track || typeof this.track.requestFrame !== 'function') throw new Error('浏览器不支持按新帧发布画面，请使用当前Edge/Chrome');
        if (this.recording) this.recording.video(this.track);
        this.track.enabled = Boolean(this.lastFrame && Date.now()-this.lastFrame <= FRAME_AGE_MS);
        await this.engine.setExternalVideoTrack(mainStream, this.track);
        current();
        await this.engine.joinRoom(session.token, session.roomId, { userId: session.userId }, {
          isAutoPublish: false, isAutoSubscribeAudio: true, isAutoSubscribeVideo: false,
          roomProfileType: this.sdk.RoomProfileType ? this.sdk.RoomProfileType.chat : 5
        });
        current();
        await this.engine.publishStream(this.sdk.MediaType ? this.sdk.MediaType.VIDEO : 2);
        current();
        this.track.requestFrame();
        await this.post('/api/start', { sessionId: session.sessionId });
        current();
        this.showSubmittedState();
        this.timers.push(setTimeout(() => {
          if (live() && !this.botJoined && !this.cloudMessages)
            this.onState('模型尚未入房或返回消息，继续等待；可手动结束连接。');
        }, 30000));
        this.timers.push(setInterval(() => {
          if (live() && this.lastFrame && Date.now()-this.lastFrame > FRAME_AGE_MS) {
            this.clearFrame();
          }
        }, 100));
      } catch (error) {
        if (generation === this.connectionGeneration) await this.stop(String(error.message || error));
        throw error;
      }
    }

    showSubmittedState() {
      // A bot callback can arrive before StartVoiceChat's HTTP response.
      // Never overwrite an already observed conversation with "waiting".
      if (!this.cloudMessages) this.onState(this.botJoined
        ? '模型已入房，可以说话。收到回复后确认语音链路。'
        : '启动请求已提交，正在等待模型入房…');
    }

    installEvents(generation = this.connectionGeneration) {
      const events = this.api.events || this.sdk.events || {};
      const engine = this.engine;
      const on = (name, handler) => engine.on(events[name] || name, event => {
        if (this.active && generation === this.connectionGeneration && this.engine === engine) {
          if (this.recording && !name.includes('BinaryMessage')) {
            try { this.recording.event('rtc_callback', { name, value: event }); } catch (_) {}
          }
          handler(event);
        }
      });
      const receive = event => {
        if (!this.active || !this.session || event.userId !== this.session.botId) return;
        try { this.cloudMessages = true; this.rtcMessage(decodeTLV(event.message)); }
        catch (error) { this.onState('忽略无效模型消息：' + String(error.message || error)); }
      };
      on('onUserJoined', event => {
        const user = event.userInfo && event.userInfo.userId || event.userId;
        if (!this.session || user !== this.session.botId) return;
        this.botJoined = true;
        this.showSubmittedState();
      });
      on('onRoomBinaryMessageReceived', receive);
      on('onUserBinaryMessageReceived', receive);
      on('onError', error => this.onState('RTC暂时异常：' + String(error.errorCode || error.code || 'unknown')));
      for (const event of ['onTokenWillExpire', 'onTokenPublishPrivilegeWillExpire',
                           'onTokenSubscribePrivilegeWillExpire']) {
        on(event, () => this.renewToken().catch(error => {
          if (this.active && generation === this.connectionGeneration && this.engine === engine)
            this.onState('令牌续期暂时失败：' + String(error.message || error));
        }));
      }
      on('onUserLeave', event => {
        const user = event.userInfo && event.userInfo.userId || event.userId;
        if (this.session && user === this.session.botId) this.onState('模型已离开房间，等待重新入房；可手动结束连接。');
      });
      on('onAutoplayFailed', () => this.onAutoplay(true));
      on('onTrackEnded', event => { if (event.kind === 'audio') this.pauseCapture(); });
      on('onConnectionStateChanged', event => {
        if ([1, 4, 6].includes(Number(event.state))) this.onState('RTC连接暂时中断，等待SDK恢复；可手动结束连接。');
      });
      on('onAudioDeviceStateChanged', event => {
        if (event.deviceState !== 'inactive') return;
        const device = event.mediaDeviceInfo;
        if (device?.deviceId === this.inputId) this.pauseCapture();
        if (device?.deviceId === this.outputId) {
          engine.stop(this.session.botId, this.sdk.MediaType ? this.sdk.MediaType.AUDIO : 1);
          this.onAutoplay(true);
          this.onState('所选耳机已断开，播放已暂停；请查看文字，不会切换输出设备。');
        }
      });
    }

    rtcMessage(message) {
      if (!this.active || !this.session || !this.bound) return;
      this.send({ type: 'cloud_event', ...this.identity(), event: message });
      if (message.type === 'conv') {
        const states = { 1: '模型正在听', 2: '模型正在理解', 3: '模型正在回答', 4: '模型已中断', 5: '本轮回复结束' };
        const state = states[message.value.Stage?.Code];
        if (state) this.onState(state);
      } else if (message.type === 'subv') {
        for (const item of Array.isArray(message.value.data) ? message.value.data : []) {
          if (item.definite === true && typeof item.text === 'string')
            this.onMessage((item.userId === this.session.userId ? '你：' : '模型：') + item.text);
        }
      }
    }

    identity() {
      const { sessionId, roomId, taskId, userId, botId } = this.session;
      return { sessionId, roomId, taskId, userId, botId };
    }

    async bridgeMessage(message) {
      if (!message || !this.active) return;
      if (message.type === 'bound') {
        this.bound = true;
        this.notify();
      } else if (message.type === 'tool_result') {
        this.onMessage('执行：' + JSON.stringify(message.result || {}));
      } else if (message.type === 'frame') {
        await this.renderFrame(message);
      } else if (message.type === 'frame_request') {
        this.frameRequest = message;
      } else if (message.type === 'invalid_frame') {
        this.clearFrame();
      } else if (message.type === 'session_reset_required') {
        await this.stop('任务已取消。请重新连接，再说一次要求；旧会话不会恢复执行。');
      } else if (message.type === 'error') {
        this.onState('本次请求未执行：' + String(message.reason || '本机控制拒绝请求'));
      }
    }

    async renderFrame(message) {
      if (typeof message.jpeg !== 'string' || message.jpeg.length > 4000000
        || !Number.isFinite(message.timestamp) || !Number.isInteger(message.id)) return this.clearFrame();
      if (Date.now()-message.timestamp*1000 < -50 || Date.now()-message.timestamp*1000 > FRAME_AGE_MS) return this.clearFrame();
      if (this.lastFrameId !== null && message.id <= this.lastFrameId) return;
      this.lastFrameId = message.id;
      const generation = ++this.frameGeneration;
      const image = new Image();
      const decoded = await new Promise(resolve => {
        image.onload = () => resolve(true); image.onerror = () => resolve(false);
        image.src = 'data:image/jpeg;base64,' + message.jpeg;
      });
      if (!this.active || generation !== this.frameGeneration || Date.now()-message.timestamp*1000 > FRAME_AGE_MS) return;
      if (!decoded || !image.naturalWidth || image.naturalWidth > 1920 || image.naturalHeight > 1920) return this.clearFrame();
      if (this.canvas.width !== image.naturalWidth) this.canvas.width = image.naturalWidth;
      if (this.canvas.height !== image.naturalHeight) this.canvas.height = image.naturalHeight;
      this.canvas.getContext('2d').drawImage(image, 0, 0);
      this.lastFrame = message.timestamp*1000;
      if (this.recording) this.recording.frame(message.id, message.timestamp);
      if (this.track) { this.track.enabled = true; this.track.requestFrame(); }
      if (this.track && this.frameRequest && message.timestamp > this.frameRequest.after) {
        this.send({ type: 'frame_published', sessionId: this.session.sessionId,
          request_id: this.frameRequest.request_id, frame_id: message.id, timestamp: message.timestamp });
        this.frameRequest = null;
      }
      this.onFrame({ id: message.id, ageMs: Date.now()-this.lastFrame });
      this.notify();
    }

    clearFrame() {
      this.frameGeneration += 1;
      this.lastFrame = 0;
      if (this.track) this.track.enabled = false;
      const context = this.canvas.getContext('2d');
      context.fillStyle = '#101820'; context.fillRect(0, 0, this.canvas.width, this.canvas.height);
      this.onFrame(null);
      if (this.active) this.onState('图像暂不可用；语音和控制连接保留，视觉任务需等待新帧。');
    }

    async renewToken() {
      const session = this.session, engine = this.engine, generation = this.connectionGeneration;
      if (!this.active || !session || !engine) return;
      if (this.tokenRenewal?.generation === generation) return this.tokenRenewal.promise;
      const renewal = { generation, promise: null };
      this.tokenRenewal = renewal;
      renewal.promise = (async () => {
        const result = await this.post('/api/token', { sessionId: session.sessionId });
        if (!this.active || generation !== this.connectionGeneration || this.session !== session || this.engine !== engine)
          return;
        const now = Math.floor(Date.now() / 1000);
        if (result.sessionId !== session.sessionId || typeof result.token !== 'string' || !result.token
            || !Number.isInteger(result.tokenExpiresAt) || result.tokenExpiresAt <= now
            || result.tokenExpiresAt > now + 3600 || result.expiresAt !== session.expiresAt)
          throw new Error('invalid_renewed_token');
        await engine.updateToken(result.token);
        if (this.active && generation === this.connectionGeneration && this.session === session) {
          session.token = result.token;
          session.tokenExpiresAt = result.tokenExpiresAt;
        }
      })();
      try { return await renewal.promise; }
      finally { if (this.tokenRenewal === renewal) this.tokenRenewal = null; }
    }

    async interrupt(land, source = 'operator') {
      if (!this.active || !this.bound) throw new Error('本机控制尚未连接，请使用物理急停键');
      this.send({ type: 'interrupt', land: Boolean(land), source });
      this.onState(land ? '已请求降落，请核对实际状态' : '已请求停止，请核对实际状态');
    }

    async setMicrophoneMuted(muted) {
      if (!this.active || !this.engine || !this.session) throw new Error('请先连接语音');
      if (this.microphoneChanging || muted === this.microphoneMuted) return;
      const engine = this.engine, session = this.session, generation = this.connectionGeneration;
      const live = () => this.active && generation === this.connectionGeneration && engine === this.engine;
      this.microphoneChanging = true;
      this.onMicrophone(this.microphoneMuted, true);
      try {
        if (muted) {
          // Start the official turn-end signal BEFORE stopping capture, without awaiting either operation.
          let submitted;
          try { submitted = Promise.resolve(engine.sendUserBinaryMessage(session.botId, finishSpeechMessage())); }
          catch (error) { submitted = Promise.reject(error); }
          this.microphoneMuted = true;
          this.onMicrophone(true, true);
          if (this.recording) this.recording.setMicrophoneMuted(true);
          const main = this.sdk.StreamIndex ? this.sdk.StreamIndex.STREAM_INDEX_MAIN : 0;
          const track = engine.getLocalStreamTrack?.(main, 'audio');
          if (track) track.enabled = false;
          let stopped;
          try { stopped = Promise.resolve(engine.stopAudioCapture()); }
          catch (error) { stopped = Promise.reject(error); }
          const result = await Promise.allSettled([submitted, stopped]);
          if (!live()) return;
          this.onMessage(result[0].status === 'fulfilled'
            ? '已发送本轮输入结束指令；等待云端响应。' : '本轮输入提交未确认：' + String(result[0].reason));
          if (result[1].status === 'rejected') this.onMessage('麦克风轨道已静音，但关闭设备采集失败：' + String(result[1].reason));
          this.send({ type: 'input_control', ...this.identity(), action: 'mute_and_submit',
            submit: result[0].status, capture_stop: result[1].status });
        } else {
          if (this.recording) this.recording.setMicrophoneMuted(false);
          await engine.startAudioCapture(this.inputId);
          if (!live()) return;
          await engine.publishStream(this.sdk.MediaType ? this.sdk.MediaType.AUDIO : 1);
          if (!live()) return;
          this.microphoneMuted = false;
          this.capturePaused = false;
          this.send({ type: 'input_control', ...this.identity(), action: 'microphone_started' });
        }
      } catch (error) {
        if (live()) {
          this.microphoneMuted = true;
          if (this.recording) this.recording.setMicrophoneMuted(true);
          try { await engine.stopAudioCapture(); } catch (_) {}
        }
        throw error;
      } finally {
        if (live()) {
          this.microphoneChanging = false;
          this.onMicrophone(this.microphoneMuted, false);
        }
      }
    }

    async resumeAudio() {
      if (!this.engine || !this.session) return;
      const engine = this.engine, generation = this.connectionGeneration;
      if (this.capturePaused && !this.microphoneMuted) {
        await engine.startAudioCapture(this.inputId);
        if (!this.active || generation !== this.connectionGeneration || engine !== this.engine) return;
        this.capturePaused = false;
      }
      await engine.setAudioPlaybackDevice(this.outputId);
      if (!this.active || generation !== this.connectionGeneration || engine !== this.engine) return;
      await this.engine.play(this.session.botId, this.sdk.MediaType ? this.sdk.MediaType.AUDIO : 1);
      this.onAutoplay(false);
    }

    pauseCapture() {
      if (this.microphoneMuted) return;  // Intentional mute must not become a device-disconnection recovery.
      this.capturePaused = true;
      Promise.resolve(this.engine.stopAudioCapture()).catch(() => {});
      this.onAutoplay(true);
      this.onState('所选麦克风已断开，语音输入已暂停；控制连接保留。');
    }

    fail(error) {
      if (!this.active) return;
      this.stop(String(error && error.message || error)).catch(() => {});
    }

    async stop(reason = '语音连接已结束，已请求停止；如需降落请使用物理降落键或在终端结束程序') {
      if (this.ending) return this.ending;
      this.connectionGeneration += 1;
      this.active = false;
      this.microphoneMuted = true;
      this.microphoneChanging = false;
      this.onMicrophone(true, false);
      this.frameRequest = null;
      this.ending = (async () => {
        for (const timer of this.timers.splice(0)) clearTimeout(timer);
        for (const waiter of this.readyWaiters.splice(0)) { clearTimeout(waiter.timer); waiter.reject(new Error('连接已结束')); }
        if (this.socket && this.socket.readyState === 1) {
          try { this.socket.send(JSON.stringify({ type: 'interrupt', land: false,
            source: 'client_stop', reason: String(reason).slice(0, 1000) })); } catch (_) {}
        }
        const recording = this.recording;
        this.recording = null;
        const recordingDone = recording ? recording.stop() : Promise.resolve();
        const session = this.session;
        this.session = null;
        const engine = this.engine;
        this.engine = null;
        const work = [recordingDone];
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
        this.clearFrame();
        if (session) work.push(this.post('/api/stop', { sessionId: session.sessionId }).catch(error => {
          this.onMessage('云端停止未确认：' + String(error.message || error));
        }));
        await Promise.allSettled(work);
        this.onMessage('连接结束：' + reason);
        this.onState(reason);
      })();
      return this.ending;
    }
  }

  const exported = { Client, decodeTLV, explicitDevice, finishSpeechMessage };
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
      byId('microphoneToggle').disabled = !active || !client.engine || client.microphoneChanging;
      for (const id of ['devices', 'microphone', 'speaker']) byId(id).disabled = active;
    }
    const client = new Client({ sdk: global.VERTC, canvas: byId('camera'), onState: state, onMessage: add,
      onMicrophone: (muted, changing) => {
        byId('microphoneToggle').textContent = muted ? '开始说话' : '发送并静音';
        byId('microphoneToggle').disabled = !client.active || !client.engine || changing;
        byId('microphoneStatus').textContent = muted ? '麦克风已静音；说话前点击开始。' : '正在听取输入；说完点击发送并静音。';
      },
      onRecordingStatus: text => { byId('recordingStatus').textContent = text; },
      onFrame: frame => { byId('cameraStatus').textContent = frame ?
        '无人机新帧 #' + frame.id + ' · 图像年龄 ' + Math.round(frame.ageMs) + 'ms' : '无有效新帧；画面已清空'; },
      onAutoplay: failed => { byId('resumeAudio').hidden = !failed; if (failed) add('音频已暂停，请点击恢复所选设备音频。'); } });
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
    byId('microphoneToggle').onclick = () => client.setMicrophoneMuted(!client.microphoneMuted)
      .catch(error => add(String(error.message || error)));
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
