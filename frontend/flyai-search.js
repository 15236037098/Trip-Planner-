/**
 * 飞猪 CLI 检索基础设施：JSON 解析、并发闸门、TTL 缓存与同参数请求合并。
 * 供 products.js / hotels.js 复用，避免各业务模块重复实现。
 */

const { runFlyai } = require("./flyai-runner");

class FlyaiError extends Error {
  constructor(message) {
    super(message);
    this.status = 502;
  }
}

// 单次飞猪查询：整体预算 + 至多重试一次，保证 Node 侧总耗时可控
// CLI 实测约 6-7 秒（Windows 上输出 JSON 后可能异常退出），预算过短会误杀有效结果
const RETRY_DELAYS = [500];
const DEFAULT_TIMEOUT_MS = 9000;
const DEFAULT_QUEUE_TIMEOUT_MS = 3000;

function isRetryable(error) {
  return /429|rate limit|too many requests/i.test(String((error && error.message) || ""));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function parseJson(text) {
  const raw = String(text || "").trim();
  if (!raw) throw new FlyaiError("飞猪返回为空");
  try {
    return JSON.parse(raw);
  } catch {
    // CLI 可能混入日志，截取最外层 JSON 再试一次
  }
  const start = raw.indexOf("{");
  const end = raw.lastIndexOf("}");
  if (start >= 0 && end > start) {
    try {
      return JSON.parse(raw.slice(start, end + 1));
    } catch {
      // 落到下方统一报错
    }
  }
  throw new FlyaiError("飞猪返回格式异常");
}

async function runFlyaiJson(args, options = {}) {
  const timeoutMs =
    Number(options.timeoutMs || process.env.FLYAI_JSON_TIMEOUT_MS) || DEFAULT_TIMEOUT_MS;
  // 整个查询（含重试）共享同一个预算，避免重试把耗时放大到分钟级
  const deadline = Date.now() + timeoutMs;
  let lastError = null;

  for (let attempt = 0; attempt <= RETRY_DELAYS.length; attempt += 1) {
    const remaining = deadline - Date.now();
    if (remaining <= 0) break;

    try {
      const payload = parseJson(await runFlyai(args, { timeoutMs: remaining }));
      if (payload.status !== 0) {
        throw new FlyaiError(payload.message || "飞猪检索失败");
      }
      return payload;
    } catch (error) {
      lastError = error;
      if (!isRetryable(error) || attempt === RETRY_DELAYS.length) break;
      const delay = Math.min(RETRY_DELAYS[attempt], Math.max(0, deadline - Date.now()));
      if (delay <= 0) break;
      await sleep(delay);
    }
  }

  if (lastError instanceof FlyaiError) throw lastError;
  throw new FlyaiError(lastError ? lastError.message : "飞猪检索超时，请稍后重试");
}

function createSearchRunner(options) {
  const ttl = (options && options.ttl) || 10 * 60 * 1000;
  const maxConcurrent = (options && options.maxConcurrent) || 3;
  const maxCache = (options && options.maxCache) || 200;
  const maxQueue = (options && options.maxQueue) || 100;
  const queueTimeoutMs = (options && options.queueTimeoutMs) || DEFAULT_QUEUE_TIMEOUT_MS;

  const cache = new Map();
  const inflight = new Map();
  const waiting = [];
  let running = 0;

  function acquire() {
    if (running < maxConcurrent) {
      running += 1;
      return Promise.resolve();
    }
    if (waiting.length >= maxQueue) {
      return Promise.reject(new FlyaiError("飞猪查询人数较多，请稍后重试"));
    }
    return new Promise((resolve, reject) => {
      const entry = { resolve, reject, timer: null };
      entry.timer = setTimeout(() => {
        const index = waiting.indexOf(entry);
        if (index !== -1) waiting.splice(index, 1);
        reject(new FlyaiError("飞猪查询排队超时，请稍后重试"));
      }, queueTimeoutMs);
      waiting.push(entry);
    });
  }

  function release() {
    const next = waiting.shift();
    if (next) {
      clearTimeout(next.timer);
      next.resolve();
    } else {
      running -= 1;
    }
  }

  function prune() {
    if (cache.size <= maxCache) return;
    const now = Date.now();
    for (const [key, entry] of cache) {
      if (entry.expires <= now) cache.delete(key);
    }
    while (cache.size > maxCache) {
      cache.delete(cache.keys().next().value);
    }
  }

  /**
   * 相同 key 的并发请求只执行一次；结果缓存 ttl 毫秒。
   * 返回 { value, cached }。
   */
  async function run(key, loader) {
    const hit = cache.get(key);
    if (hit && hit.expires > Date.now()) {
      return { value: hit.value, cached: true };
    }

    let pending = inflight.get(key);
    if (!pending) {
      pending = (async () => {
        await acquire();
        try {
          const value = await loader();
          cache.set(key, { expires: Date.now() + ttl, value });
          prune();
          return value;
        } finally {
          release();
        }
      })().finally(() => inflight.delete(key));
      inflight.set(key, pending);
    }

    return { value: await pending, cached: false };
  }

  return { run };
}

module.exports = { FlyaiError, runFlyaiJson, createSearchRunner };
