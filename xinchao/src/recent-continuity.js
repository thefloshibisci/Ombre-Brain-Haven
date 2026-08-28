import { createHash } from 'node:crypto';

const MAX_ITEMS = 24;
const MAX_TEXT_CHARS = 2000;
const MAX_PROFILES = 16;
const DEFAULT_TTL_HOURS = 24;

function clean(value, max = 120) {
  return String(value ?? '').replace(/\s+/g, ' ').trim().slice(0, max);
}

function profileKey(value) {
  return clean(value || 'default', 120) || 'default';
}

function fingerprint(value) {
  return createHash('sha256').update(String(value ?? ''), 'utf8').digest('hex').slice(0, 24);
}

function ttlHours(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(1, Math.min(168, parsed)) : DEFAULT_TTL_HOURS;
}

function normalizeRole(value) {
  const role = clean(value || 'user', 24).toLowerCase();
  return ['user', 'assistant', 'system', 'tool'].includes(role) ? role : 'user';
}

export function newContinuityState() {
  return { schemaVersion: 1, profiles: {} };
}

export function pruneRecentContinuity(input, now = new Date()) {
  const state = structuredClone(input ?? newContinuityState());
  const cutoff = new Date(now).getTime();
  state.profiles = Object.fromEntries(
    Object.entries(state.profiles ?? {})
      .map(([profile, items]) => [
        profile,
        (Array.isArray(items) ? items : [])
          .filter((item) => Date.parse(item?.expiresAt ?? '') > cutoff),
      ])
      .filter(([, items]) => items.length > 0)
  );
  state.schemaVersion = Math.max(1, Number(state.schemaVersion) || 0);
  return state;
}

export function recordRecentTurn(input, {
  profileId = 'default', sessionId, turnId, role, text, source = 'unknown',
  ttlHours: ttl = DEFAULT_TTL_HOURS, now = new Date(),
} = {}) {
  const state = pruneRecentContinuity(input, now);
  const profile = profileKey(profileId);
  const cleanSession = clean(sessionId, 160);
  const cleanTurn = clean(turnId, 160);
  const cleanText = clean(text, MAX_TEXT_CHARS);
  if (!cleanSession) throw new Error('session_id 是必填项');
  if (!cleanTurn) throw new Error('turn_id 是必填项');
  if (!cleanText) throw new Error('text 是必填项');
  const createdAt = new Date(now);
  const eventId = fingerprint(`${profile}\0${cleanSession}\0${cleanTurn}\0${normalizeRole(role)}`);
  const bucket = Array.isArray(state.profiles?.[profile]) ? state.profiles[profile] : [];
  if (bucket.some((item) => item?.eventId === eventId)) return { state, duplicate: true, eventId };
  const item = {
    eventId, sessionId: cleanSession, turnId: cleanTurn, role: normalizeRole(role),
    source: clean(source, 64) || 'unknown', text: cleanText,
    createdAt: createdAt.toISOString(),
    expiresAt: new Date(createdAt.getTime() + ttlHours(ttl) * 3_600_000).toISOString(),
  };
  const active = [...bucket, item]
    .filter((entry) => Date.parse(entry?.expiresAt ?? '') > createdAt.getTime())
    .sort((a, b) => Date.parse(a.createdAt) - Date.parse(b.createdAt))
    .slice(-MAX_ITEMS);
  state.profiles = { ...(state.profiles ?? {}), [profile]: active };
  const profiles = Object.entries(state.profiles)
    .sort((a, b) => (b[1]?.at(-1)?.createdAt ?? '').localeCompare(a[1]?.at(-1)?.createdAt ?? ''))
    .slice(0, MAX_PROFILES);
  state.profiles = Object.fromEntries(profiles);
  state.schemaVersion = Math.max(1, Number(state.schemaVersion) || 0);
  return { state, duplicate: false, eventId };
}

export function recentTurns(input, {
  profileId = 'default', sessionId = '', excludeSessionId = '',
  limit = 8, includeAllSessions = true, now = new Date(),
} = {}) {
  const profile = profileKey(profileId);
  const cleanSession = clean(sessionId, 160);
  const excludedSession = clean(excludeSessionId, 160);
  const rows = (input?.profiles?.[profile] ?? []).filter((item) => (
    Date.parse(item?.expiresAt ?? '') > new Date(now).getTime()
    && (!excludedSession || item.sessionId !== excludedSession)
    && (includeAllSessions || !cleanSession || item.sessionId === cleanSession)
  ));
  return rows.slice(-Math.max(1, Math.min(24, Number(limit) || 8)));
}

export function renderRecentTurns(input, options = {}) {
  return recentTurns(input, options)
    .map((item) => `${item.createdAt} [${item.source}/${item.sessionId}] ${item.role}: ${item.text}`)
    .join('\n');
}

export const RECENT_CONTINUITY_LIMITS = {
  maxItems: MAX_ITEMS, maxTextChars: MAX_TEXT_CHARS, maxProfiles: MAX_PROFILES,
  defaultTtlHours: DEFAULT_TTL_HOURS,
};
