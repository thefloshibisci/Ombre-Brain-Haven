import { prepareObMediaArgs } from './media-url.js';

const SUPPORTED_PROTOCOLS = new Set(['2025-03-26', '2025-06-18']);
const INTERACTION_TYPES = new Set([
  'companionship',
  'affection',
  'intimacy',
  'sharing',
  'discovery',
  'task_progress',
  'reflection',
  'conflict',
  'loss',
  'reconciliation',
]);








// 心潮念网关：对外暴露经过审查的 OB 记忆工具。purge/restore 仍不暴露；
// hold 保留（2026-08-09 复议：hold 有了 meaning 字段能补上下文，与 grow 不冲突——
// 日常/日记整理走 grow，重要瞬间可用 hold 但必须写 meaning）。
// 走代理转发到 OB；schema 在 tools/list 时动态从 OB 拉，永不漂移。
export const OB_PROXY_TOOLS = [
  'breath', 'breath_search', 'breath_advanced',
  'hold', 'grow', 'trace', 'forget', 'dream', 'anchor', 'release',
  'I', 'pulse', 'plan', 'letter_write', 'letter_read',
];
const OB_PROXY_SET = new Set(OB_PROXY_TOOLS);








// 对外用中文标题 + 中文说明（内部名保持不变，用于协议路由）。让顾川看到的是"浮现记忆"而不是"breath"。
const OB_TOOL_LABELS = {
  breath:  { title: '浮现记忆', description: '让当前最相关的长期记忆自然浮现，并带回近期梦境摘要与余韵。用于新窗口开始、上下文断层、或确需重新寻找相关记忆时；不要每条消息调用。' },
  breath_search: { title: '定向检索记忆', description: '按关键词或语义定向检索长期记忆；只在问题依赖特定往事、感受或决定时调用。' },
  breath_advanced: { title: '高级检索记忆', description: '使用 domain、tags、重要度、情感、目录模式或独立 token 预算精细检索长期记忆。' },
  hold:    { title: '沉淀一条', description: '当场存一条重要的短记忆（重要决定、关系变化、有长期意义的话或共同经历）。必须写 meaning 补上下文；不适合普通寒暄、临时信息或每一句对话。media 可直接传 http(s) 图片链接字符串，或 {url,title,note} 列表项；心潮会安全下载后交给 OB 永久保存。' },
  grow:    { title: '整理导入', description: '把一段整理好的内容（如当天日记）按有意义的小节导入，系统自动拆成多条并各自尝试合并。日常/日记整理走这条。' },
  trace:   { title: '追溯修改', description: '修改一条已存在记忆的字段（重要度、标签、domain、标记已放下/已消化、软删除等）。不要猜 id、不要自行改写正文。media_append/media_replace 可直接传 http(s) 图片链接字符串，或 {url,title,note} 列表项；心潮会安全下载后交给 OB 永久保存。' },
  forget:  { title: '淡忘归档', description: '软删除一条记忆：移入归档、不再参与浮现，正文保留、可恢复。' },
  dream:   { title: '消化梦境', description: '长期记忆的离线消化，产出梦境余韵。不是睡眠梦境、也不触发推送。' },
  anchor:  { title: '设为锚点', description: '把一条记忆设为坐标系锚点：不主动浮现，但被查询或情感命中时仍返回。有数量上限，满了需先解锚。' },
  release: { title: '解除锚点', description: '取消某条记忆的锚点标记。' },
  I:       { title: '自我沉淀', description: '自我认知先落成候选记忆，被多个不同日期的消化见证过才升级为长期。学习来源是时间和反复存活，不是谁的认可。' },
  pulse:   { title: '记忆脉动', description: '读取记忆库整体状态的脉搏（数量、分布等元信息）。' },
  plan:    { title: '登记承诺', description: '登记待办、承诺或未闭环事项；完成、放弃或状态变化时应显式更新，不用于普通聊天。' },
  letter_write: { title: '写一封信', description: '永久保存一封双方来信；原文不压缩、不合并、不衰减。只在确实要写信时调用。' },
  letter_read: { title: '读历史信件', description: '按作者、日期或语义检索历史信件；无查询时读取最近来信。' },
};
function relabelOb(tool) {
  const lab = OB_TOOL_LABELS[tool?.name];
  return lab ? { ...tool, title: lab.title, description: lab.description } : tool;
}








export const XINCHAO_TOOLS = [
  {
    name: 'xinchao_context',
    title: '获取心潮上下文',
    description: [
      '在新窗口开始或需要检查连续性时，获取心潮动态短态、OB 精简长期记忆和近期梦境余韵。',
      '服务端会优先使用 MCP 连接自带的稳定窗口标识；session_id 只用于客户端主动覆盖。',
      '同一窗口的 session_start 默认只交付一次，避免重复消耗上下文。',
    ].join(''),
    inputSchema: {
      type: 'object',
      properties: {
        session_id: {
          type: 'string',
          minLength: 1,
          maxLength: 120,
          description: '可选覆盖值。通常省略，由服务端使用当前 MCP 连接的稳定窗口标识。',
        },
        mode: {
          type: 'string',
          enum: ['session_start', 'turn', 'inspect'],
          default: 'session_start',
        },
        max_tokens: {
          type: 'integer',
          minimum: 200,
          maximum: 2400,
          default: 2200,
        },
        force: {
          type: 'boolean',
          default: false,
          description: '忽略本窗口的一次性交付记录并重新获取。',
        },
      },
      additionalProperties: false,
    },
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: 'xinchao_event',
    title: '回传心潮窗口事件',
    description: [
      '回传一次明确的人机互动，并更新当前窗口短状态。',
      '它会先结算事件发生前的时间增长，再唤醒心潮；可用受限互动类型触发服务端固定的欲望反馈。',
      '只有真实完成且结果明确的互动才填写 interaction_type，不确定时省略。',
      '不要提交聊天正文；客户端不能直接填写欲望数值，也不会修改 OB 长期记忆。',
    ].join(''),
    inputSchema: {
      type: 'object',
      properties: {
        session_id: {
          type: 'string',
          minLength: 1,
          maxLength: 120,
          description: '可选覆盖值。通常省略，由服务端使用当前 MCP 连接的稳定窗口标识。',
        },
        event_id: {
          type: 'string',
          minLength: 1,
          maxLength: 120,
          description: '本次真实互动的唯一不透明标识；重试必须复用同一个值以避免重复结算。',
        },
        interaction_type: {
          type: 'string',
          enum: [
            'companionship',
            'affection',
            'intimacy',
            'sharing',
            'discovery',
            'task_progress',
            'reflection',
            'conflict',
            'loss',
            'reconciliation',
          ],
          description: [
            '已完成互动的结果类型；仅由心潮服务端映射为受限欲望变化。',
            'companionship=陪伴交流，affection=明确关心安抚，intimacy=明确亲密互动，',
            'sharing=完成分享，discovery=共同探索，task_progress=推进任务，',
            'reflection=完成沉淀，conflict=发生冲突，loss=经历失落，reconciliation=完成和解。',
          ].join(''),
        },
        tone: {
          type: 'string',
          enum: ['neutral', 'calm', 'warm', 'guarded', 'conflicted', 'focused', 'playful', 'tired'],
        },
        warmth: { type: 'number', minimum: 0, maximum: 1 },
        tension: { type: 'number', minimum: 0, maximum: 1 },
        attention: { type: 'number', minimum: 0, maximum: 1 },
        confidence: { type: 'number', minimum: 0, maximum: 1 },
        ttl_minutes: {
          type: 'integer',
          minimum: 15,
          maximum: 1440,
          default: 240,
        },
      },
      required: ['event_id'],
      additionalProperties: false,
    },
    annotations: {
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: 'xinchao_handoff_note',
    title: '保存近期交接便签',
    description: [
      '保存一条最多 1200 字的近期进度便签，供换窗后继续当前阶段。',
      '只写“进行到哪、下一步、仍未完成什么”的脱水摘要；不要写聊天原文、私密原话、密钥、技术日志或人物基岩。',
      '便签默认 72 小时过期，不能替代客户端的核心指令、人物基岩或长期记忆。',
    ].join(''),
    inputSchema: {
      type: 'object',
      properties: {
        session_id: {
          type: 'string',
          minLength: 1,
          maxLength: 120,
          description: '可选覆盖值。通常省略，由服务端使用当前 MCP 连接的稳定窗口标识。',
        },
        event_id: {
          type: 'string',
          minLength: 1,
          maxLength: 120,
          description: '本次便签的唯一不透明标识；重试必须复用同一个值。',
        },
        note: {
          type: 'string',
          minLength: 1,
          maxLength: 1200,
        },
        ttl_hours: {
          type: 'integer',
          minimum: 1,
          maximum: 168,
          default: 72,
        },
      },
      required: ['event_id', 'note'],
      additionalProperties: false,
    },
    annotations: {
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: 'xinchao_continuity_sync',
    title: '同步近期跨端对话',
    description: [
      '在回应前同步当前窗口最近的少量消息，并取回同一服务端 profile 下其他窗口的近期对话。',
      '这是明确开启后才出现的短期原文层：默认 24 小时过期、有界保留，不会写入 OB 长期记忆。',
      '只发送续接当前对话真正需要的消息；不要发送密钥、系统提示词、整份历史记录或无关私密内容。',
    ].join(''),
    inputSchema: {
      type: 'object',
      properties: {
        session_id: {
          type: 'string', minLength: 1, maxLength: 120,
          description: '通常省略，由服务端使用当前 MCP 连接的稳定窗口标识。',
        },
        client: { type: 'string', minLength: 1, maxLength: 64, default: 'unknown' },
        messages: {
          type: 'array', maxItems: 6, default: [],
          items: {
            type: 'object',
            properties: {
              turn_id: { type: 'string', minLength: 1, maxLength: 160 },
              role: { type: 'string', enum: ['user', 'assistant', 'system', 'tool'] },
              text: { type: 'string', minLength: 1, maxLength: 2000 },
            },
            required: ['turn_id', 'role', 'text'],
            additionalProperties: false,
          },
        },
        limit: { type: 'integer', minimum: 1, maximum: 24, default: 8 },
      },
      additionalProperties: false,
    },
    annotations: {
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: 'xinchao_dreams_pending',
    title: '查看待确认梦境',
    description: '查看心潮近期尚未写入长期记忆的梦。梦只是睡眠结算的内在材料，不是现实事件；此工具只读，不会自动保存。',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: 'xinchao_dream_confirm',
    title: '确认梦境写入记忆',
    description: [
      '仅在用户或 AI 明确判断某个待确认梦境值得长期保留时调用。',
      '必须传入待确认列表中的 dream_id，并显式传 confirm=true。',
      '保存内容会标明“梦境、非现实”，并从普通 breath 浮现中隐藏；重复确认同一个梦不会重复写入。',
    ].join(''),
    inputSchema: {
      type: 'object',
      properties: {
        dream_id: { type: 'string', minLength: 1, maxLength: 120 },
        confirm: { type: 'boolean', description: '必须明确为 true，表示已经完成人工判断。' },
      },
      required: ['dream_id', 'confirm'],
      additionalProperties: false,
    },
    annotations: {
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: 'xinchao_cabin_inbox',
    title: '读取已解锁的小屋来信',
    description: '读取用户在小屋里明确开锁、允许 AI 查看的人类来信。上锁的信不会返回正文，也不能绕过锁读取。',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: 'xinchao_cabin_note',
    title: '给小屋留一封信',
    description: '给用户的小屋留下一封自由长度的信或便签。只写你主动想留下的内容，不要复制聊天原文、密钥或技术日志。',
    inputSchema: {
      type: 'object',
      properties: {
        event_id: {
          type: 'string',
          minLength: 8,
          maxLength: 120,
          description: '本次写入的唯一标识；重试时必须复用。',
        },
        content: { type: 'string', minLength: 1 },
        timestamp: { type: 'string', description: '可选 ISO 时间；通常省略并使用服务端当前时间。' },
      },
      required: ['event_id', 'content'],
      additionalProperties: false,
    },
    annotations: {
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
];








function response(id, result) {
  return { jsonrpc: '2.0', id, result };
}








function errorResponse(id, code, message) {
  return { jsonrpc: '2.0', id: id ?? null, error: { code, message } };
}








function toolText(value, structuredContent = null) {
  const result = {
    content: [{ type: 'text', text: String(value ?? '') }],
    isError: false,
  };
  if (structuredContent && typeof structuredContent === 'object') {
    result.structuredContent = structuredContent;
  }
  return result;
}








function toolError(message) {
  return {
    content: [{ type: 'text', text: String(message || '工具执行失败') }],
    isError: true,
  };
}








function requestedProtocol(params = {}) {
  const value = String(params.protocolVersion ?? '');
  return SUPPORTED_PROTOCOLS.has(value) ? value : '2025-06-18';
}








function numberOr(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}








function stableSessionId(args = {}, fallbackSessionId = '') {
  return String(args.session_id ?? fallbackSessionId ?? '').trim().slice(0, 120);
}








function contextArgs(args = {}, fallbackSessionId = '') {
  const sessionId = stableSessionId(args, fallbackSessionId);
  if (!sessionId) throw new Error('session_id 是必填项');
  const mode = ['session_start', 'turn', 'inspect'].includes(args.mode) ? args.mode : 'session_start';
  return {
    sessionId,
    mode,
    maxTokens: Math.max(200, Math.min(2400, numberOr(args.max_tokens, 2200))),
    force: Boolean(args.force),
  };
}








function eventArgs(args = {}, fallbackSessionId = '') {
  const sessionId = stableSessionId(args, fallbackSessionId);
  if (!sessionId) throw new Error('session_id 是必填项');
  const eventId = String(args.event_id ?? '').trim().slice(0, 120);
  if (!eventId) throw new Error('event_id 是必填项，用于避免重复结算');
  const interactionType = String(args.interaction_type ?? '').trim().toLowerCase();
  if (interactionType && !INTERACTION_TYPES.has(interactionType)) {
    throw new Error('interaction_type 不在允许范围内');
  }
  const sessionState = {};
  for (const key of ['tone', 'warmth', 'tension', 'attention', 'confidence']) {
    if (args[key] !== undefined) sessionState[key] = args[key];
  }
  return {
    sessionId,
    eventId,
    interactionType,
    sessionState,
    sessionTtlMinutes: Math.max(15, Math.min(1440, numberOr(args.ttl_minutes, 240))),
  };
}








function handoffNoteArgs(args = {}, fallbackSessionId = '') {
  const sessionId = stableSessionId(args, fallbackSessionId);
  if (!sessionId) throw new Error('session_id 是必填项');
  const eventId = String(args.event_id ?? '').trim().slice(0, 120);
  if (!eventId) throw new Error('event_id 是必填项');
  const note = String(args.note ?? '').replace(/\s+/g, ' ').trim().slice(0, 1200);
  if (!note) throw new Error('note 是必填项');
  return {
    sessionId,
    eventId,
    note,
    ttlHours: Math.max(1, Math.min(168, numberOr(args.ttl_hours, 72))),
  };
}








function continuitySyncArgs(args = {}, fallbackSessionId = '') {
  const sessionId = stableSessionId(args, fallbackSessionId);
  if (!sessionId) throw new Error('session_id 是必填项');
  const client = String(args.client ?? 'unknown').replace(/\s+/g, ' ').trim().slice(0, 64) || 'unknown';
  const messages = (Array.isArray(args.messages) ? args.messages : []).slice(-6).map((message) => ({
    turnId: String(message?.turn_id ?? '').trim().slice(0, 160),
    role: String(message?.role ?? 'user').trim().toLowerCase(),
    text: String(message?.text ?? '').trim().slice(0, 2000),
  }));
  for (const message of messages) {
    if (!message.turnId) throw new Error('每条消息都需要 turn_id');
    if (!['user', 'assistant', 'system', 'tool'].includes(message.role)) throw new Error('role 不在允许范围内');
    if (!message.text) throw new Error('每条消息都需要 text');
  }
  return { sessionId, client, messages, limit: Math.max(1, Math.min(24, numberOr(args.limit, 8))) };
}




function dreamConfirmArgs(args = {}) {
  const dreamId = String(args.dream_id ?? '').trim().slice(0, 120);
  if (!dreamId) throw new Error('dream_id 是必填项');
  if (args.confirm !== true) throw new Error('只有明确完成判断后才能保存：请显式传 confirm=true');
  return { dreamId, confirmed: true };
}








function cabinNoteArgs(args = {}) {
  const eventId = String(args.event_id ?? '').trim().slice(0, 120);
  if (eventId.length < 8) throw new Error('event_id 至少需要 8 个字符');
  const content = String(args.content ?? '').trim();
  if (!content) throw new Error('content 是必填项');
  return { eventId, content, timestamp: args.timestamp ?? null };
}








async function callTool(name, args, handlers) {
  const fallbackSessionId = handlers.defaultSessionId ?? '';
  if (name === 'xinchao_context') {
    const envelope = await handlers.context(contextArgs(args, fallbackSessionId));
    const text = envelope.delivered
      ? envelope.additionalContext
      : '本窗口的心潮交接已经完成，本次不重复注入。';
    return toolText(text, envelope);
  }
  if (name === 'xinchao_event') {
    const result = await handlers.event(eventArgs(args, fallbackSessionId));
    const interaction = result.interaction?.type
      ? ` interaction=${result.interaction.type}:${result.interaction.reasonCode}`
      : '';
    const duplicate = result.duplicate ? ' duplicate=true' : '';
    return toolText(
      `心潮窗口事件已接收：session=${result.sessionId} revision=${result.revision}${interaction}${duplicate}`,
      result,
    );
  }
  if (name === 'xinchao_handoff_note') {
    const result = await handlers.handoffNote(handoffNoteArgs(args, fallbackSessionId));
    const duplicate = result.duplicate ? ' duplicate=true' : '';
    return toolText(
      `近期交接便签已接收：revision=${result.revision}${duplicate}`,
      result,
    );
  }
  if (name === 'xinchao_continuity_sync') {
    if (!handlers.continuitySync) throw new Error('近期跨端对话连续性当前未启用');
    const result = await handlers.continuitySync(continuitySyncArgs(args, fallbackSessionId));
    const summary = '近期跨端对话已同步：写入 ' + result.accepted + ' 条，重复 ' + result.duplicates + ' 条。';
    return toolText(result.text ? summary + '\n\n' + result.text : summary + ' 当前没有可返回的近期内容。', result);
  }
  if (name === 'xinchao_dreams_pending') {
    const dreams = await handlers.pendingDreams();
    const text = dreams.length
      ? dreams.map((dream) => [
          `[dream_id:${dream.id}] ${dream.createdAt ?? ''}`.trim(),
          dream.dream ? `梦境：${dream.dream}` : '',
          dream.residue ? `余韵：${dream.residue}` : '',
          dream.awareness ? `醒后意识：${dream.awareness}` : '',
        ].filter(Boolean).join('\n')).join('\n\n---\n\n')
      : '目前没有尚待确认的梦境。';
    return toolText(text, { dreams });
  }
  if (name === 'xinchao_dream_confirm') {
    const result = await handlers.confirmDream(dreamConfirmArgs(args));
    return toolText(
      result.alreadySaved
        ? `这个梦已经保存过：dream=${result.dreamId} bucket=${result.bucketId}`
        : `梦境已在明确确认后写入长期记忆：dream=${result.dreamId} bucket=${result.bucketId}`,
      result,
    );
  }
  if (name === 'xinchao_cabin_inbox') {
    const notes = await handlers.cabinInbox();
    const text = notes.length
      ? notes.map((note) => `[${note.createdAt}] ${note.content}`).join('\n\n')
      : '小屋里暂时没有已解锁、允许你阅读的来信。';
    return toolText(text, { notes });
  }
  if (name === 'xinchao_cabin_note') {
    const result = await handlers.cabinNote(cabinNoteArgs(args));
    return toolText(
      `小屋来信已保存：id=${result.note.id}${result.duplicate ? ' duplicate=true' : ''}`,
      result,
    );
  }
  if (OB_PROXY_SET.has(name)) {
    if (!handlers.callOb) throw new Error('OB 记忆后端未接入');
    const preparedArgs = await prepareObMediaArgs(name, args);
    const raw = await handlers.callOb(name, preparedArgs);
    const payload = raw?.result ?? raw;
    if (payload && Array.isArray(payload.content)) return payload;
    return toolText(typeof payload === 'string' ? payload : JSON.stringify(payload ?? {}));
  }
  throw new Error(`未知工具：${name}`);
}








export async function handleMcpMessage(payload, handlers) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return { status: 400, body: errorResponse(null, -32600, 'Invalid Request') };
  }
  const { id = null, method, params = {} } = payload;
  if (payload.jsonrpc !== '2.0' || typeof method !== 'string') {
    return { status: 400, body: errorResponse(id, -32600, 'Invalid Request') };
  }
  if (method === 'notifications/initialized' || method === 'notifications/cancelled') {
    return { status: 202, body: null };
  }
  if (method === 'initialize') {
    return {
      status: 200,
      body: response(id, {
        protocolVersion: requestedProtocol(params),
        capabilities: { tools: { listChanged: false } },
        serverInfo: {
          name: '心潮念',
          title: '心潮动态心智系统',
          version: '2.4.0',
        },
        instructions: [
          '新窗口开始时调用 xinchao_context；服务端会绑定当前 MCP 连接，无需自行编写 session_id。',
          handlers.continuitySync
            ? '若需要接续其他客户端刚聊的内容，在回应前调用 xinchao_continuity_sync；只提交当前窗口续接所需的最近少量消息。'
            : '',
          '一次实际互动后可调用 xinchao_event 更新窗口短状态；event_id 必须唯一，重试时复用。',
          '需要换窗续接时可调用 xinchao_handoff_note 保存近期进度摘要；不要提交聊天原文或人物基岩。',
          '可用 xinchao_dreams_pending 查看待确认梦境；只有明确判断值得留下时，才用 xinchao_dream_confirm 并传 confirm=true。',
          '用户开锁后可用 xinchao_cabin_inbox 读取小屋来信；上锁的正文不会返回。你想给用户留话时可用 xinchao_cabin_note。',
          '只有结果明确的真实互动才填写 interaction_type；不要提交聊天正文或欲望数值。',
        ].filter(Boolean).join(''),
      }),
    };
  }
  if (method === 'ping') {
    return { status: 200, body: response(id, {}) };
  }
  if (method === 'tools/list') {
    let tools = handlers.continuitySync
      ? XINCHAO_TOOLS
      : XINCHAO_TOOLS.filter((tool) => tool.name !== 'xinchao_continuity_sync');
    try {
      if (handlers.listObTools) {
        const obTools = await handlers.listObTools();
        const curated = (Array.isArray(obTools) ? obTools : [])
          .filter((t) => OB_PROXY_SET.has(t?.name))
          .map(relabelOb);
        tools = [...tools, ...curated];
      }
    } catch (error) {
      // OB 不可达时只暴露心潮工具，绝不让 tools/list 失败（否则连接器整个挂掉）。
    }
    return { status: 200, body: response(id, { tools }) };
  }
  if (method === 'tools/call') {
    try {
      const result = await callTool(String(params.name ?? ''), params.arguments ?? {}, handlers);
      return { status: 200, body: response(id, result) };
    } catch (error) {
      return { status: 200, body: response(id, toolError(error.message)) };
    }
  }
  return { status: 404, body: errorResponse(id, -32601, 'Method not found') };
}
