# 验收清单

每条都能直接复制粘贴执行。`$` 开头的行是命令，下面一行是**通过判据**。
所有命令在仓库根目录、用系统 Python（3.13，已装 numpy/pandas/pyarrow）执行。

```powershell
cd E:\桌面\i3pro
```

---

## A1 · 解析正确性（硬门限）

`.ld` 解析结果必须与 MoTeC 自己导出的 CSV 逐样本一致。CSV 是 i2 Pro 写的，
所以这是拿官方实现当裁判，不是自说自话。

```powershell
python tools/verify_ld_vs_csv.py
```

**通过判据**：末行 `PASS - 0 channel(s) outside tolerance`。
每个场次打印 `channels compared` = `within MoTeC display precision`。

**当前结果**：

| 场次 | 比对通道 | 精度内 | 最大偏差 |
| --- | --- | --- | --- |
| 20260522-yjw第二次直线3.72 | 213 | 213 | 0.005（`AccelX`，半个显示位） |
| 20260524-耐久正赛 | 213 | 213 | 0.4（`Aceinna GyroX`，半个显示位） |

> 慢通道（10/20/50 Hz）不参与比对：CSV 导出时 MoTeC 会把它们重采样到 100 Hz，
> 索引不再对应。这是导出格式的差异，不是解析误差。

---

## A2 · 独立实现交叉验证（硬门限）

用**另一套**独立逆向出来的解析器（`vendor/ldparser`，gotzl/ldparser，GPL-3.0）
读同一个文件，逐通道比对通道名、单位、点数与数值。

```powershell
python -m unittest tests.test_i3pro.TestIndependentParsers -v
```

**通过判据**：2 项 `ok`，其中 `test_agrees_with_gotzl_ldparser` 要求通道数、
通道名、单位、采样点数完全一致，且抽样通道数值 `rtol=1e-9` 一致。

---

## A3 · 大文件性能（硬门限）

```powershell
Measure-Command { .\i3pro.cmd convert "i2pro_data\20260524-耐久正赛.ld" --out out }
```

**通过判据**：118.8 MB 的场次，**解析 + 写 Parquet < 5 s**（实测 1.3 s），
产物 `out\20260524-耐久正赛.parquet` **< 25 MB**（实测 19.0 MB，压掉 84%）。

```powershell
.\i3pro.cmd series out\20260524-耐久正赛.parquet --channels "Vx KF,MCU1 FR TempMotor" --from 400 --to 405
```

**通过判据**：2 秒时间窗内返回数据，命令整体（含 Python 启动）< 3 s。

---

## A4 · 通道完整性

```powershell
.\i3pro.cmd info i2pro_data\*.ld
```

**通过判据**：8 个 `.ld` 全部列出，无报错；通道数在 342–437 之间；
设备均为 `C125`；主采样率 100 Hz。

```powershell
.\i3pro.cmd channels "i2pro_data\20260908-cjh 高避5圈.ld" --filter temp
```

**通过判据**：列出电机/逆变器温度相关通道，带单位与采样率。

---

## A5 · 切圈正确性（硬门限）

车队的 C125 beacon 输入没有接线，`.ldx` 里 `Total Laps` 恒为 1，
所以圈次必须由平台自己从 GPS 轨迹切出来。

```powershell
.\i3pro.cmd track "i2pro_data\20260908-cjh 高避5圈.ld"
```

**通过判据**：**5 个完整圈**（文件名「高避5圈」就是独立的人为基准），
完整圈圈速全部落在 30–70 s、单圈里程 600–1000 m；首尾两个进出场段被标记为
`(进出场/泊车/异常段)`。

**当前结果**：7 段 / 5 完整圈，40.44 s – 51.18 s，806–816 m。

```powershell
.\i3pro.cmd track "i2pro_data\20260524-耐久正赛.ld"
```

**通过判据**：≥ 20 个完整圈，最快圈在 50–70 s。

**当前结果**：26 段 / 23 完整圈，最快 54.900 s，单圈约 810 m。

---

## A6 · 距离轴对齐

这是「不抛弃 i2 Pro 才值得自研」的核心功能：两圈按**赛道距离**而不是时间叠加。

```powershell
.\i3pro.cmd delta "i2pro_data\20260908-cjh 高避5圈.ld" --ref 2 --cmp 5
```

**通过判据**：输出中 `delta at ... m (common distance)` 与两圈圈速差之差的绝对值
< 0.6 s（对齐正确时二者必须收敛）。同时给出最大损失点与最大收益点。

自动化版本：

```powershell
python -m unittest tests.test_i3pro.TestLaps.test_overlay_and_delta -v
```

---

## A7 · 渲染与交互

```powershell
.\i3pro.cmd render "i2pro_data\20260908-cjh 高避5圈.ld" --out out\demo.html
node tools\smoke_viewer.js out\demo.html
$env:I3PRO_HASH="mode=overlay"; node tools\smoke_viewer.js out\demo.html; Remove-Item Env:I3PRO_HASH
```

**通过判据**：两条都 `PASS`，并且打印出 437 条通道行、7 条圈速行、
上千条线段与文字标签（说明波形真的画出来了，不是空 canvas）。

人工检查（可选）：双击 `out\demo.html`，确认

1. 滚轮缩放 / 拖动平移 / 鼠标移动时所有图与赛道图的光标同步；
2. 左上角搜索框输入 `temp` 能筛出电机温度通道，勾选后立即出图；
3. 切到「双圈对比」，蓝色基准圈与橙色对比圈按距离重叠，下方 Δ 图有红/绿填充；
4. 点圈速表换一圈，对比图即时重算；
5. 「导出 PNG」下载的图包含文件名、设备与当前模式。

---

## A8 · 非技术队员可用性

```powershell
.\i3pro.cmd serve --data i2pro_data --open
```

**通过判据**：浏览器自动打开场次列表；点任意场次进入工作台；
把地址栏链接发给同一局域网的另一台机器，对方能打开同一个视图。

> 局域网共享用 `--host 0.0.0.0`。数据不出本机、不上传，只是把页面交给对方。

---

## A9 · 缩放模型（对齐 i2 Pro）

i2 Pro 的缩放不是"滚轮放大"这么简单，它是一整套鼠标 + 键盘分工。照搬后必须逐条成立：

```powershell
.\i3pro.cmd render "i2pro_data\20260908-cjh 高避5圈.ld" --out out\demo.html
node tools\smoke_viewer.js out\demo.html
```

无头脚本会真的驱动这些交互并断言视图真的变了：

| 操作 | 期望 |
| --- | --- |
| `双击→拖拽→松开` | 横向框选放大到该时间区间 |
| `Alt` + 同上 | 纵向放大到框选值域 |
| `Ctrl` + 同上 | 同时放大 X 和 Y |
| 双击（不拖拽） | 以点击处为中心放大 2 倍 |
| 滚轮 | 以指针处为中心缩放 |
| `↑` `↓` | 横向放大 / 缩小 |
| `Alt`+`↑`/`↓` | 纵向放大 / 缩小 |
| `F2` | 横向全出 |
| `W` | 缩放到默认（一圈） |
| `Z` | 缩放到基准光标与主光标之间 |
| `H` | 以光标为中心 |
| `F` / `B` | 向前 / 向后翻页 |
| 拖拽图内或坐标轴 | 平移 |
| 双击横向滚动条 | 全出 |
| `Esc` | 取消框选 |

**通过判据**：`PASS - ... interactions verified`，且 15 项交互断言无失败。

**概览条**：主图下方有一条全程缩略图，窗口矩形可拖拽平移、可拖两端改宽度、双击全出。

---

## A10 · 光标与测量

| 操作 | 期望 |
| --- | --- |
| 鼠标移动 | 所有图 + 赛道图 + 散点同步高亮同一时刻 |
| `←` `→` | 光标步进 0.01 s；`Ctrl`+方向键 步进 1 s |
| `D` | 打开基准（Datum）光标 |
| `空格` | 在当前位置放置基准光标 |
| `X` | 交换主光标与基准光标 |

**通过判据**：左侧「光标值」面板在打开基准光标后，每行除了当前值还显示 `Δ`（两光标处数值之差），
表头显示 `Δ 时间`。右侧图例在每个通道名旁显示**当前可见区间的** min / max / avg（按 `M` 开关），
与 i2 Pro 的 Measurements 行为一致。

---

## A11 · 散点（Scatter）

i2 Pro 的散点有两个关键性质，缺一不可：**两个通道互相对照**、**只画当前缩放区间的数据**并与时间图共享光标。

```powershell
.\i3pro.cmd serve --data i2pro_data --open
```

打开任一场次后：

1. 右上角散点区有三个下拉框：`X` / `Y` / `色`；选 `G Force Lat` 与 `G Force Long` 得到 G-G 图。
2. 在波形图上框选一段时间 → 散点只显示该区间的点。
3. 移动鼠标 → 散点上对应时间的那一点被白色圆圈高亮。
4. 双击散点 → 在「散点」和「散点+趋势线」之间切换。

**通过判据**：`/api/session/<场次>/points?channels=...&from=...&to=...` 返回**原始样本**
（不经过 min/max 抽稀，`stride=1` 时点数 = 窗口内样本数），10 s 窗口 < 10 ms。

> 快照（`render` 出的单个 HTML）不内嵌散点数据，散点区会提示"散点需要 serve 模式"。
> 这是刻意的：散点必须用全分辨率数据，否则形状是假的。

---

## A12 · 通道分组

i2 Pro 原话：*"The channels are arranged in groups which should normally have the same units as
they will share axis values."*

**通过判据**：

```powershell
python -m unittest tests.test_i3pro.TestChannelGroups -v
```

两条都 `ok`：每个通道恰好落在一个分组里（不重不漏），且同一分组内所有通道单位一致；
速度组排在最前；状态位通道被识别为状态通道而不是普通测量通道。

界面上：通道列表按单位分组显示，每组标题右侧有「全选」；按 `G` 在「分栏」与「重叠」之间切换。

---

## A13 · 状态与故障带

**通过判据**：在通道列表底部「状态与故障」分组里勾选若干通道，按 `E` 打开状态带——
每个通道一行，值非零处画成彩色段（i2 Pro 的 Status and Errors 面板）。
`E` 在没有选中任何状态通道时只给出提示，不会一次性塞进几十条通道。

---

## A14 · 交互式缩放（serve 模式）

快照模式的数据是预先抽稀好的，放大不会增加细节；`serve` 模式每次缩放都按可见区间重新取样，
所以在任意缩放级别都是全分辨率。

```powershell
.\i3pro.cmd serve --data i2pro_data --open
```

**通过判据**：

| 请求 | 期望 |
| --- | --- |
| `trace?channels=Vx KF&buckets=1200`（全程） | 秒级返回，约 1200 桶 |
| `trace?channels=Vx KF&from=200&to=201`（1 秒） | **返回 100 个原始样本**，不是折线 |
| 连续缩放 / 平移 | 无明显卡顿（每个请求 < 30 ms） |

---

## 全量回归

```powershell
python -m unittest discover -s tests -v
```

**通过判据**：`Ran 25 tests` + `OK`（无数据文件时相关用例自动 skip，不算失败）。

测试覆盖：

| 分组 | 内容 |
| --- | --- |
| `TestHeader` | 元数据、通道链表自洽性、数据块连续性 |
| `TestScaling` | 缩放公式、负小数位（×10）、与 CSV 导出对照 |
| `TestDerived` | 距离单调性、GPS 轨迹尺度合理 |
| `TestLaps` | 两种赛道的切圈、距离轴重叠、Δ 收敛 |
| `TestStore` | Parquet 往返、列式裁剪、SQL 查询 |
| `TestCsvReader` | i2 Pro CSV 导出结构解析 |
| `TestChannelGroups` | 通道按单位分组：不重不漏、单位一致、状态通道识别 |
| `TestPoints` | 散点原始样本、时间窗裁剪、超窗口自动 stride |
| `TestRender` | 静态/服务两种 payload、自包含性 |
| `TestServer` | HTTP 端到端：场次列表、工作台页、通道、时间窗、散点、概览、对比圈、赛道、404 |
| `TestIndependentParsers` | 第二套实现交叉验证、213 通道 CSV 全量对照 |
| `TestViewerScript` | 无头驱动前端：15 项交互断言 + 时间轴 / 双圈两条渲染路径 |
