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

**当前结果**：7 圈 / 5 完整圈，40.44 s – 51.18 s，806–816 m。

```powershell
.\i3pro.cmd track "i2pro_data\20260524-耐久正赛.ld"
```

**通过判据**：≥ 20 个完整圈，最快圈在 50–70 s。

**当前结果**：26 圈 / 23 完整圈，最快 54.900 s，单圈约 810 m。

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

**通过判据**（单测 `TestCsvSession`，12 项）：

| 断言 | 说明 |
| --- | --- |
| i2 Pro 导出的 CSV 能当会话读 | 341 通道、100 Hz、847 s，单位取自文件里的单位行 |
| **两条来源不打架** | 同一场次的 `.ld` 与 `.csv`，凡是共有且采样率相同的通道，逐样本差异 ≤ 该通道的显示精度（>200 个通道参与比对） |
| 每一列都有着落 | 报告条目数 = 通道数 + 1（时间列）；没有"被静默丢掉"的列 |
| 别的队的 CSV 也能吃 | 不同列名走三级回落：原名 → 别名表（`Wheel Speed FL` → `SpeedFL`、`Throttle Position` → `TH`）→ 保留原名；单位取单位行 |
| 手工指定能持久化 | `--map "原始列=通道名"` / `--unit` 写进 `<场次>.map.json`，重开仍然生效；显式参数优先于边车 |
| **没有时间列就拒绝** | 报错里写明"找不到时间列"并给出下一步（`--map "原始列=Time"`），**不拿第一列冒充时间** |
| CSV 能切圈、有距离 | 距离轴与切圈在 CSV 会话上给出非零结果 |
| **CSV 会话能比两圈** | 在耐久赛的 CSV 上取两个完整圈，`overlay` 到同一条距离轴：网格严格递增、两圈与每个通道都对齐到网格长度、`time_delta` 有有限值、`lap_table` 行数与圈数一致 |
| **单位是参与判断的信号** | 单位行留空的列会按该通道惯用单位补齐；单位与惯用值冲突时报告里给出 `单位不符：文件写 mph，该通道通常是 km/h`，而不是默默接受 |
| **采样率来源可查** | 报告与载荷写明 `rate_from`：i2 Pro 导出用元数据里声明的采样率（`元数据`），其它 CSV 用时间列推出来的（`时间列`） |
| **web 路径可达** | `/api/sessions` 列出 CSV 会话；`/api/session/<csv>/trace?from=&to=` 返回样本；工作台页面带 `"format": "csv"` |

**第三轮补做的三件（两轴评审发现的两个 Partial + 一个覆盖缺口）**

1. **单位/采样率信号**：原先只做了"原名 → 别名"，规格里承诺的"单位、采样率"两级没有兑现。现在元数据里的采样率被真正采用并标注来源，单位参与补齐与冲突检查。
2. **CSV 上的双圈对比**：原先只测了切圈与距离轴，`overlay` + Δ 与圈速表没在 CSV 上验证过——现已补齐（用耐久赛那份 406 MB 的导出）。
3. **web 路径**：原先没有任何断言证明 CSV 会话能被浏览器侧访问。

**这一步又暴露一个真问题**：同一场次同时存在 `.ld` 与其 `.csv` 导出时，两者同名，**CSV 会被 `.ld` 静默遮蔽**——按名字取永远拿到 `.ld`，CSV 根本不可达。现在两者都保留：`.ld` 用原名，CSV 用 `<场次> (csv)`。

**实跑**：`laps` 在 yjw 那份 CSV 上给出 1 段 / 63.59 s / 356.9 m（此前是 0 m，见下）。

**这一条顺带修掉一个真 bug**：`derive.distance_series` 原先只检查 `max−min > 1`，于是一个"除了一个尖峰全是 0"的 `Distance` 列会被当成有效距离轴，**把每一圈的长度都算成 0**。CSV 导出恰好暴露了它（`.ld` 里的 `Distance` 全 0 所以被跳过）。现在要求：首尾差 > 1 m **且** 曲线 95% 以上非递减。

---

## A26 · 给信标改名（ticket #4）

信标名是它那条圈速序列的**标签前缀**（`左环 3`），所以改名不只是改个字符串：
标签变了，按标签存的「可信 / 不可信」标记就会集体失配。四条命名规则与一次标记迁移，
全部实现为纯函数 `laps.reconcile_edits`，由服务端在每次保存配置时应用——界面、CLI、
手改边车文件走的是同一份规则。

| 规则 | 行为 |
| --- | --- |
| 去首尾空格 | `"  左环A  "` → `左环A` |
| 空名回退 | `"   "` → 保持原名（不是"改成一个没名字的信标"） |
| 重名加后缀 | 第二个 `左环` → `左环 2`，第三个 → `左环 3` |
| 超长截断 | 40 个字的名字截到 `laps.MAX_BEACON_NAME`（24） |
| **可信标记迁移** | `{"左环 1": false}` → 改名后 `{"左环A 1": false}` |

**通过判据**（`python -m unittest tests.test_i3pro.TestBeaconEditing -v`，6 项）：

```powershell
python -m unittest tests.test_i3pro.TestBeaconEditing -v
python -m unittest tests.test_i3pro.TestBeaconEditingOverHttp -v
```

单测断言：四条规则各一条；**连续插入两个信标的名字互不相同**；
改名后 `trusted` 的三个键分别落到新名字与"与本次改名无关的键原样保留"；
以及一条容易错的边界——信标名本身以数字结尾时（`左环` 改名为 `左环 2`）会产生
`左环 2 1` 这样的标签，迁移只认"前缀去掉之后是纯数字"的键，
**不会把 `左环 2 1` 误当成 `左环` 的一条圈**。

HTTP 端到端（同一份配置提交，走真实 `PUT /api/session/<场次>/laps`）：

1. 放一个带经纬度的信标 → 圈速表标签全部以 `左环 ` 开头；
2. 改名 ` 左环A  ` → 响应里名字已规范化，**`trusted` 已迁移成 `{"左环A 1": false}`**，
   圈速表标签全部跟着变成 `左环A `；重开该场次仍是新名字（它在 `<场次>.laps.json` 里）；
3. 同名放第二个信标 → `["左环A", "左环A 2"]`。

界面：点信标名就地编辑，`回车` 保存、`Esc` 取消（取消时一个字节都不会写进边车）。
快照模式下输入框只读，并提示改用 `serve` 模式。

**两轴评审后补做的三件（都是"改名"这件事本身的语义漏洞）**

1. **删信标曾经被当成改名。** 界面提交的是**整份配置**，原先按位置配对 old/new，于是删掉中间
   一个信标时，它后面那位的 `trusted` 标记会被搬到"滑进这个位置"的信标上，并写进边车——
   #4 要的是"标记跟着改名迁移、不会丢"，这里给的是**错的序列**。现在：长度不变 → 按位置配对
   （改名改的是那个物理信标）；长度变了 → 先按名字配对（插入 / 删除时，名字才是活下来的东西），
   剩下的只有在一对一时才按位置补。
   回归用例：`test_deleting_a_beacon_does_not_move_its_marks`（删掉中间一个 →
   `{"左环 1": false, "右环 1": true}` 原样不动）、`test_a_rename_in_place_still_carries_the_marks`。
2. **边车里已经越界的穿越改得了名了。** `check_new_crossings` 原先按 `(名字, 时刻)` 判断
   "是不是本次新增"，于是给一条越界穿越改名会被当成新条目 → 400，用户只能删不能改；
   现在按时刻判断。用例：`test_renaming_a_stale_crossing_is_not_treated_as_a_new_one`。
3. **快照模式下输入框只读、打字毫无反应**——现在聚焦即提示改用 `serve` 模式；删空再回车也明确
   提示"名字不能为空，仍然是「X」"，而不是静默。

---

## A27 · 在光标处插入一次穿越（ticket #5）

i2 Pro 的 **Missed Beacons**：车确实穿过了起终点，但没被检出，于是手工把那个**时刻**补进去。
在 i3pro 里它就是"只有 `time`、没有 `lat/lon`"的信标。

**最关键的一条语义**：插入的穿越**只增加边界，绝不替换已有的圈次集合**。
（旧实现在没有摆放信标时是"用两个时刻自己切"，于是一个时刻会把整张圈速表清空——
用户看到的是"我一插入，圈就全没了"。）

**通过判据**：

| 断言 | 命令 / 位置 |
| --- | --- |
| 插入后**正好多一条边界**，且新边界等于插入的时刻 | `TestBeaconEditing::test_an_inserted_crossing_splits_the_automatic_laps` |
| 插入的穿越**不另立一条序列**（标签里不出现 `手工穿越 n`） | 同上 |
| 超出本场时长的时刻被拒绝，报文写明"超出本场时长 … 请把光标放到图上再插入" | `TestBeaconEditing::test_only_new_crossings_are_range_checked` |
| 边车里**原本就有**的越界时刻不会把这一场锁死（只校验本次新增的） | 同上 |
| HTTP：插入后圈速表行数 +1、新边界 = 插入时刻、条目 `lat` 为空、边车落盘 | `TestBeaconEditingOverHttp` |
| UI：没有光标时**不插入**（不会插到 0 秒）；插入的条目在信标列表里是虚线药丸 + `t = 123.456 s`；`✕` 能单独删掉它、圈速表复原 | `tools/smoke_viewer.js` 第 21 组 |
| 距离轴上的光标是**米**，先换算成秒再插入；双圈对比的距离轴对应两条圈，**明确拒绝**并提示切到时间轴 | 同上 |
| 快照模式（没有服务端）提示改用 `serve` 模式，而不是点了没反应 | 同上 |
| 距离轴的换算**精确到采样**，且答案是"**首次到达**该距离的时刻"（车停着时同一距离会持续几分钟） | `TestDistanceAxisLookup` |
| 落在已有边界上的穿越**不制造幽灵圈**（不再留下 0.4 ms 的圈） | `TestBeaconEditing::test_a_crossing_on_a_boundary_that_is_already_there_changes_nothing` |
| 一条边界都切不出来时，响应里带 `notice`，界面把它显示出来 | `TestBeaconEditing::test_a_crossing_that_split_nothing_says_so` + HTTP 用例 |
| 两份金标准数据实跑通过 | `20260908-cjh 高避5圈` / `20260524-耐久正赛` 两个快照的 `smoke_viewer.js` 均 `PASS` |

**两轴评审后补做的四件（时刻精度 + "点了没反应"）**

1. **距离轴换成精确换算。** 新增 `/api/session/<场次>/at?distance=<米>`，由
   `laps.time_at_distance` 在距离序列上**定位**（不是插值）。语义上最要紧的一条：车停着不动时
   同一个距离会持续几分钟，所以答案是**首次到达该距离的时刻**。耐久赛实测：`d = 9878.9 m`
   处车停了 **127.5 s**（922.94 s 到、1050.44 s 走），返回 **922.94 s**。精度：在 200 个
   "正在走"的采样点上最大误差 **0.000 s**（一个采样 = 0.01 s），而 900 桶的全程概览桶宽是
   **2.16 s**——这正是评审指出的"全场视图下差可达 ±1 s，而且放大也不会变准"。
2. **幽灵圈**：与已有边界相差 ≤ `laps.CROSSING_SNAP`（0.05 s）的穿越视为同一条。此前对着
   i2 Pro 自己检出的边界点一下，会留下一个 0.4 ms 的圈（边车里的时刻是四舍五入过的，
   两者永远不会逐位相等）。
3. **"没切出新圈"要说出来。** 一条新穿越总是会增加一条边界，所以当圈数没变时（时刻与已有边界
   重合、或本场没有任何可插入的边界），响应里带 `notice`，界面直接提示"这次穿越没有切出新圈 …"。
   本场的两份金标准数据都测不到"没有基线"这条路径（7 圈 / 26 段），所以它由纯函数单测覆盖。
4. **无头断言补上两段。** 此前第 21 组只断言 `fetch` **之前**的本地状态（`saveLaps` 是先改
   `state` 再发请求），等于没验界面。现在断言请求体（`Enter` 必须只发一个 `PUT`、名字已去空格；
   `Esc` 一个请求都不发），以及"响应 → 界面"（界面必须吃下服务端返回的新名字与新圈速行）。

---

## A28 · 数学通道编辑器（本地 + 全局作用域，ticket #3）

写一条表达式就得到一个**新通道**，它在下游与原生通道没有区别：可勾选、可画图、可进散点、
可参与切圈与报表。求值是白名单（`maths.py` 里一张函数表 + 逆波兰序列），**完全不碰 `eval`**。

**两种作用域**：本地跟着场次（`<场次>.maths.json`），全局在仓库里（`maths/global.json`）。
同名时**本地赢**，界面上用角标（本地 / 全局）把"这条规则影响谁"写清楚。

**通过判据**：

| 断言 | 命令 / 位置 |
| --- | --- |
| 白名单：`maths.py` 的 AST 里没有任何 `eval` / `exec` / `compile` / `__import__` 调用 | `TestMaths::test_whitelist_only_never_calls_eval` |
| `__import__('os').system('calc')`、`os.system(...)`、`lambda`、列表推导**连编译都过不去** | 同上 |
| 未知函数 / 未知通道 / 括号不配平 / 结尾缺运算数 / 看不懂的字符，各给一条**说下一步做什么**的中文报错 | `TestMaths::test_unknown_function_says_what_to_do`、`test_unknown_channel_says_what_to_do`、`test_unbalanced_and_dangling_expressions_explain_themselves` |
| 函数集覆盖验收点列出的每一类（算术 / 三角 / 对数 / 取整 / min·max·abs / 区间统计 / 条件选择 / 无效标记 / 平滑滤波 / 微分积分），共 **53** 个 | `TestMaths::test_function_catalogue_covers_what_the_ticket_promised` |
| `filter_cheby_*` / `rand_*` **明确不提供**，报错里说清原因与替代（`filter_lp` / `filter_hp`） | `TestMaths::test_unsupported_functions_say_why_and_what_to_use_instead` |
| 区间统计：`reset` 切段、条件筛样本；段里没有合格样本时给 **NaN 而不是 0** | `TestMaths::test_interval_statistics_honour_condition_and_reset`、`test_interval_statistic_without_qualified_samples_is_nan_not_zero` |
| 微分（斜坡 → 斜率）、积分（常数 → 斜坡）、平滑与低通（标准差下降）各一条数值断言 | `TestMaths::test_derivative_of_a_ramp_is_the_slope`、`test_integrate_of_a_constant_is_a_ramp`、`test_smooth_reduces_ripple`、`test_low_pass_keeps_the_average_and_drops_the_ripple` |
| 引用成环报"绕成一个圈"，而不是递归到栈溢出；前向引用能解开 | `TestMaths::test_a_cycle_is_reported_instead_of_recursing`、`test_forward_references_resolve` |
| 一条定义坏了不拖累其它条：能算的照算，坏的把原因列出来（含"引用了坏定义"的那条） | `TestMaths::test_one_broken_definition_does_not_take_the_others_down` |
| 本地覆盖同名全局，且两条都能看出作用域；`shadowed` 列出被盖住的名字 | `TestMaths::test_local_overrides_global_and_both_are_visible` |
| 缓存：同一份定义第二次不重算（返回同一个数组对象）；表达式一改立刻换新列 | `TestMaths::test_cache_reuses_the_column_until_the_definition_changes`、`test_cache_key_changes_with_the_expression_and_the_source` |
| 派生列在下游与原生通道等价：`has` / `channel` / `unit` / `sample_rate` / 主时间基长度一致，通道索引里带 `derived: true` | `TestMaths::test_a_derived_column_looks_like_a_native_channel_downstream` |
| 重复挂载不产生重复通道；删掉定义后不留下取不到值的"幽灵通道" | `TestMaths::test_attaching_twice_does_not_duplicate_the_channel`、`test_removing_a_definition_does_not_leave_a_ghost_channel` |
| **同名覆盖一条慢的原生通道**时，派生列不会再被按原生采样率拉一遍（曲线不被毁） | `TestMaths::test_a_derived_channel_that_shadows_a_slow_channel_is_kept_as_is` |
| 派生通道**真的**参与切圈：给一场挂上 `Lap Number = round_down(integrate(1)/10)`，`laps.detect_laps` 就按这条派生序列切成 **46** 段（原生那条是 1 Hz 的常量 0） | 见下方实测 |
| HTTP：存一条本地定义 → 侧车落盘 → `trace` 能取到该列（带单位）→ `info` 里 `derived: true` | `TestMathsOverHttp` |
| HTTP：坏表达式在保存时就被 400 挡住，**且不改动已经存好的侧车**；同名两条也被挡住 | 同上 |
| HTTP：`scope=global` 写进 `maths/global.json`，**不会把本地定义一起搬进本地文件** | 同上 |
| HTTP：本地同名覆盖全局后，响应里 `shadowed` 列出被盖住的名字 | 同上 |
| 试算接口：能算的给样本数 / 最小 / 最大 / 平均，通道不存在给原因，语法错误给 400 | 同上 |
| UI：定义列表显示作用域角标与算不出来的原因；点一行把它装进编辑器且作用域跟着走 | `tools/smoke_viewer.js` 第 22 组 |
| UI：保存时**只带同一个作用域**的定义；改名字是替换而不是留下旧名字 | 同上 |
| UI：名字或表达式为空时**一个请求都不发** | 同上 |
| UI：删掉一条全局定义只写全局文件（`scope=global`），且请求体里只剩全局定义 | 同上 |
| UI：保存后的通道索引会刷新（新列出现在通道表里）；定义名里的尖括号被转义 | 同上 |
| 命令行与网页一致：`render` / `snapshot` / `convert` 都先挂上数学通道再产出，派生列随 Parquet 一起落盘 | `i3pro render ... --out out/x.html`（通道数 +4）、`i3pro convert ...`（Parquet 里能读到 `总G`） |
| 仓库自带的 `maths/global.json` 在**两份金标准数据上都算得出来**（0 条报错） | 见下方实测数字 |
| 两份金标准数据实跑通过 | 两个快照的 `smoke_viewer.js` 均 `PASS` |

**仓库自带的全局定义（实测，两份金标准都无报错）**

| 定义 | 表达式 | 高避5圈 | 耐久正赛 |
| --- | --- | --- | --- |
| 总G | `sqrt('G Force Lat'^2 + 'G Force Long'^2)` | 0 – 2.228 g | 0 – 2.052 g |
| 纵向加速度g | `'G Force Long'` | −1.16 – 0.71 g | −1.30 – 0.82 g |
| 速度kmh | `'Vx KF'` | −0.14 – 87.89 km/h | −1.65 – 80.78 km/h |
| 平滑纵向G | `smooth('G Force Long', 0.1)` | −0.916 – 0.588 g | −1.275 – 0.795 g |

**两轴评审后补做的三件（"零参数函数"、"中文标识符"、"布尔值"）**

1. **函数参数个数改成调用点决定。** 一开始把参数个数写死在函数表里，于是 `integrate(x)`、
   `smooth(x)`、`stat_max(x)` 这些"后几个参数可省"的写法全部编译不过。现在由编译期数出
   实参个数、写进逆波兰序列，参数个数不合法时**在保存前**就报出来（`1~3` 这种区间也会写清楚）。
2. **标识符要吃中文。** 通道名是「车速」这种，原来的标识符规则只认 `[A-Za-z_]`，于是
   `车速 * 2` 会报"看不懂的字符 `车`"，逼用户每条都打单引号。现在用 Unicode 的
   `[^\W\d]\w*`，`车速 * 2` 与 `'车速' * 2` 等价。
3. **JSON 里的布尔值被写成了数字。** `_json_safe` 把 `isinstance(value, int)` 排在
   `bool` 前面，而 Python 里 `isinstance(True, int)` 为真，于是 `{"ok": true}` 变成
   `{"ok": 1}`、通道索引里的 `derived` 变成 `1`。布尔判断已挪到整数之前。

**自查"派生通道参与切圈"这一条时又逮到一个真 bug（同名覆盖慢通道）**

验收点第 6 条要求"派生通道能参与切圈、比圈与报表"。为了证明它不是一句空话，我把
`Lap Number = round_down(integrate(1) / 10)` 挂到高避5圈上，`laps.detect_laps` 却只切出
**1 段**（应当按每 10 秒一段切成 46 段）。追下去是：

* 原生 `Lap Number` 是 **1 Hz、464 点**的常量通道；
* 派生列在 **100 Hz、46400 点**的主时间基上；
* 名字撞上之后，`log.channel('Lap Number')` 交回的是**原生**那条通道对象，于是
  `derive.hold_to_master` 按它的 1 Hz 把 100 Hz 的派生列 `repeat(100)` 再截回 46400 点
  ——取到的是开头那 100 个 0 拉长出来的常数列。**不报任何错，曲线直接被毁。**

修法：`ld.is_derived_channel()` 作为唯一判据，`derive.hold_to_master`、`store.build_table`
跳过重采样；`render.channel_index` / `render.trace` 对派生列报**定义里的单位**与**主采样率**
（否则界面会拿一条 1 Hz 原生通道的元数据去描述 100 Hz 的曲线）。修完之后同样的探针切出
**46 段**，`channel_index` 报 `rate: 100.0`、`derived: true`。

顺带补上：`maths.detach()` —— 删掉一条定义后不能留下"名字还在列表里、点开却取不到值"的
幽灵通道；`TestMaths::test_removing_a_definition_does_not_leave_a_ghost_channel` 盯着它。

**已知缺口（不算做完的部分）**：`filter_cheby_*` 与 `rand_*` 不提供；单位标注
（`'车轮速度'[km/h]`）接受但**忽略**，并在试算结果里原样告诉用户"不做单位换算"；
二维查表与 `Setup Sheets` 不做（`AGENTS.md` 规则 9）。

**第三轮（用户反馈 + Spec 轴评审后补做的三件）**

1. **通道名要能直接打出来。** 用户反馈"输入通道不太好识别，比如 `FSD-Distance1` 做不了运算"。
   实测三条路都走不通：`Vx KF * 2` 报"两个运算数挨在一起了（`KF`）"、`Distance (2) * 2` 报
   "未知函数 `Distance`"、`FSD-Distance1 * 2` 被当成 `FSD` 减 `Distance1`。现在把**已知通道名**
   交给编译器（`maths.known_names(session, definitions)`，含已定义的数学通道名），词法阶段按
   最长匹配认出整名，并在名字后紧跟字母数字／下划线时拒绝命中：

   | 写法 | 现在 |
   | --- | --- |
   | `Vx KF * 2` / `Distance (2) * 2` / `FSD-Distance1 * 2` | 直接算（旧写法 `'Vx KF' * 2` 一如既往地能用） |
   | `FSD13Distance1 * 2`（漏了空格） | 报错里给出 `最接近的是 'FSD13 Distance1'`（**第四轮改成直接算**） |
   | `VxKF * 2` | 同上，指向 `Vx KF`（**第四轮改成直接算**） |
   | `notachannel * 2` | **不硬凑**建议（只用共有字符比相似度时它会被认成 `Channel 9`，比不给更糟；现在用 `difflib` 且要求开头能对上） |

   界面同时加了「插入通道」下拉：**不用打字**，选中即把 `'名字'` 插到表达式光标处，
   完全绕开"什么时候要加单引号"这条规则。

2. **缓存键含"被引用定义的内容"。** 评审（P2-1）实测：`甲 = 'Vx KF' * 2`、`乙 = 甲 + 1`，
   把甲改成 `'Vx KF' * 4` 之后，乙仍然命中旧列（最大偏差 **175.780**）。原因是键里只有被引用
   通道的**名字**。现在键里带上被引用定义的**传递闭包**（名字 + 表达式），
   `TestMaths::test_a_changed_dependency_invalidates_the_cache` 盯着它；同一份定义仍然命中
   （`assertIs` 同一个数组对象），没有退化成"永远重算"。

3. **文档口径纠正**（评审 P3-1）：原文写"派生通道能参与切圈 / 比圈 / **报表** —— 满足"，
   而"报表"这一条当时没有任何证据——**仓库里还没有报表**（它是 ticket #11）。切圈（46 段）与
   比圈（`build_overlay` 用派生通道出 164 点曲线）是实测过的；报表留到 #11 走同一套访问器继承。

**这一轮当时的实测数字**：单测 107 项 OK（`TestMaths` 37 项、`TestMathsOverHttp` 2 项）；
`verify_ld_vs_csv` PASS；两份金标准快照 smoke 均 PASS；无头断言点 151 个。第四轮之后是
**112 / 155**（见 A28 末尾）。

**第四轮（用户复查原话："输入通道，不是能很好识别，比如 `FSD-Distance1` 通道，对他做不了运算"）**

第三轮把已知通道名交给了编译器，但只认**逐字相同**的名字——用户那条路还是不通。先把
`FSD-Distance1` 查清是谁：本仓库 15 个场次（`out/probe_names.py` 逐个列出通道名）**没有
任何一条通道名带短横线**；真身是 `FSD13 Distance1`，只出现在 `20260912-TV0` 与
`20260912-雨胎TV1` 两条新场次里（`FSD13 Distance2` 是它的兄弟）。也就是说用户记的是
**同一条通道的另一种写法**：漏了空格、还把空格记成了短横线。

| 用户打的 | 第三轮（旧） | 现在 |
| --- | --- | --- |
| `FSD13Distance1 * 2`（漏空格） | 报"本场次没有这个通道" | 认成 `FSD13 Distance1`：133 × 2 = **266** 起步、143800 点，与 `'FSD13 Distance1' * 2` 逐点相同 |
| `vx kf * 2`（小写几个字母） | 报"两个运算数挨在一起了（`kf`）" | 认成 `Vx KF` |
| `Distance(2) * 2`（少打一个空格） | 报"`Distance` 不是函数" | 认成 `Distance (2)` |
| `FSD-Distance1 * 2`（这条真没有） | 只说 `FSD` 不存在，用户还是不知道那条叫什么 | 在 TV0 上给出"本场次以 `FSD` 开头的通道有：`FSD13 Distance1`、`FSD13 Distance2`"；在高避（真没有 FSD 系）上老实说"检查拼写" |
| `G Force Late * 2`（多词的名字打错） | "两个运算数挨在一起了（`Force`）" | 同一句话 + "本场次以 `G Force` 开头的通道有：`G Force Lat`、`G Force Long`、`G Force Vert`"（实在不像才退到 `最接近的是`） |

规则一句话：**名字里的空格／短横线／下划线可以省掉或互换，大小写不计较；逐字相同的
写法永远优先；同一个位置认出多条（本场次真有两条只差一个分隔符的通道）就不猜**，报错
让用户写全。字母数字必须逐个对上——`FSD13 Distance12` 不会被认成 `FSD13 Distance1`
的前缀 `FSD13 Distance1`，`FSD 13 Distance1`（数字前凭空多一个空格）也不算同一条。

**顺带修掉的三处**：

1. **词法器按"名字长度"往前跳，不是按"文本位置"。** 名字里少一个空格时两者差一个字符：
   `FSD13Distance1*2` 会多吃一格、把 `*` 吞掉，然后报一个跟通道名毫无关系的语法错。
   现在 `_longest_name_at` 把结束下标一起交回，`TestMaths::test_a_name_typed_without_its_space_is_still_that_channel`
   里的 `FSD13Distance1*2` 就是这个回归的钉子。
2. **试算与本地保存先检查通道名。** 以前名字打错要等画图时才报；现在「试算」
   （`POST /api/session/<n>/maths`）与本地定义保存（`PUT ...?scope=local`）都在编译阶段
   报 400 并说清该改成哪条，坏定义**不进侧车**。**全局定义保持宽松**：它本来就是跨场次
   复用的，某一场缺那条通道是正常情况（仓库自带的 `速度kmh = 'Vx KF'` 在高避场次里就
   没有），照旧存下来、显示成红字。保存时"哪些名字算存在"**含另一份作用域的定义名**
   （`SessionLibrary.maths_names()`），否则本地定义引用全局定义会被误判成"没有这个通道"。
3. **试算结果念出认到的通道**（"；用到通道 `FSD13 Distance1`"）。不然用户不知道自己那串
   算成了谁——这正是他上一次报错时缺的那句话。

**通过判据**：

| 断言 | 命令 / 位置 |
| --- | --- |
| `FSD13Distance1 * 2` / `FSD13Distance1*2` / `fsd13distance1 * 2` / `FSD13-Distance1 * 2` / `FSD13_Distance1 * 2` 都算成同一条 `FSD13 Distance1`（数值与带引号的写法逐点相同） | `TestMaths::test_a_name_typed_without_its_space_is_still_that_channel` |
| 名字后面接着数字就是另一条通道，不许往前凑 | 同上（`FSD13 Distance12 * 2` 仍报错） |
| `vx kf` / `VXKF` / `Vx_KF` / `'vx kf'` 都算成 `Vx KF` | `TestMaths::test_case_and_separators_are_interchangeable` |
| 逐字相同的写法优先；两种写法都对得上时不猜，报错里把两条名字都列出来 | `TestMaths::test_exact_spelling_wins_over_a_lookalike` |
| `Distance(2) * 2` 认成 `Distance (2)`，不再报"未知函数 `Distance`" | `TestMaths::test_a_channel_called_with_brackets_can_skip_the_space` |
| 真的没有的名字：列出"以它开头的通道" + 「插入通道」提示；完全不像的名字**不硬凑**建议 | `TestMaths::test_a_typo_gets_the_right_name_back` |
| 严格检查只在要求时生效（不打开时编译旧行为不变） | `TestMaths::test_strict_channel_check_happens_before_anything_is_computed` |
| 试算 `VXKF * 2` 返回 200 且 `channels == ["Vx KF"]`（服务端把认到的真名说出来） | `TestMathsOverHttp::test_a_bare_channel_name_with_a_space_saves_and_computes` |
| 漏空格的名字存档后能真的算出曲线（`trace` 里有样本） | 同上 |
| 试算不存在的通道 → 400，报错含 `本场次没有这个通道` 与 `插入通道` | 同上、`TestMathsOverHttp::test_saving_a_definition_reaches_the_viewer` |
| 本地保存坏名字 → 400，且 `<场次>.maths.json` 里**没有**这条坏定义 | 同上 |
| 全局保存缺通道的定义 → 200，并在 `errors` 里报出来（跨场次复用不受影响） | 同上 |
| UI：试算文案里带"用到通道 `FSD13 Distance1`"，失败时原样显示服务端那句话 | `tools/smoke_viewer.js` 第 22 组 |

**这一轮的实测数字**：单测 **112 项** OK（`TestMaths` 42 项、`TestMathsOverHttp` 2 项）；
`verify_ld_vs_csv` PASS（`0 channel(s) outside tolerance`）；两份金标准快照 smoke 均 PASS
（7 圈 / 26 段）；无头断言点 **155** 个。

**第四轮（用户："数学通道可以改成不用手写，直接从下拉里选"）**

表达式不再只能靠手打：**通道下拉**（按单位分组、带单位后缀，一场 400+ 条通道也找得着）
与**函数下拉**（53 个函数，标出参数个数 `1~3` 与说明）——点一下就把 `'通道名'` / `函数(`
插到表达式光标处：通道名自动带单引号，函数插完光标停在括号里，紧接着再从通道下拉挑参数。
光标位置优先，没有 `selectionStart` 的环境退化成追加。

```powershell
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"   # 第 22 组：插入通道 / 插入函数
```

无头断言（`tools/smoke_viewer.js` 第 22 组）：通道下拉**按单位分组**（`<optgroup`）且列出带空格的通道名；
选中即插入 `'名字'`；在已有内容后面插入不丢前半段（`1 + ` → `1 + 'Vx KF'`）；
函数下拉列出目录并显示参数个数（`smooth(1~3)`）；插入函数得到 `smooth(`；接着插通道得到
`smooth('Vx KF'`（光标停在括号里，可以继续挑参数）。

真 Edge 渲染的观感验证截图：`out/shots/maths-editor.png`（下拉插入后表达式框是 `'FSD13 Distance1' * 2`）。

**真场次再走一遍（临时探针，`out\probe_maths_http.py`：把 `20260912-TV0.ld` 复制到临时
目录后起真服务、走真 HTTP 接口；`out\probe_names.py` 用来列各场次的通道名）**：

| 请求 | 结果 |
| --- | --- |
| 试算 `FSD13Distance1 * 2` | 200，`channels = ["FSD13 Distance1"]`，最小 -0.002 / 最大 **563.0** m |
| 试算 `vx kf * 2` | 200，`channels = ["Vx KF"]`，最小 -0.26 / 最大 81.92 km/h |
| 试算 `Distance(2) * 2` | 200，`channels = ["Distance (2)"]` |
| 试算 `FSD-Distance1 * 2` | 400，"本场次以 `FSD` 开头的通道有：`FSD13 Distance1`、`FSD13 Distance2`" |
| 试算 `G Force Late * 2` | 400，"本场次以 `G Force` 开头的通道有：`G Force Lat`、`G Force Long`、`G Force Vert`" |
| 存本地定义 `两倍FSD距离 = FSD13Distance1 * 2` | 200，`errors: []`；`trace` 拉回 40 个抽稀样本（最大 563.0） |
| 15 个场次的通道名逐条列出 | **0 条**带短横线；`FSD13 Distance1` / `FSD13 Distance2` 只出现在 TV0 与雨胎TV1 |

**第五轮（用户："数学通道可以改为非手写输入，可以选择使用提供下拉选项来选择需要的通道这种形式"）**

第四轮把**通道**和**函数**做成了下拉，但运算符还得回键盘上打：`*`、`(` 这些键不在
"通道名什么时候要加单引号"那条知识里，可是用户要的"不用手写"仍然没做全。这一轮把
**运算符也变成按钮**，整条表达式只用鼠标就能点出来；再加一个**筛选框**——按单位分组之后
400 多条通道还是得滚很久。

| 能点什么 | 点出什么 |
| --- | --- |
| `＋ － × ÷` | 插符号，**左右各补一个空格**（`A * B` 比 `A*B` 好读）；紧挨空白 / 左括号 / 右括号时不重复补 |
| `(` `)` `,` | 原样插入（逗号给多参数函数用） |
| `退格` | 删掉光标前一个字符（有选中就删选中的那段） |
| `清空` | 表达式清空，换个算法重来比在长表达式里改快 |
| `插入通道…` | 第四轮就有：按单位分组、自动加单引号 |
| `插入函数…` | 第四轮就有：53 个函数带参数个数，插完光标停在括号里 |
| `筛选通道…` | 打几个字把下拉里的选项缩到几条；**选完自动清空**，下次挑通道从头找 |

按钮走**事件委托**：符号写在 markup 的 `data-ins` 里，以后加按钮只改 HTML，不用一根根接线。

```powershell
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"   # 第 22 组：运算符键盘 / 筛选框
```

无头断言（`tools/smoke_viewer.js` 第 22 组，这一轮 **+13 个断言点**）：键盘 markup 里
`+ - * / ( )` 齐全；空表达式点 `×` 得到 `* `；点过通道之后点 `×` 得到 `'名字' * `（左右各一个空格）；
接着从通道下拉挑第二个运算数，得到 `'名字' * '名字'`——**键盘和下拉能拼成一条真表达式**；
再点 `＋` 追加在末尾（不是插到开头）；`退格` 少一个字符；`清空` 清空；筛选框打通道名前三个字，
面板报出的**匹配条数与 `channels.filter(...)` 实测一致**，全不匹配时报"没有名字含…的通道"，
清空筛选后按单位分组的列表回来。

真 Edge 渲染的观感验证（脚手架 `out/shot_maths.py`，`out/` 已 gitignore）：

| 截图 | 里面是什么 |
| --- | --- |
| `out/shots/maths-keypad.png` | 键盘排成一行（`＋ － × ÷ ( ) , 退格 清空`）；筛选框写 `fsd13`，下拉头变成"匹配 2 条，点一条插进表达式"；表达式框是 `'FSD13 Distance1' * 2` |
| `out/shots/maths-pickers.png` | **全程只用鼠标**：`插入函数` 选 `smooth` → `插入通道` 选 `Vx KF` → 点 `＋` → 点 `(`，表达式框得到 `smooth('Vx KF' + (` |

**这一轮的实测数字**：单测 **129 项** OK（这一轮没动 Python，与上一轮同数）；
`verify_ld_vs_csv` PASS（`0 channel(s) outside tolerance`，213/213 通道在显示精度内）；
两份金标准快照 smoke 均 PASS（7 圈 / 26 段）；无头断言点 **188 → 201** 个。

---

## A29 · 撤销上一步信标编辑（ticket #6）

**语义**：撤销不是"反向编辑"，而是**把上一版配置原样再提交一次**。"原样"是关键：撤销
改名之后 `trusted` 标记能回到旧名字上，是因为它们本来就挂在旧名字上（上一版就是这么存
的），不是有人又迁移了一遍；撤销插入，那条边界不在上一版里，圈速表自然复原；撤销删除，
信标还在列表里。服务端拿到它之后只做落盘，**不再跑 `reconcile_edits`**——上一版正是那些
规则自己的输出。

**只留一版，而且只在内存里**（`server.SessionLibrary._laps_undo`，一个槽）。这是 ticket
定的边界，也是它诚实的边界：同一份服务进程里刷新页面，撤销按钮依然说得准（页面注入的
`laps_can_undo` 来自服务的槽）；**服务一重启，可撤销的那一步就没了**——撤的是"这个进程里
刚才那一步"，不是历史。一级撤销，用掉就清空，**不做重做**。

槽按**场次文件**记，不按浏览器标签记：两个人同时开着同一场次时，谁后改一步，撤销撤的就是
那一步，另一个人按撤销也会看到这一步被撤掉。这是"一个槽"的直接后果，摆在明面上比藏起来好。

**通过判据**：

| 断言 | 命令 / 位置 |
| --- | --- |
| "同一版"按边车存下来的形状比（名字 / 位置 / 时刻 / 可信标记 / 切分方式任一不同都算另一版） | `TestBeaconUndo::test_same_config_compares_what_the_sidecar_stores` |
| 没有上一版、或上一版与当前版一样时，`undo_config` 给 `None`（按钮该灰的判据） | `TestBeaconUndo::test_nothing_to_undo_is_said_out_loud` |
| 撤销交回去的是**上一版本身**，不是一份重算过的近似 | `TestBeaconUndo::test_undo_hands_the_previous_version_back_untouched` |
| 改名之后撤销：名字回到旧名字，**`trusted` 标记一起回到旧标签**，圈速表标签跟着变回 | `TestBeaconUndoOverHttp::test_rename_insert_and_delete_are_each_one_step_back` |
| 插入之后撤销：圈速表行数复原、那条穿越从配置与边车里都消失 | 同上 |
| 删信标之后撤销：信标回到配置里，边车也是 | 同上 |
| **不是"看起来改了"**：三步撤销之后读回 `<场次>.laps.json`，落盘的就是撤销后的结果 | 同上（`on_disk()`） |
| 一次**什么都没改**的保存不吃掉上一步（"顺手保存一下"不能让用户撤不回来） | 同上 |
| 没有可撤销的一步时，接口 400 并说下一步做什么（"改一次信标再来撤销"），且不动边车 | 同上 |
| 刷新页面后按钮仍然说得准：页面注入的 `laps_can_undo` 由服务给出，`false` → `true` → `false` 三态都对 | 同上（`page_payload()`） |
| UI：没有可撤销的一步时按钮是 **disabled**（不是静默失败） | `tools/smoke_viewer.js` 第 24 组（第 23 组是赛道区段） |
| UI：**灰按钮不会被点**——浏览器不会把 click / 焦点送给 disabled 控件，所以"点了为什么没反应"的解释要有别的出口：代码里另留一道守卫（脚本直接调 `undoBeaconEdit()` 时说明原因），按钮 `title` 也写着当前状态 | 同上（注意：无头 harness 的假元素**不做**这条浏览器规则的模拟，它的"点了有提示"不能当成真实浏览器行为） |
| UI：撤销发的是 `{"undo":true}`，**不是**把整份配置重发一遍 | 同上 |
| UI：按服务返回的 `can_undo` 决定按钮亮灰，并弹出"已撤销上一步信标编辑" | 同上 |
| 快照模式：按钮灰、标题说明要改用 `serve`、一个请求都不发 | 同上 |
| 两份金标准数据实跑通过 | `20260908-cjh 高避5圈` / `20260524-耐久正赛` 两个快照的 `smoke_viewer.js` 均 `PASS` |

**几个不显然但必要的判断（写在代码注释里，也写在这里）**：

1. **只有真的改出一版新的，才更新那个槽。** 否则用户在改完名之后随手保存一次同样的配置，
   上一步就被"当前版"顶掉了，撤销变成原地踏步。判据是 `laps.same_config`。
2. **撤销路径不跑 `check_new_crossings`。** 要交回去的那一版本来就存在过、也被接受过；
   重跑一次反而有害——那条规则放过边车里**已经存在**的越界穿越，于是用户删掉一条旧侧车里的
   越界穿越之后就再也撤不回来了。
3. **撤销路径也不跑 `reconcile_edits`。** 槽里存的本来就是上一次规范化之后的配置，"原样交回去"
   才是这个功能的本义；再规范化一遍只会给"撤销"加入它不该有的判断。

---

## A30 · 赛道区段：自动切分与手动编辑（ticket #7）

**语义**（`CONTEXT.md` 里也钉了一条）：赛道区段 = 沿**距离**量出来的一段（弯道或直道），
边界以米记在一条**参考圈**上，套在每一条圈上使用。它不是圈（计时的单位，见 A8），
也不是环（几何回环）。自动切分给出第一版，人改过之后由人说了算。

```powershell
python -m unittest tests.test_i3pro.TestSections tests.test_i3pro.TestSectionsOverHttp -v
.\i3pro.cmd snapshot --data i2pro_data --out out
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"     # 第 23 组断言
```

**这次实测出来的三件事，决定了实现的样子**：

1. **场次里那条叫 `Curvature` 的通道整场都是 0**（两份金标准场次都是），`Radius` 在耐久正赛里
   也整场是 0（高避5圈里中位 18 m，不像真实半径）。拿它们当判据会"切出一整条直道"却看着像
   成功了。所以两个判据都是现算的：

   | 判据 | 是什么 | 单位 | 特点 |
   | --- | --- | --- | --- |
   | 曲率（`curvature`） | GPS 轨迹的 `\|d(航向)/d(距离)\|` | 1/m | 与速度无关；位移 < 5 cm 的相邻点记为直行（车几乎不动时方位角抖动除以小位移会炸出天文数字） |
   | 横向加速度（`lateral_g`） | `\|G Force Lat\|` | G | 与速度有关：慢的发夹弯在这里显得弱 |

2. **阈值用分位数算**：`10 分位 + (90 分位 − 10 分位) × 0.35 ÷ 灵敏度`。不同车、不同赛道的
   曲率量级差几倍，绝对值阈值在这条赛道上能用、换条就废。灵敏度是唯一旋钮，实测**单调**：
   高避5圈按曲率把灵敏度从 0.3 调到 3.0，判成弯的里程是 0 / 136 / 218 / 408 / 461 / 547 / 595 m
   （每一步都不减；条数不一定增——两条弯之间的短直道短于最短段长时会被并成一条大弯）。
3. **边界定在 1 m 的均匀距离网格上**：按时间采样的样本在慢弯里挤成一团，直接对样本做阈值
   会让边界全挤在同一个地方。

**实测数字**（临时探针 `out\probe_sections.py` / `out\probe_sections2.py`；单测里钉了同样的值）：

| 场次 | 参考圈（最快完整圈） | 判据 @ 灵敏度 1.0 | 结果 |
| --- | --- | --- | --- |
| 20260908-cjh 高避5圈 | 第 5 圈，40.440 s / 812.1 m | 曲率 | 弯 3 条 408.0 m + 直 4 条 404.0 m = 812.0 m（圈长 812.1） |
| 20260908-cjh 高避5圈 | 同上 | 横向加速度 | 弯 4 条 634.0 m + 直 4 条 178.0 m |
| 20260524-耐久正赛 | 第 10 圈，54.900 s / 813.8 m | 曲率 | 弯 6 条 376.0 m + 直 7 条 438.0 m |
| 20260524-耐久正赛 | 同上 | 横向加速度 | 弯 2 条 674.9 m + 直 3 条 139.0 m |
| 20260912-TV0 | 第 8 圈，10.980 s / 113.4 m | 曲率 | 中位半径 **6.2 m** → 整圈都在转弯（"整圈一条弯"是实话，不是噪声） |
| 合肥八字陈君灏 | 第 2 圈，14.300 s / 148.1 m | 曲率 | 中位半径 **8.4 m** → 同上，八字本来就是一直在转 |

**通过判据**：

| 验收条目（ticket #7 原话） | 断言 / 位置 |
| --- | --- |
| 自动切分给出弯道与直道两类区段，**覆盖整圈、不重叠、不留缝** | 合成数据：边界首 0 尾 L、严格递增、相邻种类必不同、各段长度和 == 圈长（±0.5 m）——`TestSections::test_auto_split_tiles_the_lap_without_gaps_or_overlaps`；真数据：`TestSections::test_the_golden_hill_lap_splits_into_corners_and_straights` |
| 自动切分放在该在的地方（不是"切了就算"） | 两个已知位置的弯，切出来的起止距离与放进去的相差 < 8 m：`TestSections::test_the_two_corners_land_where_they_were_put` |
| **灵敏度可调** | 0.3→3.0 七档：判成弯的里程单调不降、且首尾必须不同（合成数据与两份金标准都测）：`test_sensitivity_only_moves_the_corner_mileage_up`、`test_the_golden_hill_lap_splits_into_corners_and_straights` |
| **看得出按什么切** | 载荷里带 `basis` + `basis_labels`，界面下拉与提示行直接写"按曲率 / 按横向加速度"；判据没得选（没有 GPS 轨迹 / 没有 `G Force Lat`）时 400 并说清缺什么——`sections.available_bases`、`TestSectionsOverHttp` |
| 判据不是那条坏通道 | 金标准里 `Curvature` 整场为 0，而 GPS 曲率的 90 分位 > 0.01：`test_the_curvature_basis_is_not_the_dead_Curvature_channel` |
| 坏参数要说下一步 | 最短段长 > 半圈、灵敏度 ≤ 0、不认识的判据，三种都给出可操作的报错：`test_absurd_parameters_say_what_to_change` |
| **边界与名字可手动编辑** | `PUT /api/session/<n>/sections`（带 `boundaries`/`kinds`/`names`）→ 归一化（排序、去重、夹在 [0, 圈长]、补齐首尾）、名字不许重复、名字空着给默认：`test_manual_edits_get_sorted_clamped_and_still_cover_the_lap`、`test_duplicate_names_are_shifted_apart` |
| 只给一条边界也不许留洞 | 补成覆盖整圈并给出 `notice`：`test_a_partial_boundary_list_is_completed_not_left_with_holes` |
| **手动改过之后自动切分不会悄悄覆盖** | 侧车里 `edited` 立起来；再点重切得到 **400 + `needs_force`**，侧车一个字节不动；带 `force` 才覆盖并说明"覆盖了手工改动"：`TestSectionsOverHttp::test_sections_are_served_saved_and_protected` |
| "同一份再存一次"不算手工改过 | `edited` 只在内容真的变了（`sections.same_layout`）时才立起来：`test_same_layout_tells_a_real_edit_from_a_no_op` |
| **区段随场次持久化** | `<场次>.sections.json` 侧车（`.ld` 只读、字节不变）；存过之后重新 GET 拿到的就是存下来的那一份：`test_the_sidecar_round_trips_and_refuses_nonsense`、同上 HTTP 用例 |
| 侧车坏了要说下一步 | 不认识的 `basis` / 种类会报出文件名与"改掉它或删掉这个文件"：`test_the_sidecar_round_trips_and_refuses_nonsense` |
| **时间轴上能把当前区段可视化出来** | 载荷带每条圈自己的边界时刻（`lap_marks`）→ 前端在时间轴面板画弯/直带子 + 边界线；`tools/smoke_viewer.js` 第 23 组断言：时间轴模式画出 ≥2 条带子、关掉开关后一条不画、**距离轴模式一条不画** |
| 每条圈的带子按**那条圈自己的速度**定位 | 边界时刻在圈内递增、首尾正好是该圈起止时刻；两条圈的"边界相对时刻"必须不同（同一条参考圈秒数平移是错的）：`test_the_golden_endurance_lap_and_its_per_lap_marks`、`test_bands_land_inside_the_lap_they_are_asked_about` |
| 换了参考圈要提醒 | 侧车里记着"切在哪条圈上"，与当前参考圈不同时给出"这份区段是按第 1 圈切的，当前参考圈是第 5 圈（圈长差 …）"：`test_a_stored_split_on_another_lap_says_so` |
| UI：左侧区段表（种类 / 名字 / 起点距离 / 长度），改名字、挪边界、点种类各发一次 PUT 且带完整定义；重切第一次不带 `force`、被拦之后第二次带 `force` | `tools/smoke_viewer.js` 第 23 组 |

**边界（明确不做 / 说清代价）**：

* 带子**只画在时间轴**上。距离轴模式下每条圈的走线长度不同（参考圈 812.1 m，别的圈 810～815 m），
  服务端不替用户猜该按哪条圈画，宁可不画。
* 参考圈换了（信标挪了、切分方式变了）不会自动把边界"搬"到新圈上：只提醒，重切由用户点。
* 不做 i2 Pro 那种带计时意义的 S1/S2/S3 计时段——那是另一个概念（`CONTEXT.md` 里也标了）。

## A31 · 双击区段放大到该区段（ticket #8）

i2 Pro 的 `To Zoom to a Range: double-click on the range band`：双击一条区段，
横轴就缩到这一段。我们的做法是**两个入口、同一件事**——双击左边区段表里的一行，
或者双击时间轴顶上那条色条。

**为什么要单独画一条色条**（这条是跑完整回归才发现的）：带子的淡色原本铺满整个
绘图区当背景，第一版就把"绘图区里的双击"整个当成了"双击区段"——结果**原来的
"双击原地放大 2 倍"在时间轴模式下几乎点不到了**（绘图区基本被带子盖满）。
`out\20260524-耐久正赛.html` 的无头驱动直接把这条抓了出来
（`zoom moved away from the clicked position`）。现在只有顶端那条
`SECTION_STRIP_PX = 9` 像素的实心色条算数，别处照旧是"原地放大 2 倍"。

语义定义不在浏览器里独此一份：`sections.band_at_time()`（谁属于哪一段）与
`sections.band_window()`（一段的时间窗口）是纯函数，前端那两个 JS 函数是**薄移植**
——快照必须能离线回答，不能回头问服务端。两边用同一批边界情况钉住。

**通过判据**：

| 断言 | 命令 / 位置 |
| --- | --- |
| 时间点落在哪一段：起点属于它自己、**边界归后一段**、圈与圈之间的空档**不属于任何一段** | `TestSections::test_which_section_a_time_falls_in` |
| 最后一条圈的最后一段**包含终点那一瞬间**（否则双击终点线没有任何反应） | 同上（`band_at_time(marks, 60.0)` 命中、`60.1` 落空） |
| 坏输入不猜：空表 / `None` / NaN / 只有起点没有终点的圈，一律返回 `None` | 同上 |
| 一段的时间窗口：正常给出 `(起, 止)`；**零宽度的一段不给窗口**（界面要说"没有能用的时间范围"，而不是把视图缩成一个点）；下标越界也返回 `None` | `TestSections::test_the_window_of_one_section_row` |
| 真数据钉子：金标准里**每条圈、每一段**的起点拿去问，都要问回它自己（≥10 段） | `TestSections::test_double_clicking_every_band_finds_that_same_band` |
| 缩过去之后横轴真的变成那一段，光标跟着进去，并且**说出缩到了哪一段** | `tools/smoke_viewer.js` 第 23 组 |
| 左边表里**点得到**的入口 = 同一件事：行尾的 `⤢`；**双击行中间的名字不跳视图**（那是"选词"） | 同上 + `tools/verify_clicks.py` |
| 距离轴上双击区段：先切回时间轴再缩，并且提示里写出"切回时间轴"——不许静默换轴 | 同上 |
| **只有顶端色条算"双击区段"**：同一列落在绘图区里的双击仍然是原地放大 2 倍 | 同上（这一条就是耐久快照抓出来的那个回归） |
| 每段两笔填充（淡色背景 + 色条）、一笔描边；`strip=false` 时只剩一笔填充 | 同上 |
| 缩到一段之后按"全出"要能回到**整场**，不是卡在刚加载的那一段 | `tools/verify_clicks.py`（A33） |
| 无头断言点 **223 → 260**（#11 加到 255，本轮修三个缺陷 +5） | `rg -o "check\(" tools/smoke_viewer.js \| Measure-Object` |

```powershell
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"   # 7 圈
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"       # 26 圈 / 23 完整圈
```

真 Edge 渲染的观感验证（脚手架 `out/shot_sections.py`，`out/` 已 gitignore）：
`out/shots/sections-strip.png` 与放大图 `sections-strip-zoom.png`——第一个绘图区
顶端那条橙/蓝实心色条就是双击的落点；下面几个绘图区只有淡色背景，没有色条。

**边界（说清代价）**：

* 色条只画在**第一个绘图区**顶上（横轴是整张图共用的，画一条就够）；双击第二个
  绘图区顶上的同类位置**不算**——那里没有画色条，不给看不见的落点。
* 距离轴模式下不画带子（各圈走线长度不同，见 A30），所以那儿的双击是切回时间轴再缩，
  不是"在距离轴上缩到某一段"。

**当前结果**：`TestSections` 19 项、全套 **150 项单测 OK**；`verify_ld_vs_csv` PASS；
两份金标准快照 smoke 均 PASS（无头断言点 260）；同一批交互在**真浏览器真鼠标**下
也过（A33 的 21 项，含"全出要回到全场"）。

---

## A33 · 真浏览器 / 真鼠标验收（`tools/verify_clicks.py`）

无头驱动（`tools/smoke_viewer.js`）跑在假 DOM 上：元素没有面积、没有遮挡、没有
`pointer-events`，disabled 的控件照样派发 `click`。它能证明"代码调用了它该调用的
函数"，**证明不了用户点得到**。这一条用 Edge 自己的 DevTools 协议发真正的
`Input.dispatchMouseEvent` / `dispatchKeyEvent`（真命中测试、真焦点、真键盘），
跑在 `i2pro_data` 的**副本**上（`out\_verify_data_<端口>`，侧车写副本里）。

一上来就抓到三件事，**假 DOM 全都放过了**：

1. **"双击区段表里的一行"在真实命中测试下点不到。** 行的中间是名字输入框
   （`.sname{flex:1}`），双击它落在 `INPUT` 上，按规则那是"选词"、不跳视图；
   真正有效的只有行尾那一小段长度文字。现在行尾多了一个 `⤢`（`data-section-zoom`），
   点了就缩到那一段，提示文字也照实写。
2. **serve 模式下"全出"回不到全场。** `fullRange()` 原先按"当前已加载的 traces"
   算，而 serve 模式里那只有当前这一段：缩到一段之后按"全出"，横轴只剩刚加载的
   那一段（实测 `lane = [119.58, 119.63]`，整场 463.99 s）。现在整场范围改问
   **全程概览**（整场的 900 桶），实测"全出"回到 `[0.00, 463.48]`。
   顺带修掉色条双击的 **1 个采样**误差：段窗口 `[119.55, 119.66]` 之前被夹成
   `[119.55, 119.65]`，现在 `[119.550, 119.660]` 精确相等。
3. **信标改名：按过一次 Esc（或成功保存过一次）之后，同一个输入框就再也存不进去。**
   `dirty` 只在渲染时置 `true`，Esc 与 commit 把它置 `false` 之后没有任何地方
   再置回来，于是"重新打字 + 回车"静默不存——界面看着像改了，侧车里还是旧名字。
   现在补上 `input` 监听。`tools/smoke_viewer.js` 第 21 组为此加了两条断言，
   **把修复拆掉就红**（`out/_nofix_dirty.html` 实测：
   `after Esc the same name box must still save (got 左环A)`）。

**通过判据**（`python tools\verify_clicks.py`，21 项）：

| 断言 | 实测 |
| --- | --- |
| 真 Edge 里 serve 模式加载出数据 | 真场次，445 通道 |
| #8 真双击顶端色条 → 横轴正好是那一段 | `view=[119.550, 119.660]`（与段窗口逐位相等） |
| #8 真双击绘图区（同列、非色条）→ 照旧原地放大 2 倍 | `0.110 s → 0.055 s`，点击处落在视图里 |
| #8 真点区段表行尾的 `⤢` → 缩到那一段 | `view=[316.870, 324.830]` = 参考圈第 2 段 |
| #8 真双击行中间的名字 → 不跳视图（那是选词） | 视图不变 |
| #8 缩到一段后"全出" → 回到整场 | `lane=[0.00, 463.48]`（整场 463.99 s） |
| #5 没设光标按「＋穿越」→ 说清"先把鼠标移到图上"，且不加信标 | toast 原文命中，信标数不变 |
| #5 真点图设光标 → 按「＋穿越」→ 侧车多一次穿越，时刻就是光标处 | `t=231.74` = 光标 `231.740 s` |
| #5 手工穿越画成虚线药丸、真的落进侧车 | `pill manual`；侧车读得回来 |
| #4 真点信标名 → 输入框拿到焦点；真键盘输入进得去 | `activeElement.className = bname` |
| #4 Esc → 输入框弹回原名，**侧车一个字节没动** | 盘上仍是原名 |
| #4 回车 → 侧车真的改名，**`trusted` 标记跟着迁走** | `{"改名之后 1": false}` |
| #6 真点 ↶ 撤销 → 名字退回上一步 | `['改名之后','手工穿越 2'] → ['手工穿越','手工穿越 2']` |
| 整场没有页面级报错 | 无 `Runtime.exceptionThrown` |

```powershell
python tools\verify_clicks.py                                              # 高避5圈 21/21
python tools\verify_clicks.py --session "20260524-耐久正赛" --port 8752    # 耐久正赛 21/21
```

两份金标准各 **21/21 通过**。逐帧截图落在 `out/shots/verify-*.png`（`out/` 已 gitignore），
想用眼睛复核时看它们。

**边界（说清代价）**：

* 这条要 **Microsoft Edge**（系统自带那份即可，不装任何东西）：脚本自己起无头 Edge、
  发真事件；出问题时 Edge 的日志落在 `out/_edge_stderr.log`。没有 Edge 的机器打印
  SKIP 退出——和缺数据一样，**SKIP 不算通过**。
* 一人一份数据目录与浏览器配置（按端口命名），两个人同时跑不会互相删数据。
* 它替代不了人眼：拖拽手感、输入法、字号这类还得自己看一眼；中文输入走的是
  `dispatchKeyEvent` 的 `text`，与真 IME 仍有差别。
* 以后凡是"点了才出现"的状态都往这里加断言；`tools/smoke_viewer.js` 继续负责
  "逻辑对不对"（它更快、不需要浏览器）。

---

## A32 · 时间报告与通道报告（ticket #11）

i2 Pro 的两张 Pro 独有报表：**时间报告**（分段计时 + 理论最快圈 + 连续最快圈）与
**通道报告**（按圈或按区段列统计量）。这里做成两个工作表组件：`时间报告` 与
`通道报告`。

两张表都是 **DOM 表格**而不是 canvas——报告是要拿走的（复制一格、导出 CSV），
画在画布上就变成一张图，连一个数字都选不中。

**算法在 Python 里，只算一次**：`report.py` 是不依赖框架的纯函数，快照、本地服务、
命令行三个入口全走 `render.report_payload()`。界面上不做二次统计——"这条通道这一段
的均值"必须能在命令行里复现，不能只在浏览器里成立。

```powershell
.\i3pro.cmd report "i2pro_data\20260908-cjh 高避5圈.ld" --limit 3
.\i3pro.cmd report "i2pro_data\20260524-耐久正赛.ld" --table channels --by section --filter corner --limit 5
.\i3pro.cmd report "i2pro_data\20260524-耐久正赛.ld" --csv out\时间报告.csv
```

**口径**（写进 `CONTEXT.md`，改实现就要改那里）：

| 项 | 定义 |
| --- | --- |
| 分段用时 | 那条圈**自己的**边界时刻之差（`sections.lap_marks`），不是拿参考圈的秒数平移；所以每条圈各列加起来正好等于该圈圈速（实测两份金标准，误差 0.0000 s） |
| 理论最快圈 | 每个区段各自的最快用时相加；只取**完整圈**的段（被截断的进出场段会把成绩拉到跑不出来的值）。界面与 CLI 都写明"这是参考下限" |
| 连续最快圈 | 在一整段连续行驶上滑一个"一圈长度"的窗口，取用时最短的那个；窗口两端都是**首次到达**该距离的时刻，和 `laps.time_at_distance` 一个口径 |
| 起值 / 终值 | 窗口内第一个 / 最后一个**有效**样本（NaN 不算数）；没有有效样本给 `None`——给 0 会被当成一次真实测量 |
| 标准差 | 总体标准差（`ddof=0`） |
| 统计窗口 | 半开区间 `[起, 止)`：止点那一刻的样本算下一条圈，否则相邻两条圈会重复计同一个样本 |
| 「接近最快」分档 | 相对该段最快：≤ +0.5 % 最快 / ≤ +3 % 接近 / ≤ +8 % 一般（相对量，直道与发夹弯不能共用一个绝对秒数） |

**通过判据**：

| 断言 | 命令 / 位置 |
| --- | --- |
| 时间报告是"区段 × 圈"的矩阵，**每条圈的整列加起来等于该圈圈速** | `TestReport::test_matrix_lists_one_column_per_lap_and_sums_to_the_lap_time` |
| 没跑完的圈在表头就写着`（未完）`，且**不能赢任何一段的"段最快"** | `TestReport::test_a_truncated_lap_cannot_win_a_section` |
| 只看弯道 / 只看直道：行数跟着变，**序号仍是原序号**（和区段面板对得上），理论最快圈只剩被显示的那些段 | `TestReport::test_kind_filter_keeps_the_index_and_narrows_the_theoretical_lap` |
| 统计量：最小 / 最大 / 绝对最大 / 均值 / 起值 / 终值 / 变化量 / 标准差；窗口全 NaN 时**每一项都是 `None` 而不是 0** | `TestReport::test_stats_use_the_first_and_last_valid_sample` |
| 分档阈值就是文档里那三个数（1.005 / 1.03 / 1.08） | `TestReport::test_band_thresholds_match_the_documented_ratios` |
| 通道报告按圈 / 按区段分组：行数 = 圈（或段）× 通道，窗口取**那条圈自己的**边界时刻 | `TestReport::test_channel_report_windows_do_not_share_the_boundary_sample`、`test_channel_report_by_section_uses_that_lap_own_boundaries` |
| 停车段算进连续最快圈：合成数据里"10 s 跑 100 m → 停 90 s → 40 s 跑 400 m"给出 **130.0 s**，把停车尾端当起点会给出 40 s | `TestReport::test_a_stop_inside_the_window_counts_against_the_rolling_lap` |
| 赛道不够一圈时连续最快圈给 `None`，不给一段假成绩 | `TestReport::test_rolling_lap_needs_a_full_lap_of_track` |
| 缺失通道被**报出来**而不是静默丢掉 | `TestReport::test_missing_channels_are_reported_not_silently_dropped` |
| CSV：表头 = 列标签，含逗号/引号的格子被正确转义，小数位按列走 | `TestReport::test_csv_header_is_the_column_labels_and_cells_are_escaped` |
| HTTP：`/report` 两种表、`table=` 只算一张、`csv=` 直接吐 CSV（带 BOM）、`filter=` 写错给 400 并说清只认什么 | `TestReportOverHttp::test_report_endpoint_serves_both_tables_and_csv` |
| 界面：表渲染成 DOM 表格、行数 = 段数；表下面那句话报出**理论最快圈 / 连续最快圈**，数字与载荷一致 | `tools/smoke_viewer.js` 第 26 组 |
| 界面：切「只看弯道」后表格行数与 CSV 行数一起变 | 同上 |
| 前端的 CSV 转义/小数位和 Python **逐字节一致**（`T1, 入弯` → 加引号）、数字 `12.3456` → `12.346` | 同上 |
| 按圈分组时"区段过滤"下拉框**灰掉并说明切成「按区段」**（按圈分组时每一行就是一整条圈） | 同上 |
| 快照离线可用：**不许**去请求 `/report`；快照里没带的通道要说明"导出快照时没选中" | 同上 |
| 分享链接带走报表配置（区段过滤、通道清单） | 同上 |
| 真数据钉子：金标准两条圈的**每条圈、每一段**加起来等于圈速，且 理论 ≤ 连续 ≤ 最快圈 | `TestReport::test_golden_hill_sections_sum_to_each_lap`、`test_golden_endurance_report_is_consistent` |
| 无头断言点 **223 → 255** | `rg -o "check\(" tools/smoke_viewer.js \| Measure-Object` |

```powershell
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"   # 7 圈
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"       # 26 圈 / 23 完整圈
```

**当前结果（实测）**：

| 场次 | 区段 | 理论最快圈 | 连续最快圈 | 最快圈 |
| --- | --- | --- | --- | --- |
| 高避5圈 | 7 段（3 弯 / 4 直） | 39.470 s | 39.750 s | 第 5 圈 40.440 s |
| 耐久正赛 | 13 段（6 弯 / 7 直） | 52.990 s | 54.450 s | 第 10 圈 54.900 s |

快照体积：高避 1.27 MB、耐久 1.59 MB（报表给耐久那份加了约 0.3 MB）。

**边界（说清代价）**：

* 快照里的通道报告只带**导出快照时选中的那几条**通道（耐久一份要几十万行，全带上
  就不是"双击即开"了）。界面会说明哪几条没带，要看别的通道用 `serve` 模式。
* 快照的"按区段"分组只有**参考圈**那一份；换一条圈要联网。
* 一张表最多报告 8 条通道（再多就不是表，是一份要滚半天的清单）。
* 连续最快圈是**整段**连续数据的滑动窗口，所以它会跨过起点线；它和"最快圈"不是一个
  概念，界面上两个都显示、不互相替代。

**真 Edge 渲染的观感验证**（脚手架 `out/verify_report.py`，`out/` 已 gitignore）：
真 Chromium + 真鼠标，17 项全过——**表格是真 DOM**（7 圈的矩阵 1238 px 刚好放下、
26 圈的矩阵 1357 px 触发横向滚动而表头 `sticky`）、每一段至少有一格落在「最快」档、
切「只看弯道」后行数从 7 变 3，以及**真点一次 CSV 按钮、文件真的落到磁盘上**：
`20260908-cjh 高避5圈-时间报告.csv`，4 行（表头 + 3 段）、表头与表格列标签一字不差、
前三字节是 BOM。截图：`out/shots/report-worksheet.png`、`report-corners.png`、
`report-endurance.png`。

这一步抓出三个只有真浏览器才会露头的问题，都已修：

1. **组件标题不跟着走**——改成「只看弯道」之后表只剩弯道，标题还写着"全部区段"；
2. 表下面那行说明里 `**原始采样**` 的星号**原样显示**（那是纯文本，不当 Markdown 渲染）；
3. 空白的文字格显示成 `--`（和"这段没有有效样本"的数字缺失撞在一起），文字列空着就该是空的。

另外把表改成显式 `<thead>/<tbody>`：浏览器虽然会自动补，但表头要 `sticky` 钉住，
外边数行数、读表头也不该把表头行算进数据行。

**当前结果**：`TestReport` + `TestReportOverHttp` 18 项、全套 **150 项单测 OK**；
`verify_ld_vs_csv` PASS；两份金标准快照 smoke 均 PASS；真 Edge 验收 17/17。

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

## A33 · 真浏览器、真鼠标的第四道回归（以及它抓到的三个 bug）

前三道回归里，`tools/smoke_viewer.js` 跑在**假 DOM** 上：它能证明"代码调用了它该调用的
函数"，证明不了**点得到**——假 DOM 里元素没有面积、没有遮挡、没有 `pointer-events`，
disabled 的控件照样派发 `click`。所以多了第四道回归：`tools/verify_clicks.py` 用 Edge
自己的 DevTools 协议发真正的 `Input.dispatchMouseEvent` / `dispatchKeyEvent`，
**真命中测试、真焦点、真键盘**，跑在金标准场次的**副本**上（随便点都不会碰到车队数据）。
只用标准库，Edge 走系统自带那一份；没有 Edge 或没有数据时自动跳过（退出码 0）。

```powershell
python tools\verify_clicks.py                       # 21 项，全过退出 0
python tools\verify_clicks.py --session "20260524-耐久正赛" --port 8790
```

**它抓到的三个 bug（都是"假 DOM 里看不出来"的）**：

| # | 现象 | 根因 | 修法 |
| --- | --- | --- | --- |
| 1 | 缩到某一段之后按「全出」，视图**回不到全场** | `fullRange()` 问的是 `state.traces`，而 serve 模式下那是"当前这一段"，上限一路缩水 | 改问**全程概览**（整场的 900 桶）：时间轴用 `overview.time`，距离轴用 `overview.distance` |
| 2 | 信标改名按过一次 `Esc` 之后再改、回车**静默不存** | `dirty` 被 `Esc` 关掉后再也没有地方重新置真（`input` 事件没接） | 输入框接 `input` 事件重新置脏；无头断言补"Esc 之后同一个框还能存" |
| 3 | 区段表里"双击一行"在真命中测试下**几乎点不到** | 行中间是名字输入框（`flex:1`），双击它等于**选词** | 行尾加一个点得到的 `⤢`（与双击顶端色条同一件事），面板说明里写明"双击名字是选词" |

**实测**：`python tools\verify_clicks.py` → **21 项检查：21 通过，0 失败**（含上面三条各自的正向与反向断言：
点 `⤢` 缩到那一段 / 双击输入框不跳视图 / 缩完「全出」回到 `[0.00, 463.48]`（整场 463.99 s）；
真键盘改名后读边车确认那条信标真的叫新名字；`↶` 撤销真的退回上一步）。

顺带修掉工具自己的两个问题：Windows 控制台默认 GBK，界面里的 `↶` / `⤢` 会让它在打印
断言结果时 `UnicodeEncodeError` 崩在半途（现在强制 UTF-8）；`⤢` 那条断言有界重试三次
（面板刚渲染完就点会命中旁边的输入框——竞态要报成"不稳定"，不能报成 PASS，也不能当成 bug 修）。

**这一轮的回归**：`Ran 150 tests` + `OK`；`verify_ld_vs_csv` PASS（0 channel(s) outside tolerance）；
两份金标准快照 `smoke_viewer.js` 均 PASS；无头断言点 **260** 个。

---

## 全量回归

```powershell
python -m unittest discover -s tests -v
```

**通过判据**：`Ran 132 tests` + `OK`（无数据文件时相关用例自动 skip，不算失败）。

测试覆盖：

| 分组 | 内容 |
| --- | --- |
| `TestHeader` | 元数据、通道链表自洽性、数据块连续性 |
| `TestScaling` | 缩放公式、负小数位（×10）、与 CSV 导出对照 |
| `TestDerived` | 距离单调性、GPS 轨迹尺度合理 |
| `TestLaps` | 两种赛道的切圈、距离轴重叠、Δ 收敛 |
| `TestLapModes` | 切分方式（auto / run / figure8）、一个信标一条序列、旧侧车四种结构仍能读 |
| `TestBeaconEditing` | 信标改名的四条规则、可信标记迁移（含"删除 ≠ 改名"与"改名到已存在的名字"两种配对）、插入的穿越只加边界不换集合、落在已有边界上不造幽灵圈、越界时刻被拒、没切出新圈要有 `notice` |
| `TestBeaconEditingOverHttp` | 改名 / 插入穿越走真实 `PUT .../laps`：规范化、标记迁移、落盘、越界 400、`/at` 距离换算、`notice` |
| `TestDistanceAxisLookup` | 距离 → 时刻：停在原地的距离返回**首次到达**的时刻、精确到采样（不受 900 桶概览限制）、没开到的距离与 NaN 一律拒绝、映射不倒退 |
| `TestStore` | Parquet 往返、列式裁剪、SQL 查询 |
| `TestCsvReader` | i2 Pro CSV 导出结构解析 |
| `TestChannelGroups` | 通道按单位分组：不重不漏、单位一致、状态通道识别 |
| `TestPoints` | 散点原始样本、时间窗裁剪、超窗口自动 stride |
| `TestMaths` | 数学通道引擎（42 项）：白名单与 AST 断言、函数集、区间统计的条件与复位、微分积分、平滑与低通、成环与前向引用、一条坏了不拖累其它、本地覆盖全局、缓存命中与失效、派生列在下游等价于原生通道、通道名的识别（少空格/换分隔符/大小写/歧义不猜） |
| `TestSections` | 赛道区段（19 项）：切分覆盖整圈不重不漏、弯切在该在的位置、灵敏度单调、测度整条平线时不造弯、最短段长决定"尖峰算不算弯"、手工编辑的排序/夹紧/补齐、名字去重、同一份不算改过、侧车往返与坏文件、真数据的份数与里程、每条圈的边界时刻（含"换了参考圈要提醒"）、**落在哪一段**（边界归后一段、空档不算、最后一段含终点、坏输入不猜）与**一段的时间窗口**（零宽度不给窗口） |
| `TestSectionsOverHttp` | 赛道区段走到 HTTP：GET 不落盘、重切落盘、手工改名字与边界、`edited` 立起来、被挡住的重切 400 + `needs_force` 且侧车不动、带 `force` 才覆盖、坏请求的下一步、`.ld` 字节不变 |
| `TestMathsOverHttp` | 数学通道走到 HTTP：存本地 / 全局、侧车落盘、坏表达式 400 且不动已存侧车、同名拦截、`shadowed`、试算接口、函数表 |
| `TestRender` | 静态/服务两种 payload、自包含性 |
| `TestServer` | HTTP 端到端：场次列表、工作台页、通道、时间窗、散点、概览、对比圈、赛道、404 |
| `TestIndependentParsers` | 第二套实现交叉验证、213 通道 CSV 全量对照 |
| `TestBeaconUndo` | 撤销的纯函数层：什么是"同一版"、什么时候没有可撤销的一步、交回去的是上一版本身 |
| `TestBeaconUndoOverHttp` | 撤销走真实 `PUT`：改名 / 插入 / 删除各自一步回到原样、`trusted` 迁移、落盘、一次无改动的保存不吃掉上一步、没有可撤销的一步时 400 并说明下一步、页面注入的 `laps_can_undo` 三态 |
| `TestViewerScript` | 无头驱动前端：脚本里 **260 个 `check(...)` 断言点**（`rg -o "check\(" tools/smoke_viewer.js | Measure-Object`）+ 时间轴 / 双圈两条渲染路径 + 直接打开模板的提示 |
| `TestLaunchers` | 一键启动：快照批量导出 + 索引页、缺数据目录的报错、端口占用自动换端口 |
