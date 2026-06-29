import test from 'node:test';
import assert from 'node:assert/strict';

import { parseBooleanEnv, shouldForwardInboundMessage } from './policy.js';

function matchesAllowedUser(senderId, allowedUsers) {
  return allowedUsers.has(senderId);
}

test('parseBooleanEnv defaults false and accepts common truthy values', () => {
  assert.equal(parseBooleanEnv(undefined), false);
  assert.equal(parseBooleanEnv(''), false);
  assert.equal(parseBooleanEnv('false'), false);
  assert.equal(parseBooleanEnv('true'), true);
  assert.equal(parseBooleanEnv('1'), true);
  assert.equal(parseBooleanEnv('yes'), true);
});

test('group messages bypass user allowlist for ambient Python history', () => {
  assert.equal(shouldForwardInboundMessage({
    isGroup: true,
    fromMe: false,
    whatsappMode: 'bot',
    senderId: 'stranger@s.whatsapp.net',
    allowedUsers: new Set(),
    sessionDir: '/tmp/no-session',
    matchesAllowedUser,
  }), true);
});

test('self-chat mode still rejects stranger DMs', () => {
  assert.equal(shouldForwardInboundMessage({
    isGroup: false,
    fromMe: false,
    whatsappMode: 'self-chat',
    senderId: 'stranger@s.whatsapp.net',
    allowedUsers: new Set(['stranger@s.whatsapp.net']),
    sessionDir: '/tmp/no-session',
    matchesAllowedUser,
  }), false);
});

test('bot mode DMs still require the allowlist', () => {
  assert.equal(shouldForwardInboundMessage({
    isGroup: false,
    fromMe: false,
    whatsappMode: 'bot',
    senderId: 'allowed@s.whatsapp.net',
    allowedUsers: new Set(['allowed@s.whatsapp.net']),
    sessionDir: '/tmp/no-session',
    matchesAllowedUser,
  }), true);
  assert.equal(shouldForwardInboundMessage({
    isGroup: false,
    fromMe: false,
    whatsappMode: 'bot',
    senderId: 'stranger@s.whatsapp.net',
    allowedUsers: new Set(['allowed@s.whatsapp.net']),
    sessionDir: '/tmp/no-session',
    matchesAllowedUser,
  }), false);
});

test('bot mode skips its own group echoes', () => {
  assert.equal(shouldForwardInboundMessage({
    isGroup: true,
    fromMe: true,
    whatsappMode: 'bot',
    senderId: 'bot@s.whatsapp.net',
    allowedUsers: new Set(['bot@s.whatsapp.net']),
    sessionDir: '/tmp/no-session',
    matchesAllowedUser,
  }), false);
});
