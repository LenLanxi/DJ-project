'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { Readable } = require('node:stream');
const { wav, ReplayRecorder } = require('../../web/ReplayRecorder');
const { saveRecording } = require('../../web/ReplayStore');
const { createService, loadConfig } = require('../../web/RTCService');

test('float WAV preserves original samples without lossy speech encoding', () => {
  const data = wav([new Float32Array([0.25, -0.75])], 1, 48000);
  const v = new DataView(data);
  assert.equal(v.getUint16(20, true), 3);
  assert.equal(v.getUint32(24, true), 48000);
  assert.equal(v.getFloat32(44, true), 0.25);
  assert.equal(v.getFloat32(48, true), -0.75);
});

test('SDK audio callbacks save microphone and model separately and flush the last partial second', async () => {
  const uploaded = [], callbacks = new Map();
  const recorder = new ReplayRecorder({ sessionId: 'test', botId: 'bot' }, async (_, request) => {
    uploaded.push({ meta: JSON.parse(request.headers['X-Replay-Meta']), body: request.body });
    return { ok: true };
  }, () => {});
  const engine = { setAudioFrameCallback(index, user, callback) { callbacks.set(user || 'local', callback); } };
  recorder.start(engine, 0);
  const frame = { channels: [new Float32Array([0.1, 0.2])], channelCount: 1, sampleRate: 48000 };
  callbacks.get('local')(frame); callbacks.get('bot')(frame);
  await recorder.stop();
  assert.equal(uploaded.filter(row => row.meta.kind === 'mic').length, 1);
  assert.equal(uploaded.filter(row => row.meta.kind === 'model').length, 1);
  assert.equal(uploaded.find(row => row.meta.kind === 'mic').meta.samples, 2);
  assert.equal(recorder.pending, 0);
  assert.equal(callbacks.get('local'), undefined);
});

test('recording upload retries do not duplicate PCM and retain time metadata', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'ReplayTest'));
  try {
    const bytes = Buffer.from(wav([new Float32Array([0.25])], 1, 48000));
    const send = () => {
      const req = Readable.from([bytes]);
      req.headers = { 'x-replay-meta': JSON.stringify({ kind: 'mic', sequence: 1, browserTimestamp: 1000 }) };
      return saveRecording(req, root, 'Session');
    };
    const first = await send(); await send();
    assert.deepEqual(await fs.readFile(path.join(root, 'Audio', 'Session', first.file)), bytes);
    const meta = JSON.parse(await fs.readFile(path.join(root, 'Audio', 'Session', first.file + '.json'), 'utf8'));
    assert.equal(meta.browserTimestamp, 1000);
    assert.ok(meta.serverReceivedAt > 0);
  } finally {
    if (path.dirname(root) !== path.resolve(os.tmpdir())) throw new Error('unexpected test directory');
    await fs.rm(root, { recursive: true });
  }
});

test('operator mute flushes the microphone tail and excludes later microphone PCM only', async () => {
  const uploaded = [];
  const recorder = new ReplayRecorder({ sessionId: 'test', botId: 'bot' }, async (_, request) => {
    uploaded.push(JSON.parse(request.headers['X-Replay-Meta'])); return { ok: true };
  }, () => {});
  const frame = { channels: [new Float32Array([0.1])], channelCount: 1, sampleRate: 48000 };
  recorder.receive('mic', frame);
  recorder.setMicrophoneMuted(true);
  recorder.receive('mic', frame); recorder.receive('model', frame);
  recorder.flush('model'); await recorder.chain;
  assert.equal(uploaded.filter(row => row.kind === 'mic').length, 1);
  assert.equal(uploaded.find(row => row.kind === 'mic').samples, 1);
  assert.equal(uploaded.filter(row => row.kind === 'model').length, 1);
});

test('HTTP recording requires the active local session and does not expose the archive path', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'ReplayTest'));
  const config = loadConfig({ RTC_APP_ID: 'testapp', RTC_APP_KEY: 'testkey', RTC_RECORDING_DIR: root });
  config.missing = [];
  const service = createService({ config, invoke: async () => { throw new Error('cloud_not_used'); } });
  try {
    await new Promise(resolve => service.server.listen(0, '127.0.0.1', resolve));
    const base = 'http://127.0.0.1:' + service.server.address().port;
    const session = await (await fetch(base + '/api/session')).json();
    assert.equal(session.recording, true);
    assert.equal(JSON.stringify(session).includes(root), false);
    const upload = id => fetch(base + '/api/record?sessionId=' + id, { method: 'POST',
      headers: { 'X-Replay-Meta': JSON.stringify({ kind: 'mic', sequence: 1 }) },
      body: Buffer.from(wav([new Float32Array(48000)], 1, 48000)) });
    assert.equal((await upload('wrong')).status, 403);
    assert.equal((await upload(session.sessionId)).status, 200);
    assert.equal((await fs.stat(path.join(root, 'Audio', session.sessionId, 'mic0000000001.wav'))).size, 192044);
  } finally {
    await service.close();
    if (path.dirname(root) !== path.resolve(os.tmpdir())) throw new Error('unexpected test directory');
    await fs.rm(root, { recursive: true });
  }
});
