import { createHash } from 'node:crypto';

const MAX_SESSION_ID_CHARS = 160;
const MAX_CLIENT_CHARS = 64;
const MAX_TURN_CHARS = 160;
const MAX_TEXT_CHARS = 2000;
const MAX_MESSAGES = 24;
const DEFAULT_LIMIT = 8;
const GATEWAY_ROLES = new Set(['user', 'assistant']);

function clean(value, max) {
  return String(value ?? '').replace(/\s+/g, ' ').trim().slice(0, max);
}

export function parseBearerAuthorization(request) {
  const header = String(request?.headers?.authorization ?? '').trim();
  const match = header.match(/^Bearer\s+(.+)$/i);
  return match ? match[1].trim() : null;
}

function gatewaySessionId(value) {
  const raw = String(value ?? '').trim();
  if (!raw) throw new Error('session_id 是必填项');
  if (/[\u0000-\u001f\u007f]/.test(raw)) throw new Error('session_id 包含控制字符');
  const normalized = raw.split(':')
    .map((segment) => segment.trim().replace(/[^A-Za-z0-9._-]+/g, '-'))
    .join(':');
  if (normalized.length <= MAX_SESSION_ID_CHARS) return normalized;
  const prefix = normalized.slice(0, MAX_SESSION_ID_CHARS - 25);
  const suffix = createHash('sha256').update(normalized, 'utf8').digest('hex').slice(0, 24);
  return `${prefix}:${suffix}`;
}

function messageText(value) {
  return String(value ?? '')
    .replace(/\r\n?/g, '\n')
    .trim()
    .slice(0, MAX_TEXT_CHARS);
}

function rejectSensitiveText(text) {
  const credentialPattern = /(api[_-]?key|authorization|bearer\s+[a-z0-9._-]+|cookie|password|passwd|secret|service[_-]?token|session[_-]?token|access[_-]?token|refresh[_-]?token)\s*[:=]/i;
  const contextPattern = /(Xinchao Recent Context|Recalled Memory|Core Memory|Memory Detail Request|Memory Reading Policy|Targeted Memory Detail|Diffused Memory)/i;
  if (credentialPattern.test(text) || contextPattern.test(text)) {
    throw new Error('message text 包含凭据或注入块，已拒绝');
  }
}

export function validateGatewayContinuityRequest(payload, options = {}) {
  const sessionId = gatewaySessionId(payload?.session_id);
  const client = clean(payload?.client ?? 'unknown', MAX_CLIENT_CHARS) || 'unknown';
  const messages = Array.isArray(payload?.messages) ? payload.messages.slice(-MAX_MESSAGES) : [];
  if (!messages.length) throw new Error('messages 必须是非空数组');
  const seenTurnIds = new Set();
  const normalized = messages.map((message, index) => {
    const role = clean(message?.role, 24).toLowerCase();
    if (!GATEWAY_ROLES.has(role)) throw new Error('role 只能是 user 或 assistant');
    const turnId = clean(message?.turn_id, MAX_TURN_CHARS);
    if (!turnId) throw new Error('每条消息都需要 turn_id');
    if (seenTurnIds.has(turnId)) throw new Error(`重复 turn_id：${turnId}`);
    seenTurnIds.add(turnId);
    const text = messageText(message?.text);
    if (!text) throw new Error('每条消息都需要 text');
    rejectSensitiveText(text);
    return { turnId, role, text, index };
  });
  const requestedLimit = Number(payload?.limit ?? options.defaultLimit ?? DEFAULT_LIMIT);
  const limit = Number.isFinite(requestedLimit)
    ? Math.max(1, Math.min(MAX_MESSAGES, Math.floor(requestedLimit)))
    : DEFAULT_LIMIT;
  return { sessionId, client, messages: normalized, limit };
}
