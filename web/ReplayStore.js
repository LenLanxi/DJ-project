'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');

async function saveRecording(req, root, sessionId) {
  const meta = JSON.parse(req.headers['x-replay-meta'] || '{}');
  if (!['mic', 'model', 'video', 'event'].includes(meta.kind) || !Number.isSafeInteger(meta.sequence)
      || meta.sequence < 1 || meta.sequence > 1000000000) throw new Error('invalid_recording_metadata');
  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 32000000) throw new Error('recording_segment_too_large');
    chunks.push(chunk);
  }
  const folder = path.join(root, 'Audio', sessionId);
  await fs.mkdir(folder, { recursive: true });
  const file = meta.kind + String(meta.sequence).padStart(10, '0') + (meta.kind === 'event' ? '.json' : meta.kind === 'video' ? '.webm' : '.wav');
  const bytes = Buffer.concat(chunks);
  // Retrying an acknowledged-but-lost HTTP response replaces the same segment, never appends audio twice.
  await fs.writeFile(path.join(folder, file), bytes);
  await fs.writeFile(path.join(folder, file + '.json'), JSON.stringify({ ...meta,
    serverReceivedAt: Date.now(), bytes: size, file }, null, 2), 'utf8');
  return { ok: true, file };
}
module.exports = { saveRecording };
