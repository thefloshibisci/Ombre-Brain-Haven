// Conservative client-side admission hints for automatic Ombre reads.
//
// Ombre remains the source of truth and performs the actual semantic gate.
// This module only chooses a small result budget for each caller and makes
// the zero-hit behaviour explicit in the query sent by autonomous paths.

const EXPLICIT_HISTORY_MARKERS = [
  '原文', '原句', '具体细节', '哪一天', '什么时候', '日期', '当时',
  '之前说过', '你记得', '我记得', '回看', '查一下', '查查', '历史',
  'original', 'exact', 'when did', 'history',
];

const EXPLICIT_RECENT_MARKERS = [
  '最近', '刚才', '刚刚', '今天', '昨天', '近期', '未完成', '没说完',
  'recent', 'latest', 'unfinished',
];

function containsMarker(query, markers) {
  const text = String(query ?? '').toLowerCase();
  return markers.some((marker) => text.includes(marker.toLowerCase()));
}

export function classifyRecallIntent(query, purpose = 'ambient') {
  const value = String(purpose ?? 'ambient').trim().toLowerCase();
  if (value === 'detail' || containsMarker(query, EXPLICIT_HISTORY_MARKERS)) return 'explicit_detail';
  if (value === 'continuity' || containsMarker(query, EXPLICIT_RECENT_MARKERS)) return 'explicit_recent';
  if (value === 'dream') return 'dream';
  return 'ambient';
}

export function planRecall({
  query = '',
  purpose = 'ambient',
  maxResults = 3,
  maxTokens = 800,
} = {}) {
  const intent = classifyRecallIntent(query, purpose);
  const requestedResults = Math.max(1, Number(maxResults) || 1);
  const requestedTokens = Math.max(200, Number(maxTokens) || 200);

  if (intent === 'explicit_detail') {
    return {
      intent,
      maxResults: Math.min(requestedResults, 5),
      maxTokens: Math.min(requestedTokens, 3000),
      emptyAllowed: true,
      suffix: '只返回有直接证据的目标记忆；没有直接证据就返回空，不要用相似记忆凑数。',
    };
  }

  if (intent === 'explicit_recent') {
    return {
      intent,
      maxResults: Math.min(requestedResults, 5),
      maxTokens: Math.min(requestedTokens, 1800),
      emptyAllowed: true,
      suffix: '优先最近且直接相关的内容；没有直接相关就返回空，不要用旧的相似内容凑数。',
    };
  }

  if (intent === 'dream') {
    return {
      intent,
      maxResults: Math.min(requestedResults, 2),
      maxTokens: Math.min(requestedTokens, 1000),
      emptyAllowed: true,
      suffix: '只给一两条具体、真正有共鸣的记忆；没有共鸣就返回空，不要填充相似碎片。',
    };
  }

  // Autonomous daytime/thought reads should normally be one candidate at
  // most. A quiet result is valid and preferable to irrelevant recall.
  return {
    intent: 'ambient',
    maxResults: 1,
    maxTokens: Math.min(requestedTokens, 700),
    emptyAllowed: true,
    suffix: '这是自然浮现，不是搜索任务；最多给一条直接相关的具体记忆，没有就返回空。',
  };
}

export function withRecallPolicyHint(query, policy) {
  const base = String(query ?? '').trim();
  const suffix = String(policy?.suffix ?? '').trim();
  return suffix ? `${base}。${suffix}` : base;
}

