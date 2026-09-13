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

修法：设一个「这条通道是不是算出来的」判据，`derive.hold_to_master`、`store.build_table`
跳过重采样；`render.channel_index` / `render.trace` 对派生列报**定义里的单位**与**主采样率**
（否则界面会拿一条 1 Hz 原生通道的元数据去描述 100 Hz 的曲线）。修完之后同样的探针切出
**46 段**，`channel_index` 报 `rate: 100.0`、`derived: true`。

> 这个判据当年落在 `ld.is_derived_channel()`，于是同一个问题在五个调用点各判了一遍；
> ticket #18 把它们统一收进 `channels.py`（见 A39）。这段历史留着，是因为它正好说明
> 为什么那条缝值得存在。

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

**当时的结果**（本条验收时；全套现在 189 项）：`TestSections` 19 项、全套 **150 项单测 OK**；`verify_ld_vs_csv` PASS；
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

工具自己也有两个问题，顺手修掉：Windows 控制台默认 GBK，界面里的 `↶` / `⤢` 会让它在
打印断言结果时 `UnicodeEncodeError` 崩在半途（现在强制 UTF-8）；`⤢` 那条断言有界重试三次
（面板刚渲染完就点会命中旁边的输入框——竞态要报成"不稳定"，既不能报成 PASS，也不能当成 bug 修）。

**通过判据**（`python tools\verify_clicks.py`，28 项）：

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
python tools\verify_clicks.py                                              # 默认高避5圈 28/28
python tools\verify_clicks.py --session "20260524-耐久正赛" --port 8752    # 耐久正赛 28/28
```

两份金标准各 **28/28 通过**（21 项是这一条刚建时的信标 / 区段 / 撤销，**7 项是 #9 直方图
后来加的**，见 A34）。逐帧截图落在 `out/shots/verify-*.png`（`out/` 已 gitignore），
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

**当时的结果**（本条验收时；全套现在 189 项）：`TestReport` + `TestReportOverHttp` 18 项、全套 **150 项单测 OK**；
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

## A34 · 直方图组件（ticket #9）

i2 Pro 的 Histogram（以及车用的 Suspension Histogram）=「一条通道在一段区间里取值的分布」。
这里做成第 8 个组件类型：`bars` / `line` / `text` 三种画法、4–500 格、按第三通道着色、
门槛（gating）、窗口跟当前缩放走。**四条硬口径**，为的都是一件事：别画出"看着像分布、
其实不是分布"的东西。

| 口径 | 为什么 |
| --- | --- |
| **数原始样本，不数画图那份抽稀结果** | 时间曲线走的是 Min/Max 抽稀，每一格塞进一个极小值和一个极大值；拿那份数据做直方图会长出一截**假长尾**。所以分布在 serve 模式下按当前窗口现问 `/api/session/<名>/histogram`；快照里内嵌导出时算好的几份（整场 + 每条完整圈，`render.snapshot_histograms`） |
| **没进去的样本要报出来** | 被门槛排除的（`excluded`）与 NaN **分开**计数。混进"样本数"里，用户会以为通道真有那么多样本 |
| **门槛只有一套语法** | `gate=` 可以直接写通道名（非零为真），也可以写数学通道表达式；解析只在 `histogram.gate_values` 里做一次，报错复用数学通道那一套（会念出认到的通道名 + 下一步）。**不另造第二套条件语法** |
| **快照里改格数只能往粗里并** | 快照只带了固定格数的计数，再细分就是编出来的；并格是精确的（计数之和不变），所以并格放行、细分拦下来并说明。要更细就用 serve 模式 |

**算法在 Python 里、是不依赖框架的纯函数**：`histogram.py` 只吃数组
（`histogram()` / `gate_keeps()` / `summarize()` / `clamp_bins()`），取数在 `render.histogram()`。
界面不重算一遍——"这一箱里有多少个样本"必须能在命令行复现。

```powershell
# 一次性复现（不起界面）：
python -c "import sys,numpy as np;sys.path.insert(0,'src');from i3pro import ld,render;log=ld.LogFile.read(r'i2pro_data\20260908-cjh 高避5圈.ld');t=np.arange(int(round(log.duration*log.sample_rate))+1)/log.sample_rate;p=render.histogram(log,'Vx KF',t,bins=40);print(p['count'],p['range'],p['stats'])"

# 界面上：起服务 -> 「＋ 添加组件」选「直方图」-> 通道 / 格 / 画法 / 色 / 门槛 / 窗口
.\i3pro.cmd serve --data i2pro_data --open
```

**通过判据**（`python -m unittest tests.test_i3pro.TestHistogram -v`、`TestHistogramOverHttp`）：

| 断言 | 位置 |
| --- | --- |
| 计数之和 = 参与统计的样本数；格边界**严格递增**（`np.histogram` 的等宽边界不重复） | `TestHistogram::test_counts_add_up_and_edges_are_strictly_increasing` |
| 门槛「非零为真」把 0 的那些样本剔掉，剔掉的条数**报出来** | `TestHistogram::test_gate_nonzero_drops_the_zeros` |
| 门槛 `range` / `outside` 两种模式：区间内 / 区间外在内，缺上下限时**报错并说清要补什么** | `TestHistogram::test_gate_range_and_outside_modes` |
| 按第三通道着色时，每箱给的是该箱的**均值**（同时给极值，只看均值会把"一会儿 0 一会儿 100"画成一片中间色） | `TestHistogram::test_colour_is_the_mean_of_that_box` |
| 整段是同一个值（一直没踩的刹车）时，区间**显式撑开**并说明，不然画不出来 | `TestHistogram::test_a_constant_channel_still_gets_a_real_range` |
| NaN 分开报，**不算样本**；全是 NaN 时统计量给 `None` 而不是 0 | `TestHistogram::test_nan_samples_are_reported_not_counted` |
| 格数夹在 4–500，夹过了**带一句话**出来（`0` 会让 `np.histogram` 抛异常，`100000` 会画出一万根一像素的柱子） | `TestHistogram::test_bins_are_clamped_and_said_out_loud` |
| 门槛可以是**通道名**，也可以是**数学通道表达式**（两者走同一个出口） | `TestHistogram::test_gate_accepts_a_channel_or_a_maths_expression` |
| 报错要写下一步：通道不存在 / 门槛写错 / 着色通道不存在 / 窗口选反了，各有各的句子 | `TestHistogram::test_bad_input_says_what_to_do_next` |
| 真数据钉子：金标准场次的窗口统计与手算一致 | `TestHistogram::test_golden_session_window_stats` |
| HTTP：`/histogram` 要 `?channel=`、通道不存在给 400、格数被夹过要带 `notice`、`from/to` 是半开区间 | `TestHistogramOverHttp::test_histogram_endpoint` |
| 界面：加得出来（默认自己挑一条通道）、画布上真的有柱子（不是白纸）、改格数/缩放会**重新问服务端**、门槛报错写到表头、删掉写错的门槛**图能回来**、选着色通道后表头写明色是什么 | `tools/verify_clicks.py`（真浏览器真鼠标，A33 的第四道回归） |
| 界面：快照模式不许去请求 `/histogram`；内嵌格数之外只能并格；没带分布的通道要说明"导出快照时没选中" | `tools/smoke_viewer.js` 第 27 组 |

**这一轮的实测数字**（两份金标准，都是这条命令跑出来的）：

| 场次 / 通道 | 样本数 | 区间 | 中位 | 说明 |
| --- | --- | --- | --- | --- |
| `20260908-cjh 高避5圈` · `Vx KF`，整场 40 格 | 46400 | −0.14 – 87.89 km/h | 52.46 | 与 463.99 s × 100 Hz 对得上 |
| `20260524-耐久正赛` · `Vx KF`，整场 40 格 | 194300 | −1.65 – 80.78 km/h | 45.15 | 1943 s × 100 Hz |
| 同上 + 门槛 `Brake Signal`（非零为真） | 2187 | −0.07 – 85.79 km/h | 38.24 | 排除 44213 个样本，`notice` 里报出来 |
| `G Force Lat` + 门槛 `Vx KF > 60` | 17473 | −2.14 – 2.11 g | −0.15 | 排除 28927；标准差 1.131 g，无门槛时 0.846 g |
| 门槛 `Brake Signal > 10`（写错了：这条是 0/1 信号） | 0 | — | — | 46400 个全被排除，`notice` 让你放宽条件；统计量给 `None` 不给 0 |
| 窗口 `[200, 200)`（空窗口） | 0 | — | — | `bins` 为空 + "时间轴的起止是不是选反了？" |

**这一轮顺带修掉的一个真 bug（#9 把它逼出来的）：组件 id 撞车。**
`SHEET` 是拿 `comp.id` 当键的，而恢复自 `localStorage` 的布局带着**上一次会话的 id**，
`compSeq` 每次开页却从 0 重新数——于是"恢复回来的 `histogram-3`"和"新加的第 3 个组件"
撞成同一个 id，两份组件共用一个 bundle。真 Edge 里的表现是直方图**无限重新请求**
（真点一次"改格数"之后 `/histogram` 刷了 **229 次**，页面卡到 CDP 调用超时，画布空白）。
这在直方图之前就存在（任何拿 bundle 存缓存键的组件都会中招），只是没有服务端请求时看不出来。
修法：`makeComponent` 兜底（id 已存在就换号）+ `restoreWorksheet` 之后调
`adoptComponentIds`（新号接在已有最大号之后，并顺手修掉旧布局里已经重复的 id）。
`tools/smoke_viewer.js` 第 27.0 组钉住三件事：id 不重复、`SHEET` 的 bundle 数 = 组件数、
恢复一份带重复 id 的旧布局能被修好。

**这一轮的回归**：`Ran 161 tests` + `OK`；`verify_ld_vs_csv` PASS（`0 channel(s) outside tolerance`）；
两份金标准快照 `smoke_viewer.js` 均 PASS（7 圈 / 26 圈）；无头断言点 **260 → 281**；
`tools\verify_clicks.py` **28 项：28 通过，0 失败**。（#10 之后是 174 项单测、308 个断言点，见 A35。）

**第四道回归自己抓到的第一个 bug，是它自己写错了**（照实记下来，因为它说明这道回归值得留）：
清空门槛那几步少了"重新点一次输入框"——前一步的 `Tab` 已经把焦点交给下一个控件，
再按 `Ctrl+A` / `Backspace` 是打在别的控件上的，门槛原封不动留在请求里，后面的着色
断言因此看到的是**上一条错误的表头**，报成"着色坏了"。真浏览器里点一遍才发现是脚本的
问题（用重新量的坐标点一下，门槛清掉、表头立刻回到统计量）。顺带把这条断言的证据从
"打印 URL"改成"打印 URL **和表头**"——只打印 URL，看的人没法判断是请求错了还是渲染错了。

---

## A35 · 频谱组件（ticket #10）

i2 Pro 的 FFT / Spectrum =「一条通道的频域成分」，车上的用处主要是看悬架与振动的频率。
这里做成第 9 个组件类型：Welch 平均周期图，点数 128–8192（**就近吸附到 2 的幂**）、
五种窗（hann / hamming / blackman / rectangular / flattop）、段间重叠 0 / 50 % / 75 %、
纵轴功率谱密度或有效值、频域平滑、可叠一条对比通道；窗口跟当前缩放（双击区段就是那一段）
或整场走。

**四条硬口径**，为的都是"别把一条画得很好看但其实错的谱交给车手"：

| 口径 | 为什么 |
| --- | --- |
| **按通道自己的采样率算** | 主时间基是 100 Hz，可悬架位移常常是 20 Hz 采的。拿主基去算，Nyquist 会写成 50 Hz，图上多出一整片**根本不存在**的高频。频率轴、分辨率、Nyquist 全部由该通道自己的采样率定 |
| **Nyquist 写在界面里** | 能分析到的最高频率就是 `采样率/2`，这句话必须出现在表头（默认分辨率 `采样率/点数` 也写出来），否则用户会以为 40 Hz 的峰是"看到了 40 Hz 的振动" |
| **点数吸收与补零都要说出来** | 点数不是 2 的幂就吸附（`clamp_points` 带 `notice`）；数据比点数短就补零，补零**不增加真实分辨率**，只让曲线好看——`notice` 里明说 |
| **NaN 先补再算，并且报出来** | FFT 遇到 NaN 会整段变 NaN。`fill_gaps` 用前值补齐并报出补了几个点，用户才知道这条谱里有插值 |

**算法在 Python 里、是不依赖框架的纯函数**：`spectrum.py` 只吃数组
（`welch()` / `window_values()` / `fill_gaps()` / `clamp_points()` / `_smooth()`），
取数在 `render.spectrum()`；界面不重算一遍。

```powershell
# 一次性复现（不起界面）：
python -c "import sys;sys.path.insert(0,'src');from i3pro import ld,render;log=ld.LogFile.read(r'i2pro_data\20260524-耐久正赛.ld');s=render.spectrum(log,'G Force Vert');print(s['sample_rate'],s['points'],round(s['resolution'],4),s['segments'],round(s['peak_frequency'],3),round(s['peak_value'],4))"

# 界面上：起服务 -> 「＋ 添加组件」选「频谱」-> 通道 / 对比 / 点数 / 窗 / 重叠 / 纵轴 / 窗口
.\i3pro.cmd serve --data i2pro_data --open
```

**通过判据**（`python -m unittest tests.test_i3pro.TestSpectrum -v`、`TestSpectrumOverHttp`，共 13 项）：

| 断言 | 位置 |
| --- | --- |
| 一条已知频率的正弦落进**正确的那一格**（频率轴不是"看着像"） | `TestSpectrum::test_a_sine_lands_in_the_right_bin` |
| Parseval：谱的总功率 = 时域方差（量纲与归一化都对） | `TestSpectrum::test_parseval_power_matches_the_variance` |
| hann / blackman 窗把泄漏压住：非整格频率的正弦，主瓣附近那 7 格的能量占比明显高于矩形窗（`hann > rectangular + 0.01`，blackman 更高） | `TestSpectrum::test_hann_window_holds_the_leakage_down` |
| 点数吸附到 2 的幂并**说明**；短数据补零并**说明** | `test_points_snap_to_a_power_of_two_and_say_so` / `test_short_data_is_zero_padded_and_said_out_loud` |
| 50 % 重叠真的多切了段：10000 样本 / 1024 点，10 段 → **19 段** | `TestSpectrum::test_overlapping_segments_average` |
| `amplitude` 纵轴 = 该频带的有效值（RMS），不是随手乘的系数 | `TestSpectrum::test_amplitude_scale_is_the_rms_of_the_band` |
| 频域平滑把峰压低但**总功率守恒** | `TestSpectrum::test_smoothing_lowers_the_peak_but_keeps_the_power` |
| NaN 补齐并报出补了几个点 | `TestSpectrum::test_nan_is_filled_and_reported` |
| 报错写下一步（通道不存在 / 窗不认识 / 点数不认识 / 重叠越界） | `TestSpectrum::test_bad_input_says_what_to_do_next` |
| 金标准场次按**通道自己的采样率**算（100 Hz 与 20 Hz 两条各验一遍） | `TestSpectrum::test_golden_sessions_use_the_channel_own_sample_rate` |
| 快照只内嵌勾选的那几条通道 | `TestSpectrum::test_snapshot_spectra_only_embed_what_was_selected` |
| HTTP：`/spectrum` 的参数、单位、400 的下一步 | `TestSpectrumOverHttp::test_spectrum_endpoint` |
| 界面：快照离线可用（一次 `/spectrum` 都不发）、点数/窗在快照里灰掉并说明、没有内嵌的通道**换一条带了的并写明画的是谁** | `tools/smoke_viewer.js` 第 28 组 |

**这一轮的实测数字**（两份金标准，都是这条命令跑出来的；默认 1024 点、hann 窗、50 % 重叠）：

| 场次 | 通道 | 采样率 | 分辨率 | 段数 | 主频 | 主频处 PSD |
| --- | --- | --- | --- | --- | --- | --- |
| `20260908-cjh 高避5圈` | `G Force Vert` | 100 Hz | 0.0977 Hz | 90 | 3.027 Hz | 0.0029 |
| `20260908-cjh 高避5圈` | `Vx KF` | 100 Hz | 0.0977 Hz | 90 | 0.098 Hz | 257.10 |
| `20260524-耐久正赛` | `G Force Vert` | 100 Hz | 0.0977 Hz | 379 | 4.883 Hz | 0.0010 |
| `20260524-耐久正赛` | `Vx KF` | 100 Hz | 0.0977 Hz | 379 | 0.098 Hz | 227.62 |

读法：车身垂向的主频在 3–5 Hz（正是悬架该关注的频段），而车速那条的能量压在最低那一格
（0.098 Hz = 分辨率本身，也就是"整场几乎没有周期性起伏"）——这两个数对得上，才说明频率轴
不是摆设。

**已知缺口**：快照里只内嵌**整场 + 默认参数**那一份（换点数、换窗、按区段都要 serve 模式，
表头会写明这一点）；点数上限 8192；不做倍频程 / 1/3 倍频程谱。

**这一轮的回归**：`Ran 174 tests` + `OK`；`verify_ld_vs_csv` PASS（`0 channel(s) outside
tolerance`）；两份金标准快照 `smoke_viewer.js` 均 PASS（7 圈 / 26 圈）；无头断言点 **308**；
`tools\verify_clicks.py` **28 项：28 通过，0 失败**。

---

## A36 · 横轴随缩放自适应（时间轴与距离轴）

原先横轴**永远四等分**：看整场时标签是 `0.0 / 115.9 / 231.9 / 347.8 / 463.7`，放大到 0.5 s
也还是四等分，标签成了 `231.75 / 231.88` 这种读不出来的数。现在按**当前可见区间**挑"整齐"的步长：

- **时间轴用钟表档位**：`1/2/5/10/15/30 s`、`1/2/5/10/15/30 min`（更长回到 1/2/5×10ⁿ）。
  为什么单列一张表：通用 1/2/5×10ⁿ 在 464 s 的整场上给的是 **50 s**，标签就成了
  `0:50 / 1:40 / 2:30`——能读，但不是读秒表的习惯（i2 Pro 给的是 `0:30 / 1:00 / 1:30`）。
- **距离轴仍用 1/2/5×10ⁿ**（米没有"整分钟"这回事）。
- 步长 ≥ 1 s 的时间标签印成 `mm:ss`，亚秒才给小数；刻度条数按画布宽度定（`plotW/110`，
  夹在 3–12 条之间），并在每个刻度上画竖网格线。

实测（真 Edge 渲染，截图 `out/shots/axis-whole.png` / `out/shots/axis-zoom.png`）：

| 视图 | 步长 | 标签 |
| --- | --- | --- |
| 整场 463.99 s | 60 s | `0:00 1:00 2:00 3:00 4:00 5:00 6:00 7:00` |
| 缩到 1 s（231.7–232.7） | 0.1 s | `231.70 231.80 … 232.70` |

**通过判据**（`node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"`，第 29 组）：
`niceTicks(231.7, 232.2, 8, true).step == 0.1`，刻度全部落在窗口内、且是步长的整数倍；
`niceTicks(0, 500, 8, true).step == 120`、`niceTicks(0, 300, 8, true).step == 60`、
距离轴仍取 100；**窗口逐档缩小（600 → 0.1 s 共 12 档）时步长单调不增**，且每一步都落在钟表档位表上；
`axisTickLabel(125, 5, 0, true) == "2:05"`、`3661 s` 配 60 s 档得 `"61:01"`（不许静默进位成小时）、
亚秒档印 `"231.80"`、距离轴不印时钟标签；最后断言画布上真的换了档。

**第四道回归（真 Edge 真画布，`tools\verify_clicks.py` 的 `#36` 组）**：canvas 里没有文字节点，
上面那张表此前**只有截图能证明**。这一组在真浏览器里截住每一次 `fillText`（钩子只装在这条
验收开的那个浏览器里，`addScriptToEvaluateOnNewDocument` 注入，页面本身没有被改），按
`(canvas, y)` 还原出一条条横轴，再把标签读回数值——断言的是**画出来的字符串**：

| 视图 | 画出来的步长 | 判据 |
| --- | --- | --- |
| 整场 463.99 s | 60 s | 全是钟点标签、落在钟表档位、相邻标签 ≥ 40 px、是步长的整数倍 |
| 1/4 场 | 15 s | 同上 |
| 20 s（100–120） | 2 s | 同上 |
| 1 s（231.7–232.7） | 0.1 s | 标签带小数、**不**印钟点 |

实测输出：`#36 缩得越窄，时间轴的档位只降不升（60.0 -> 15.0 -> 2.0 -> 0.1）`；距离轴那一组
读回 `1500 2000 2500 3000 3500 4000 4500`（步长 500，落在 1/2/5 档上），并且**不印钟点标签**。

顺带修掉一处脆的写法：`drawGrid` 原先靠"标签里含不含字母 `s`"猜这是不是时间轴，现在由调用方
显式传（`state.mode === "time"`）——距离轴的标签是 `m`，可横向通道名里带个 `s` 就会猜错，
那时候距离轴会印出 `0:50` 这种钟点。

---

## A37 · 注释组件（ticket #15）

i2 Pro 的 Notes：在数据上放一条带文字的标记（"这里换了刹车点"），复盘时一眼看到。
它和**信标**是两码事——信标是穿过一次线、会切圈；注释只是记号。

| # | 交付物 | 在哪 |
| --- | --- | --- |
| ① | 纯函数（校验 / 增删改 / 落点 / 侧车） | `src/i3pro/notes.py`（`normalize` / `add_note` / `update_note` / `remove_note` / `marks`） |
| ② | 单元测试 15 项 | `tests/test_i3pro.py` 的 `TestNotes`（13 项）与 `TestNotesOverHttp`（1 项，另有 1 项金标准实跑） |
| ③ | 无头交互断言（第 30 组） | `tools/smoke_viewer.js` |
| ④ | 本条 | `docs/ACCEPTANCE.md` |
| ⑤ | 两份金标准实跑 | `高避5圈`（7 圈）与 `耐久正赛`（26 圈）快照 smoke 均 PASS |

**通过判据**（可复制，在本机跑出来的）：

```powershell
python -m unittest discover -s tests -v      # Ran 189 tests + OK
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"   # PASS（第 30 组）
python tools\verify_clicks.py                # 40 项检查：40 通过，0 失败（#15 那 8 条）
```

实测（真 Edge，截图 `out/shots/verify-notes.png`）：

| 做了什么 | 结果 |
| --- | --- |
| 没设光标就按「＋ 注释」 | toast 说"先把鼠标移到图上"，侧车不落盘 |
| 真点图上 + 真点「＋ 注释」 | 侧车多一条，`time` = 231.74 s（就是光标处） |
| 真键盘改成「这里换了刹车点」+ 回车 | 侧车里那句就是它 |
| 那行字真的画出来了 | 捕到的真画布 `fillText` 里有 `这里换了刹车点` |
| `Esc` 取消 | 侧车一个字节没动（逐字节比对） |
| 加注释前后数圈速表 | **9 行 → 9 行**（注释不参与切圈） |
| 真点 ✕ | 侧车里那条没了 |

**位置怎么算**（为什么不是前端插值）：`notes.marks` 在**主采样序列**上按时刻插值出距离
（100 Hz 的格子，误差小于一个像素），轨迹图上取**最近的抽稀采样**（抽稀点之间隔着几米，
插值没有意义）。没有 GPS、或这条注释落在序列之外时给 `null`，界面就只画时间轴、不画轨迹，
不猜一个位置出来。

**边界（说清代价）**：

* 快照模式只能看不能改：按钮禁用，点它会说"用 serve 模式打开"——快照里没有服务可写。
* 文字上限 200 字、一场最多 500 条；写超了报错会带上"先删掉几条"这种下一步。
* 双圈对比模式下「＋ 注释」不加：那根距离轴对应两条圈，说不清落在哪一条。
* 注释**不进**圈速表、比圈与报表——它有自己的一张侧车文件，写坏了也不会动圈速一个数
  （`TestNotes.test_注释不参与切圈也不改报表` 就是钉这一条）。

---

## A38 · GPS 校正（ticket #14）

C125 的定位有三种坏法，都不是"接口声明"能看出来的，得在这批日志上量：

1. **掉星时给 `(0, 0)`**——不滤掉就等于把车放到几内亚湾，距离轴、轨迹与切圈一起被带歪。
   `高避5圈` 有 **638 个**（开头 12.76 s 一段），`20260912-TV0` 有 **32256 个**（占 44.9%）。
2. **卫星数不足**：`GPS Sats Used` 通道在有些场次是死的（`高避5圈` 恒 0、`TV0` 恒 −1），
   拿来当唯一判据会把整场判死，所以只在**采样数对得上**时才用它。
3. **跳点**：经纬度看着完全合法，但定位**整体跳到几百米外并留在那里**。这批数据里
   16 个场次有 **12 个**能测到（>200 km/h 的相邻跳变），最狠的是 `FSS_jhy_endu` 的
   **621 m / 105 s**、`高避陈君灏` 的 **495 m / 0.30 s**，黄金数据 `耐久正赛` 里也有一次
   **214.5 m / 0.05 s**（隐含 15443 km/h）。

**16 个场次里一个孤立毛刺都没有**，每一次都是"跳过去就不回来"。所以这一版**不删点**
（删哪一边都是猜），做的是**断开连线 + 标出来**：轨迹图不再画一条不存在的直线，
切圈也不会把一次跳变当成一次过门。

| # | 交付物 | 在哪 |
| --- | --- | --- |
| ① | 纯函数（标注 / 时移 / 分段插值 / 路径里程 / 侧车） | `src/i3pro/gpsfix.py`（`classify` / `annotate` / `correct` / `path_distance` / `resolve` / `load_config` / `save_config`） |
| ② | 单元测试 15 项 | `tests/test_i3pro.py` 的 `TestGpsFix`（14 项）与 `TestGpsFixOverHttp`（1 项，覆盖 GET / PUT / 侧车 / 400 / `.ld` 字节不变） |
| ③ | 无头交互断言（第 31 组） | `tools/smoke_viewer.js` |
| ④ | 本条 | `docs/ACCEPTANCE.md` |
| ⑤ | 两份金标准实跑 | `高避5圈`（638 个空定位 / 0 个跳点）与 `耐久正赛`（1 个跳点 / 0 个空定位）单测直接钉住，快照 smoke 均 PASS |

**通过判据**（可复制，在本机跑出来的）：

```powershell
python -m unittest discover -s tests -v                          # Ran 204 tests + OK（含 TestGpsFix 15 项）
python tools\verify_ld_vs_csv.py                                 # PASS - 0 channel(s) outside tolerance
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"          # PASS（第 31 组）
python tools\verify_clicks.py                                    # 51 项检查：51 通过，0 失败（#14 那 8 条）
python tools\verify_clicks.py --session "20260524-耐久正赛"       # 53 项检查：53 通过，0 失败（真跳点那一场多 2 条）
```

**三件事分开**，这是这一版的核心设计：

* **标注**永远算（`breaks` / `jumps` / `holes` / `dropped`），和开关无关——"这段数据不可信"
  本身就是结论，不该藏在某个开关后面。
* **修正**（时间偏移、插值到主采样率）只在 `enabled` 打开时动数值。
* **作用域**（`scope_track` / `scope_laps` / `scope_distance`）决定修正结果给谁用。

实测（真 Edge，`tools/verify_clicks.py` 的 #14 那 8 条）：

| 做了什么 | 结果 |
| --- | --- |
| 真鼠标点「启用校正」 | 复选框真的翻了（假 DOM 里 disabled 的控件也照样派发 click，这一步只有真浏览器算数） |
| 真点「应用」 | `高避5圈.gps.json` 出现，`enabled: true` |
| 应用之后 | 页面**真的重新载入**，新页面里 `i3pro.data.gps.config.enabled === true` |
| 真键盘打 `5`，再点「应用」 | 重载后轨迹第一点 **12.76 s → 17.76 s**，正好平移 5 秒（"效果在轨迹上可见"不是口号） |
| 面板那行字 | "已应用：时间偏移 5 s、保持原始采样、没有要断开的地方" |
| 没跳点/没空档的场次 | 页头**不**说"断开"、`breaks` 为空——阈值不是"总有东西可报" |
| 有跳点的场次（`耐久正赛`） | 页头写"断开 1 处"、`breaks=[1533]`，而且**被断开的那一段长度 214.4 m**——断的是幽灵线本身，不是它前面那 0.2 m 的正常线段 |

**真机截图抓到的一个真 bug**（值得单独记）：抽稀时把断点算在了"桶首"，于是
`breaks` 指向了跳变**前面**那一段，真正该断的 214 m 幽灵线照样画了出来——
无头断言当时是绿的（它验的是"断开处少画一段线"，而确实少画了一段，只是错的那段）。
是 `out/shots/gps-track-endurance.png` 里那条通向红点的蓝线露的馅。修法是
`render._downsample_breaks`：源下标 `i` 上的断点属于抽稀后的第 `(i-1)//step` 段，
现在单测与真浏览器断言各钉了一条（`test_downsample_keeps_the_break_on_the_right_segment`
与"被断开的那一段就是幽灵线本身"）。

**关闭校正 = 一个数都不动**（ticket 的硬条件）：`TestGpsFix.test_off_means_identical_numbers`
对 `time` / `x` / `y` / `lat` / `lon` 逐点 `array_equal`，HTTP 那条测试再验一次
`PUT enabled=false` 前后 `/track` 的 `x` / `time` / `breaks` 完全一致。

**量出来的代价与收益**：

| 项 | 数值（本机实测） |
| --- | --- |
| `耐久正赛` serve 模式整套 payload | **0.203 s**（其中区段自动切分 0.172 s、GPS 校正面板 0.002 s） |
| 单次 `gps_track`（38860 点） | 0.002 s |
| 距离轴换用 GPS 路径（`20260524-耐久正赛`，scope_distance 打开） | 速度积分 **19696.1 m** vs GPS 路径 **20215.2 m**，差 **2.64%** |
| 抽稀后仍保留的断点数（`TV0`，1500 点载荷） | 59 处（原始 112 处跳点 + 3 段空档，同一格内的合并） |

**边界（说清代价，也写清没做什么）**：

* **距离轴默认不跟着变**：圈速、区段、报表都建立在速度积分的距离轴上，换基准要用户
  自己点头。实测两条轴差 2.64%，换成 GPS 路径会让已存的区段边界（按米写的）对不上。
* **空档里不插值**：`resample` 是"按段插值"，段与段之间留一个时间跳变，宁可少画也不编。
* **跳点阈值 200 km/h** 是"车做不到"的物理界（这批日志最快 78.7 km/h）。实测它会连 GPS
  噪声一起报（`TV0` 报 112 处、中位只有 2.2 m）；要只看真错位就把阈值调到 800。
* **快照模式只能看**：能显示标注与断线（载荷里已经带上了），改不了——按钮禁用并说明原因。
* **不重采样到 100 Hz 之外**：主采样率就是这个场次自己的速率，没有选项。

---

## A39 · 通道接缝：数学通道与原生通道只差一处（ticket #18）

`CONTEXT.md` 那句话——数学通道「除此之外与原生通道完全一样」——以前在**五个地方**各写了一遍
（`derive.hold_to_master`、`store.build_table`、`render.trace`、`render.spectrum`、
`render.channel_index`），写法都是「派生就换采样率」。本项目因此出过两次真错：一条数学通道
和一条**慢**的原生通道同名时（本地定义覆盖原生通道）下游仍按原生那档再 `repeat`，曲线被整段
毁掉且不报错；频谱按原生采样率切窗口，慢通道的台阶被当成高频。

现在两类通道的差别只在 `src/i3pro/channels.py` 里实现一次：采样率、单位、保持因子、列放在哪。
调用方调一条接口拿元数据，不再需要问「这条是不是派生出来的」。

| # | 交付物 | 在哪 |
| --- | --- | --- |
| ① | 纯函数（`slot` / `names` / `units` / `is_derived` / `unit` / `sample_rate` / `hold_factor` / `info` / `attach` / `clear`） | `src/i3pro/channels.py` |
| ② | 单元测试 10 项 | `tests/test_i3pro.py` 的 `TestChannelSeam` |
| ③ | 无头交互断言 | **不适用**：这一票不改界面（没有新组件、新按钮、新状态），没有"点了才出现"的东西可断言。前端能看见的两项（通道索引里的 `derived` 角标与 `rate`）由 ② 的 `render.channel_index` 断言与既有的 smoke 数学通道那一组覆盖 |
| ④ | 本条 | `docs/ACCEPTANCE.md` |
| ⑤ | 两份金标准实跑 | `高避5圈`（437 条通道、169 条慢通道）与 `耐久正赛`：**逐条**核对保持因子与通道索引，每场另取 3 条（最慢的一条 + 第 1、6 条）与旧实现逐点 `array_equal`；两份快照 `smoke_viewer.js` 均 PASS |

**通过判据**（可复制，在本机跑出来的）：

```powershell
python -m unittest discover -s tests -v                     # Ran 217 tests + OK（含 TestChannelSeam 10 项）
python tools\verify_ld_vs_csv.py                            # PASS - 0 channel(s) outside tolerance
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"   # PASS
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"      # PASS
python tools\verify_clicks.py                               # 51 项检查：51 通过，0 失败
```

**三条验收判据对应到哪条断言**：

* **「派生就换采样率」的重写归零** → `test_the_rule_lives_in_exactly_one_module`。它不是测
  某个函数算得对，而是**扫源码**：`is_derived_channel`、`hasattr(session, "derived…`、
  `if derived else`、`if is_derived else` 这四种写法只要出现在 `channels.py` 之外就红。
  这一条是防止这条缝再被抄回去的（本项目已经有两次前科）。
* **挂载/卸载不再靠 `hasattr` 探测能力** → `maths.attach` / `maths.detach` 只调
  `channels.attach` / `channels.clear`；会话必须**显式声明** `derived_target`（放列的 dict）、
  `derived_names`、`derived_units`。没声明就报 `TypeError` 并写明下一步
  （`test_a_session_must_declare_where_derived_columns_go` 验错误文本，
  `test_both_real_session_types_declare_the_same_three_things` 验 `LogFile` 与 `CsvSession`
  都声明了这三样，`test_attach_and_detach_go_through_the_declaration` 验挂上/撤下的往返）。
  实测这条严格性**立刻抓到两个没声明的测试替身**（`_MathSession`、`_TableLog`），两个都补了声明——
  这正是「新加一种会话，忘了声明就立刻红」想要的效果。
* **场次里没有数学通道时行为逐点一致** → `test_a_session_without_maths_channels_is_unchanged_point_by_point`：
  在 `高避5圈`（437 条通道、主采样率 100 Hz、其中 **169 条**慢通道要 repeat）上，
  逐条比对保持因子与旧公式 `max(1, round(master / ch))`、逐条比对通道索引五元组
  （`name` / `unit` / `rate` / `samples` / `derived`），再对最慢的一条与第 1、6 条
  `np.testing.assert_array_equal` 逐点比对 `hold_to_master` 与旧实现。
* **同名覆盖原生慢通道那条已知坑** → `test_a_derived_channel_is_held_once_even_when_it_shadows_a_slow_channel`
  （保持因子 1、采样率 = 主采样率、单位取定义里的「圈」、通道索引只有一条），
  外加原有的 `test_a_derived_channel_that_shadows_a_slow_channel_is_kept_as_is` 继续盯着
  「曲线没被 repeat 毁掉」这个可见后果。
* **Parquet 也走同一条缝** → `test_a_derived_column_reaches_parquet_as_itself`：盖住慢原生通道的
  派生列写进 `store.build_table` 后与源列逐点相等（以前这里也有一份"派生就换采样率"的副本，
  但没有任何断言盯着"同名覆盖"的那种输入）。

**顺手改掉的一处不一致**：`render.spectrum` 给同名覆盖的派生通道回报的是**原生通道的单位**
（载荷里的 `unit` 取 `channel.unit`），现在与通道索引、图表一致，取定义里的单位。
两种情形下 `rate` 都已经是主采样率，所以频率轴没变，变的是那一栏单位文字。

**边界**：这一票是纯重构，界面、HTTP 端点、侧车格式、`.ld` 读取路径一个字节都没改；
`channels.hold_factor` 的语义与旧公式**逐点相同**（`max(1, round(目标采样率 / 通道采样率))`，
派生通道恒为 1），`store.build_table` 的 `--rate` 行为也没变（目标时间基可以不是主采样率，
数学通道仍只重复 1 次——那是它已经在主时间基上的意思，不是"跟着 `--rate` 走"）。

---

## A40 · 组件类型注册表（ticket #17）

架构评审选出的第一件事：在这个仓库里"再加一种显示形式"是**最高频的动作**，而它当时要改
七个地方——`viewer.html` 里散落着 **88 处** `type === "…"` 分派（本机实测
`rg -o 'type === "' … | Measure-Object`）。这一步先把**声明表**立起来，并把三类最简单的
形式（时间差 Δ / 状态与故障带 / 赛道轨迹）迁过去。**行为零变化**是硬要求：这一票不改任何
界面上看得见的东西，只改"它由谁说了算"。

| # | 交付物 | 在哪 |
| --- | --- | --- |
| ① | 注册表 + 通用分派（纯数据 + 取声明的小函数） | `src/i3pro/web/viewer.html` 的 `COMPONENT_TYPES` / `specOf` |
| ② | 单元测试 3 项 | `tests/test_i3pro.py` 的 `TestComponentRegistry` |
| ③ | 无头交互断言（第 32 组，**+18 个断言点**） | `tools/smoke_viewer.js` |
| ④ | 本条 | `docs/ACCEPTANCE.md` |
| ⑤ | 两份金标准实跑 | `高避5圈`（7 圈）与 `耐久正赛`（26 圈）快照 smoke 均 PASS |

**通过判据**（可复制，在本机跑出来的）：

```powershell
python -m unittest discover -s tests -v      # Ran 217 tests + OK
python tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"   # PASS（第 32 组）
python tools\smoke_viewer.js "out\20260524-耐久正赛.html"      # PASS（第 32 组）
rg -o 'type === "' src\i3pro\web\viewer.html | Measure-Object  # 73（这一票之前 88）
```

**一条声明里能说什么**（`COMPONENT_TYPES` 的字段；没写就是这种形式不需要那件事）：

| 字段 | 说什么 |
| --- | --- |
| `label` / `cols` / `rows` | 标题、默认宽度（网格列）与高度（行） |
| `tabular` | DOM 表格而不是 canvas（报表类） |
| `defaults(o)` | 默认配置 |
| `title(comp)` | 标题栏那行字；缺省用 `label` |
| `needs(comp, add)` | 这个组件要用到哪些通道（一次取数喂整张表） |
| `controls(comp, b, body)` | 控件条 |
| `hooks(comp, b)` | canvas 上的事件 |
| `refreshWindow(comp)` | 缩放窗口变了要不要重新取数：返回 Promise 或 null |
| `render(comp, b)` | 画 |
| `encode(comp)` / `decode(comp, payload)` | 分享链接里的紧凑载荷 |

**"行为不许变"是怎么证出来的**（第 32 组断言，逐条对应）：

| 迁移点 | 迁移前 | 迁移后 | 证据 |
| --- | --- | --- | --- |
| 渲染分派 | `renderComponents` 里六个 `else if` | 有 `render` 声明就走它，其余留过渡分支 | 三类形式在两条金标准上都画出来了 |
| 默认配置 | `makeComponent` 里的 `if (type === "track")` 两行 | `track` 声明 `defaults()` | 预设里的轨迹组件仍是"整场"（第 16b 组） |
| 缩放重取数 | `scheduleRefresh` 里判 `type === "track" && window === "zoom"` | `track` 声明 `refreshWindow()` | 窗口切换仍会去问 `/track`（第 16b 组） |
| 通道需求 | `neededChannels` 里 `else if (type === "status")` | `status` 声明 `needs()` | 状态通道仍随一次取数一起要来 |
| 控件条 | `buildWorksheet` 里整段 `if (type === "track")` | `track` 声明 `controls()` | 轨迹的"全程 / 当前时间段"下拉仍在（第 16b 组） |
| canvas 事件 | `if (type === "track")` 里挂 click | `track` 声明 `hooks()` | 点轨迹仍能放信标（第 21 组） |
| 分享链接 | `encodeLayout` 的三元链里 `c.type === "track"` | `track` 声明 `encode` / `decode` | 三类形式的往返都保住 config（第 32 组） |
| E 键 | 写死 `type === "status"` | `status` 声明 `hotkey: "e"`，按声明查 | 按 E 仍是加/去状态组件（第 10 组） |

**顺带修正的一处旧缺陷**：轨迹的分享链接载荷原来只有通道名，`window`（整场 / 当前时间段）
**进不了链接**——按 i2 Pro 的说法就是"分享出去的图少了一半设置"。现在载荷是
`通道名|窗口`，而**老链接（没有 `|`）照旧解成整场**，第 32 组专门钉了这条向后兼容。

**边界（说清代价）**：

* 迁移是**分批**的：图表类（图 / 散点 / 直方图 / 频谱，#19）与表格 + 仪表类
  （仪表 / 时间报告 / 通道报告 / GPS 面板，#20）还没迁，上面那 73 处 `type === "…"`
  里的大部分是它们。等 #20 收口，这张过渡表才该整段消失。
* 注册表里还有一条**只给验收用**的"只有标题（自检）"形式：它挂在
  `window.__I3PRO_SELFTEST__` 上，队员的浏览器里不会注册（无头驱动会设这个标记）。
  它存在的理由只有一个——证明"加一种显示形式"真的只要一条声明：
  第 32 组会把它加进工作表、读出它的标题与默认配置、走一遍分享链接往返，再拿掉。
* 这一票**不碰** Python，也不动任何界面上看得见的东西：验收里关于外观的部分
  （时间轴、圈速表、报表）仍然是它们自己那几条。
* 声明里的**字段名会继续演进**（#19 / #20 / #21 会把"缩放后要不要重新取数"这类
  字段收成统一的 `data`）。上面那张字段表记的是 #17 交付时的形状；单测与第 32 组
  断言只钉"结构"（每个类型都声明了 `render`、取数在它自己的声明块里说了），
  所以 #21 换写法时该改的是它，不该因为这条守卫变红。

---

## 全量回归

```powershell
python -m unittest discover -s tests -v      # 1. 单测：全部通过
python tools\verify_ld_vs_csv.py             # 2. 解析对照：0 channel(s) outside tolerance
node tools\smoke_viewer.js out\<场次>.html   # 3. 无头驱动前端：PASS
python tools\verify_clicks.py                # 4. 真 Edge 发真鼠标/键盘：全过（没有 Edge 的机器打印 SKIP，不算通过）
```

**通过判据**：`Ran 217 tests` + `OK`（无数据文件时相关用例自动 skip，不算失败）；
`PASS - 0 channel(s) outside tolerance`；`PASS - workbench ran headless ... interactions verified`；
`51 项检查：51 通过，0 失败`。**四条全绿才算改完**（AGENTS.md 规则 7）。

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
| `TestHistogram` | 直方图（10 项）：计数与格边界、门槛三种模式、着色取箱内均值、常量通道的区间撑开、NaN 分开报、格数夹取并说明、门槛可以是通道名或数学表达式、四种坏输入的下一步、金标准窗口统计 |
| `TestHistogramOverHttp` | `/histogram` 端点（1 项）：缺 `channel=` 400、通道不存在 400、半开区间、格数夹过带 `notice` |
| `TestSpectrum` | 频谱（12 项）：正弦落格、Parseval 与方差对上、hann/blackman 压泄漏、点数吸附到 2 的幂并说明、短数据补零并说明、50 % 重叠段数 10 → 19、`amplitude` 就是有效值、平滑压低峰但功率守恒、NaN 补齐并报数、坏输入的下一步、金标准按**通道自己的采样率**、快照只内嵌勾选的那些 |
| `TestSpectrumOverHttp` | `/spectrum` 端点（1 项）：参数一个不少、单位与采样率、400 带下一步 |
| `TestSectionsOverHttp` | 赛道区段走到 HTTP：GET 不落盘、重切落盘、手工改名字与边界、`edited` 立起来、被挡住的重切 400 + `needs_force` 且侧车不动、带 `force` 才覆盖、坏请求的下一步、`.ld` 字节不变 |
| `TestMathsOverHttp` | 数学通道走到 HTTP：存本地 / 全局、侧车落盘、坏表达式 400 且不动已存侧车、同名拦截、`shadowed`、试算接口、函数表 |
| `TestRender` | 静态/服务两种 payload、自包含性 |
| `TestServer` | HTTP 端到端：场次列表、工作台页、通道、时间窗、散点、概览、对比圈、赛道、404 |
| `TestIndependentParsers` | 第二套实现交叉验证、213 通道 CSV 全量对照 |
| `TestBeaconUndo` | 撤销的纯函数层：什么是"同一版"、什么时候没有可撤销的一步、交回去的是上一版本身 |
| `TestBeaconUndoOverHttp` | 撤销走真实 `PUT`：改名 / 插入 / 删除各自一步回到原样、`trusted` 迁移、落盘、一次无改动的保存不吃掉上一步、没有可撤销的一步时 400 并说明下一步、页面注入的 `laps_can_undo` 三态 |
| `TestViewerScript` | 无头驱动前端：脚本里 **372 个 `check(...)` 断言点**（`rg -o "check\(" tools/smoke_viewer.js | Measure-Object`）+ 时间轴 / 双圈两条渲染路径 + 直接打开模板的提示 |
| `TestComponentRegistry` | 组件类型注册表（#17，3 项）：已迁移的类型不再留 `type === "…"` 分派、三类形式各自声明该声明的东西、自检形式只挂在无头驱动的标记上 |
| `TestChannelSeam` | 通道接缝（#18，8 项）：那条规则只准写在一个模块里（扫源码）、会话必须显式声明三样、原生通道保留自己的采样率与单位、同名覆盖时保持因子/采样率/单位、Parquet 写出的是派生列本身、挂载与卸载走声明、金标准 437 条通道逐条与旧公式一致（无数学通道时逐点不变） |
| `TestNotes` / `TestNotesOverHttp` | 注释（#15，15 项）：文字折行与截断、时刻校验的下一步、增删改不改原表、距离在主采样上插值、轨迹取最近抽稀点、越界不猜位置、侧车往返与坏文件、**注释不动圈速表**、HTTP 的 PUT 落盘 / 400 说明下一步 / `.ld` 字节不变 |
| `TestGpsFix` / `TestGpsFixOverHttp` | GPS 校正（#14，15 项）：`(0,0)` 只计数不进轨迹、跳点与空档各自断开、跳变两端都算坏点、**关掉校正逐点不变**、按秒与按更新周期两种偏移、分段插值绝不跨空档、路径里程跳过跳变、距离轴作用域的开关、抽稀后断点必须落在**跨着跳变的那一段**上、参数校验的中文下一步、侧车往返与坏文件、金标准（耐久 1 个 214.5 m 跳点且断的就是那 214 m 幽灵线 / 高避 0 跳点 638 个空定位）；HTTP 的 GET / PUT / 落盘 / 400 不动侧车 / `.ld` 字节不变 |
| `TestLaunchers` | 一键启动：快照批量导出 + 索引页、缺数据目录的报错、端口占用自动换端口 |
