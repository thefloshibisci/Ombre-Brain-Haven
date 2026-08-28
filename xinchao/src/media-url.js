import { lookup as dnsLookup } from 'node:dns/promises';
import http from 'node:http';
import https from 'node:https';
import { isIP } from 'node:net';

const DEFAULT_MAX_BYTES = 2.5 * 1024 * 1024;
const DEFAULT_TIMEOUT_MS = 12_000;
const DEFAULT_MAX_REDIRECTS = 3;
const URL_MEDIA_FIELDS = {
  hold: ['media'],
  trace: ['media_append', 'media_replace'],
};

const MIME_EXTENSIONS = new Map([
  ['image/avif', '.avif'],
  ['image/gif', '.gif'],
  ['image/jpeg', '.jpg'],
  ['image/png', '.png'],
  ['image/webp', '.webp'],
]);

function ipv4Bytes(address) {
  const bytes = address.split('.').map(Number);
  return bytes.length === 4 && bytes.every((value) => Number.isInteger(value) && value >= 0 && value <= 255)
    ? bytes
    : null;
}

function mappedIpv4(address) {
  const match = address.toLowerCase().match(/^::(?:ffff:)?(\d+\.\d+\.\d+\.\d+)$/);
  if (match) return match[1];
  const hexMatch = address.toLowerCase().match(/^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/);
  if (!hexMatch) return null;
  const high = Number.parseInt(hexMatch[1], 16);
  const low = Number.parseInt(hexMatch[2], 16);
  return `${high >> 8}.${high & 255}.${low >> 8}.${low & 255}`;
}

export function isPublicAddress(address) {
  const family = isIP(address);
  if (family === 4) {
    const bytes = ipv4Bytes(address);
    if (!bytes) return false;
    const [a, b, c] = bytes;
    return !(
      a === 0
      || a === 10
      || a === 127
      || (a === 100 && b >= 64 && b <= 127)
      || (a === 169 && b === 254)
      || (a === 172 && b >= 16 && b <= 31)
      || (a === 192 && b === 0 && c === 0)
      || (a === 192 && b === 0 && c === 2)
      || (a === 192 && b === 88 && c === 99)
      || (a === 192 && b === 168)
      || (a === 198 && (b === 18 || b === 19))
      || (a === 198 && b === 51 && c === 100)
      || (a === 203 && b === 0 && c === 113)
      || a >= 224
    );
  }
  if (family !== 6) return false;

  const normalized = address.toLowerCase().split('%')[0];
  const mapped = mappedIpv4(normalized);
  if (mapped) return isPublicAddress(mapped);
  return !(
    normalized.startsWith('::')
    || normalized.startsWith('fc')
    || normalized.startsWith('fd')
    || /^fe[89a-f]/.test(normalized)
    || normalized.startsWith('ff')
    || normalized.startsWith('64:ff9b:')
    || normalized.startsWith('100:')
    || normalized.startsWith('2001:db8:')
    || normalized === '2001:db8::'
  );
}

function hostnameWithoutBrackets(url) {
  const hostname = url.hostname;
  return hostname.startsWith('[') && hostname.endsWith(']') ? hostname.slice(1, -1) : hostname;
}

function parseMediaUrl(value) {
  let parsed;
  try {
    parsed = new URL(String(value));
  } catch {
    throw new Error('图片链接不是有效 URL');
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error('图片链接只允许 http 或 https');
  }
  if (parsed.username || parsed.password) throw new Error('图片链接不能包含账号或密码');
  if (parsed.hostname.toLowerCase() === 'localhost' || parsed.hostname.toLowerCase().endsWith('.localhost')) {
    throw new Error('图片链接不能指向本机或内网地址');
  }
  return parsed;
}

async function resolvePublicTarget(url, lookup = dnsLookup) {
  const hostname = hostnameWithoutBrackets(url);
  const literalFamily = isIP(hostname);
  const rawRecords = literalFamily
    ? [{ address: hostname, family: literalFamily }]
    : await lookup(hostname, { all: true, verbatim: true });
  const records = (Array.isArray(rawRecords) ? rawRecords : [rawRecords])
    .map((record) => ({
      address: String(record?.address ?? ''),
      family: Number(record?.family) || isIP(String(record?.address ?? '')),
    }))
    .filter((record) => record.address && record.family);
  if (!records.length || records.some((record) => !isPublicAddress(record.address))) {
    throw new Error('图片链接不能指向本机、内网或保留地址');
  }
  return records;
}

export function createPinnedLookup(records) {
  let cursor = 0;
  return (...lookupArgs) => {
    const callback = lookupArgs[lookupArgs.length - 1];
    const requestOptions = lookupArgs[1] ?? {};
    if (requestOptions.all) {
      callback(null, records);
      return;
    }
    const record = records[cursor % records.length];
    cursor += 1;
    callback(null, record.address, record.family);
  };
}

function openPinnedRequest(url, records, { signal } = {}) {
  const transport = url.protocol === 'https:' ? https : http;
  return new Promise((resolve, reject) => {
    const request = transport.get(url, {
      agent: false,
      headers: {
        Accept: 'image/*',
        'User-Agent': 'xinchao-media-fetch/1.0',
      },
      lookup: createPinnedLookup(records),
      signal,
    }, resolve);
    request.once('error', reject);
  });
}

export async function readBoundedResponse(response, maxBytes = DEFAULT_MAX_BYTES) {
  const declared = Number(response.headers?.['content-length']);
  if (Number.isFinite(declared) && declared > maxBytes) {
    response.destroy?.();
    throw new Error(`图片超过 ${Math.floor(maxBytes / 1024 / 1024 * 10) / 10} MiB 上限`);
  }
  const chunks = [];
  let total = 0;
  for await (const chunk of response) {
    const data = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    total += data.length;
    if (total > maxBytes) {
      response.destroy?.();
      throw new Error(`图片超过 ${Math.floor(maxBytes / 1024 / 1024 * 10) / 10} MiB 上限`);
    }
    chunks.push(data);
  }
  return Buffer.concat(chunks, total);
}

function filenameFrom(url, mimeType, requestedName = '') {
  let name = String(requestedName || '').trim();
  if (!name) {
    try { name = decodeURIComponent(url.pathname.split('/').pop() || 'image'); } catch { name = 'image'; }
  }
  name = name.replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_').slice(0, 180) || 'image';
  if (!/\.[a-zA-Z0-9]{1,10}$/.test(name)) name += MIME_EXTENSIONS.get(mimeType) || '.img';
  return name;
}

function hasImageSignature(data, mimeType) {
  if (mimeType === 'image/png') {
    return data.length >= 8 && data.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
  }
  if (mimeType === 'image/jpeg') return data.length >= 3 && data[0] === 0xff && data[1] === 0xd8 && data[2] === 0xff;
  if (mimeType === 'image/gif') return data.length >= 6 && ['GIF87a', 'GIF89a'].includes(data.subarray(0, 6).toString('ascii'));
  if (mimeType === 'image/webp') {
    return data.length >= 12 && data.subarray(0, 4).toString('ascii') === 'RIFF' && data.subarray(8, 12).toString('ascii') === 'WEBP';
  }
  if (mimeType === 'image/avif') {
    return data.length >= 12 && data.subarray(4, 8).toString('ascii') === 'ftyp'
      && /(?:avif|avis)/.test(data.subarray(8, Math.min(data.length, 64)).toString('ascii'));
  }
  return false;
}

export async function downloadImageFromUrl(rawUrl, options = {}) {
  const lookup = options.lookup ?? dnsLookup;
  const requestOnce = options.requestOnce ?? openPinnedRequest;
  const maxBytes = options.maxBytes ?? DEFAULT_MAX_BYTES;
  const maxRedirects = options.maxRedirects ?? DEFAULT_MAX_REDIRECTS;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(new Error('图片下载超时')), options.timeoutMs ?? DEFAULT_TIMEOUT_MS);
  let current = parseMediaUrl(rawUrl);

  try {
    for (let redirects = 0; ; redirects += 1) {
      const records = await resolvePublicTarget(current, lookup);
      const response = await requestOnce(current, records, { signal: controller.signal });
      const status = Number(response.statusCode ?? 0);
      if ([301, 302, 303, 307, 308].includes(status)) {
        response.resume?.();
        if (redirects >= maxRedirects) throw new Error('图片链接重定向次数过多');
        const location = response.headers?.location;
        if (!location) throw new Error('图片链接重定向缺少目标地址');
        current = parseMediaUrl(new URL(location, current));
        continue;
      }
      if (status < 200 || status >= 300) {
        response.resume?.();
        throw new Error(`图片下载失败：HTTP ${status || 'unknown'}`);
      }
      let mimeType = String(response.headers?.['content-type'] ?? '').split(';', 1)[0].trim().toLowerCase();
      if (mimeType === 'image/jpg' || mimeType === 'image/pjpeg') mimeType = 'image/jpeg';
      if (!MIME_EXTENSIONS.has(mimeType)) {
        response.resume?.();
        throw new Error(`链接返回的不是受支持图片（${mimeType || '未知类型'}）`);
      }
      const data = await readBoundedResponse(response, maxBytes);
      if (!hasImageSignature(data, mimeType)) throw new Error('链接返回的数据与图片类型不符');
      return { data, mimeType, finalUrl: current };
    }
  } catch (error) {
    if (controller.signal.aborted) throw new Error('图片下载超时');
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function urlFromItem(item) {
  if (typeof item === 'string') return /^https?:\/\//i.test(item.trim()) ? item.trim() : '';
  if (!item || typeof item !== 'object' || Array.isArray(item)) return '';
  return typeof item.url === 'string' && item.url.trim() ? item.url.trim() : '';
}

async function prepareMediaValue(value, options) {
  if (value == null) return value;
  const items = Array.isArray(value) ? value : [value];
  const budget = options.budget ?? { downloadedBytes: 0 };
  const prepared = [];
  for (const item of items) {
    const url = urlFromItem(item);
    if (!url) {
      prepared.push(item);
      continue;
    }
    const downloaded = await (options.download ?? downloadImageFromUrl)(url, options);
    budget.downloadedBytes += downloaded.data.length;
    if (budget.downloadedBytes > (options.maxBytes ?? DEFAULT_MAX_BYTES)) {
      const limit = Math.floor((options.maxBytes ?? DEFAULT_MAX_BYTES) / 1024 / 1024 * 10) / 10;
      throw new Error(`一次请求中的图片总大小超过 ${limit} MiB 上限`);
    }
    const source = typeof item === 'object' ? item : {};
    const converted = { ...source };
    delete converted.url;
    delete converted.path;
    converted.data_base64 = downloaded.data.toString('base64');
    converted.filename = filenameFrom(downloaded.finalUrl ?? parseMediaUrl(url), downloaded.mimeType, source.filename);
    converted.type = downloaded.mimeType;
    prepared.push(converted);
  }
  if (Array.isArray(value)) return prepared;
  return prepared[0];
}

export async function prepareObMediaArgs(toolName, args = {}, options = {}) {
  const fields = URL_MEDIA_FIELDS[toolName];
  if (!fields || !args || typeof args !== 'object' || Array.isArray(args)) return args;
  let prepared = args;
  const sharedOptions = { ...options, budget: { downloadedBytes: 0 } };
  for (const field of fields) {
    if (!Object.hasOwn(args, field)) continue;
    if (prepared === args) prepared = { ...args };
    prepared[field] = await prepareMediaValue(args[field], sharedOptions);
  }
  return prepared;
}
