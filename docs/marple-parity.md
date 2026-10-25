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
| 运行时采集 | 2026-10-17 18:39–18:42，Edge 155.0.4283.45，CDP `127.0.0.1:9222`，页面 `/visualisation`（已登录 Aurora workspace） |
| 运行时 `evidence_id` | 页面 `ev_1a9890e96f045901b1eab5caab3bf0215fc31e5dd08ac2c7d0fc89607c3a9602`、无障碍 `ev_c03064287be6cf8b8c7ed58153ea0a4fa5c5cc85469b5152f22e1b4dc40eec07`、场景 `ev_d9cc36ef5d6b27761b4f347ee748ba8a5aa1f916aaf75d6371480ae77c531197` |
| 运行时（光标/取数） | 2026-10-17 18:51，同一页面摆好 A/B 光标后做 60 秒观察，`evidence_id ev_32ba7a57c38faaa6b08764d364f1cf202492388e014b686ec08931776591d94b` |

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
| 6 | **姿态图** | `ATTITUDE="attitude"`（图标 `3d_rotation`，显示名 `Attitude`）；`FigureAttitude-*.js`：`rollSignal` / `pitchSignal` / `yawSignal` + `angleUnits` + **每通道 multiplier** + `eulerOrder` + `customModels` + 相机复位事件；**光标 A 处叠一个 30% 透明模型**与实时姿态同屏对比。**许可证门控（2026-10-17 实测）**：`Add Plot` 的三组是 `Explore(BASE) / Aggregate(BASE) / Aerospace(AEROSPACE)`，最后按 `license.features` 过滤，`Attitude` 与 `PFD` 只在 Aerospace 组；本 workspace 的 license `features = [DATAMINER, OVERVIEW, FUNCTIONS, REPORTING_V2, MOTORSPORT]` **不含 AEROSPACE**，所以菜单里根本看不到——运行时观察这一步在你们账号上做不到，P1 的证据只能用静态包 | 无（但 `dbc/261004` 已解出 Xsens MTi 的姿态/角速度通道，数据已经在场次里） | **做（P1）** |
| 7 | **主飞行显示** | `PFD="pfd"`（图标就是 `pfd`，显示名 `PFD`），与 `Attitude` 同属 **Aerospace** 组，同样被 license 挡住（见第 6 行） | 无 | **做（P1，与姿态图同一票或紧随）** |
| 8 | 热力图 | `HEATMAP="heatmap"` | **已做完**（ticket [#45](https://github.com/fyl66/i3pro/issues/45)，验收 [A90](ACCEPTANCE.md)）：组件 `heatmap`，两个通道分箱 + 第三通道/样本数着色；**只在 serve 模式算**（快照里不内嵌，见 A90 的边界） | 做（P3） |
| 9 | 通用表格 | `TABULAR="tabular"`；渲染三选一 `TABLE` / `NUMBER` / `BARCHART` | `report`（时间报告）+ `chreport`（通道报告）两张专用表 | 押后（现有两张覆盖了主要用途） |
| 10 | 报文/元数据图 | `MESSAGE="message"` / `METADATA="metadata"` | CAN 帧表已能导入成场次 + `status` 组件 | 押后 |
| 11 | **光标 A–B 区间统计** | 端点 `POST /api/v1/signals/cursor/stats`；**实测响应只有三个数** `{request_time, message:[{uuid, stats:{min, mean, max}}]}`（请求带 `signals[].cursors:[t,t]` 与通道元数据）——并不比"可见区间统计"多做多少 | **已做完**（ticket [#44](https://github.com/fyl66/i3pro/issues/44)，验收 [A89](ACCEPTANCE.md)）：组件栏「显示 → 统计范围」在**可见区间 / 光标 A–B** 之间切，左闭右闭、反序自动交换、空区间印 `--` | 做（P2，比原设想小）；σ / 斜率 / 积分属于**超出 Marple** 的自选增量，**不做**（规格 §1 非目标） |
| 12 | **绘图分辨率（兼"曲线为什么丝滑"）** | `PLOT_RESOLUTION`（默认 `2`）与 `SHOW_DATAPOINTS`（默认 `true`）；档位枚举 `["Min","Low","Medium","High","Max"]`，取值 **`500 × 2^档位`** → 500 / 1000 / **2000（默认）** / 4000 / 8000 点；`subsampled = 估计采样率 × 窗口秒数 > 2 × resolution`。**取数端点 `POST /api/v1/signals/t_base` 实测为分桶包络**：`message[].{uuid, status, subsampled, bucket_count[], low_bound[], up_bound[], time[], x_base[], x_base_name, z_signal[]}`——每桶给下界与上界；`x_base_name` 说明横轴可以是命名基准（距离轴）；`z_signal` 是第三通道（着色用） | serve 模式按可见区间重取全分辨率；快照预先抽稀；A79 已有"曲线连接"（线性/阶梯保持） | 做（P4）：收成一个**五档旋钮**，并照抄**按桶取 min/max 包络**的思路——它"看起来丝滑"靠的是包络，不是插值 |
| 13 | 通道清单与元数据 | `POST /api/v1/sources/signals`：请求 `{dataset_filter:{dataset,stream,layer_shift}, signal_filters:[{name}]}` → 响应 `{message:{signal_list:[{count, datatype, display_name, frequency, metadata:{id, unit, min, max, mean, sum, storage_status, description, count_pre, count_value, count_text}}]}}` | `channels` 命令 + 侧边栏通道列表（单位 / 采样率 / 小数位 / min / max） | 已覆盖；只有 `storage_status` 可作参考 |
| 14 | 区段轴 | 横轴模式枚举 `{DISTANCE, SEGMENTS}`；端点 `/sources/segments` | 区段是**带子 + 报表分组**；横轴只有时间/距离/双圈 | 押后 |
| 15 | 导出任务化 | `exportJobs`；`DATA_EXPORT` / `PLOT_EXPORT` / `FIGURES_EXPORT` 三种模态 | 导出是同步的（已有预计行数 + 进度条 + 取消） | 押后 |
| 16 | 函数目录 | 前端 12 个：`calibration` `clamp` `cumsum` `difference` `discrete_derivative` `integral` `moving_aggregate` `outliers_fixed` `outliers_stddev` `sample_shift` `time` `zeroing`；**参数带类型**（signal / enum / number），表单按类型渲染 | `maths.py` **56 个函数** + 插入通道下拉 + 本地/全局作用域 | 已超越（只借鉴"参数类型驱动表单"这一条表述） |
| 17 | 通道别名与库筛选 | `FILTER_FUNCTION_ONLY_MINE` / `FILTER_*_TAGS`、`/tags`、`SETTING_LIBRARY_COLUMNS` | 通道别名（有序候选）、工作表 `pick` 挑通道 | 已超越（服务端库/标签体系不做） |
| 18 | 配色方案 | `SETTING_OVERLAY_PALETTE`（含 "Matplotlib (tab10)"） | 三套调色板 + 手选色 + ΔE/WCAG 可辨性实测 | 已覆盖 |
| 19 | 标注 | 端点 `/annotations/add`、`/annotations/remove`、`LOAD_ANNOTATIONS` | Notes 组件（琥珀色虚线 + 就地改名 + 侧车） | 已覆盖 |
| 20 | 仪表族 | `jr = {TABLE, NUMBER, BARCHART}`；组件子类型 | `gauge` 组件五种：数值 / 列表 / 条形 / 表盘 / 方向盘 | 已覆盖 |
| 21 | 通道处理 | `zeroing`（"Offset the signal to be 0 at the first datapoint inside zoom range"）等 | A75 通道处理：低通（含零相位）/ 移动平均 / 增益 / 偏置 / 光标归零 | 已覆盖 |

## 不做（除非单独翻案）

| 能力 | Marple 证据 | 为什么不搬 |
| --- | --- | --- |
| Workbook / Dashboard / Report 服务端库 | `/library/workbook/*`、`/dashboard`、`/reporter/template` | 与 `docs/PLAN.md` "不做云端多人协作 / 账号体系"冲突。i3pro 的对应物是 `worksheets/*.json` + 快照 + 分享链接 |
| 外部数据连接器 | `ADX` `MIREO` `POSTGRES` `TIMESCALE` `INFLUXDB1_8/2/3` `MARPLEDB_V2` `CLICKHOUSE` `TRINO` | 同上；且会把"装四个包就能跑"变成"得先有个库" |
| AI 出图 / 对话 | `/ai/responses`、`/conversations`、`GET_AI_PLOT` | 同上 |
| 沙箱与工作区账号 | `/sandbox/*`、`/workspace/accounts/join`、`/workspace/tokens` | 同上 |
| 在线地图瓦片 | `api.mapbox.com`、`/library/map_background/download` | i3pro 的第一约束是离网可用（试车现场没网） |

## 本轮候选（P 排序）

1. **P1 姿态图 + PFD**：数据已经在场次里（Xsens MTi 与 Aceinna 的 roll/pitch/yaw），缺的只是画法。
   这是"**数据齐了、只差视图**"的能力，也是 i2 Pro 没有的差异点；也是唯一还需要补一次运行时观察的
   （要点到那一类图才知道它长什么样）。
2. **P4 绘图分辨率 + 分桶包络**：机制已经量清（五档 `500 × 2^档位`、`subsampled` 判据、
   `t_base` 的 `low_bound/up_bound/bucket_count`）。它顺带回答"为什么 i2 Pro 看起来更丝滑"：
   不是插值，是**按桶取 min/max 包络**。可以直接开票。
3. **P2 光标 A–B 区间统计**：实测 Marple 只有 `min/mean/max`，对齐成本很小
   （把现有三个量的范围从"可见区间"换成 A–B）；σ / 斜率 / 积分是**自选增量**，不属于对齐。
4. **P3 热力图**：复用现有的散点着色与直方图取样管线，做 RPM×MAP / 速度×横向加速度这类图。

每一条落到 issue 时必须带**可跑的验收命令或截图判据**；落不下的说明它还是需求而不是计划。
功能票的回归沿用四道，其中 `tools/verify_ld_vs_csv.py` **不可谈判**。

## 运行时证据（2026-10-17 采了一轮）

采集流程见 [marple-runtime-capture.md](marple-runtime-capture.md)，原始证据在
`out/_marple/runtime/`（gitignored，逐文件 sha 见那里的 README）。**登录态**下实测到：

- **启动序列**（进 `/visualisation` 必跑，三次加载完全一致）：
  `GET /api/v1/version` → Auth0 `/authorize`(web_message, `prompt=none`) + `/oauth/token` →
  `GET /api/v1/auth/config` → `/user/info` → `/workspace/info` → `/workspace/license?refresh_cache=true` →
  `/connection` → `POST /sources/search` → `/library/workbook/all` → `/reporter/template/all` →
  `/dashboard/all` → `/sandbox/all`，外加 `POST /api/v1/log` 上报。**没有 WebSocket**——
  "实时"靠轮询与请求，不是推送。
- **数据集模型**（`POST /sources/search` 的响应，这是最值钱的一条）：
  `{request_time, message:[{display_name, dataset_filter:{dataset, stream, layer_shift}, metadata[], timestamp_start, timestamp_stop, realtime}]}`；
  `metadata[].key` 实测出现 `dataset / stream / layer_shift / created_at / datapoints / nsignals / datapool / stream_name`。
  也就是它把一份记录拆成 **dataset → stream → layer（按位偏移寻址）**，通道数记在 `nsignals`。
- **顶层导航**（无障碍树实测）：`Home / Analysis(/visualisation) / Overview / Reports / Functions / Database`，
  外加 workspace 切换（菜单项实测文案 `Workspace AURORA Formula Student Team`）、Settings、Help。
- **分析页控件**：`Add Plot`、`refresh`、`download`、`image`、`save`、`save_as`、`share`、`layers`、
  `merge`、`Organise datasets`、`Signals`（sort / filter / settings）、`Datasets`、工作表分页 `Tab 1 + add`。
- **本地存储键**：`user-settings`、`workspace-settings:<workspace uuid>`、`loglevel`，
  以及第三方统计（`__tea_*`）与客服（`gleap-widget-*`）。静态包里
  `workspace-settings:${id}` 这条推断得到了字面确认。
- **第三方**：Auth0（登录）、Gleap（客服）、`gator.volces.com`（统计）、`rsms.me/inter`（字体）。
- **摆好 A/B 光标后的 60 秒观察**（用户操作，18:51）又抓到三条：`POST /signals/cursor/stats`
  （响应只有 `min/mean/max`）、`POST /signals/t_base`（**分桶 min/max 包络**）+ `x_base_name` + `z_signal`、
  `POST /sources/signals`（按 `dataset_filter` 列通道，附每条通道的 min/max/mean/sum/frequency/storage_status）。

**没有"还差一项"了**：姿态图/PFD 在你们账号上**看不到**（license 不含 `AEROSPACE`，见对照表第 6 行），
所以它的证据就停在静态 chunk 那一层——而那一层已经给出 `roll/pitch/yaw` 三通道 + 单位 + 每通道倍数 +
欧拉序 + 自定义模型 + 光标处 30% 幽灵模型这些关键行为，够写 spec 了。
另外顺带量到 license 本身：`type=SPONSORSHIP`、`features=[DATAMINER, OVERVIEW, FUNCTIONS, REPORTING_V2, MOTORSPORT]`、
`editors 10`、`storage -1`；`DATAMINER` 这条管着 Aggregate 组（Table/Number/Bar/Heatmap），你们有。
P1 / P2 / P3 / P4 四条的证据现在都齐了，可以整体进 `/to-spec`。
