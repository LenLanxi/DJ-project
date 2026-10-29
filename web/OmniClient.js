/* Local-only PCM client. Cloud credentials and aircraft commands stay on host.
 * No camera API, intent shortcuts, automatic reconnect, or default audio route.
 */
(function (global) {
  'use strict';
  const MAX_MESSAGE = 256 * 1024, MAX_UPSTREAM = 16 * 1024, MAX_PLAYBACK_SECONDS = 6;
  const MAX_BUFFERED_AUDIO_SECONDS = 30;
  const FRAME_AGE_MS = 1000;
  function explicitDevice(device) {
    const id = typeof device === 'string' ? device : device?.deviceId;
    return typeof id === 'string' && id.length > 0 && !['default', 'communications'].includes(id.toLowerCase());
  }
  function encodePCM(bytes) {
    let value = '';
    for (const byte of bytes) value += String.fromCharCode(byte);
    return global.btoa(value);
  }
  function decodePCM(value) {
    if (typeof value !== 'string' || !value.length || value.length > MAX_MESSAGE || value.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value))
      throw new Error('invalid_audio_base64');
    const raw = global.atob(value);
    if (!raw.length || raw.length % 2) throw new Error('invalid_audio_pcm16');
    const samples = new Float32Array(raw.length / 2);
    for (let i = 0; i < samples.length; i++) {
      const unsigned = raw.charCodeAt(i * 2) | raw.charCodeAt(i * 2 + 1) << 8;
      samples[i] = (unsigned >= 32768 ? unsigned - 65536 : unsigned) / 32768;
    }
    return samples;
  }

  class Client {
    constructor(options = {}) {
      this.fetch = options.fetch || global.fetch.bind(global);
      this.Socket = options.WebSocket || global.WebSocket;
      this.media = options.mediaDevices || global.navigator?.mediaDevices;
      this.AudioContext = options.AudioContext || global.AudioContext || global.webkitAudioContext;
      this.WorkletNode = options.AudioWorkletNode || global.AudioWorkletNode;
      this.audioFactory = options.audioFactory || (() => new global.Audio());
      this.imageFactory = options.imageFactory || (() => new global.Image());
      this.now = options.now || Date.now;
      this.setInterval = options.setInterval || global.setInterval.bind(global);
      this.clearInterval = options.clearInterval || global.clearInterval.bind(global);
      this.canvas = options.canvas;
      this.onState = options.onState || (() => {});
      this.onMessage = options.onMessage || (() => {});
      this.onFrame = options.onFrame || (() => {});
      this.phaseTimeoutMs = options.phaseTimeoutMs || 10000;
      this.generation = 0; this.connection = null; this.active = false; this.sessionConsumed = false;
    }
    current(bundle) { return this.active && this.connection === bundle && bundle.generation === this.generation; }
    check(bundle) { if (!this.current(bundle)) throw new Error('connection_cancelled'); }
    async bounded(promise, milliseconds = this.phaseTimeoutMs) {
      let timer;
      try {
        return await Promise.race([promise, new Promise((_, reject) => { timer = global.setTimeout(() => reject(new Error('connection_phase_timeout')), milliseconds); })]);
      } finally { global.clearTimeout(timer); }
    }
    async connect(microphone, speaker) {
      if (!explicitDevice(microphone) || !explicitDevice(speaker)) throw new Error('请选择具体麦克风与耳机，不能使用默认设备');
      if (this.active) throw new Error('已有连接，请先结束');
      if (this.sessionConsumed) throw new Error('本次入口会话已结束，请重新启动入口后连接');
      if (!this.media?.getUserMedia || !this.AudioContext || !this.WorkletNode) throw new Error('浏览器不支持所需音频接口');
      const bundle = { generation: ++this.generation, microphone, speaker, sources: new Set(), timers: [], nodes: [], listeners: [], ready: false,
        abort: new AbortController(), nextAudioAt: 0, pendingAudio: [], pendingAudioSeconds: 0,
        lastFrameId: null, lastFrameTimestamp: null };
      this.connection = bundle; this.active = true;
      this.onState('正在准备所选耳机和麦克风');
      try {
        bundle.context = new this.AudioContext({ latencyHint: 'interactive', sinkId: speaker });
        // Called in the operator's click stack, before any network await.
        const resumed = Promise.resolve(bundle.context.resume());
        resumed.catch(() => {});
        const capture = this.media.getUserMedia({ audio: { deviceId: { exact: microphone }, channelCount: 1,
          echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false });
        capture.then(stream => { if (!this.current(bundle)) for (const track of stream.getTracks()) track.stop(); }, () => {});
        bundle.stream = await this.bounded(capture);
        this.check(bundle);
        const tracks = bundle.stream.getAudioTracks();
        if (tracks.length !== 1 || tracks[0].getSettings?.().deviceId !== microphone) throw new Error('麦克风端点未得到确认');
        bundle.trackEnded = () => this.pauseInput(bundle, '所选麦克风已断开，语音输入已暂停；控制连接保留。');
        tracks[0].addEventListener?.('ended', bundle.trackEnded);
        if (typeof bundle.context.setSinkId === 'function') {
          await this.bounded(bundle.context.setSinkId(speaker));
          this.check(bundle);
          if (bundle.context.sinkId !== speaker) throw new Error('耳机端点未得到确认');
          bundle.output = bundle.context.destination;
        } else {
          bundle.audio = this.audioFactory();
          if (typeof bundle.audio.setSinkId !== 'function') throw new Error('浏览器不能指定耳机输出，请使用支持该功能的浏览器');
          await this.bounded(bundle.audio.setSinkId(speaker));
          this.check(bundle);
          if (bundle.audio.sinkId !== speaker) throw new Error('耳机端点未得到确认');
          bundle.output = bundle.context.createMediaStreamDestination();
          bundle.audio.srcObject = bundle.output.stream;
          await this.bounded(bundle.audio.play());
          this.check(bundle);
        }
        await this.bounded(resumed);
        this.check(bundle);
        if (!Number.isInteger(bundle.context.sampleRate) || bundle.context.sampleRate < 8000 || bundle.context.sampleRate > 192000)
          throw new Error('invalid_microphone_sample_rate');
        await this.bounded(bundle.context.audioWorklet.addModule('/OmniAudio.js'));
        this.check(bundle);
        bundle.capture = new this.WorkletNode(bundle.context, 'companioncapture', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
        this.listen(bundle, bundle.capture, 'processorerror', () => {
          bundle.captureFailed = true;
          this.pauseInput(bundle, '麦克风音频处理已停止；控制连接保留，需要重新启动入口恢复语音。');
        });
        this.listen(bundle, bundle.context, 'error', () => this.pausePlayback(bundle, '音频设备错误，播放已暂停；请查看文字。'));
        this.listen(bundle, bundle.context, 'statechange', () => {
          if (bundle.ready && bundle.context.state !== 'running') {
            this.pauseInput(bundle, '音频设备已暂停，语音输入已暂停；控制连接保留。');
            this.pausePlayback(bundle, '音频设备已暂停，请恢复原设备音频或查看文字。');
          }
        });
        this.listen(bundle, bundle.context, 'sinkchange', () => {
          if (typeof bundle.context.setSinkId === 'function' && bundle.context.sinkId !== speaker)
            this.pausePlayback(bundle, '耳机输出端点发生变化，播放已暂停；不会切换输出设备。');
        });
        if (bundle.audio) this.listen(bundle, bundle.audio, 'error', () => this.pausePlayback(bundle, '耳机播放错误，请恢复原设备音频或查看文字。'));
        bundle.input = bundle.context.createMediaStreamSource(bundle.stream);
        bundle.mute = bundle.context.createGain(); bundle.mute.gain.value = 0;
        bundle.nodes.push(bundle.input, bundle.capture, bundle.mute);
        bundle.capture.port.onmessage = event => {
          if (!this.current(bundle)) return;
          try {
            if (event.data?.error) throw new Error('microphone_conversion_failed');
            if (!bundle.ready || bundle.inputPaused) return;
            const bytes = new Uint8Array(event.data.pcm);
            this.send(bundle, { type: 'audio', audio: encodePCM(bytes) });
          } catch (error) { this.pauseInput(bundle, '麦克风处理异常，语音输入已暂停；控制连接保留。'); }
        };
        bundle.input.connect(bundle.capture); bundle.capture.connect(bundle.mute); bundle.mute.connect(bundle.output);
        bundle.deviceChange = () => { void this.checkDevices(bundle); };
        this.media.addEventListener?.('devicechange', bundle.deviceChange);
        this.onState('正在连接语音服务');
        const response = await this.bounded(this.fetch('/api/session', { signal: bundle.abort.signal, cache: 'no-store' }));
        this.check(bundle);
        const session = await this.bounded(response.json());
        this.check(bundle);
        if (!response.ok || session.ok !== true) throw new Error(session.reason || 'session_request_failed');
        const url = new URL(session.url);
        if (url.protocol !== 'ws:' || !['127.0.0.1', '[::1]', 'localhost'].includes(url.hostname) || !url.searchParams.get('token'))
          throw new Error('invalid_local_session_url');
        if (!Number.isFinite(session.expiresAt) || session.expiresAt <= this.now()) throw new Error('session_expired');
        bundle.expiresAt = session.expiresAt;
        this.sessionConsumed = true; // The host deliberately admits one session.
        bundle.socket = new this.Socket(url.href);
        const ready = new Promise((resolve, reject) => { bundle.resolveReady = resolve; bundle.rejectReady = reject; });
        bundle.socket.onopen = () => {
          if (!this.current(bundle)) { bundle.socket.close(); return; }
          try { this.send(bundle, { type: 'start' }); } catch (error) { this.fail(bundle, error); }
        };
        bundle.socket.onmessage = event => {
          if (!this.current(bundle)) return;
          try { this.message(bundle, event.data); } catch (error) { this.fail(bundle, error); }
        };
        bundle.socket.onerror = () => this.fail(bundle, new Error('语音连接错误'));
        bundle.socket.onclose = () => this.fail(bundle, new Error('语音连接已关闭'));
        bundle.timers.push(this.setInterval(() => {
          if (!this.current(bundle)) return;
          try {
            if (this.now() >= bundle.expiresAt) throw new Error('本次连接已到期');
            if (bundle.lastFrameTimestamp !== null && (this.now() - bundle.lastFrameTimestamp * 1000 > FRAME_AGE_MS || this.now() < bundle.lastFrameTimestamp * 1000))
              this.clearFrame(bundle);
            this.pumpPlayback(bundle);
          } catch (error) { this.fail(bundle, error); }
        }, 100));
        await this.bounded(ready, Math.min(15000, session.expiresAt - this.now()));
        this.check(bundle);
        this.onState('语音已连接，可以提出要求');
      } catch (error) {
        if (this.current(bundle)) await this.stop(error.message || '连接失败', 'connection_error');
        else await this.dispose(bundle);
        throw error;
      }
    }
    send(bundle, value) {
      this.check(bundle);
      if (!bundle.socket || bundle.socket.readyState !== 1) throw new Error('local_socket_not_open');
      // Drop stale microphone packets under pressure; operator commands still pass.
      if (value.type === 'audio' && bundle.socket.bufferedAmount > MAX_UPSTREAM) return false;
      const raw = JSON.stringify(value);
      bundle.socket.send(raw);
      return true;
    }
    listen(bundle, target, type, callback) {
      target.addEventListener?.(type, callback);
      bundle.listeners.push([target, type, callback]);
    }
    message(bundle, raw) {
      if (typeof raw !== 'string' || new TextEncoder().encode(raw).byteLength > MAX_MESSAGE) throw new Error('invalid_server_message_size');
      const message = JSON.parse(raw);
      if (!message || typeof message !== 'object' || Array.isArray(message)) throw new Error('invalid_server_message');
      switch (message.type) {
        case 'ready': bundle.ready = true; bundle.resolveReady?.(); break;
        case 'audio':
          if (!bundle.ready) break;
          try {
            if (message.sampleRate !== undefined && message.sampleRate !== 24000 || message.sample_rate !== undefined && message.sample_rate !== 24000)
              throw new Error('invalid_output_sample_rate');
            this.play(bundle, decodePCM(message.audio));
          } catch (error) { this.onState('收到无法播放的音频片段，已跳过；文字和控制连接保留。'); }
          break;
        case 'interrupted': this.clearPlayback(bundle); break;
        case 'user_text':
          this.clearPlayback(bundle);
          this.onMessage('用户', String(message.text || '').slice(0, 12000)); break;
        case 'reply_text': this.onMessage('助手', String(message.text || '').slice(0, 12000), { delta: true }); break;
        case 'state': this.onState(String(message.text || '').slice(0, 1000)); break;
        case 'frame': this.frame(bundle, message); break;
        case 'invalid_frame': this.clearFrame(bundle); break;
        case 'error': throw new Error(String(message.reason || 'host_error').slice(0, 1000));
        default: break;
      }
    }
    play(bundle, samples) {
      if (bundle.playbackPaused) return;
      const context = bundle.context;
      const seconds = samples.length / 24000;
      const ahead = Math.max(0, bundle.nextAudioAt - context.currentTime);
      if (ahead + bundle.pendingAudioSeconds + seconds > MAX_BUFFERED_AUDIO_SECONDS)
        this.clearPlayback(bundle); // Keep recent speech instead of requiring a manual restart.
      bundle.pendingAudio.push(samples); bundle.pendingAudioSeconds += seconds;
      this.pumpPlayback(bundle);
    }
    pumpPlayback(bundle) {
      const context = bundle.context;
      if (!context || !bundle.ready) return;
      const time = context.currentTime;
      // Cloud generation may be faster than real-time. Keep a bounded PCM queue
      // and schedule only the near future, without dropping or speeding up speech.
      while (bundle.pendingAudio.length) {
        const samples = bundle.pendingAudio[0], seconds = samples.length / 24000;
        const start = Math.max(time + .015, bundle.nextAudioAt);
        if (start + seconds - time > MAX_PLAYBACK_SECONDS) break;
        const buffer = context.createBuffer(1, samples.length, 24000);
        buffer.getChannelData(0).set(samples);
        const source = context.createBufferSource();
        source.buffer = buffer; source.connect(bundle.output);
        bundle.sources.add(source);
        source.onended = () => { bundle.sources.delete(source); try { source.disconnect(); } catch (_error) {} };
        source.start(start); bundle.nextAudioAt = start + seconds;
        bundle.pendingAudio.shift(); bundle.pendingAudioSeconds = Math.max(0, bundle.pendingAudioSeconds - seconds);
      }
    }
    clearPlayback(bundle) {
      for (const source of bundle.sources) { try { source.stop(); } catch (_error) {} try { source.disconnect(); } catch (_error) {} }
      bundle.sources.clear(); bundle.nextAudioAt = bundle.context?.currentTime || 0;
      bundle.pendingAudio.length = 0; bundle.pendingAudioSeconds = 0;
    }
    pausePlayback(bundle, text) {
      if (!this.current(bundle)) return;
      this.clearPlayback(bundle); bundle.playbackPaused = true;
      this.onState(text);
    }
    pauseInput(bundle, text) {
      if (!this.current(bundle) || bundle.inputPaused) return;
      bundle.inputPaused = true;
      for (const track of bundle.stream?.getAudioTracks() || []) track.enabled = false;
      if (bundle.ready) {
        try { this.send(bundle, {type:'audio_reset'}); }
        catch (error) { this.fail(bundle,error); return; }
      }
      this.onState(text);
    }
    async resumeAudio() {
      const bundle = this.connection;
      if (!bundle || !this.current(bundle)) return;
      if (bundle.captureFailed) throw new Error('麦克风音频处理已停止，请结束连接并重新启动入口。');
      if (bundle.inputPaused) {
        const capture = this.media.getUserMedia({audio:{deviceId:{exact:bundle.microphone},
          channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
        let expired = false;
        capture.then(stream => { if (expired || !this.current(bundle)) for (const track of stream.getTracks()) track.stop(); }, () => {});
        let stream;
        try { stream = await this.bounded(capture); }
        catch (error) { expired = true; throw error; }
        if (!this.current(bundle)) { for (const track of stream.getTracks()) track.stop(); return; }
        const tracks = stream.getAudioTracks();
        if (tracks.length !== 1 || tracks[0].getSettings?.().deviceId !== bundle.microphone) {
          for (const track of stream.getTracks()) track.stop();
          throw new Error('所选麦克风端点未得到确认');
        }
        for (const track of bundle.stream.getTracks()) { track.removeEventListener?.('ended',bundle.trackEnded); track.stop(); }
        bundle.input.disconnect(); bundle.nodes = bundle.nodes.filter(node => node !== bundle.input);
        bundle.stream = stream; tracks[0].addEventListener?.('ended',bundle.trackEnded);
        bundle.input = bundle.context.createMediaStreamSource(stream); bundle.nodes.push(bundle.input);
        bundle.input.connect(bundle.capture);
      }
      if (typeof bundle.context.setSinkId === 'function') {
        await this.bounded(bundle.context.setSinkId(bundle.speaker)); this.check(bundle);
        if (bundle.context.sinkId !== bundle.speaker) throw new Error('所选耳机端点未得到确认');
      } else {
        await this.bounded(bundle.audio.setSinkId(bundle.speaker)); this.check(bundle);
        if (bundle.audio.sinkId !== bundle.speaker) throw new Error('所选耳机端点未得到确认');
        await this.bounded(bundle.audio.play()); this.check(bundle);
      }
      await this.bounded(bundle.context.resume()); this.check(bundle);
      bundle.inputPaused = bundle.playbackPaused = false;
      this.onState('已恢复所选麦克风与耳机；丢弃的旧语音不会重播。');
    }
    clearFrame(bundle) {
      bundle.frameGeneration = (bundle.frameGeneration || 0) + 1;
      bundle.lastFrameTimestamp = null; this.clearCanvas();
      this.onFrame('图像暂不可用；语音和控制连接保留。');
    }
    frame(bundle, message) {
      if (!Number.isInteger(message.id) || message.id < 0 || !Number.isFinite(message.timestamp)) return this.clearFrame(bundle);
      if (message.id === bundle.lastFrameId) return; // never refresh a repeated source frame
      if (bundle.lastFrameId !== null && message.id < bundle.lastFrameId) return;
      const age = this.now() - message.timestamp * 1000;
      if (age < 0 || age > FRAME_AGE_MS || bundle.lastFrameTimestamp !== null && message.timestamp < bundle.lastFrameTimestamp)
        return this.clearFrame(bundle);
      if (typeof message.jpeg !== 'string' || !message.jpeg.length) return this.clearFrame(bundle);
      bundle.lastFrameId = message.id; bundle.lastFrameTimestamp = message.timestamp;
      const generation = bundle.frameGeneration = (bundle.frameGeneration || 0) + 1;
      const image = this.imageFactory();
      image.onload = () => {
        if (!this.current(bundle) || generation !== bundle.frameGeneration || this.now() - message.timestamp * 1000 > FRAME_AGE_MS) return;
        const context = this.canvas?.getContext('2d');
        if (context) context.drawImage(image, 0, 0, this.canvas.width, this.canvas.height);
        this.onFrame('无人机画面 · 帧 ' + message.id);
      };
      image.onerror = () => { if (this.current(bundle)) this.clearFrame(bundle); };
      image.src = 'data:image/jpeg;base64,' + message.jpeg;
    }
    async checkDevices(bundle) {
      try {
        const devices = await this.media.enumerateDevices();
        if (!this.current(bundle)) return;
        for (const [kind, id] of [['audioinput', bundle.microphone], ['audiooutput', bundle.speaker]])
          if (!devices.some(device => device.kind === kind && device.deviceId === id && explicitDevice(device))) {
            if (kind === 'audioinput') this.pauseInput(bundle, '所选麦克风已断开，语音输入已暂停；控制连接保留。');
            else this.pausePlayback(bundle, '所选耳机已断开，播放已暂停；不会切换输出设备。');
          }
      } catch (error) { if (this.current(bundle)) this.onState('暂时无法检查音频设备；控制连接保留。'); }
    }
    interrupt(land = false) {
      const bundle = this.connection;
      if (!bundle || !this.current(bundle)) return;
      this.clearPlayback(bundle);
      try { this.send(bundle, { type: 'interrupt', land: land === true }); }
      catch (error) { this.fail(bundle, error); }
    }
    fail(bundle, error) {
      if (!this.current(bundle)) return;
      bundle.rejectReady?.(error);
      void this.stop(error.message || '连接已停止', 'client_error');
    }
    async stop(reason = '连接已结束', source = 'operator') {
      const bundle = this.connection;
      if (bundle) bundle.stopReport = {
        type: 'stop', source, reason: String(reason).slice(0, 512), diagnostics: {
          scheduled_audio_seconds: Math.max(0, bundle.nextAudioAt - (bundle.context?.currentTime || 0)),
          pending_audio_seconds: bundle.pendingAudioSeconds,
          audio_state: String(bundle.context?.state || 'unavailable').slice(0, 32),
          frame_age_ms: bundle.lastFrameTimestamp === null ? null : this.now() - bundle.lastFrameTimestamp * 1000
        }
      };
      this.connection = null; this.active = false; this.generation++;
      if (!bundle) return;
      bundle.rejectReady?.(new Error(reason));
      this.onState(reason);
      this.clearCanvas();
      await this.dispose(bundle);
    }
    clearCanvas() {
      const context = this.canvas?.getContext('2d');
      if (context) { context.fillStyle = '#101820'; context.fillRect(0, 0, this.canvas.width, this.canvas.height); }
      this.onFrame('图传尚未连接');
    }
    async dispose(bundle) {
      // Everything before the first await revokes local media synchronously.
      bundle.abort.abort(); bundle.ready = false;
      for (const timer of bundle.timers.splice(0)) this.clearInterval(timer);
      for (const [target, type, callback] of bundle.listeners.splice(0)) target.removeEventListener?.(type, callback);
      this.media.removeEventListener?.('devicechange', bundle.deviceChange);
      for (const track of bundle.stream?.getTracks() || []) { track.removeEventListener?.('ended', bundle.trackEnded); track.stop(); }
      if (bundle.capture) { bundle.capture.port.onmessage = null; bundle.capture.port.close?.(); }
      for (const node of bundle.nodes.splice(0)) { try { node.disconnect(); } catch (_error) {} }
      this.clearPlayback(bundle);
      if (bundle.audio) { bundle.audio.pause(); bundle.audio.srcObject = null; bundle.audio.removeAttribute?.('src'); }
      if (bundle.socket) {
        bundle.socket.onopen = bundle.socket.onmessage = bundle.socket.onerror = bundle.socket.onclose = null;
        try { if (bundle.socket.readyState === 1 && bundle.socket.bufferedAmount <= MAX_UPSTREAM) bundle.socket.send(JSON.stringify(bundle.stopReport || { type: 'stop' })); } catch (_error) {}
        try { bundle.socket.close(); } catch (_error) {}
      }
      try { if (bundle.context && bundle.context.state !== 'closed') await this.bounded(bundle.context.close(), 2000); } catch (_error) {}
    }
  }

  function mount() {
    const element = id => global.document.getElementById(id);
    if (!element('microphone')) return;
    const microphone = element('microphone'), speaker = element('speaker');
    const messages = element('messages');
    let listing = false, replyRow = null;
    function displayState(text) {
      return /[\u4e00-\u9fff]/.test(text) ? text : '音频或连接准备失败，请检查设备后重新启动入口。';
    }
    const client = new Client({ canvas: element('camera'),
      onState(text) { element('status').textContent = displayState(text); refresh(); },
      onFrame(text) { element('cameraStatus').textContent = text; },
      onMessage(who, text, metadata) {
        if (who === '助手' && metadata?.delta && replyRow) replyRow.textContent = (replyRow.textContent + text).slice(0, 24000);
        else {
          const row = global.document.createElement('div'); row.textContent = who + '：' + text;
          messages.appendChild(row); replyRow = who === '助手' ? row : null;
        }
        while (messages.children.length > 80) messages.firstChild.remove(); messages.scrollTop = messages.scrollHeight;
      }
    });
    function refresh() {
      element('connect').disabled = client.active || client.sessionConsumed || listing || !explicitDevice(microphone.value) || !explicitDevice(speaker.value);
      element('disconnect').disabled = element('stop').disabled = element('land').disabled = !client.active;
      element('resumeAudio').disabled = !client.active;
      microphone.disabled = speaker.disabled = element('devices').disabled = client.active || listing;
    }
    element('devices').onclick = async () => {
      let stream;
      listing = true; refresh();
      try {
        // Permission-only probe: close immediately, then the operator selects
        // exact endpoints before a production session can start.
        stream = await global.navigator.mediaDevices.getUserMedia({ audio: true, video: false });
        for (const track of stream.getTracks()) track.stop(); stream = null;
        const devices = await global.navigator.mediaDevices.enumerateDevices();
        for (const [select, kind] of [[microphone, 'audioinput'], [speaker, 'audiooutput']]) {
          select.replaceChildren(new global.Option('请选择具体设备', ''));
          for (const device of devices.filter(device => device.kind === kind && explicitDevice(device))) select.add(new global.Option(device.label || '未命名音频设备', device.deviceId));
        }
        element('status').textContent = '请选择本次使用的麦克风和耳机，再连接';
      } catch (error) { element('status').textContent = error.message || '无法列出音频设备'; }
      finally { for (const track of stream?.getTracks() || []) track.stop(); listing = false; refresh(); }
    };
    microphone.onchange = speaker.onchange = refresh;
    element('connect').onclick = () => { void client.connect(microphone.value, speaker.value).catch(error => { if (!client.active && error.message !== 'connection_cancelled') element('status').textContent = displayState(error.message || '连接失败'); refresh(); }); };
    element('disconnect').onclick = () => { void client.stop(); };
    element('resumeAudio').onclick = () => { void client.resumeAudio().catch(error => { element('status').textContent=displayState(error.message); }); };
    element('stop').onclick = () => client.interrupt(false);
    element('land').onclick = () => client.interrupt(true);
    global.addEventListener('pagehide', () => { void client.stop('页面已关闭', 'pagehide'); });
    client.clearCanvas(); refresh();
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = { Client, explicitDevice, decodePCM, encodePCM, MAX_UPSTREAM, MAX_PLAYBACK_SECONDS, MAX_BUFFERED_AUDIO_SECONDS };
  else { global.CompanionOmni = { Client }; if (global.document) global.addEventListener('DOMContentLoaded', mount); }
})(globalThis);
