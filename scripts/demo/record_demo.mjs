#!/usr/bin/env node
/**
 * 录制 README 演示：按脚本走一遍「聊天流式 → 引用 → 知识库 → 路由网关 → 告警」，逐帧截图。
 *
 * 前置：
 *   1. 后端在跑（fake 模型 + hash 关键词模式即可）：`python -m tsukiyohub`
 *   2. 前端 dev server 在跑，并代理到后端：`VITE_API_PROXY=http://127.0.0.1:8080 npm run dev -- --port 5175`
 *   3. 想录「路由网关」页时，再起 Go 网关 + 假上游（见 docs/gateway.md）
 *
 * 用法：
 *   node scripts/demo/record_demo.mjs
 *   BASE_URL=http://127.0.0.1:5175 OUT_DIR=docs/assets/demo-frames node scripts/demo/record_demo.mjs
 *
 * 环境变量：
 *   BASE_URL        前端地址（默认 http://127.0.0.1:5175）
 *   OUT_DIR         帧目录（默认 docs/assets/demo-frames）
 *   PLAYWRIGHT_ROOT 已安装 playwright 的项目目录（脚本从它的 node_modules 解析，默认自动探测）
 *   WIDTH/HEIGHT    视口（默认 1440x900）
 *   QUESTION        提问内容（默认「检索是怎么工作的？」）
 *
 * 产物：OUT_DIR/frame-0001.png ...；随后用 build_gif.py 合成 GIF。
 */
import { createRequire } from "node:module";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import process from "node:process";

const ROOT = process.cwd();
const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:5175";
const OUT_DIR = path.resolve(ROOT, process.env.OUT_DIR || "docs/assets/demo-frames");
const STILL_DIR = path.resolve(ROOT, process.env.STILL_DIR || "docs/assets");
const WIDTH = Number(process.env.WIDTH || 1440);
const HEIGHT = Number(process.env.HEIGHT || 900);
const STREAM_MS = Number(process.env.STREAM_MS || 12000);
const STREAM_STEP = Number(process.env.STREAM_STEP || 420);
const QUESTION = process.env.QUESTION || "部署到生产环境要注意什么？";

function loadPlaywright() {
  const candidates = [
    process.env.PLAYWRIGHT_ROOT && path.join(process.env.PLAYWRIGHT_ROOT, "package.json"),
    path.join(ROOT, "web", "package.json"),
    path.join(ROOT, "package.json"),
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      const req = createRequire(candidate);
      return req("playwright");
    } catch {
      /* 继续尝试下一个 */
    }
  }
  return createRequire(import.meta.url)("playwright");
}

const { chromium } = loadPlaywright();

let frameIndex = 0;
const shots = [];

async function main() {
  rmSync(OUT_DIR, { recursive: true, force: true });
  mkdirSync(OUT_DIR, { recursive: true });
  mkdirSync(STILL_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: WIDTH, height: HEIGHT },
    deviceScaleFactor: 1,
    locale: "zh-CN",
  });
  const page = await context.newPage();

  const snap = async (label) => {
    frameIndex += 1;
    const file = path.join(OUT_DIR, `frame-${String(frameIndex).padStart(4, "0")}.png`);
    await page.screenshot({ path: file, animations: "disabled" });
    shots.push({ file, label });
  };
  /** 在 ms 毫秒内每 step 毫秒截一帧（配合流式输出） */
  const snapLoop = async (ms, step = 320) => {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      await snap("stream");
      await page.waitForTimeout(step);
    }
  };

  console.log(`→ 打开 ${BASE_URL}`);
  await page.goto(BASE_URL, { waitUntil: "networkidle", timeout: 60_000 });
  await page.waitForTimeout(1200);
  await snap("chat-empty");

  // 1) 输入问题（逐字输入更好看）
  const textarea = page.locator("textarea").first();
  await textarea.waitFor({ state: "visible", timeout: 15_000 });
  await textarea.click();
  await textarea.pressSequentially(QUESTION, { delay: 45 });
  await snap("typing");

  // 2) 发送并录流式
  await textarea.press("Enter");
  await page.waitForTimeout(600);
  await snapLoop(STREAM_MS, STREAM_STEP);

  // 3) 等回答结束（停止按钮消失），截引用
  try {
    await page.waitForSelector("button:has-text('发送')", { timeout: 60_000 });
  } catch {
    /* 文案可能不同，忽略 */
  }
  await page.waitForTimeout(1200);
  await snap("answer");
  await page.screenshot({ path: path.join(STILL_DIR, "chat.png"), animations: "disabled" });

  // 4) 知识库页
  await page.goto(`${BASE_URL}/knowledge`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1400);
  await snap("knowledge");
  await page.screenshot({ path: path.join(STILL_DIR, "knowledge.png"), animations: "disabled" });

  // 5) 路由网关页（需要网关在跑，否则会是空态——空态也能录，但价值低）
  await page.goto(`${BASE_URL}/routing`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1800);
  await snap("routing");
  await page.screenshot({ path: path.join(STILL_DIR, "routing.png"), animations: "disabled" });

  // 6) 告警页
  await page.goto(`${BASE_URL}/alerts`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  await snap("alerts");
  await page.screenshot({ path: path.join(STILL_DIR, "alerts.png"), animations: "disabled" });

  // 7) 设置页收尾
  await page.goto(`${BASE_URL}/settings`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  await snap("settings");
  await snap("settings-2");

  await browser.close();
  console.log(`\n✓ 共 ${shots.length} 帧 → ${OUT_DIR}`);
  console.log("  合成 GIF：python scripts/demo/build_gif.py");
}

main().catch((error) => {
  console.error("录制失败：", error);
  process.exit(1);
});