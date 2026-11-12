# Demo 素材（README 录屏）

README 里的演示 GIF 和截图**不是手截的**，而是一个脚本按固定剧本走一遍界面、
逐帧截图再合成 GIF，所以可以随时重录、保持一致。

| 产物 | 内容 |
|---|---|
| `docs/assets/demo.gif` | 主演示：聊天流式 → 引用 → 知识库 → 路由网关 → 告警 → 设置（24 帧 / ~7.7s / 1.6MB） |
| `docs/assets/chat.png` | 聊天页（带引用） |
| `docs/assets/knowledge.png` | 知识库页 |
| `docs/assets/routing.png` | 路由网关页（需网关在跑） |
| `docs/assets/alerts.png` | 告警页 |

---

## 1. 起三个服务

```powershell
# A) 后端（演示模型，不需要任何 Key）
$env:LLM_PROVIDER="fake"; $env:EMBEDDING_PROVIDER="hash"
$env:VECTOR_BACKEND="memory"; $env:ANSWER_JUDGE="off"
# 让流式慢一点，录出来才看得清（只影响演示模型）
$env:TSUKIYO_FAKE_DELAY_MS="110"
Copy-Item -Recurse -Force examples\knowledge data\        # 放几篇文档
python -m tsukiyohub                                        # :8080

# B) 前端（代理到后端）
cd web
$env:VITE_API_PROXY="http://127.0.0.1:8080"
npm run dev -- --port 5175                                  # :5175

# C) 可选：路由网关 + 假上游（不启动也行，路由页会显示「网关未连接」空态）
python scripts\mock_upstream.py --port 18080
gateway\bin\tsukiyo-gateway.exe -config gateway\config.test.json -addr ":19081" -admin-addr ":19082"
# 后端要能读网关管理接口：
# $env:GATEWAY_ADMIN_URL="http://127.0.0.1:19082"
```

灌一点数据（可选，但演示更饱满）：

```powershell
# 知识库 + 会话
curl.exe -F "file=@examples/knowledge/部署说明.md" http://127.0.0.1:8080/api/knowledge/upload
# 给路由页造追踪数据
curl.exe -H "Authorization: Bearer sk-demo-local" -H "Content-Type: application/json" `
  -d '{"model":"chat","messages":[{"role":"user","content":"demo"}]}' `
  http://127.0.0.1:19081/v1/chat/completions
```

## 2. 录帧

需要 Playwright（本仓库前端没有内置，用任意已安装的项目即可）：

```powershell
# 在任意项目里装一次：
npm i -D playwright
npx playwright install chromium

# 回到仓库根目录录：
$env:PLAYWRIGHT_ROOT="E:\path\to\project-with-playwright"   # 从它的 node_modules 解析
$env:BASE_URL="http://127.0.0.1:5175"
$env:QUESTION="部署到生产环境要注意什么？"
$env:STREAM_MS="13000"      # 流式阶段录多久
$env:STREAM_STEP="380"      # 每帧间隔（毫秒）
node scripts/demo/record_demo.mjs
```

产物：`docs/assets/demo-frames/frame-0001.png …` 与四张页面截图。
（帧目录已加入 `.gitignore`，只提交 GIF 和截图。）

## 3. 合成 GIF

```powershell
python scripts/demo/build_gif.py
# 可选：控制体积/节奏
python scripts/demo/build_gif.py --width 1100 --duration 320 --hold-ms 1600
```

脚本会：

1. 去掉**连续重复帧**（流式结束后界面不再变化，这一步能砍掉一半体积）；
2. 等比缩放到目标宽度；
3. 用 Pillow 输出优化后的调色板 GIF。

> GitHub 对 README 里的 GIF 没有硬限制，但 **>10MB 会明显变慢**；当前产物 1.6MB。

## 4. 自定义剧本

改 `scripts/demo/record_demo.mjs` 里的 `snap()` / `snapLoop()` 调用顺序即可，例如：

- 只录聊天：去掉 `/knowledge`、`/routing` 等段；
- 换成英文界面：把 `QUESTION` 换英文、`newContext({ locale: "en-US" })`；
- 录移动端：`viewport: { width: 390, height: 844 }`。

## 5. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 页面显示「后端未连接」 | `VITE_API_PROXY` 没指到后端，或后端没起 |
| 路由页空态 | 网关没起（或 `GATEWAY_ADMIN_URL` 不对）；空态也能录，但信息量少 |
| GIF 只有几帧、几乎不动 | 流式太快：调大 `TSUKIYO_FAKE_DELAY_MS` 与 `STREAM_MS` |
| `Cannot find module 'playwright'` | 没设 `PLAYWRIGHT_ROOT`，或那个项目没装 playwright |
| 字体发虚 | 调大 `WIDTH`/`HEIGHT` 或 `deviceScaleFactor: 2`（GIF 会变大） |