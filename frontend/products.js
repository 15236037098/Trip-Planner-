/**
 * 景点商品检索：调用飞猪 flyai 的 keyword-search，返回可预订项目与价格。
 *
 * 低耦合：只依赖 flyai-search 基础设施，不感知技能目录与 LLM；
 * 高并发：TTL 缓存 + 同参数请求合并 + CLI 并发闸门（由 flyai-search 提供）；
 * 输出固定 JSON，便于网页与小程序共用。
 */

const { runFlyaiJson, createSearchRunner } = require("./flyai-search");

const CACHE_TTL = 10 * 60 * 1000;
const MAX_ITEMS = 10;
const DEFAULT_LIMIT = 6;

const runner = createSearchRunner({
  ttl: CACHE_TTL,
  maxConcurrent: Number(process.env.FLYAI_PRODUCT_CONCURRENCY) || 2,
  maxQueue: Number(process.env.FLYAI_QUEUE_SIZE) || 100,
  queueTimeoutMs: Number(process.env.FLYAI_QUEUE_TIMEOUT_MS) || 3000,
  maxCache: 200,
});

class ProductError extends Error {
  constructor(message) {
    super(message);
    this.status = 400;
  }
}

function toPrice(value) {
  const amount = Number(String(value === null || value === undefined ? "" : value).replace(/[^\d.]/g, ""));
  return Number.isFinite(amount) && amount > 0 ? amount : 0;
}

function priceText(price) {
  const text = Number.isInteger(price) ? String(price) : price.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
  return `¥${text}`;
}

function toTags(value) {
  if (!Array.isArray(value)) return [];
  return value.filter((tag) => typeof tag === "string" && tag.trim()).slice(0, 3);
}

function cleanName(title) {
  const value = String(title || "").replace(/\s+/g, " ").trim();
  const first = value.match(/^\[([^\]]+)\]/);
  if (!first) return value;

  const head = first[0];
  const name = first[1].trim();
  let rest = value.slice(head.length);
  // 部分商品标题会重复同一段，去掉重复部分
  while (rest.startsWith(head)) rest = rest.slice(head.length);
  return `${name}${rest}`.trim();
}

function collect(data) {
  const list = data && Array.isArray(data.itemList) ? data.itemList : [];
  const seen = new Set();
  const items = [];

  for (const entry of list) {
    const info = entry && entry.info ? entry.info : entry;
    if (!info) continue;
    const name = cleanName(info.title || info.name);
    const price = toPrice(info.price);
    const key = `${name}|${price}`;
    if (!name || !price || seen.has(key)) continue;

    seen.add(key);
    items.push({
      name,
      price,
      price_text: priceText(price),
      image: String(info.picUrl || info.mainPic || ""),
      url: String(info.jumpUrl || info.detailUrl || ""),
      tags: toTags(info.tags),
    });
  }
  return items;
}

async function fetchProducts(city, poi) {
  const place = city && !poi.includes(city) ? city : "";
  const query = `${place} ${poi} 门票 游玩项目`.replace(/\s+/g, " ").trim();
  const payload = await runFlyaiJson(["keyword-search", "--query", query]);
  const items = collect(payload.data);

  return {
    city,
    poi,
    query,
    items,
    total: items.length,
    system_message: typeof payload.systemMessage === "string" ? payload.systemMessage : "",
    source: "fliggy",
    updated_at: new Date().toISOString(),
  };
}

async function searchProducts(options) {
  const input = options || {};
  const poi = String(input.poi || "").trim().slice(0, 40);
  if (!poi) throw new ProductError("缺少景点名称");
  const city = String(input.city || "").trim().slice(0, 20);
  const limit = Math.max(1, Math.min(Number(input.limit) || DEFAULT_LIMIT, MAX_ITEMS));

  const { value, cached } = await runner.run(`${city}|${poi}`, () => fetchProducts(city, poi));
  return { ...value, items: value.items.slice(0, limit), cached };
}

module.exports = { searchProducts, ProductError };
