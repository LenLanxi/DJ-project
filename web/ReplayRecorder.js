/* Lossless float PCM from the existing RTC tracks; never opens another microphone. */
(function (global) {
  'use strict';
  function wav(blocks, channels, rate) {
    const length = blocks.reduce((n, block) => n + block.length, 0);
    const bytes = new ArrayBuffer(44 + length * 4), v = new DataView(bytes);
    const tag = (offset, text) => [...text].forEach((c, i) => v.setUint8(offset + i, c.charCodeAt(0)));
    tag(0, 'RIFF'); v.setUint32(4, bytes.byteLength - 8, true); tag(8, 'WAVE'); tag(12, 'fmt ');
    v.setUint32(16, 16, true); v.setUint16(20, 3, true); v.setUint16(22, channels, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate * channels * 4, true);
    v.setUint16(32, channels * 4, true); v.setUint16(34, 32, true);
    tag(36, 'data'); v.setUint32(40, length * 4, true);
    let offset = 44;
    for (const block of blocks) for (const sample of block) { v.setFloat32(offset, sample, true); offset += 4; }
    return bytes;
  }

  class ReplayRecorder {
    constructor(session, fetcher, notice) {
      this.session = session; this.fetch = fetcher; this.notice = notice;
      this.sequence = 0; this.pending = 0; this.chain = Promise.resolve();
      this.savedAudio = new Set();
      this.frames = [];
      this.samples = { mic: 0, model: 0 };
      this.audio = {}; this.failed = false; this.stopped = false;
    }
    error(error) {
      this.failed = true;
      this.notice('复盘录制不完整：' + String(error.message || error));
    }
    upload(kind, data, metadata = {}) {
      if (this.pending >= 30) { this.error('本地保存队列积压，当前分段未保存'); return; }
      const sequence = ++this.sequence;
      this.pending++;
      const meta = { ...metadata, kind, sequence, browserTimestamp: Date.now(), browserMonotonic: performance.now() };
      this.chain = this.chain.then(async () => {
        let last;
        for (let attempt = 0; attempt < 2; attempt++) {
          const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 5000);
          try {
            const response = await this.fetch('/api/record?sessionId=' + encodeURIComponent(this.session.sessionId), {
              method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Replay-Meta': JSON.stringify(meta) },
              body: data, signal: controller.signal });
            if (!response.ok) throw new Error('保存失败 HTTP ' + response.status);
            if (['mic', 'model'].includes(kind) && !this.savedAudio.has(kind)) {
              this.savedAudio.add(kind);
              if (!this.failed) this.notice(this.savedAudio.size === 2
                ? '麦克风和模型语音均已实际落盘，复盘录制进行中。'
                : (kind === 'mic' ? '麦克风' : '模型语音') + '已实际落盘，等待另一路音频。');
            }
            return;
          } catch (error) { last = error; }
          finally { clearTimeout(timer); }
        }
        throw last;
      }).catch(error => this.error(error)).finally(() => { this.pending--; });
    }
    event(event, details = {}) {
      this.upload('event', new TextEncoder().encode(JSON.stringify({ event, ...details })), {});
    }
    frame(id, decodedAt) {
      this.frames.push({ id, decodedAt, renderedAt: Date.now() });
      if (this.frames.length >= 10) this.flushFrames();
    }
    flushFrames() {
      if (this.frames.length) this.event('frames_rendered', { frames: this.frames.splice(0) });
    }
    receive(kind, frame) {
      if (this.stopped || kind === 'mic' && this.microphoneMuted) return;
      try {
        const { channels, channelCount, sampleRate } = frame;
        this.samples[kind] += channels[0].length;
        let state = this.audio[kind];
        if (state && (state.rate !== sampleRate || state.channels !== channelCount)) this.flush(kind);
        state = this.audio[kind] ||= { blocks: [], samples: 0, rate: sampleRate, channels: channelCount,
          start: Date.now() - channels[0].length / sampleRate * 1000 };
        const interleaved = new Float32Array(channels[0].length * channelCount);
        for (let i = 0; i < channels[0].length; i++)
          for (let c = 0; c < channelCount; c++) interleaved[i * channelCount + c] = channels[c][i];
        state.blocks.push(interleaved); state.samples += channels[0].length;
        if (state.samples >= sampleRate) this.flush(kind);
      } catch (error) { this.error(error); }
    }
    flush(kind) {
      const state = this.audio[kind];
      if (!state) return;
      delete this.audio[kind];
      this.upload(kind, wav(state.blocks, state.channels, state.rate), {
        estimatedStart: state.start, samples: state.samples, sampleRate: state.rate, channels: state.channels });
    }
    setMicrophoneMuted(muted) {
      this.microphoneMuted = muted;
      if (muted) this.flush('mic');
      this.event('microphone_recording_state', { muted });
    }
    start(engine, mainStream) {
      this.engine = engine; this.mainStream = mainStream;
      try {
        engine.setAudioFrameCallback(mainStream, undefined, frame => this.receive('mic', frame));
        engine.setAudioFrameCallback(mainStream, this.session.botId, frame => this.receive('model', frame));
        this.event('recording_started', { timeOrigin: performance.timeOrigin, userAgent: navigator.userAgent });
        this.notice('复盘录制已开启：麦克风仅在开始说话后录制，模型语音和图传持续记录。');
      } catch (error) { this.error(error); }
    }
    video(track) {
      try {
        this.videoRecorder = new MediaRecorder(new MediaStream([track]), { mimeType: 'video/webm;codecs=vp8' });
        this.videoRecorder.ondataavailable = event => {
          if (event.data.size) this.upload('video', event.data, { mimeType: this.videoRecorder.mimeType });
        };
        this.videoRecorder.onerror = event => this.error(event.error || 'video_recorder_error');
        this.videoRecorder.start(1000);
        this.event('video_recording_started');
      } catch (error) { this.error(error); }
    }
    async stop() {
      if (this.stopped) return this.chain;
      this.stopped = true;
      try {
        this.engine.setAudioFrameCallback(this.mainStream, undefined, undefined);
        this.engine.setAudioFrameCallback(this.mainStream, this.session.botId, undefined);
      } catch (error) { this.error(error); }
      if (this.videoRecorder && this.videoRecorder.state !== 'inactive') {
        await new Promise(resolve => {
          const timer = setTimeout(() => { this.error('视频末段保存超时'); resolve(); }, 2000);
          this.videoRecorder.onstop = () => { clearTimeout(timer); resolve(); };
          this.videoRecorder.stop();
        });
      }
      this.flushFrames();
      this.flush('mic'); this.flush('model');
      this.event('recording_stopped', { incomplete: this.failed, samples: this.samples });
      await this.chain;
      this.notice(this.failed ? '复盘录制存在缺失，请保留现场录像。' : '音频分段已保存；请等终端退出后再关闭页面。');
    }
  }
  global.ReplayRecorder = ReplayRecorder;
  if (typeof module !== 'undefined') module.exports = { ReplayRecorder, wav };
})(typeof globalThis !== 'undefined' ? globalThis : this);
