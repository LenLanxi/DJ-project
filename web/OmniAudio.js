/* Streaming microphone conversion shared by the AudioWorklet and offline tests.
 * The low-pass FIR precedes resampling; phase and filter history span callbacks.
 */
(function (global) {
  'use strict';
  class StreamingResampler {
    constructor(sourceRate, targetRate = 16000) {
      if (!Number.isInteger(sourceRate) || sourceRate < 8000 || sourceRate > 192000 || targetRate !== 16000)
        throw new Error('Unsupported microphone sample rate');
      this.ratio = sourceRate / targetRate;
      this.index = -1; this.outputCount = 0; this.previous = 0; this.head = 0;
      this.coefficients = null;
      if (sourceRate > targetRate) {
        const count = Math.min(255, Math.ceil(63 * sourceRate / 48000) | 1);
        const middle = (count - 1) / 2, cutoff = 7200 / sourceRate;
        this.coefficients = new Float64Array(count);
        let sum = 0;
        for (let i = 0; i < count; i++) {
          const x = i - middle;
          const sinc = x === 0 ? 2 * cutoff : Math.sin(2 * Math.PI * cutoff * x) / (Math.PI * x);
          const window = .42 - .5 * Math.cos(2 * Math.PI * i / (count - 1)) + .08 * Math.cos(4 * Math.PI * i / (count - 1));
          sum += this.coefficients[i] = sinc * window;
        }
        for (let i = 0; i < count; i++) this.coefficients[i] /= sum;
        this.history = new Float64Array(count);
      }
    }
    push(input) {
      const output = [];
      for (const sample of input) {
        if (!Number.isFinite(sample)) throw new Error('Invalid microphone sample');
        let filtered = sample;
        if (this.coefficients) {
          const count = this.coefficients.length;
          this.history[this.head] = sample;
          filtered = 0;
          for (let i = 0; i < count; i++) filtered += this.coefficients[i] * this.history[(this.head - i + count) % count];
          this.head = (this.head + 1) % count;
        }
        this.index++;
        let position = this.outputCount * this.ratio;
        while (position <= this.index) {
          const fraction = this.index === 0 ? 1 : position - (this.index - 1);
          output.push(this.previous + (filtered - this.previous) * fraction);
          position = ++this.outputCount * this.ratio;
        }
        this.previous = filtered;
      }
      return Float32Array.from(output);
    }
  }

  class PCMFramer {
    constructor() { this.frame = new Uint8Array(640); this.used = 0; }
    push(input) {
      const frames = [];
      for (const sample of input) {
        if (!Number.isFinite(sample)) throw new Error('Invalid microphone sample');
        const value = Math.max(-1, Math.min(1, sample));
        const signed = Math.round(value * (value < 0 ? 32768 : 32767));
        this.frame[this.used++] = signed & 255;
        this.frame[this.used++] = (signed >> 8) & 255;
        if (this.used === 640) {
          frames.push(this.frame);
          this.frame = new Uint8Array(640); this.used = 0;
        }
      }
      return frames;
    }
  }

  if (typeof global.registerProcessor === 'function' && global.AudioWorkletProcessor) {
    class CompanionCapture extends global.AudioWorkletProcessor {
      constructor() { super(); this.resampler = new StreamingResampler(global.sampleRate); this.framer = new PCMFramer(); }
      process(inputs, outputs) {
        for (const channel of outputs[0] || []) channel.fill(0);
        const channels = inputs[0];
        if (!channels?.length || !channels[0].length) return true;
        try {
          const mono = new Float32Array(channels[0].length);
          for (const channel of channels) for (let i = 0; i < mono.length; i++) mono[i] += channel[i] / channels.length;
          for (const pcm of this.framer.push(this.resampler.push(mono))) this.port.postMessage({ pcm: pcm.buffer }, [pcm.buffer]);
          return true;
        } catch (_error) {
          this.port.postMessage({ error: 'microphone_conversion_failed' });
          return false;
        }
      }
    }
    global.registerProcessor('companioncapture', CompanionCapture);
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = { StreamingResampler, PCMFramer };
})(globalThis);
