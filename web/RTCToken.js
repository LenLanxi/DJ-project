/**
 * RTC token wire format adapted from volcengine/rtc-aigc-demo/server/token.js.
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd.
 * SPDX-License-Identifier: BSD-3-Clause. See License.txt and Provenance.txt.
 */
'use strict';
const crypto = require('node:crypto');

function uint16(value) { const out = Buffer.alloc(2); out.writeUInt16LE(value); return out; }
function uint32(value) { const out = Buffer.alloc(4); out.writeUInt32LE(value); return out; }
function bytes(value) {
  const out = Buffer.isBuffer(value) ? value : Buffer.from(value, 'utf8');
  if (out.length > 65535) throw new Error('token_field_too_large');
  return Buffer.concat([uint16(out.length), out]);
}

function createToken(appId, appKey, roomId, userId, expiresAt, options = {}) {
  for (const value of [appId, appKey, roomId, userId]) {
    if (typeof value !== 'string' || !value) throw new Error('missing_token_field');
  }
  const now = options.now ?? Math.floor(Date.now() / 1000);
  if (!Number.isInteger(expiresAt) || expiresAt <= now || expiresAt > now + 3600)
    throw new Error('token_expiry_must_be_short_lived');
  const nonce = options.nonce ?? crypto.randomBytes(4).readUInt32LE();
  // Official PrivPublishStream=0 expands to audio/video/data privileges 1..3;
  // PrivSubscribeStream=4. Privilege expiry and overall expiry are both bounded.
  const privileges = Buffer.concat([uint16(5), ...[0, 1, 2, 3, 4].flatMap(
    value => [uint16(value), uint32(expiresAt)])]);
  const message = Buffer.concat([uint32(nonce), uint32(now), uint32(expiresAt),
    bytes(roomId), bytes(userId), privileges]);
  const signature = crypto.createHmac('sha256', appKey).update(message).digest();
  return '001' + appId + Buffer.concat([bytes(message), bytes(signature)]).toString('base64');
}

module.exports = { createToken };
