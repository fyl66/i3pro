# 运行时证据采集规程（Marple Insight）

[marple-parity.md](marple-parity.md) 那张表的证据全部来自**静态包**——只能看到端点名、
枚举值和文案。运行时那一轮补的是静态看不到的东西：

- `signal` / `segment` / `figure` / `workbook` / `function` 的**真实字段名**（现在是推断）；
- 光标区间统计（`/signals/cursor/stats`）的**返回形状**——决定 P2 要做哪几个量；
- 姿态图的实际渲染与配置面板（决定 P1 要不要 3D）；
- `PLOT_RESOLUTION` 的**取值与效果**（决定 P4 的档位设计）。

## 为什么需要人来做第一步

开一个带调试端口的浏览器**只能由你执行**：Agent 侧的启动命令会被安全策略挡住，
而且需要你在窗口里登录 Aurora workspace。

## 第一步：开一个带 CDP 的 Edge 窗口（人做，约 1 分钟）

用**独立用户目录**开，避免动你日常那个 Edge 配置：

```powershell
& 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe' `
  --remote-debugging-port=9222 `
  --remote-allow-origins=* `
  --user-data-dir="$env:TEMP\marple-re-profile" `
  '<把 workspace 邀请链接粘在这里>'
```

窗口起来后在里头登录、进到 Aurora workspace 的某一场数据上，**把要看的画面摆好**
（比如打开一个含姿态图的 workbook、摆好光标 A/B）。然后把上面这条命令行、窗口是否已登录
告诉 Agent，剩下由 Agent 跑。

> 邀请链接带一次性邀请码，**不要粘进仓库文件、issue 或截图里的地址栏**。
> 这个用户目录用完可以整个删掉（`$env:TEMP\marple-re-profile`）。

## 第二步：Agent 跑 REA（无需再动鼠标）

系统里的 `node` 还是 22.14（REA 要求 ≥22.19），所以先把它指到用户目录那份：

```powershell
$env:PATH = "$env:LOCALAPPDATA\Programs\node-v22.23.3;" + $env:PATH
New-Item -ItemType Directory -Force -Path out\_marple\runtime | Out-Null

rea list-browser-targets http://127.0.0.1:9222
```

拿到 `targetId` 之后按需要挑命令（都只读，不改远程数据）：

| 目的 | 命令 |
| --- | --- |
| 页面结构 / 请求形状 / WebSocket 形状 / 存储键 | `rea inspect-web-page http://127.0.0.1:9222 <targetId> --include-websocket-shapes --include-json-body-shapes --include-storage-keys --include-console-text --format json` |
| 抓整包（含运行时补上的懒加载分块） | `rea analyze-web-bundle http://127.0.0.1:9222 <targetId> --fetch-source-maps --format json` |
| 你边点我边看 | `rea observe-web-session http://127.0.0.1:9222 <targetId>`（在它给的窗口里操作） |
| 取视觉证据（姿态图、PFD） | `rea capture-web-screenshot http://127.0.0.1:9222 <targetId> --format json` |
| 事件监听器落在哪一行 | `rea inspect-web-event-listeners http://127.0.0.1:9222 <targetId> <选择器>` |
| 分块之间的依赖 | `rea trace-web-module-imports <已抓的脚本>` |

网络记录的两种来法：DevTools 里 **Network → 右键 → Save all as HAR**（人点两下，最省事），
或者让 REA 记录；拿到 HAR 后用：

```powershell
rea inspect-web-network-capture out\_marple\runtime\<name>.har --format json
```

## 第三步：要拿接口与数据模型，用 connect 模式的场景（本轮实测可用）

`inspect-web-page` 是**附着之后**才开始记录的，所以页面已经在跑的时候抓不到启动请求。
场景命令可以**连到同一个已登录的浏览器**并驱动它重载，把整段启动序列抓下来
（实测一次拿到 56 个请求 / 292 条事件）。存成 `scenario.json`：

```json
{
  "browser": {
    "mode": "connect",
    "cdp_endpoint": "http://127.0.0.1:9222",
    "target_id": "<list-browser-targets 给的 target_id>"
  },
  "start_url": { "url": "https://insight.marpledata.com/visualisation" },
  "actions": [
    { "step_id": "settle", "action": "wait_for_timeout", "duration_ms": 12000 }
  ],
  "capture": {
    "at_end": ["url", "accessibility"],
    "events": ["network", "console", "page-errors"],
    "network": { "request_body": true, "response_body": true, "header_values": false }
  }
}
```

```powershell
rea capture-browser-scenario scenario.json --format json > out\_marple\runtime\<时间戳>-scenario.json
```

要点：`mode` 必须用 `connect`（`launch` 会开一个没有登录态的新实例，只会停在登录页）；
响应体挂在 `events.items` 里 `kind === "network-content"` 的条目上，要按 `transaction_id`
与 `kind === "request"` 的条目配起来才知道是哪个 URL。

## 第四步：只在操作时才发的请求，靠"人点 + observe-web-session"

光标区间统计这类请求只在**操作时**才发。开着那个窗口时跑：

```powershell
rea observe-web-session http://127.0.0.1:9222 <target_id> --observation-ms 60000
```

这 60 秒里在那台 Edge 上操作即可。**2026-10-17 已用这一步拿到光标统计与两个取数端点**
（见下面"判据与现状"）。

> **姿态图/PFD 在 Aurora 的账号上抓不到**：`Add Plot` 的选项按 license 过滤，
> `Attitude` 与 `PFD` 属于 `AEROSPACE` 包，而这份 sponsorship license 的
> `features` 里没有它。换到有 AEROSPACE 的账号（或官方 demo）时，这一步仍然可用：
> 点 `Add Plot` → Aerospace 组 → 选 Attitude/PFD，等它画出来。

## 第五步：证据怎么存

- 全部落在 **`out\_marple\runtime\`**（`out/` 已 gitignore，证据不进仓库）。
- 文件名带**时间戳 + 目标**：`2026-10-17T21-30_page.json`、`…_har.har`、`…_attitude.png`。
- 采集完在 `out\_marple\runtime\README.md` 里记一行：文件名、命令、当场 sha256。
- 结论回填 [marple-parity.md](marple-parity.md) 对应行的"证据"栏；**顺手给出字面值**
  （字段名、枚举取值、数值档位），不要只写"和预期一致"。

## 判据与现状

**2026-10-17 已完成**：启动序列（12 个 `/api/v1/*` 端点）、数据集模型
（`dataset_filter.dataset/stream/layer_shift`、`nsignals`、`datapoints`、`datapool`）、
顶层导航与各页路径、分析页控件清单、本地存储键、以及"没有 WebSocket"这一条——
全部拿到字面证据（汇总见 [marple-parity.md](marple-parity.md) 的运行时一节）。

**2026-10-17 18:51 又补了一轮**（人摆好 A/B 光标后开 60 秒窗口）：拿到了
`/signals/cursor/stats`（响应只有 `min/mean/max`）、`/signals/t_base`（**分桶 min/max 包络**，
带 `x_base_name` 与 `z_signal`）、`/sources/signals`（通道清单 + 每条通道元数据）三份字段路径。
P2 与 P4 由此证据齐备，可以直接进 `/to-spec`。

**姿态图/PFD 不再需要补采集**：它在你们账号上被 license 挡住（`AEROSPACE` 不在
`features` 里），证据停在静态 chunk 那一层，而那一层已经够写 spec。
目前 P1–P4 四条都不缺证据了。
