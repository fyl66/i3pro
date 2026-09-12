# i3pro 规划书

> 保留 MoTeC C125 硬件，用开源工具链补上 i2 Pro 缺的那一半。
> 本文是**决策记录 + 里程碑 + 验收标准**，`docs/ACCEPTANCE.md` 是可执行的验收清单。

---

## 1. 一句话定位

把「插线拔线拷 `.ld` → 各自开 i2 Pro → 截图传群」这套流程，换成
**一个 `.bat` 启动、浏览器打开、能按赛道距离重叠两圈、链接直接发群里**的本地工作台。

不做的事（本版明确排除）：

| 排除项 | 原因 |
| --- | --- |
| 实时遥测 / 边缘记录仪 / MQTT / Grafana | 车上现在没有这套硬件，先做等于凭空多半个项目 |
| CAN 原始帧回溯、MF4/BLF、AiM 解析 | 数据源不存在或不在痛点上，留到 v2 |
| 写回 `.ld` 给 i2 Pro | 要完整复刻私有二进制并对齐校验字段，风险高、受益面窄 |
| 云端多人协作、账号体系 | 依赖赛场网络；局域网共享 + 分享链接先覆盖 90% 场景 |
| 通用表达式 / Lua 引擎 | 成本在编辑器与缓存失效，不在求值；v1.5 再做 |

---

## 2. 事实基础（这些不是假设，是量出来的）

| 事实 | 数值 | 影响 |
| --- | --- | --- |
| 真实数据 | 8 个 C125 `.ld`，36.6–118.8 MB | 解析器必须 mmap，不能整读进内存 |
| 通道规模 | 342–437 通道，100 Hz 主采样率 | 必须搜索 + 分组，不能平铺下拉框 |
| MoTeC `Distance` 通道 | **恒为 0**（beacon 未接线） | 距离轴必须自己积分速度算出来 |
| `.ldx` 圈信息 | 全部 `Total Laps = 1` | 切圈不能读文件，必须自己检测 |
| GPS 可用性 | 部分场次 `GPS Speed/Sats/Heading` 全 0，但经纬度有效 | 切圈要有兜底，不能只信 GPS 速度 |
| 速度源 | `Vx KF`（100 Hz 卡尔曼滤波纵向速度）在全部日志中可用 | 距离积分的首选通道 |
| 运行环境 | Windows + Python 3.13（已有 numpy/pandas/pyarrow）；**没有 Rust** | 不能用 i3rs 自编译；参数栈必须纯 Python |
| 网络 | 沙箱/赛场都可能没有外网 | 运行时零依赖、零 CDN、零 npm |

---

## 3. 架构

```
i2pro_data/*.ld ──► i3pro.ld (mmap 原生解析) ──┬──► i3pro.store ──► Parquet + meta.json ──► SQL / 列式裁剪
                                              │
                                              ├──► i3pro.derive ──► 速度 / 距离 / GPS 轨迹
                                              ├──► i3pro.laps   ──► GPS 起终点门切圈 + 距离轴重叠 + Δ时间
                                              └──► i3pro.render ──► 自包含 HTML 工作台
                                                                 └──► i3pro.server (stdlib HTTP) ──► 局域网 + 可分享 URL
```

技术选型的理由，逐条对应第 2 节的事实：

* **纯 Python 标准库 + numpy/pandas/pyarrow**，不引入 FastAPI/Flask/DuckDB：队里已有这几个包，
  运行时零安装、零运维，明年换人只要会 Python 就能改。
* **算法层全是纯函数模块**（`derive` / `laps` / `render.downsample`），不绑框架、有单测，
  这是「人毕业了项目不烂尾」的唯一保障。
* **前端是手写 Canvas + 原生 ES 模块**，不引入 React/uPlot：免 npm、免构建、
  单个 HTML 文件可以邮件发给队友，双击就开。
* **服务端用 `http.server`**：只为「多人同时看 + 链接可分享」存在，不需要进程管理。

---

## 4. 里程碑与交付物

### M0 — 数据底座（已完成）

| 交付物 | 位置 |
| --- | --- |
| 原生 `.ld` 解析器（mmap、含单位/小数位/采样率元数据） | `src/i3pro/ld.py` |
| 格式逆向记录（可复现的字节偏移表） | `docs/ld-format.md` |
| 解析正确性对照工具 | `tools/verify_ld_vs_csv.py` |
| Parquet + 元数据落盘 / 列式裁剪 / SQL | `src/i3pro/store.py` |
| CLI：`info / channels / convert / query / series / export` | `src/i3pro/cli.py` |

### M1 — 分析能力（已完成）

| 交付物 | 位置 |
| --- | --- |
| 速度源选择 + 距离轴积分（含 GPS 兜底） | `src/i3pro/derive.py` |
| GPS 起终点门切圈、圈速表、异常段标记 | `src/i3pro/laps.py` |
| 距离轴双圈重叠 + Δ时间曲线 | `laps.overlay` / `laps.time_delta` |
| CLI：`laps / track / delta` | `src/i3pro/cli.py` |

### M2 — 工作台（已完成）

| 交付物 | 位置 |
| --- | --- |
| 多通道波形：同步光标 / Min-Max 降采样 | `src/i3pro/web/viewer.html` |
| 时间轴 / 距离轴 / 双圈对比三种模式 | 同上 |
| 全通道搜索与勾选（342–437 个） | 同上 |
| 圈速表点选基准圈 / 对比圈，即时重算对比 | 同上 |
| 赛道轨迹按速度着色 + 光标联动 | 同上 |
| 导出 PNG、复制可分享链接 | 同上 |
| 自包含 HTML 快照 / 本地服务两种分发 | `render.py` / `server.py` |

### M4 — i2 Pro 交互对齐（已完成）

需求来自实际使用反馈："双击放大具体时间段没有实现"、"没有散点显示"。
对照 MoTeC i2 Pro 帮助文件的 `Components` / `Keyboard Shortcuts` 两章逐条实现：

| 交付物 | i2 Pro 对应功能 | 位置 |
| --- | --- | --- |
| 双击拖拽框选缩放（横向 / `Alt` 纵向 / `Ctrl` 框选） | `Double-click, move, click` | `viewer.html` |
| 双击放大、`F2` 全出、`W` 默认一圈、`Z` 缩到光标、`H` 居中、`F`/`B` 翻页 | Zoom / Pan 一节 | 同上 |
| 横向滚动条 + 可拖拽全程概览条 | Outing Graph | 同上 |
| 基准（Datum）光标 + `Δ` 时间/数值 | Datum Cursor | 同上 |
| 可见区间 min/max/avg 测量 | Measurements（`M`） | 同上 |
| 光标处全通道数值面板 | Values 窗口（`V`） | 同上 |
| 点样式 / 线样式（`S`） | Trace Style | 同上 |
| 按同单位分组的共享纵轴，分栏 / 重叠（`G`） | Channel Groups / Overlapped | `render.groups` + 同上 |
| 状态与故障带（`E`） | Status and Errors | 同上 |
| 散点组件：X×Y、按第三通道着色、跟随缩放区间、光标联动、趋势线 | Scatter Plot | `render.points` + `server.py` `/points` |
| 缩放即重新按可见区间取全分辨率数据 | （i2 本地全量数据） | `server.py` `/trace?from=&to=` |

### M5 — 一键启动与离线分发（已完成）

问题来自实际使用："双击 `src\i3pro\web\viewer.html` 打开没有数据"。
根因是那是**模板**而不是数据页——数据由 i3pro 在生成时注入。除了把它做得更不容易误解，
还补上两条真正"双击就能用"的路径：

| 交付物 | 干什么 |
| --- | --- |
| `启动.bat` | 起本地服务 + 自动开浏览器；打印本机与**局域网地址**（第一项是默认路由所在网卡，虚拟网卡单独标注）；端口占用自动往后找；出错不关窗 |
| `导出快照.bat` + `i3pro snapshot` | 批量把每个场次导出成自包含 HTML + `out\index.html` 索引；发给没装 Python 的队友也能双击打开 |
| `i3pro.cmd` 自动探测 Python | 依次试 `python` / `py -3` / `python3` / `%LOCALAPPDATA%\Programs\Python\Python3*`，用**真的跑一次 i3pro** 来判定可用（本机 `py` 启动器存在但未注册解释器，必须跳过） |
| 模板自我说明 | 直接打开 `viewer.html` 不再抛异常/白屏，而是显示中文指引 |
| 静态快照默认 2000 桶 | 快照放大不再那么快变成折线（文件增大约 60%，仍 < 2 MB） |

### M3 — 工程化（已完成）

21 项单测（真实数据回归 + HTTP 端到端 + 无头 JS 冒烟）、零第三方运行期依赖、git 仓库。

---

## 5. 验收标准

可执行清单见 `docs/ACCEPTANCE.md`。判据摘要：

| # | 判据 | 门限 |
| --- | --- | --- |
| A1 | 解析正确性 | 与 MoTeC 自己导出的 CSV 逐样本比对，**全部通道落在显示精度内** |
| A2 | 独立实现交叉验证 | 与 `gotzl/ldparser`（另一套逆向实现）逐通道数值一致 |
| A3 | 大文件性能 | 118.8 MB 场次：打开 < 1 s，转 Parquet < 5 s |
| A4 | 通道完整性 | 8 个日志全部解析成功，通道数与 CSV 导出量级一致 |
| A5 | 切圈正确性 | `高避5圈` 切出 **5 个完整圈**（文件名即基准）；`耐久正赛` ≥ 20 个完整圈 |
| A6 | 距离轴对齐 | 两圈按 1 m 步长插值，Δ 曲线终点与圈速差一致（< 0.6 s） |
| A7 | 渲染流畅度 | 437 通道日志生成工作台 < 1 s；单通道 19 万点抽稀 7 ms |
| A8 | 非技术可用性 | 双击 `.bat` → 浏览器 → 完成一次双圈对比，无需看文档 |

> A1/A2/A3/A5 是**硬门限**：任何一条不达标，本版就不能交给底盘组用。

---

## 6. 主要风险与对策

| 风险 | 概率/影响 | 对策 | 现状 |
| --- | --- | --- | --- |
| 解析值与 i2 Pro 对不上，底盘组不认 | 中/高 | CSV 逐通道对照 + 第二套实现交叉验证 | 已闭环（A1/A2） |
| beacon 未接线导致切圈失败 | 高/中 | GPS 起终点门 + 航向判据 + 异常段标记 + 速度兜底 | 已闭环（A5） |
| 部分场次 GPS 完全失效 | 中/中 | 距离积分回落 `Vx KF`；无速度通道时明确报错而不是给出错数据 | 已覆盖 |
| 直道/单圈数据切不出圈 | 高/低 | UI 接受 0 圈（显示提示），不崩 | 已覆盖（3 个直线场次 0 圈） |
| 人员流动导致烂尾 | 高/高 | 纯函数算法层 + 21 项单测 + 中文文档 + 零依赖 | 已闭环 |
| 赛场没网 | 中/中 | 运行期零安装、单文件 HTML | 已闭环 |

---

## 7. 后续路线

**v1.5（按真实反馈排序，不急）**

1. **Track Editor / 区段定义** —— 有了区段才能做「双击区段名放大到该弯/直道」（i2 Pro 的
   `To Zoom to a Range: double-click on the range band`）、区段报表、Eclectic 理论最快圈
2. 手动打点切圈（GPS 失效时兜底）
3. Histogram / Suspension Histogram / FFT（i2 Pro 的其余组件）
4. Channel Report / Time Report（表格化统计 + Eclectic）
5. 通用数学通道（白名单表达式 + 结果缓存）—— 有了它才能给直方图/散点配 gating 通道
6. Gauges（表盘 / 条形 / 方向盘 / 状态灯）+ 动画播放
7. 三电极值报表（电机温度 / 母线功率 / 能耗）

**v2（需要新硬件或新数据源才做）**

1. 边缘 CAN 记录仪 + DBC 解码 → 与 `.ld` 同 schema 入库
2. 写回 `.ld` 供 i2 Pro 打开
3. 实时遥测看板（Grafana，不自研）

**已明确不做**（第一轮 Q4 确认）：视频组件、Alarms 告警、外部数学插件（VB.net）、
Setup Sheets（依赖 Excel）、Matlab 导出、Mixture Map / 发动机调校组件、
Drag（直线加速）项目模式、多 Workbook 工程体系。

---

## 8. 待确认事项

1. **仓库托管**：代码已推到 **https://github.com/fyl66/i3pro**（private）。
   仓库名如果不对（比如你想放进队里的组织仓库），改动很便宜：
   `git remote set-url origin <新地址> && git push -u origin main`。
   ⚠️ 本机 git 直连 github.com 会超时，必须走本机代理：
   `git config --global http.proxy http://127.0.0.1:7890`（Clash 默认端口，按你自己的改）。
2. **许可证**：`vendor/ldparser` 是 GPL-3.0（格式逆向的来源，仅用于测试交叉验证）。
   当前仓库按 GPL-3.0 处理。若希望改成 MIT 发布，需要移除 `vendor/` 并重写 `docs/ld-format.md`
   中引用该实现的部分。
3. **队内数据目录约定**：目前默认读 `i2pro_data/`，如需扫描队内共享盘可以随时加 `--data`。
