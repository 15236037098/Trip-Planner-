const express = require("express");
const fs = require("node:fs");
const path = require("path");
const zlib = require("node:zlib");
const products = require("./products");
const hotels = require("./hotels");
const transport = require("./transport");

const app = express();
const PORT = Number(process.env.PORT) || 3000;
const HOST = process.env.HOST || "0.0.0.0";
const RAW_BACKEND_URL = (process.env.BACKEND_URL || "http://127.0.0.1:8000").trim();
const BACKEND_URL = (
  /^https?:\/\//i.test(RAW_BACKEND_URL) ? RAW_BACKEND_URL : `http://${RAW_BACKEND_URL}`
).replace(/\/+$/, "");
const CHAT_TIMEOUT_MS = Number(process.env.CHAT_TIMEOUT_MS) || 20000;
const MAX_INFLIGHT_CHAT = Number(process.env.MAX_INFLIGHT_CHAT) || 250;
const MAX_QUEUE_CHAT = Number(process.env.MAX_QUEUE_CHAT) || 250;
const MAX_INFLIGHT_PER_CLIENT = Number(process.env.MAX_INFLIGHT_PER_CLIENT) || 4;
const CHAT_RATE_LIMIT_PER_MIN = Number(process.env.CHAT_RATE_LIMIT_PER_MIN) || 30;
// 只有部署在受信任的反向代理后面时才信任 X-Forwarded-For；直连时取 socket 地址，
// 否则客户端可以伪造 XFF 绕过按 IP 的限流。Render 等托管平台请显式设为 1。
const TRUST_PROXY = (() => {
  const raw = String(process.env.TRUST_PROXY || "").trim().toLowerCase();
  if (!raw || raw === "false") return false;
  if (raw === "true") return true;
  const hops = Number(raw);
  return Number.isInteger(hops) && hops > 0 ? hops : false;
})();

app.set("trust proxy", TRUST_PROXY);
app.disable("x-powered-by");

const RATE_WINDOW_MS = 60_000;
const rateBuckets = new Map();

const SUPPORTED_LANGUAGES = new Set(["zh-CN", "en", "ja", "ko", "es", "fr"]);
const BUSY_MESSAGES = {
  "zh-CN": "当前咨询人数较多，请稍后重试。",
  en: "We're receiving a lot of requests right now. Please try again shortly.",
  ja: "現在アクセスが集中しています。しばらくしてから再度お試しください。",
  ko: "현재 접속자가 많습니다. 잠시 후 다시 시도해 주세요.",
  es: "Estamos recibiendo muchas solicitudes. Inténtalo de nuevo en un momento.",
  fr: "Nous recevons beaucoup de demandes. Veuillez réessayer dans un instant.",
};
const RATE_LIMIT_MESSAGES = {
  "zh-CN": "请求过于频繁，请稍后再试。",
  en: "Too many requests. Please try again shortly.",
  ja: "リクエストが多すぎます。しばらくしてから再試行してください。",
  ko: "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
  es: "Demasiadas solicitudes. Inténtalo de nuevo en un momento.",
  fr: "Trop de demandes. Veuillez réessayer dans un instant.",
};

function normalizeLanguage(value) {
  const code = String(value || "").trim().toLowerCase().replace("_", "-");
  if (!code) return "zh-CN";
  if (code.startsWith("zh")) return "zh-CN";
  const base = code.split("-")[0];
  return SUPPORTED_LANGUAGES.has(base) ? base : "zh-CN";
}

function requestLanguage(req) {
  const bodyLanguage = req.body && req.body.language;
  if (bodyLanguage) return normalizeLanguage(bodyLanguage);

  const headerLanguage = req.get("X-Language");
  if (headerLanguage) return normalizeLanguage(headerLanguage);

  const accept = String(req.get("Accept-Language") || "")
    .split(",")[0]
    .split(";")[0];
  return normalizeLanguage(accept);
}

function getClientKey(req) {
  const value = req.get("X-Client-Id") || req.ip || "unknown";
  return String(value).replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 64) || "unknown";
}

function sendBusy(res, status, language, retryAfter = 5) {
  res.set("Retry-After", String(retryAfter));
  res.status(status).json({
    detail: BUSY_MESSAGES[normalizeLanguage(language)] || BUSY_MESSAGES.en,
  });
}

function createGate(maxActive, maxQueue) {
  let active = 0;
  const queue = [];

  function release() {
    active = Math.max(0, active - 1);
    while (queue.length && active < maxActive) {
      const next = queue.shift();
      clearTimeout(next.timer);
      active += 1;
      next.resolve();
    }
  }

  function acquire(deadline) {
    if (active < maxActive) {
      active += 1;
      return Promise.resolve();
    }
    if (queue.length >= maxQueue) {
      return Promise.reject(new Error("queue_full"));
    }

    return new Promise((resolve, reject) => {
      const entry = { resolve, reject, timer: null };
      const wait = Math.max(0, deadline - Date.now());
      entry.timer = setTimeout(() => {
        const index = queue.indexOf(entry);
        if (index !== -1) queue.splice(index, 1);
        reject(new Error("queue_timeout"));
      }, wait);
      queue.push(entry);
    });
  }

  function stats() {
    return { active, queued: queue.length, maxActive, maxQueue };
  }

  return { acquire, release, stats };
}

const chatGate = createGate(MAX_INFLIGHT_CHAT, MAX_QUEUE_CHAT);
const activeByClient = new Map();

function takeClientSlot(key) {
  const count = activeByClient.get(key) || 0;
  if (count >= MAX_INFLIGHT_PER_CLIENT) return false;
  activeByClient.set(key, count + 1);
  return true;
}

function releaseClientSlot(key) {
  const count = (activeByClient.get(key) || 0) - 1;
  if (count > 0) activeByClient.set(key, count);
  else activeByClient.delete(key);
}

function rateLimit(max, ipMax = max * 20) {
  return (req, res, next) => {
    const now = Date.now();
    const clientKey = `c:${getClientKey(req)}`;
    const ipKey = `ip:${req.ip || "unknown"}`;
    const clientHits = (rateBuckets.get(clientKey) || []).filter(
      (ts) => now - ts < RATE_WINDOW_MS,
    );
    const ipHits = (rateBuckets.get(ipKey) || []).filter(
      (ts) => now - ts < RATE_WINDOW_MS,
    );

    if (clientHits.length >= max || ipHits.length >= ipMax) {
      res.status(429).json({
        detail:
          RATE_LIMIT_MESSAGES[requestLanguage(req)] || RATE_LIMIT_MESSAGES.en,
      });
      return;
    }

    clientHits.push(now);
    ipHits.push(now);
    rateBuckets.set(clientKey, clientHits);
    rateBuckets.set(ipKey, ipHits);

    if (rateBuckets.size > 2000) {
      for (const [key, times] of rateBuckets) {
        if (!times.length || now - times[times.length - 1] > RATE_WINDOW_MS) {
          rateBuckets.delete(key);
        }
      }
      // 极端情况下防止随机 Client ID 无限撑大内存
      if (rateBuckets.size > 5000) rateBuckets.clear();
    }
    next();
  };
}

app.use(express.json({ limit: "1mb" }));

// 对 JSON 响应做 gzip 压缩（仅使用 Node 内置 zlib）
app.use((req, res, next) => {
  const accept = String(req.headers["accept-encoding"] || "");
  if (!accept.includes("gzip")) {
    next();
    return;
  }

  const originalJson = res.json.bind(res);
  res.json = (body) => {
    let text;
    try {
      text = JSON.stringify(body);
    } catch {
      return originalJson(body);
    }
    if (typeof text !== "string" || text.length < 1024) {
      return originalJson(body);
    }

    zlib.gzip(Buffer.from(text), (error, buffer) => {
      if (error) {
        originalJson(body);
        return;
      }
      res.set("Content-Encoding", "gzip");
      res.set("Vary", "Accept-Encoding");
      res.set("Content-Type", "application/json; charset=utf-8");
      res.send(buffer);
    });
    return res;
  };
  next();
});

const publicDir = path.join(__dirname, "public");
const indexPath = path.join(publicDir, "index.html");
let indexGzipCache = null;

app.get("/", (req, res, next) => {
  const accept = String(req.headers["accept-encoding"] || "");
  if (!accept.includes("gzip")) {
    next();
    return;
  }

  const send = (html) => {
    zlib.gzip(html, (error, buffer) => {
      if (error) {
        res.type("html").send(html);
        return;
      }
      indexGzipCache = buffer;
      res.set("Content-Type", "text/html; charset=utf-8");
      res.set("Content-Encoding", "gzip");
      res.set("Vary", "Accept-Encoding");
      res.set("Cache-Control", "no-cache");
      res.send(buffer);
    });
  };

  if (indexGzipCache) {
    res.set("Content-Type", "text/html; charset=utf-8");
    res.set("Content-Encoding", "gzip");
    res.set("Vary", "Accept-Encoding");
    res.set("Cache-Control", "no-cache");
    res.send(indexGzipCache);
    return;
  }

  fs.readFile(indexPath, (error, html) => {
    if (error) {
      next();
      return;
    }
    send(html);
  });
});

app.use(
  express.static(publicDir, {
    maxAge: process.env.NODE_ENV === "production" ? "1h" : 0,
    setHeaders(res, filePath) {
      if (filePath.endsWith("index.html")) {
        res.setHeader("Cache-Control", "no-cache");
      }
    },
  }),
);

// 地图图片单独限流：<img> 请求无法携带 X-Client-Id，只能按 IP 计数
app.get("/api/map", rateLimit(600), async (req, res) => {
  const params = new URLSearchParams(req.query).toString();
  try {
    const upstream = await fetch(`${BACKEND_URL}/map?${params}`, {
      signal: AbortSignal.timeout(12000),
    });
    if (!upstream.ok) {
      res.status(502).json({ error: "地图加载失败" });
      return;
    }

    const contentType = upstream.headers.get("content-type") || "";
    if (!contentType.startsWith("image/")) {
      res.status(502).json({ error: "地图加载失败" });
      return;
    }

    const buffer = Buffer.from(await upstream.arrayBuffer());
    res.set("Content-Type", contentType);
    res.set("Cache-Control", "public, max-age=600");
    res.send(buffer);
  } catch {
    res.status(502).json({ error: "地图加载失败" });
  }
});

app.use("/api", rateLimit(60));

app.get("/api/products", async (req, res) => {
  try {
    const result = await products.searchProducts({
      city: req.query.city,
      poi: req.query.poi,
      limit: req.query.limit,
    });
    res.json(result);
  } catch (error) {
    res.status(error.status || 500).json({ error: error.message });
  }
});

app.get("/api/hotels", async (req, res) => {
  try {
    const result = await hotels.searchHotels({
      city: req.query.city,
      hotel: req.query.hotel,
      preference: req.query.preference,
      check_in: req.query.check_in,
      check_out: req.query.check_out,
      limit: req.query.limit,
    });
    res.json(result);
  } catch (error) {
    res.status(error.status || 500).json({ error: error.message });
  }
});

app.get("/api/transport", async (req, res) => {
  try {
    const result = await transport.searchTransport({
      origin: req.query.origin,
      destination: req.query.destination,
      date: req.query.date,
    });
    res.json(result);
  } catch (error) {
    res.status(error.status || 500).json({ error: error.message });
  }
});

app.get("/healthz", (req, res) => {
  res.json({
    ok: true,
    chat: chatGate.stats(),
    active_clients: activeByClient.size,
  });
});

app.post("/chat", rateLimit(CHAT_RATE_LIMIT_PER_MIN), async (req, res) => {
  const language = normalizeLanguage(req.body && req.body.language);
  const clientKey = getClientKey(req);
  const deadline = Date.now() + CHAT_TIMEOUT_MS;

  if (!takeClientSlot(clientKey)) {
    sendBusy(res, 429, language);
    return;
  }

  let admitted = false;
  try {
    try {
      await chatGate.acquire(deadline);
      admitted = true;
    } catch {
      sendBusy(res, 503, language);
      return;
    }

    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      sendBusy(res, 504, language);
      return;
    }

    let upstream;
    try {
      upstream = await fetch(`${BACKEND_URL}/chat`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Forwarded-For": req.ip || "",
          "X-Client-Id": clientKey,
          "X-Language": language,
        },
        body: JSON.stringify(req.body || {}),
        signal: AbortSignal.timeout(remaining),
      });
    } catch (error) {
      const timeout =
        error && (error.name === "TimeoutError" || error.name === "AbortError");
      sendBusy(res, timeout ? 504 : 502, language);
      return;
    }

    const text = await upstream.text();
    res.status(upstream.status);
    res.set(
      "Content-Type",
      upstream.headers.get("content-type") || "application/json; charset=utf-8",
    );
    res.send(text);
  } catch (error) {
    if (!res.headersSent) sendBusy(res, 502, language);
  } finally {
    if (admitted) chatGate.release();
    releaseClientSlot(clientKey);
  }
});

app.get("/", (req, res) => {
  res.sendFile(path.join(__dirname, "public", "index.html"));
});

app.use((err, req, res, next) => {
  if (err && err.type === "entity.too.large") {
    res.status(413).json({ error: "请求体过大" });
    return;
  }
  if (err instanceof SyntaxError && err.status === 400 && "body" in err) {
    res.status(400).json({ error: "请求体不是合法 JSON" });
    return;
  }
  res.status(500).json({ error: "服务内部错误" });
});

app.listen(PORT, HOST, () => {
  console.log(`前端服务已启动: http://${HOST}:${PORT}`);
  console.log(`后端地址: ${BACKEND_URL}`);
});