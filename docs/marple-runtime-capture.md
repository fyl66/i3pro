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

## 第三步：证据怎么存

- 全部落在 **`out\_marple\runtime\`**（`out/` 已 gitignore，证据不进仓库）。
- 文件名带**时间戳 + 目标**：`2026-10-17T21-30_page.json`、`…_har.har`、`…_attitude.png`。
- 采集完在 `out\_marple\runtime\README.md` 里记一行：文件名、命令、当场 sha256。
- 结论回填 [marple-parity.md](marple-parity.md) 对应行的"证据"栏；**顺手给出字面值**
  （字段名、枚举取值、数值档位），不要只写"和预期一致"。

## 判据

本轮算完成的标志：`signal` / `segment` / `figure` / `function` 四个概念的**字段名**有字面证据；
`/signals/cursor/stats` 的返回量清单**列得出来**；姿态图的渲染方式**看一眼就能定性**
（有网格/相机/3D 模型 vs 2D 罗盘）。做到这些，P1–P4 的 spec 才有资格开票。
