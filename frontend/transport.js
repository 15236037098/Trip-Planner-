/**
 * 火车/航班检索：调用飞猪 flyai 的 search-train 与 search-flight。
 *
 * 低耦合：只依赖 flyai-search 基础设施；
 * 高并发：TTL 缓存 + 同参数请求合并 + 串行 CLI 调用（避免上游限流）；
 * 输出固定 JSON，便于网页与小程序共用。
 */

const { runFlyaiJson, createSearchRunner } = require("./flyai-search");

const CACHE_TTL = 10 * 60 * 1000;
const MAX_OPTIONS = 5;

const runner = createSearchRunner({
  ttl: CACHE_TTL,
  maxConcurrent: Number(process.env.FLYAI_TRANSPORT_CONCURRENCY) || 1,
  maxQueue: Number(process.env.FLYAI_QUEUE_SIZE) || 100,
  queueTimeoutMs: Number(process.env.FLYAI_QUEUE_TIMEOUT_MS) || 3000,
  maxCache: 100,
});

class TransportError extends Error {
  constructor(message) {
    super(message);
    this.status = 400;
  }
}

function toMinutes(value) {
  const text = String(value === null || value === undefined ? "" : value).trim();
  if (/^\d+(\.\d+)?$/.test(text)) return Math.round(Number(text));
  let minutes = 0;
  const hours = text.match(/(\d+)\s*(小时|h)/i);
  const mins = text.match(/(\d+)\s*(分钟|min)/i);
  if (hours) minutes += Number(hours[1]) * 60;
  if (mins) minutes += Number(mins[1]);
  return minutes || 0;
}

function toPrice(item) {
  const raw = item.price ?? item.ticketPrice ?? item.adultPrice ?? "";
  const amount = Number(String(raw).replace(/[^\d.]/g, ""));
  return Number.isFinite(amount) && amount > 0 ? amount : 0;
}

function collectTrains(data) {
  const list = data && Array.isArray(data.itemList) ? data.itemList : [];
  const seen = new Set();
  const trains = [];

  for (const item of list) {
    const journey = (item.journeys || [])[0] || {};
    const segments = journey.segments || [];
    if (!segments.length) continue;

    const first = segments[0];
    const last = segments[segments.length - 1];
    const no = String(first.marketingTransportNo || "").trim();
    const price = toPrice(item);
    const key = `${no}|${first.seatClassName || ""}`;
    if (!no || !price || seen.has(key)) continue;

    seen.add(key);
    trains.push({
      no,
      price,
      price_text: `¥${price}`,
      duration_min: toMinutes(journey.totalDuration || item.totalDuration || first.duration),
      dep_station: String(first.depStationName || ""),
      arr_station: String(last.arrStationName || ""),
      dep_time: String(first.depDateTime || ""),
      arr_time: String(last.arrDateTime || ""),
      seat: String(first.seatClassName || ""),
      direct: segments.length <= 1,
      url: String(item.jumpUrl || ""),
    });
  }

  trains.sort((a, b) => Number(b.direct) - Number(a.direct) || a.duration_min - b.duration_min || a.price - b.price);
  return trains.slice(0, MAX_OPTIONS);
}

function collectFlights(data) {
  const list = data && Array.isArray(data.itemList) ? data.itemList : [];
  const seen = new Set();
  const flights = [];

  for (const item of list) {
    const journey = (item.journeys || [])[0] || {};
    const segments = journey.segments || [];
    if (!segments.length) continue;

    const first = segments[0];
    const last = segments[segments.length - 1];
    const no = String(first.marketingTransportNo || "").trim();
    const price = toPrice(item);
    if (!no || !price || seen.has(no)) continue;

    seen.add(no);
    flights.push({
      no,
      airline: String(first.marketingTransportName || ""),
      price,
      price_text: `¥${price}`,
      duration_min: toMinutes(journey.totalDuration || item.totalDuration || first.duration),
      dep_airport: String(first.depStationName || ""),
      arr_airport: String(last.arrStationName || ""),
      dep_time: String(first.depDateTime || ""),
      arr_time: String(last.arrDateTime || ""),
      cabin: String(first.seatClassName || ""),
      direct: segments.length <= 1,
      url: String(item.jumpUrl || ""),
    });
  }

  flights.sort((a, b) => Number(b.direct) - Number(a.direct) || a.duration_min - b.duration_min || a.price - b.price);
  return flights.slice(0, MAX_OPTIONS);
}

async function fetchOptions(origin, destination, date) {
  const base = ["--origin", origin, "--destination", destination];
  if (date) base.push("--dep-date", date);

  const result = {
    origin,
    destination,
    date: date || "",
    trains: [],
    flights: [],
    errors: [],
    source: "fliggy",
    updated_at: new Date().toISOString(),
  };

  // 火车与航班并发查询，避免串行叠加把耗时推到会话预算之外
  const [trains, flights] = await Promise.all([
    runFlyaiJson(["search-train", ...base])
      .then((payload) => collectTrains(payload.data))
      .catch((error) => {
        result.errors.push(`火车：${error.message}`);
        return [];
      }),
    runFlyaiJson(["search-flight", ...base])
      .then((payload) => collectFlights(payload.data))
      .catch((error) => {
        result.errors.push(`航班：${error.message}`);
        return [];
      }),
  ]);
  result.trains = trains;
  result.flights = flights;

  if (!result.trains.length && !result.flights.length) {
    throw new TransportError(result.errors[0] || "未找到可用的火车或航班");
  }
  return result;
}

async function searchTransport(options) {
  const input = options || {};
  const origin = String(input.origin || "").trim().slice(0, 20);
  const destination = String(input.destination || "").trim().slice(0, 20);
  if (!origin || !destination) throw new TransportError("缺少出发地或目的地");
  const date = String(input.date || "").trim().slice(0, 10);

  const { value, cached } = await runner.run(
    `${origin}|${destination}|${date}`,
    () => fetchOptions(origin, destination, date),
  );
  return { ...value, cached };
}

module.exports = { searchTransport, TransportError };
