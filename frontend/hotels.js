/**
 * 酒店价格检索：调用飞猪 flyai 的 search-hotel，返回带真实房价的酒店/民宿/青旅。
 *
 * 低耦合：只依赖 flyai-search 基础设施；
 * 高并发：TTL 缓存 + 同参数请求合并 + CLI 并发闸门（由 flyai-search 提供）；
 * 输出固定 JSON，便于网页与小程序共用。
 */

const { runFlyaiJson, createSearchRunner } = require("./flyai-search");

const CACHE_TTL = 10 * 60 * 1000;
const MAX_ITEMS = 10;
const DEFAULT_LIMIT = 6;

const HOSTEL_WORDS = ["青旅", "青年旅舍", "青年旅社", "背包"];
const HOMESTAY_WORDS = ["民宿", "客栈", "公寓"];
const CHEAP_WORDS = ["便宜", "经济", "省钱", "低价", "性价比", "实惠", "穷游"];
const LUXURY_WORDS = ["高端", "豪华", "奢侈", "五星", "顶级", "高档"];
const COMFORT_WORDS = ["舒适", "舒服", "品质", "度假"];

const runner = createSearchRunner({
  ttl: CACHE_TTL,
  maxConcurrent: Number(process.env.FLYAI_HOTEL_CONCURRENCY) || 2,
  maxQueue: Number(process.env.FLYAI_QUEUE_SIZE) || 100,
  queueTimeoutMs: Number(process.env.FLYAI_QUEUE_TIMEOUT_MS) || 3000,
  maxCache: 200,
});

class HotelError extends Error {
  constructor(message) {
    super(message);
    this.status = 400;
  }
}

function hasWord(text, words) {
  return words.some((word) => text.includes(word));
}

const PRICE_UNITS = { 万: 10000, 千: 1000 };

function toPrice(value) {
  const raw = String(value === null || value === undefined ? "" : value).replace(/[,，\s]/g, "");
  // 兼容 "¥1,299"、"¥1.2万"、"¥328起"；无法解析时返回 0，按未知价处理
  const match = raw.match(/(\d+(?:\.\d+)?)\s*(万|千)?/);
  if (!match) return 0;
  const amount = Number(match[1]) * (PRICE_UNITS[match[2]] || 1);
  return Number.isFinite(amount) && amount > 0 ? amount : 0;
}

function toScore(value) {
  const amount = Number(String(value === null || value === undefined ? "" : value).replace(/[^\d.]/g, ""));
  return Number.isFinite(amount) && amount > 0 ? amount.toFixed(1) : "";
}

function toDateString(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

function parseDate(value) {
  const text = String(value || "").trim();
  if (!DATE_PATTERN.test(text)) return null;
  const date = new Date(`${text}T00:00:00`);
  // 排除 2026-02-31 这类不存在的日期
  if (Number.isNaN(date.getTime()) || toDateString(date) !== text) return null;
  return date;
}

function addDays(date, days) {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

// 未传日期、日期非法或已过期时，默认「明天入住、后天退房」；只给退房日期时按住一晚推算
function resolveStayDates(checkInValue, checkOutValue) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  let checkIn = parseDate(checkInValue);
  if (!checkIn || checkIn < today) checkIn = null;
  const checkOut = parseDate(checkOutValue);

  if (!checkIn) {
    checkIn = checkOut ? addDays(checkOut, -1) : addDays(today, 1);
    if (checkIn < today) checkIn = addDays(today, 1);
  }
  const end = !checkOut || checkOut <= checkIn ? addDays(checkIn, 1) : checkOut;

  return { checkIn: toDateString(checkIn), checkOut: toDateString(end) };
}

function normalizeHotel(name, city) {
  let value = String(name || "").replace(/\s+/g, " ").trim();
  if (city && value.startsWith(city)) value = value.slice(city.length);
  return value.trim() || String(name || "").trim();
}

function buildArgs(city, hotel, preference, dates) {
  let args;
  if (hotel) {
    args = ["search-hotel", "--dest-name", city, "--key-words", normalizeHotel(hotel, city), "--sort", "rate_desc"];
  } else if (hasWord(preference, HOSTEL_WORDS)) {
    args = ["search-hotel", "--dest-name", city, "--key-words", "青年旅社", "--sort", "price_asc"];
  } else if (hasWord(preference, HOMESTAY_WORDS)) {
    args = ["search-hotel", "--dest-name", city, "--hotel-types", "民宿", "--sort", "price_asc"];
  } else if (hasWord(preference, CHEAP_WORDS)) {
    args = ["search-hotel", "--dest-name", city, "--sort", "price_asc", "--max-price", "400"];
  } else if (hasWord(preference, LUXURY_WORDS)) {
    args = ["search-hotel", "--dest-name", city, "--hotel-stars", "4,5", "--sort", "rate_desc"];
  } else if (hasWord(preference, COMFORT_WORDS)) {
    args = ["search-hotel", "--dest-name", city, "--hotel-stars", "3,4,5", "--sort", "rate_desc"];
  } else {
    args = ["search-hotel", "--dest-name", city, "--sort", "rate_desc"];
  }
  return [...args, "--check-in-date", dates.checkIn, "--check-out-date", dates.checkOut];
}

function collect(data, hotel, city) {
  const list = data && Array.isArray(data.itemList) ? data.itemList : [];
  const byName = new Map();

  for (const entry of list) {
    if (!entry) continue;
    const name = String(entry.name || "").trim();
    if (!name) continue;

    const price = toPrice(entry.price);
    // 同一家酒店可能重复出现，优先保留带价格的条目；无价格的也保留，避免误报“没找到”
    const existing = byName.get(name);
    if (existing && (existing.price || !price)) continue;

    const keyword = hotel ? normalizeHotel(hotel, city) : "";
    byName.set(name, {
      name,
      price,
      price_text: price ? `¥${price}/晚` : "",
      star: String(entry.star || ""),
      // CLI 1.0.16 实际返回 rate，旧版文档里是 score/scoreDesc
      score: toScore(entry.score || entry.rate),
      score_desc: String(entry.scoreDesc || entry.rateDesc || ""),
      address: String(entry.address || ""),
      image: String(entry.mainPic || ""),
      url: String(entry.detailUrl || ""),
      review: String(entry.review || entry.comments || ""),
      tags: [entry.star, entry.interestsPoi].filter((tag) => typeof tag === "string" && tag.trim()).slice(0, 3),
      matched: Boolean(keyword) && matchScore(name, keyword) > 0,
    });
  }

  const items = [...byName.values()];

  if (hotel) {
    items.sort((a, b) => Number(b.matched) - Number(a.matched));
  }
  return items;
}

function matchScore(name, keyword) {
  if (!keyword) return 0;
  if (name === keyword) return 2;
  if (name.includes(keyword) || keyword.includes(name)) return 1;
  return 0;
}

function fallbackArgs(city, hotel, dates) {
  let args;
  if (hasWord(hotel, HOSTEL_WORDS)) {
    args = ["search-hotel", "--dest-name", city, "--key-words", "青年旅社", "--sort", "price_asc"];
  } else if (hasWord(hotel, HOMESTAY_WORDS)) {
    args = ["search-hotel", "--dest-name", city, "--hotel-types", "民宿", "--sort", "price_asc"];
  } else {
    args = ["search-hotel", "--dest-name", city, "--sort", "rate_desc"];
  }
  return [...args, "--check-in-date", dates.checkIn, "--check-out-date", dates.checkOut];
}

async function fetchHotels(city, hotel, preference, dates) {
  const payload = await runFlyaiJson(buildArgs(city, hotel, preference, dates));
  let items = collect(payload.data, hotel, city);
  let fallback = false;

  if (hotel) {
    const matched = items.filter((item) => item.matched);
    if (matched.length) {
      items = matched;
    } else {
      // 飞猪没有收录这家酒店时，退回同城同类住宿，避免展示无关结果
      const altPayload = await runFlyaiJson(fallbackArgs(city, hotel, dates));
      items = collect(altPayload.data, "", city);
      fallback = true;
    }
  }

  items = items.map(({ matched, ...rest }) => rest);
  if (!items.length) {
    throw new HotelError(
      hotel
        ? `没有找到「${hotel}」的可订房价，可以换个酒店试试`
        : `没有找到符合“${preference}”的酒店，可以换个偏好（便宜/高端/舒适/民宿/青旅）`,
    );
  }

  return {
    city,
    hotel,
    preference,
    check_in: dates.checkIn,
    check_out: dates.checkOut,
    fallback,
    items,
    total: items.length,
    system_message: typeof payload.systemMessage === "string" ? payload.systemMessage : "",
    source: "fliggy",
    updated_at: new Date().toISOString(),
  };
}

async function searchHotels(options) {
  const input = options || {};
  const city = String(input.city || "").trim().slice(0, 20);
  if (!city) throw new HotelError("缺少城市名称");
  const hotel = String(input.hotel || "").trim().slice(0, 40);
  const preference = String(input.preference || "").trim().slice(0, 20);
  const limit = Math.max(1, Math.min(Number(input.limit) || DEFAULT_LIMIT, MAX_ITEMS));
  const dates = resolveStayDates(input.check_in, input.check_out);

  const { value, cached } = await runner.run(
    `${city}|${hotel}|${hotel ? "" : preference}|${dates.checkIn}|${dates.checkOut}`,
    () => fetchHotels(city, hotel, preference, dates),
  );
  return { ...value, items: value.items.slice(0, limit), cached };
}

module.exports = { searchHotels, HotelError };
