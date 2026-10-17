# Marple 对照表：逆向证据 → i3pro 现状 → 结论

这张表回答一个问题：**Marple Insight 里哪些能力值得搬到 i3pro、代价是什么。**
每一行的"Marple 证据"都能在本地证据目录里查到原文（端点、枚举值、界面文案、artifact sha），
不是"看起来它应该有"。结论栏是**建议**，定案在 `/grill-with-docs`。

## 边界（先读这段）

- **只复刻行为、接口与交互**：不复制 Marple 的代码与静态资源，不做像素级版式抄版，
  i3pro **不调用**它的后端 API。这张表的作用是"知道该做什么"，不是"拿到一份可粘贴的实现"。
- 证据来自**公开交付给浏览器的静态包**——任何能打开这个页面的人都收得到同一份字节。
  采集过程只做读取，没有绕过鉴权，也没有访问别人 workspace 里的数据。
- 邀请链接带一次性邀请码，**不写进本仓库**（仓库是公开的）。要跑运行时采集时，
  按 [marple-runtime-capture.md](marple-runtime-capture.md) 的第一步自己粘链接。
- Marple 是**服务端产品**（时间序列库 + 连接器 + 账号体系），i3pro 是**本地单机工具**。
  凡是靠"有个后端"才成立的能力（见文末"不做"），默认不搬。

## 证据（可复现）

| 项 | 值 |
| --- | --- |
| 采集时间 | 2026-10-17 |
| 目标 | `https://insight.marpledata.com/`（应用外壳，未登录也返回 200） |
| 分析命令 | `rea analyze-javascript-application out/_marple --format json`（REA 6.5.0，静态分析，不执行 JS） |
| REA `evidence_id` | `ev_f255cf6c0ebe5d7e9a14cc430536a3fc91bf2e29edbc0d413c4b28784396c0c7` |
| 根 artifact sha256 | `8703bdbe0cad1865d9330db784f7ec9f91138a014dfecf588a0614d201e4292b`（整个 `out/_marple`） |
| 关键文件 sha256 | `index.html` `46ea1a4b…`、`index-KeLGy6Gr.js` `340d45c4…`、`vendor-charts-BallmZBw.js` `77af60a8…`、`FigureAttitude-DMq-SsjD.js` `e5846605…` |
| 规模 | 20 个源文件 / 10.5 MB / 244 万 AST 节点 / 1003 条 findings |

复现判据：重跑上面那条命令，根 artifact sha 与本节一致，事实不变。
（`out/` 已 gitignore，证据不进仓库；换机器要重新采集。）

**技术栈（实测）**：Vite + React + redux；图表 **Recharts**（`vendor-charts`，SVG）；
3D **three.js**；地图 **Leaflet** + Mapbox 瓦片；UI Blueprint + MUI；lodash / moment / date-fns。
它不是 Electron 应用——REA 的 Electron 侧（preload / IPC / contextBridge）全部为 0。

## 对照表

结论只取四种：**已覆盖**（i3pro 有同等或更好的东西）、**做**（本轮候选）、
**押后**（值得做但排在后面）、**不做**（与项目定位冲突）。

| # | 能力 | Marple 证据 | i3pro 现状 | 结论 |
| --- | --- | --- | --- | --- |
| 1 | 时间/距离曲线 | 绘图类型枚举 `TIMESERIES="timeseries"` | `graph` 组件（时间轴 / 距离轴 / 双圈三模式） | 已覆盖 |
| 2 | 散点图 | `SCATTER="scatter"` | `scatter` 组件（X×Y、第三通道着色、跟随缩放） | 已覆盖 |
| 3 | 频谱 | `FREQUENCY="frequency"` | `spectrum` 组件（Welch，五种窗，PSD/有效值） | 已覆盖 |
| 4 | 状态图 | `STATE="state"` | `status` 组件（状态与故障带） | 已覆盖 |
| 5 | 赛道地图 | `MAP="map"` + `vendor-leaflet` + `api.mapbox.com` 瓦片 + `latOffset/longOffset` | `track` 组件（本地坐标系绘制，速度着色） | 已覆盖；**在线瓦片押后**（离线优先） |
| 6 | **姿态图** | `ATTITUDE="attitude"`；`FigureAttitude-*.js`：`rollSignal` / `pitchSignal` / `yawSignal` + `angleUnits` + **每通道 multiplier** + `eulerOrder` + `customModels` + 相机复位事件；**光标 A 处叠一个 30% 透明模型**与实时姿态同屏对比 | 无（但 `dbc/261004` 已解出 Xsens MTi 的姿态/角速度通道，数据已经在场次里） | **做（P1）** |
| 7 | **主飞行显示** | `PFD="pfd"`（与 attitude 并列的一种绘图类型） | 无 | **做（P1，与姿态图同一票或紧随）** |
| 8 | 热力图 | `HEATMAP="heatmap"` | 无 | 做（P3） |
| 9 | 通用表格 | `TABULAR="tabular"`；渲染三选一 `TABLE` / `NUMBER` / `BARCHART` | `report`（时间报告）+ `chreport`（通道报告）两张专用表 | 押后（现有两张覆盖了主要用途） |
| 10 | 报文/元数据图 | `MESSAGE="message"` / `METADATA="metadata"` | CAN 帧表已能导入成场次 + `status` 组件 | 押后 |
| 11 | **光标 A–B 区间统计** | 端点 `/signals/cursor/values`、`/signals/cursor/stats`；前端 `valuesBetweenCursors`、"Loading cursor stats" | A–B 目前**只作为导出范围**；图上抬头是「可见区间 min/max/avg + 光标值 + Δ」 | **做（P2）** — 缺的是"在 A–B 这个范围上算一遍统计" |
| 12 | **绘图分辨率** | 设置项 `PLOT_RESOLUTION`、`SHOW_DATAPOINTS` | serve 模式按可见区间全分辨率重取；快照预先抽稀；A79 已有"曲线连接"（线性/阶梯） | 做（P4）——把散落的三处抽稀收成一个用户可见的旋钮 |
| 13 | 区段轴 | 横轴模式枚举 `{DISTANCE, SEGMENTS}`；端点 `/sources/segments` | 区段是**带子 + 报表分组**；横轴只有时间/距离/双圈 | 押后 |
| 14 | 导出任务化 | `exportJobs`；`DATA_EXPORT` / `PLOT_EXPORT` / `FIGURES_EXPORT` 三种模态 | 导出是同步的（已有预计行数 + 进度条 + 取消） | 押后 |
| 15 | 函数目录 | 前端 12 个：`calibration` `clamp` `cumsum` `difference` `discrete_derivative` `integral` `moving_aggregate` `outliers_fixed` `outliers_stddev` `sample_shift` `time` `zeroing`；**参数带类型**（signal / enum / number），表单按类型渲染 | `maths.py` **53 个函数** + 插入通道下拉 + 本地/全局作用域 | 已超越（只借鉴"参数类型驱动表单"这一条表述） |
| 16 | 通道别名与库筛选 | `FILTER_FUNCTION_ONLY_MINE` / `FILTER_*_TAGS`、`/tags`、`SETTING_LIBRARY_COLUMNS` | 通道别名（有序候选）、工作表 `pick` 挑通道 | 已超越（服务端库/标签体系不做） |
| 17 | 配色方案 | `SETTING_OVERLAY_PALETTE`（含 "Matplotlib (tab10)"） | 三套调色板 + 手选色 + ΔE/WCAG 可辨性实测 | 已覆盖 |
| 18 | 标注 | 端点 `/annotations/add`、`/annotations/remove`、`LOAD_ANNOTATIONS` | Notes 组件（琥珀色虚线 + 就地改名 + 侧车） | 已覆盖 |
| 19 | 仪表族 | `jr = {TABLE, NUMBER, BARCHART}`；组件子类型 | `gauge` 组件五种：数值 / 列表 / 条形 / 表盘 / 方向盘 | 已覆盖 |
| 20 | 通道处理 | `zeroing`（"Offset the signal to be 0 at the first datapoint inside zoom range"）等 | A75 通道处理：低通（含零相位）/ 移动平均 / 增益 / 偏置 / 光标归零 | 已覆盖 |

## 不做（除非单独翻案）

| 能力 | Marple 证据 | 为什么不搬 |
| --- | --- | --- |
| Workbook / Dashboard / Report 服务端库 | `/library/workbook/*`、`/dashboard`、`/reporter/template` | 与 `docs/PLAN.md` "不做云端多人协作 / 账号体系"冲突。i3pro 的对应物是 `worksheets/*.json` + 快照 + 分享链接 |
| 外部数据连接器 | `ADX` `MIREO` `POSTGRES` `TIMESCALE` `INFLUXDB1_8/2/3` `MARPLEDB_V2` `CLICKHOUSE` `TRINO` | 同上；且会把"装四个包就能跑"变成"得先有个库" |
| AI 出图 / 对话 | `/ai/responses`、`/conversations`、`GET_AI_PLOT` | 同上 |
| 沙箱与工作区账号 | `/sandbox/*`、`/workspace/accounts/join`、`/workspace/tokens` | 同上 |
| 在线地图瓦片 | `api.mapbox.com`、`/library/map_background/download` | i3pro 的第一约束是离网可用（试车现场没网） |

## 本轮候选（P 排序）

1. **P1 姿态图 + PFD**：数据已经在场次里（MTi 三轴姿态），缺的只是画法。
   这是唯一一条"**数据齐了、只差视图**"的能力，性价比最高，也是 i2 Pro 没有的差异点。
2. **P2 光标 A–B 区间统计**：把现有的"可见区间 min/max/avg"抬成"任意 A–B 区间"的一组量
   （min/max/avg/σ/斜率/积分/耗时），顺带补上你反馈过的"空格的时间轴 delta 好像没用"。
3. **P3 热力图**：复用现有的散点着色与直方图取样管线，做 RPM×MAP / 速度×横向加速度这类图。
4. **P4 绘图分辨率旋钮**：把"快照抽稀 / serve 全分辨率 / 曲线连接"收成一个用户可见的设置。

每一条落到 issue 时必须带**可跑的验收命令或截图判据**；落不下的说明它还是需求而不是计划。
功能票的回归沿用四道，其中 `tools/verify_ld_vs_csv.py` **不可谈判**。

## 运行时证据（还没做）

上表的证据全部来自**静态包**。运行时（WebSocket 消息形状、真实 API 响应字段、
姿态图的实际渲染）要靠 CDP 抓一轮，步骤见 [marple-runtime-capture.md](marple-runtime-capture.md)。
没有这一轮，`signal / segment / figure` 的**字段名**仍然是推断而不是实测。
