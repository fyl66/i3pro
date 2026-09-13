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

## A15 · 一键启动（双击就能用）

**通过判据**：在项目根目录**双击 `启动.bat`**，出现控制台窗口，内容依次是

```
 i3pro  -  MoTeC .ld data workbench
   data folder : E:\桌面\i3pro\i2pro_data
i3pro 本地服务已启动: http://127.0.0.1:8731/
  发给队友(局域网): http://10.161.26.88:8731/
  ⚠ 第一次运行 Windows 防火墙可能弹窗，选“允许访问”。
  8 个场次, 数据目录: E:\桌面\i3pro\i2pro_data
```

并且浏览器自动打开场次列表。要求：

1. **不需要装任何依赖、不需要联网**；Python 会自动探测（`python` / `py -3` / `python3` /
   `%LOCALAPPDATA%\Programs\Python\Python3*`），本机 `py` 启动器存在但没注册解释器时能正确跳过。
2. 端口被占用时**自动换端口**，不报绑定错误（`TestLaunchers.test_bind_moves_to_the_next_free_port`）。
3. 出错时窗口不关闭（`pause`），并列出最可能的原因。

### 不要双击模板文件

```powershell
node tools\smoke_viewer.js src\i3pro\web\viewer.html --expect-template
```

**通过判据**：`PASS - template without data shows instructions instead of throwing`。
即：直接打开模板**不报错**，而是显示一段中文说明，告诉用户去点 `启动.bat` 或 `导出快照.bat`。

---

## A16 · 离线快照

**通过判据**：双击 `导出快照.bat`（或 `i3pro.cmd snapshot --data i2pro_data --out out`），
控制台逐个打印进度，最后给出：

```
生成 8/8 个快照 -> out
双击这个文件开始看: out\index.html
```

* `out\` 下每个场次一个自包含 HTML（0.5–2 MB），**不需要 Python、不需要服务器、不需要联网**。
* `out\index.html` 列出所有场次与关键数据（设备/日期/时长/通道数/完整圈/最快圈）。
* 每个快照都要能通过无头驱动：

```powershell
Get-ChildItem out -Filter *.html | Where-Object { $_.Name -ne 'index.html' } |
  ForEach-Object { node tools\smoke_viewer.js $_.FullName }
```

**通过判据**：每一个都 `PASS`（含 0 圈次的场次：那时圈速面板会隐藏，不算失败）。

---

## A17 · 工作表与组件（i2 Pro 的 Worksheet → Component）

i2 Pro 的工作表可以自由组合组件。i3pro 用同一套思路：一个页面就是一张工作表，
组件按顺序纵向排列，半宽组件两两并排。**必须能被无头脚本验证**，判据在
`tools/smoke_viewer.js` 的第 14–16 组断言里：

| 操作 | 期望 |
| --- | --- |
| 初始工作表 | 有组件；默认预设是「分析」 |
| 「＋ 添加组件」 | 组件数 +1，新组件落在末尾 |
| 组件工具条 `↑` / `↓` | 顺序真的改变了 |
| 组件工具条 `✕` | 组件数 −1 |
| 预设按钮 | 切到「动力」后预设名变了，且它里面那个图确实拿到了通道 |
| `E` | 增加/移除一个「状态与故障」组件（而不是往选择里塞几十个通道） |
| 拖组件右下角 | 改高度，且会持久化 |
| 分享链接 | 工作表编码进 URL 后能**原样还原**（类型与顺序都不变） |

**通过判据**：`node tools\smoke_viewer.js out\<场次>.html` 打印
`PASS - ... interactions verified`，且 16 组断言无失败。

**持久化顺序**：URL 里的 `layout=` > 本机 `localStorage` > 内置预设。
所以"发给队友的链接"永远显示你当时的工作表，而不是他上次保存的。

**内置预设**：分析 / 双圈对比 / 动力 / 底盘 / 车手。每个预设自带通道挑选取样
（例如「动力」抓 AMK 扭矩、母线电压、电机温度）。

---

## A18 · 图表抬头（i2 Pro 的 Measurements 列）

i2 Pro 的图表抬头是四列：`通道名 | 光标处数值 | Min | Max | Avg`，光标值随光标实时跳动，
所以不用低头看左栏。

**通过判据**：无头脚本在把光标放到窗口中点后断言

1. 存在 `.graphhead` 元素；
2. 抬头里有每通道一行的 `.lrow`；
3. 至少一行的**光标值**是数字（不是 `--`）；
4. 至少一行的 **min/max/avg** 是数字。

```powershell
node tools\smoke_viewer.js out\demo-hiav.html
```

配套改动：左侧「光标值」面板降级成 i2 Pro 的 **Values 窗口**——`V` 键开关，默认关闭；
它显示的是整张工作表的全部图通道，配合基准光标显示 `Δ`。

> 每帧只重画光标：波形走离屏缓存（`componentKey()` 命中就直接 blit），
> 所以移动鼠标时抬头数字能跟着跳而不掉帧。

---

## A19 · 圈速选择与对比（这次修了两个坏交互）

i2 Pro 的 Data 窗口里"点某一圈"= 把它设成 Main 圈。i3pro 之前点一下只是改选择、不跳转，
而且**点基准圈本身会把它设成自己的对比圈**，于是界面切到双圈对比、视窗被清空——看起来就是"点了没反应"。

| 操作 | 期望 |
| --- | --- |
| 单击某圈 | 设成**基准圈**，清掉对比圈，**视窗跳到该圈的时间区间** |
| <kbd>Ctrl</kbd>+单击另一圈 | 设成**对比圈**，切到双圈对比 |
| <kbd>Ctrl</kbd>+单击对比圈本身 | 取消对比，回到单圈时间轴 |
| <kbd>Shift</kbd>+单击 | 只跳转，不改基准/对比 |

**通过判据**：无头脚本第 17 组断言——
`F2` 清零视窗后点某圈，断言 `state.view` 恰好等于该圈的 `[start_time, end_time]`、
`state.ref` 变成该圈、`state.cmp === null`；再对另一个圈 `Ctrl+点击`，断言 `cmp` 被设置且
模式切到 `overlay`。

---

## A20 · 基准光标与 Δ（空格"好像没用"的真因）

**根因不是按键坏了**，是 Δ 没有可见输出：上一轮把 min/max/avg 搬进抬头、把左侧数值面板默认关闭之后，
空格放的基准光标只剩一条竖线，没有任何数字。另外光标在鼠标离开图区时被清空，
于是在图外按空格会把基准光标放到视窗最左端。

三处修复：

1. 图表抬头在基准光标打开时多一列 **`Δ`**（琥珀色，光标值 − 基准光标值）；
2. 状态栏显示 `基准光标 x · Δ y`；
3. 光标**离开图区后保留**（i2 Pro 行为），这样键盘步进和空格都有对象；
   光标为空时基准光标落在视窗**中间**而不是最左边。

**通过判据**：无头脚本第 18 组断言——按 `D`、按空格后
`state.datumOn && state.datum !== null`；状态栏同时含"基准光标"和"Δ"；
抬头里存在 `.ldelta` 单元格且内容形如 `Δ +0.123`。

---

## A21 · 仪表组件（Gauges 的 5 种子类型）

| 子类型 | 画法 |
| --- | --- |
| `numeric` 数值仪表 | 通道名 + 大号数值 + 单位 + Δ |
| `list` 数值列表 | 多通道 `名称 / 光标值 / 单位` 列表 |
| `bar` 条形（踏板） | 横向轨道 + 填充 + 上下限 + 数值 |
| `dial` 表盘（车速） | 270° 弧 + 刻度 + 指针 + 数值 |
| `wheel` 方向盘 | 圆环 + 随角度旋转的指示条（±540°） |

**通过判据**：无头脚本第 19 组断言——依次添加 5 个子类型的仪表组件，
每种都断言 `calls.fillText` 增加（真的画了字），最后清理干净。

值取自**光标处**（i2 Pro：*"The various gauges show the value of a channel at the current cursor position"*）；
量程取当前缩放区间内的 min/max。

---

## A22 · 自由布局与吸附

工作表从"纵向流 + 半宽"改成 **12 列网格**：`x/w` 是列，`y/h` 是 24 px 的行。

| 操作 | 期望 |
| --- | --- |
| 拖标题栏 | 移动组件；边缘吸附到画布边缘、其他组件的两边，并画出吸附参考线 |
| 拖右下角 | **同时改宽和高**；右边缘与下边缘同样吸附 |
| 吸附步长 | 横向 0.25 列，纵向 0.5 行 |
| 分享链接 | 位置、尺寸也一起编码（`layout=`） |

**通过判据**：无头脚本第 20 组断言——拖标题栏后 `x` 或 `y` 改变；
拖右下角后 `w` 或 `h` 改变；且 `x/w` 是 0.25 的整数倍、`y/h` 是 0.5 的整数倍
（即吸附没有被绕过）。

---

## A23 · 缩放手柄、每组件分栏、圈速表按钮（第三次反馈的三个修复）

**① 组件"不能上下左右缩放"**：原来只有一条 6 px 的底边条纹，而画布通常填不满组件
主体，所以用户拖到的其实是空白区——手感就是"拖不动"。现在给三个手柄：

| 手柄 | 位置 | 行为 |
| --- | --- | --- |
| `rzright` | 右边缘 7 px | **只改宽度** |
| `rzbottom` | 下边缘 7 px | **只改高度** |
| `rzcorner` | 右下角 16 px | 同时改宽高 |

悬停时手柄高亮，标题栏实时显示 `宽×高 格`。纵向吸附步长从 0.5 行收紧到 0.25 行（6 px），
小幅度拖拽也能生效。**通过判据**：无头断言三个手柄都存在，
拖右边缘后 `w` 变而 `h` 不变，拖角落后两者都变。

**② 第二个时间/距离图按 `G` 不分栏**：`G` 原来是**全局**开关，两张图一起变；
而且只有单个单位分组的图看不出差别。现在 `tiled / overlapped` 是**每个组件自己的**
`config.mode`（i2 Pro 就是 Component 菜单里的操作），`G` 只切换**当前聚焦**的那张图，
标题栏直接写出「分栏 / 重叠」。**通过判据**：两张图各设一次 `G`，
断言只有被聚焦的那张变了。

**③ 圈速表改成显式按钮**：点行本身**只跳转**，不再顺手改选择；
每行两个小按钮 `基` / `比` 分别设为基准圈与对比圈，再点一次 `比` 取消对比。
**通过判据**：点行后 `state.ref` 不变、视窗等于该圈区间；`selectLap(lap, false)`
把 `ref` 设成该圈并清空 `cmp`；`selectLap(other, true)` 设 `cmp` 并切到双圈对比。

---

## A24 · 圈速切分方式与手工信标（CLI 部分）

三种切分方式 + 手工信标，全部可用命令行验收：

```powershell
.\i3pro.cmd laps "i2pro_data\20260908-cjh 高避5圈.ld"               # auto：GPS 自动挑门
.\i3pro.cmd laps "i2pro_data\20260908-cjh 高避5圈.ld" --mode run     # 按起步/停车分段
.\i3pro.cmd laps "i2pro_data\20260908-cjh 高避5圈.ld" --mode figure8 # 八字按环分段
```

**通过判据**（单测 `TestLapModes`，8 项）：

| 断言 | 说明 |
| --- | --- |
| `turn_direction` 闭环判向 | 逆时针回路 → `+1`、顺时针 → `-1`、来回一直线 → `0` |
| `--mode run` | 返回的是"整次运行"而不是圈：段数 ≤ 自动模式，每段 > 20 s，按时间有序不重叠 |
| `--mode figure8` | 至少有一段被标上 `left` / `right` |
| 两个信标 = 两条独立序列 | 给"左环""右环"两个信标后，结果里同时存在 `左环 n` 与 `右环 n`，且各自时间区间合法 |
| 边车文件往返 | `<场次>.laps.json` 存/读不丢字段；**文件不存在时返回默认配置而不是抛异常** |
| 只有一个时刻的信标 | 手工插入的穿越时刻会并进时间上最近的那条序列，并真的多切出一条边界 |
| 只有时刻、没有位置 | 两个手工时刻单独成一条序列 |
| **归并前的旧侧车仍能读** | `gate`（单个 `[lat,lon]`）、`gates`（`[{name,lat,lon}]` 或 `[name,lat,lon]`）、`beacons`（一串裸时刻）四种旧结构全部能加载成新的 `Beacon`，一条都不丢 |

手工信标写法：`--gate 34.123456,113.654321:左环`（可重复），加 `--save` 写进边车文件。
边车现在是统一的信标列表——位置与时刻都是**同一个信标**的属性：

```json
{ "mode": "auto",
  "beacons": [
    { "name": "左环", "lat": 34.67364,  "lon": 113.9358633 },
    { "name": "补一次", "time": 1305.55 }
  ],
  "trusted": {} }
```

实测（耐久正赛，左环 + 右环 + 一次手工插入）：读回后两条序列 `左环 10 段 / 右环 21 段`，
手工那次并进了最近的序列。
实跑示例（高避 5 圈，`--mode run`）：

```
lap  turn  lap_time  delta_to_best  distance  start_time  end_time
1    None  135.52    0.0            2427.4    117.87      253.39
2    None  147.92    12.4           2482.3    260.89      408.81
切分方式: run
```

> ⚠️ 交互式"在赛道图上点一下放信标"的入口还没做（见 PLAN 的 M8），
> 这一条只覆盖算法、配置与 CLI。

---

## A25 · CSV 导入（`.ld` 之外的数据源，ticket #2）

一个 CSV 会话与一个 `.ld` 会话在下游是**同一个东西**：切圈、距离轴、双圈对比、报表都走同一套访问器。

```powershell
.\i3pro.cmd info "i2pro_data\20260522-yjw第二次直线3.72.csv"
.\i3pro.cmd laps "i2pro_data\20260522-yjw第二次直线3.72.csv"
.\i3pro.cmd import "D:\别的队给我的.csv" --data i2pro_data
```

**通过判据**（单测 `TestCsvSession`，7 项）：

| 断言 | 说明 |
| --- | --- |
| i2 Pro 导出的 CSV 能当会话读 | 341 通道、100 Hz、847 s，单位取自文件里的单位行 |
| **两条来源不打架** | 同一场次的 `.ld` 与 `.csv`，凡是共有且采样率相同的通道，逐样本差异 ≤ 该通道的显示精度（>200 个通道参与比对） |
| 每一列都有着落 | 报告条目数 = 通道数 + 1（时间列）；没有"被静默丢掉"的列 |
| 别的队的 CSV 也能吃 | 不同列名走三级回落：原名 → 别名表（`Wheel Speed FL` → `SpeedFL`、`Throttle Position` → `TH`）→ 保留原名；单位取单位行 |
| 手工指定能持久化 | `--map "原始列=通道名"` / `--unit` 写进 `<场次>.map.json`，重开仍然生效；显式参数优先于边车 |
| **没有时间列就拒绝** | 报错里写明"找不到时间列"并给出下一步（`--map "原始列=Time"`），**不拿第一列冒充时间** |
| CSV 能切圈、有距离 | 距离轴与切圈在 CSV 会话上给出非零结果 |

**实跑**：`laps` 在 yjw 那份 CSV 上给出 1 段 / 63.59 s / 356.9 m（此前是 0 m，见下）。

**这一条顺带修掉一个真 bug**：`derive.distance_series` 原先只检查 `max−min > 1`，于是一个"除了一个尖峰全是 0"的 `Distance` 列会被当成有效距离轴，**把每一圈的长度都算成 0**。CSV 导出恰好暴露了它（`.ld` 里的 `Distance` 全 0 所以被跳过）。现在要求：首尾差 > 1 m **且** 曲线 95% 以上非递减。

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

**通过判据**：`Ran 29 tests` + `OK`（无数据文件时相关用例自动 skip，不算失败）。

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
| `TestLaunchers` | 一键启动：快照批量导出 + 索引页、缺数据目录的报错、端口占用自动换端口 |
