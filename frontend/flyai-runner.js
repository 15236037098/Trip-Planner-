/**
 * 飞猪 CLI 执行器：仅负责加载环境、校验命令并执行 CLI。
 * 不包含技能发现/LLM 调用，供 /api/products 等接口复用。
 */

const fs = require("node:fs");
const path = require("node:path");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");

const execFileAsync = promisify(execFile);
const FLYAI_BIN =
  process.env.FLYAI_BIN ||
  path.join(__dirname, "node_modules", "@fly-ai", "flyai-cli", "dist", "flyai-bundle.cjs");
const FLYAI_COMMANDS = new Set([
  "search-poi",
  "search-hotel",
  "search-hotels",
  "search-flight",
  "search-train",
  "keyword-search",
  "fliggy-fast-search",
  "ai-search",
  "search-marriott-hotel",
  "search-marriott-package",
]);

function redactSecrets(text) {
  return String(text || "")
    .replace(/sk-[A-Za-z0-9_-]{8,}/g, "sk-***")
    .replace(/([?&]key=)[^&\s]+/gi, "$1***");
}

let envLoaded = false;

function loadEnv() {
  if (envLoaded) return;
  envLoaded = true;
  const candidates = [
    process.env.SKILL_ENV_FILE,
    path.join(__dirname, "..", ".env"),
    path.join(__dirname, ".env"),
    path.join(__dirname, "..", "backend", "app", "config", ".env"),
  ].filter(Boolean);

  for (const file of candidates) {
    try {
      process.loadEnvFile(file);
    } catch {
      // 文件不存在时继续尝试下一个
    }
  }
}

async function runFlyai(args, options = {}) {
  if (!Array.isArray(args) || args.length === 0) {
    throw new Error("args 必须是非空字符串数组");
  }
  if (args.length > 12) {
    throw new Error("参数过多");
  }

  const argv = args.map((item) => String(item));
  if (!FLYAI_COMMANDS.has(argv[0])) {
    throw new Error(`不允许的命令：${argv[0]}`);
  }
  if (!fs.existsSync(FLYAI_BIN)) {
    throw new Error("未安装 @fly-ai/flyai-cli");
  }

  loadEnv();
  const apiKey = process.env.FLYAI_API_KEY;
  const timeoutMs = Number(options.timeoutMs) || 90000;

  try {
    const { stdout } = await execFileAsync(process.execPath, [FLYAI_BIN, ...argv], {
      timeout: timeoutMs,
      maxBuffer: 4 * 1024 * 1024,
      windowsHide: true,
      env: { ...process.env, ...(apiKey ? { FLYAI_API_KEY: apiKey } : {}) },
    });
    return stdout.trim();
  } catch (error) {
    const stdout = (error.stdout || "").trim();
    // Windows 上 CLI 可能在输出有效结果后异常退出，优先使用 stdout
    if (stdout) return stdout;
    throw new Error(redactSecrets(error.stderr || error.message || "").slice(0, 500));
  }
}

module.exports = { runFlyai };
