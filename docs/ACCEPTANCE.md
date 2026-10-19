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
* 声明里的**字段名会继续演进**：`refreshWindow` / `data` 这类字段在 #19 / #20 / #21
  里已经收成统一的 `data`（见 A41 / A42）。上面那张字段表记的是 #17 交付时的形状；
  单测与第 32 组断言只钉"结构"（每个类型都声明了 `render`、取数在它自己的声明块里
  说了），所以换写法时该改的是它，不该因为这条守卫变红。

---

## A41 · 组件类型注册表：全部显示形式迁入并收口（ticket #19 / #20）

**做了什么**：把剩下的显示形式（时间/距离图、散点、直方图、频谱、赛道轨迹、
仪表、时间报告、通道报告）全部迁进 `COMPONENT_TYPES`。迁完之后，工作表**只剩
10 处** `type === "..."`，且全部是语义判断（9 处"哪个组件是图"、1 处"哪一列是
文字列"），迁移前是 **80 处**。

每一种形式在表里自己声明：`defaults`（默认配置）/ `title` / `needs`（要哪些通道）/
`controls`（控件条）/ `hooks`（画布事件）/ `sync`（下拉填值）/ `onAdd`（刚加进来时
先看什么）/ `sidebar`（侧边栏通道清单归谁）/ `encode`+`decode`（分享链接）/
`render`（画）/ `data`（取数，见 A42）。工作表按这些声明装配，不再挨个类型判断。

**可复制命令与通过判据**：

```powershell
# 1. 类型清单只有一处：表里声明过的形式，数量与名字都钉住了
python -m unittest tests.test_i3pro.TestComponentRegistry -v
# -> Ran 6 tests / OK

# 2. 分派从 80 处收到 10 处，且只剩语义判断（单测里断言了这两件事）
rg -o '\.type === "|\.type !== "' src\i3pro\web\viewer.html | Measure-Object
# -> Count : 10

# 3. 五类图表形式的声明、分享链接往返、两张同类同屏互不干扰：无头驱动第 33 组
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"
# -> PASS - workbench ran headless (... interactions verified)
```

**这一票的实测数字**（都是上面命令跑出来的）：

| 事项 | 迁移前 | 迁移后 |
| --- | --- | --- |
| `type === "…"` 分派处数 | 80 | **10**（9 处"哪个是图" + 1 处"哪一列是文字列"） |
| 表里声明过的显示形式 | 3（Δ / 状态 / 轨迹） | **10**（全部） |
| 无头断言点 | 372 | **402** |
| 单测 | 217 | **220** |

**"加一种显示形式 = 一处声明 + 0 处分支"怎么复核**：无头驱动第 32 组会注册一个
只存在于注册表里的 `caption`（`window.__I3PRO_SELFTEST__` 才注册，队员的浏览器永远
看不到它），然后走完"加进工作表 → 标题 → 默认配置 → 渲染 → 分享链接往返"。
它要的代码只有表里那一条声明。

**这一票没做的**：`GPS 校正面板` 不在工作表里——它是侧边栏面板（`renderGps`），
不是一种"显示形式"，所以没有类型可迁。真正的显示形式全部迁完了。

---

## A42 · 组件取数收成一条路径（ticket #21）

**做了什么**：原来六条手工取数（`/trace` `/points` `/histogram` `/spectrum`
`/track` `/report`）各自拼 URL、各自记缓存键、各自写出错文案；散点是唯一把结果
放进**全表共用一格** `state.points` 的——两张散点同屏会互相顶掉。

现在：

* 地址只有一个出处 `apiUrl(route, params)`（空参数不写进 URL）；
* 请求只有一个出口 `apiGet(url)`（非 2xx 或正文带 `error` 都算失败，原因跟着异常走）；
* 每个组件的数据放在**它自己的槽**里（`bundle.data` / `bundle.data2`），键由它
  `data.key(comp)` 自己算，删掉组件时槽跟着没；
* 工作表重画与缩放都只调 `refreshComponentData`（键没变就是空转），
  `scheduleRefresh` 不再认类型。

**可复制命令与通过判据**：

```powershell
# 1. 脚本里没有第二条取数的路（六个端点都不许自己 fetch）
python -m unittest tests.test_i3pro.TestComponentRegistry.test_取数只有一条路 -v
# -> Ran 1 test / OK

# 2. 两张散点同屏互不顶掉 + 键由身份派生 + 删掉不残留：无头驱动第 33 组
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"
# -> PASS - workbench ran headless (... interactions verified)

# 3. 真浏览器里点一遍（散点 / 直方图 / 频谱 / 报表都在跑）
python tools\verify_clicks.py
# -> 51 项检查：51 通过，0 失败
```

**实测**：无头驱动第 33 组里，两张散点各塞 3 点与 5 点两份数据，重画后各自报
自己的点数（"3 点" / "5 点"），两边的缓存键不同；把它们从工作表里拿掉之后，
对应的数据槽读出来是 `undefined`（没有残留）。脚本里六个端点的
`fetch(… "/points" …)` 形态出现 **0 次**。

---

## A43 · 主时间基：一个场次的时间轴只有一处答案（ticket #22）

**做了什么**：新增 `src/i3pro/timebase.py`，`length(source, rate=None)` /
`axis(source, rate=None)` / `rate_of(source, rate=None)` 是"这条轴多长、步长多少"
唯一的那一处。删掉两份私有实现（`laps._master_time`、`maths._master_axis`），
`report.master_time` 留名但改成一行委托。`cli` / `derive` / `render` / `server` /
`laps` / `report` / `sections` / `store` 全部改从它取轴。

**实测数字**（都是下面命令跑出来的）：

| 事项 | 改动前 | 改动后 |
| --- | --- | --- |
| 票面那条命令命中的副本（`sample_rate` 写法） | 14 处 / 7 个模块 | **1 处**（`timebase.py`） |
| 同一条规则的**全部**写法（含 `store` / `sections` 把变量叫 `rate` 的两处） | 16 处 / 9 个模块 | **1 处** |
| `tests` 里自己再算一遍 | 7 处 | **0 处** |
| 高避 5 圈的主时间基 | 46400 点，末点 463.99 s，和 10764568.0 | 同上（逐点相同） |
| 耐久正赛的主时间基 | 194300 点，末点 1942.99 s，和 188761478.5 | 同上（逐点相同） |

两份金标准的数字是改动**前**用 `laps._master_time` / `report.master_time` 跑出来
冻进单测的；轴是 `arange(长度) / 步长`，所以"长度 + 首末点 + 和"对上就等于逐点相同
（`TestTimebase` 里三个数字都比对）。无头驱动跑两份金标准快照的画布统计
（1000083 / 1047584 条线段、762112 / 772137 个点矩形）改动前后一字不差。

**可复制命令与通过判据**：

```powershell
# 1. 规则只剩一处（src 1 行、tests 0 行，单测两件都钉住）
python -m unittest tests.test_i3pro.TestTimebase -v      # -> Ran 5 tests / OK
rg -n 'int\(round\(.*sample_rate.*\)\) \+ 1' src         # -> 1 行：src\i3pro\timebase.py
rg -n 'int\(round\(.*sample_rate.*\)\) \+ 1' tests       # -> 无输出

# 2. 两个出口取的是同一条轴 + --rate 换的是同一个答案
python -m unittest tests.test_i3pro.TestTimebase -v
# -> test_parquet_报表_取的是一条轴 ... ok
#    test_rate_换的是同一个答案 ... ok（50 Hz 时 Parquet 的 time 列 = timebase.axis(log, 50.0)）

# 3. 四道回归
python -m unittest discover -s tests -v                  # -> Ran 232 tests / OK
python tools\verify_ld_vs_csv.py                         # -> PASS - 0 channel(s) outside tolerance
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"  # -> 两条都 PASS
python tools\verify_clicks.py                            # -> 51 项检查：51 通过，0 失败
```

**关于 `--rate` 的一个如实说明**：`i3pro convert --rate 50` 会让 Parquet 的轴按
50 Hz 重建（`length(log, 50.0)` = 23201 点，末点 464.0 s）。这**不是**与画图那条轴
不一致，而是"同一个答案换了一档采样率"——`store.build_table` 与
`timebase.axis(log, 50.0)` 逐点相同（单测钉住）。默认（不给 `--rate`）时三处出口
逐点相同。

---

## A44 · 侧车文件：六遍写成一遍（ticket #16）

**做了什么**：新增 `src/i3pro/sidecar.py`。信标（`.laps.json`）、赛道区段
（`.sections.json`）、GPS 校正（`.gps.json`）、注释（`.notes.json`）、数学通道
（`.maths.json` + 仓库里的 `maths/global.json`）、CSV 列映射（`.map.json`）六种侧车
原先各自实现了一遍"文件放哪、缺了算什么、读坏了怎么办、写坏了算不算成功"——六份
接口、**四种**失败策略。现在这六件事只在一处回答：

| 情形 | 现在只有这一套做法 |
| --- | --- |
| 文件不在 | 返回该 kind 的空值（"还没设过"，不是错误） |
| 读不出来 | 抛 `sidecar.SidecarError`（带"修好它或删掉它"的下一步），**绝不删那个文件** |
| 顶层不是 JSON 对象/数组 | 同上，并说清该是什么 |
| 写 | 先落同目录临时文件再 `os.replace` 原子替换，写完以换行收尾 |

**一处行为改动（如实说明）**：`<场次>.notes.json` 原来"读坏了当空表"，现在和别的
侧车一样报错。理由是原来那个做法会让"图上看不见注释"和"文件坏了"长得一模一样——
用户补一条再一保存，旧的就永远没了。`.laps.json` 与 `.map.json` 同理。
`sections` / `gps` / `maths` 本来就是报错的，没变。

**可复制命令与通过判据**：

```powershell
# 1. 六个领域模块里不再有文件读写（拼路径 / 读 / 写只该出现在 sidecar.py）
python -m unittest tests.test_i3pro.TestSidecar -v
# -> Ran 7 tests / OK

# 2. 失败策略一套：缺了=空、读坏=报错且文件不动、写=原子
python -m unittest tests.test_i3pro.TestSidecar.test_读坏要报错且不删文件 -v
python -m unittest tests.test_i3pro.TestSidecar.test_写是原子的_不留临时文件 -v

# 3. 新加一种侧车只要一处登记（用一个只存在于测试里的 kind 证明）
python -m unittest tests.test_i3pro.TestSidecar.test_新加一种侧车只要一处登记 -v

# 4. 快照 / serve / 命令行三条路径读到的同一份侧车逐字节一致
python -m unittest tests.test_i3pro.TestSidecar.test_三条路径读到的侧车逐字节一致 -v

# 5. 四道回归
python -m unittest discover -s tests -v                  # -> Ran 232 tests / OK
python tools\verify_ld_vs_csv.py                         # -> PASS - 0 channel(s) outside tolerance
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"  # -> 两条都 PASS
python tools\verify_clicks.py                            # -> 51 项检查：51 通过，0 失败
```

**实测数字**：

| 事项 | 改动前 | 改动后 |
| --- | --- | --- |
| 实现了"路径 + 读 + 写"的模块 | 6 个 | **1 个**（`sidecar.py`） |
| 失败策略 | 4 种（信标/注释/CSV 映射沉默，区段/GPS/数学通道报错） | **1 种** |
| 六个模块里的 `read_text` / `write_text` / `json.load` / `json.dump` | 各若干 | **0 处** |

---

## A45 · 数据导出：范围 / 通道 / 采样 / CSV / Excel（ticket #23 / #24 / #27）

需求原话是"把**全部日志 / 当前视图 / 指定时间段或距离段**的数据，按**原始采样率或指定
采样率**导出成 CSV/Excel，以便在 Excel、Python、MATLAB 中继续分析或分享"。取数与落盘
**只有一处实现**：`src/i3pro/export.py`（`.xlsx` 的 OOXML 由 `src/i3pro/xlsx.py` 用标准库
`zipfile` 写出，**没有**新增运行期依赖）。界面上的「导出数据」面板、命令行 `i3pro export`、
HTTP `GET /api/session/<场次>/export` 三条路都调它，所以三处给的数必然是同一个答案。

| 范围 | 面板怎么填 | 发出去的参数 |
| --- | --- | --- |
| 全部日志 | 不填起止 | 不带 `from`/`to` |
| 当前视图 | 取当前时间/距离窗口（`lane()`） | `axis=time|distance&from&to` |
| 当前选中圈 | 取圈速表那一行的 `start_time`/`end_time` | 同上 |
| 光标 A–B | `min/max(state.datum, state.cursor)`；没放基准光标就提示"先按 D、再按空格" | 同上 |
| 指定时间段 | `12.5s`、`2026-09-14 12:34:56.789`，或**裸时钟** `12:35:10.123`（日期沿用场次那天） | `absolute=1` 时按场次头的 `log_date`+`log_time` 换算成相对秒 |
| 指定距离段 | `1200m`（填日期时间直接报错，不猜） | `axis=distance&from=1200&to=1850` |

通道：`channels=all` / `channels=selected&names=…`（侧边栏勾选，组头「全选」按组选），
数学通道由 `maths=0/1` 单独开关。采样：`rate=auto`（各通道保留自己的原始采样点，宽表以
时间并集为索引、缺失留空）或 `rate=1|5|10|20|50|100|200|500|<自定义>`（此时重采样方法
`linear` / `hold` / `nearest` / `mean` 四选一；**`mean` 只对降采样有意义，升采样时与
`linear` 等价**——文档、界面、实现三处写的是同一句）。主索引：`time_s`（相对秒）/
`timestamp`（绝对时间戳，见下）/ `distance_m`（米）。

**两份金标准实跑**（本机实测，`out/` 是 gitignore 的临时目录）：

| 场次 | 请求 | 结果 |
| --- | --- | --- |
| 高避5圈 | `--rate 10` 全部通道 CSV 宽表 | **4,641 行 × 438 列，6,820,857 B，1.9 s** |
| 高避5圈 | `--rate 10` 全部通道 Excel | **7,715,725 B，4.1 s**，1 张数据表 + 1 张元数据表 |
| 高避5圈 | `--rate 10` 全部通道 CSV 长表 | **2,027,573 行 × 4 列，54,076,769 B，5.3 s** |
| 高避5圈 | `--rate auto`（原始采样）全部通道 CSV 宽表 | **46,400 行 × 438 列，51,026,500 B**（169 条慢通道大半是空格） |
| 耐久正赛 | `--rate 10` 全部通道 CSV 宽表 | **19,431 行 × 343 列，23.0 MB，6.5 s** |

> 命令行与界面默认**含数学通道**（`maths/global.json` 里那 4 条：`总G` / `纵向加速度g` /
> `速度kmh` / `平滑纵向G`），所以 `i3pro export` 的预估值是 **442 列**（高避）与 **347 列**
> （耐久）；上面表里是 `--no-maths` 的 438 / 343 列。差的就是那 4 条——用
> `--estimate` 与 `--estimate --no-maths` 各跑一次就能复现这两个数。

**元数据**（`bundle=1` 时 CSV 会与 `metadata.json` 打成一个 zip；Excel 里是「元数据」
sheet，同一份内容）：日志文件名、场次、导出范围（人话 + 起止数字 + `bounds: 左闭右闭`）、
主索引与列名、采样率、重采样方法、行 × 列、通道来源、**每条通道的名字 / 单位 / 采样率 /
是不是数学通道**、`exported_at`（ISO 8601 带时区偏移），以及源头 `.ld` 的元数据。

**"预计行数 / 文件大小"对得上真文件**（需求 §5；预估常数就是按这张表标定的）：

| 出口 | 预估 | 实际 | 预估 ÷ 实际 |
| --- | --- | --- | --- |
| CSV 宽表（10 Hz） | 8,140,314 B | 6,820,857 B | **1.19** |
| Excel 宽表（10 Hz） | 8,140,314 B | 7,715,725 B | **1.06** |
| CSV 长表（10 Hz） | 56,772,044 B | 54,076,769 B | **1.05** |
| CSV 宽表（原始采样） | 81,375,400 B | 51,026,500 B | **1.59** |

分块写的代价（`TestExportPerformance` 自带的实测输出）：
`耐久正赛 200 s 窗口 × 40 通道 × rate=100` → **3.8–4.0 s（两次跑）、2.9 MB 文件、峰值分配 16 MB**
（不随"整场 × 全通道"线性涨，这是 `_CHUNK_CELLS` 分块的意义）。

**可复制命令与通过判据**：

```powershell
# 1. 范围 / 采样 / 输出形状 / 报错 / 预估：单测（含 CSV 与 Excel 两条出口）
python -m unittest tests.test_i3pro.TestExportRanges tests.test_i3pro.TestExportSampling tests.test_i3pro.TestExportFiles tests.test_i3pro.TestExportErrors tests.test_i3pro.TestExportEstimate tests.test_i3pro.TestExportTimestampIndex -v
# -> Ran 24 tests / OK

# 2. 界面拼出来的参数就是服务端认的那几个（smoke 里的「导出数据面板」那一组）
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"    # -> PASS

# 3. 真浏览器真鼠标：点开面板 -> 预估 -> 真下载一个文件（第 4 道回归的导出那几条）
python tools\verify_clicks.py                                 # -> 68 项检查：68 通过，0 失败

# 4. 两份金标准各真导一次（把 out 换成你自己的临时目录）
python -m i3pro export "i2pro_data\20260908-cjh 高避5圈.ld" --rate 10 --out out\hill.csv
python -m i3pro export "i2pro_data\20260524-耐久正赛.ld"    --rate 10 --out out\endu.csv
# -> 已导出 … 4641 行 × 438 列 / 19431 行 × 343 列
```

**这批断言钉住的是**：`rate=auto` 保留各通道原始采样点且**左闭右闭**（12.50 s 与 18.00 s
两个样本都在，逐点对上通道原始值）；绝对时间与相对秒导出**逐字节相同**；距离轴第一列是米、
不倒退、夹在范围内；统一采样率下 1 Hz 的 `Gear` 被拉到 10 Hz 后不留空格；`linear` / `hold`
在同一时刻给不同的数、`mean` 只对降采样有意义（升采样与 `linear` 逐点相同）；`plan` 的预计
行数 = 真导出的行数（宽表与长表各一遍）；CSV 是 UTF-8 带 BOM、pandas 读回来列名就是
`time_s` / `Vx KF [km/h]`；`bundle=1` 的 zip 里有 `<场次>.csv` 与 `metadata.json`；长表只
写有值的格子；`.xlsx` 用 **openpyxl** 读回来逐格比对（元数据 sheet + 数据 sheet、表头、
首行）；五类坏输入各自报出"下一步改什么"。

> **规则 1 的第 ② 条曾经漏过**：导出那 20 条单测最初只躺在 `out/_pending/test_23_pending.py`
> 里，而 `out/` 是 gitignore 的——"功能做完了"这句话当时**没有仓库内的证据**。现在它们进了
> `tests/test_i3pro.py`（`TestExport*`），`TestXlsxWriter` 也补上了（`xlsx.py` 的 docstring
> 早就写着"验证它的是 openpyxl 的 TestXlsxWriter"，而那个类当时并不存在）。

**这一票修掉的两个"静默导错"**（都不是崩溃，是给出另一段数据）：

1. **距离段被当成秒**：面板选「指定距离段」而主索引还停在「时间」时，`1200–1850` 会按
   `axis=time` 发出去——导出的是 **1200–1850 秒**那一段，一声不响。现在范围自己决定轴
   （`exportAxisOf`：距离段=米、时间段=秒），并且把主索引下拉锁到对应档位。无头那一组的两条
   断言（距离段 → `axis=distance`、时间段 → `axis=time`）钉住这件事。

   > **这条规则住在面板上，不在接口上**（实测，2026-09-14）：`range` 是面板的概念，
   > 它拼参数时就把它翻译成 `axis`，请求里**只有 `axis`**。所以直接照接口调
   > `range=distance&from=200&to=250&axis=time` 会**按秒**导出 200–250 s 那一段
   > （状态 200，不报错）；只有当这个范围本身超出场次时才会拿到
   > "本场是 0 – 463.990 秒"那句 400。要堵死这条只有两个办法：面板把 `range` 也发过来
   > 让接口对照，或者接口拒绝语义矛盾的两个参数——两条都要多一个参数/一处规则，
   > 现在**不值得**（UI 那条路已被冒烟断言钉住）。谁要是从脚本直接打这个接口，
   > 记住 `from`/`to` 的单位由 `axis` 决定，`range` 它不认识。
2. **统一采样率的右端不闭合**：10 Hz 导出 0–463.99 s 的场次，末行原来只到 **463.9**
   （`floor(span×rate)` 那个格点）。现在"左闭右闭"在格点之外补上终点本身，实测末行
   **463.99**（高避）/ **1942.99**（耐久）；代价是最后一段可能短一格，元数据里的
   `range.bounds` 写着「左闭右闭」。

**ticket #27：绝对时间主索引**（需求 §3「主索引：时间：相对秒 `time_s` 或绝对时间」）。
`index=timestamp` 是**同一根时间轴**的另一种写法：场次起点（`.ld` 头里的日期时间）+
相对秒，格式 `2026-09-08 15:48:32.000`。它只配时间轴——`axis=distance&index=timestamp`
是 400 并说"想写时间戳就写 axis=time&index=timestamp"，不是悄悄退回米。

```powershell
python -m unittest tests.test_i3pro.TestExportTimestampIndex -v     # -> Ran 7 tests / OK
python -m i3pro export "i2pro_data\20260908-cjh 高避5圈.ld" --names "Vx KF" --from 10 --to 10.2 --rate 10 --index timestamp --layout long --out out\stamp.csv
# -> 首行 timestamp,channel,value,unit；第二行 2026-09-08 15:48:32.000,Vx KF,-0.04,km/h
```

Excel 那条出口由 **openpyxl 当独立裁判**逐格验（`TestExportTimestampIndex` 里
`A1=timestamp`、`A2=2026-09-08 15:48:32.000` **是文本**、`B2` 是数字）；分表那段把
`xlsx.MAX_ROWS` 临时调到 100 跑真代码路径（300 行 → `数据1/数据2/数据3/数据4`，
99+99+99+3 一行不丢）。

**边界（说清代价，也写清没做什么）**：

* **绝对时间戳的起点精度是秒**：MoTeC 在头里只写 `dd/mm/yyyy hh:mm:ss`。所以这一列是
  "起点精确到秒 + 相对部分精确到毫秒"，不能当微秒级同步时钟用；这句话同时写进导出元数据的
  `timestamp.source_precision`，别让下一个人以为它比原始数据更准。
* **CSV 是 UTF-8 带 BOM**（`utf-8-sig`），不是纯 UTF-8：通道名是中文，Excel 双击打开靠 BOM
  才不乱码。用 `pandas.read_csv(..., encoding="utf-8-sig")` 或 `encoding="utf-8-sig"` 读回来。
* **Excel 一律宽表**（长表在表格里没法看），超 1,048,576 行自动分成 `数据1/数据2/…`，
  每张都带表头；列数超 Excel 上限时报错并说"改用 CSV"。
* **快照（离线 HTML）里导出是禁用的**：没有服务端可取数，按钮灰掉并写明"用「启动.bat」
  打开 serve 模式"。
* **取消不留垃圾（这条断言当场抓到一个真问题）**：下载走"先落临时文件、再按
  `Content-Length` 流式回传"。真浏览器验收里现在有一条**真取消**：点「导出」（整场 +
  原始采样 + 全通道）→ 按钮自己变成「取消」→ 再点一次 → 面板说"已取消"、没有文件落地、
  `%TEMP%\i3pro-export-*` 清空。**起先它是红的**：服务端要等自己把整份文件写完、发不出去
  之后才发现人走了（50 MB 的导出取消后临时目录还挂着十几秒），因为 `act_export` 是在写盘
  **之前**就返回 `Response` 的，根本不知道客户端走了。现在 HTTP 层把"客户端还在不在"
  （非阻塞 `select` + `recv(..., MSG_PEEK)` 看 EOF）做成 `Body.alive` 探针，导出每写一块
  问一次，不在了就 `ClientGone` → 立刻 `rmtree` → 499 安静收场
  （单测 `TestApiLayerWithoutASocket.test_客户端走了就早停并清掉临时目录`，
  结构守卫另外钉住 `alive=self._client_alive` 这根线不许被拆掉）。
  它是**尽力而为**：浏览器只是不读、并没有关连接时探针看不出来，那就退回"写完再删"。

---

## A46 · 服务端：769 行的请求闭包拆成三层（ticket #26）

`make_handler` 原来是一个 **767 行、25 个内嵌函数**的请求闭包："HTTP 怎么回"和"这个动作
算什么"写在一起；16 个动作里有 13 处用例各自起一个 `ThreadingHTTPServer`、各自抄一遍
`urlopen` + `json.loads`。

| 住在哪 | 管什么 | 行数 |
| --- | --- | --- |
| `src/i3pro/server.py`（改动前 1,274） | HTTP 管道：解析 URL、**按需**读请求体、发 `Response`；两张 HTML 页面 | **401** |
| `src/i3pro/api.py`（新） | `Response` / `Body` / `Api` / `_Call`；`_Call.ACTIONS` 是"动作名 → 方法名"的**一张表**，加一个动作 = 加一行 | **940** |
| `src/i3pro/library.py`（新） | 场次库：找文件、LRU 缓存、数学通道挂载、一级撤销槽 | **220** |

`make_handler` **767 → 136 行、25 → 0 个内嵌函数**（里面只剩 `class Handler` 的 12 个方法，
`ast` 数出来的 12 个是它们）；`SessionLibrary` / `bind` / `serve` 的名字与签名没变，
`cli.py`、`tests`、`tools` 里的调用方一行都没改。`library.py` 不 import `api` / `server`（无环）。

**测试侧也收成一处**：九组 `*OverHttp` 用例 + `TestServer` + `TestSidecar` 现在共用
`with http_session(场次) as http:` 一个夹具（`_Http` 上有 `get_json` / `put_json` /
`get_text` / `get_bytes` / `json` / `raw` / `page_payload`），实测 **14 个用例**用同一个夹具；
改之前这份文件里有 **13 处** `ThreadingHTTPServer(...)` 构造、**20 处**
`urllib.request.urlopen`，每个用例各抄一遍"起服务 + 拼 URL + 解 JSON + 记错误形状"。

```powershell
rg -n "ThreadingHTTPServer\(" tests\test_i3pro.py       # 2 行：1 行真构造（夹具里）+ 1 行在守卫用例的文档字符串里
rg -n "urllib\.request\.urlopen" tests\test_i3pro.py    # 9 行：7 行真调用（5 行在 _Http 上，2 行是 TestServer 故意判 400/404）+ 2 行文档字符串
rg -c "with http_session\(" tests\test_i3pro.py         # 14
```

另外钉住的：路由表里每个动作都有实现；动作清单就是那 17 个（#45 加了 `heatmap`）；`Api.handle` 对
`["sessions"]` / `["nope"]` / `["session","nope","info"]` 都**返回 `Response` 而不是 `None`**
（拆的过程中真的漏过两次 `return`）；未知动作的报错里带上"认得的动作"；不碰请求体的动作
**一次都没有读** `Body`（上传 100 MB 不会因为看一眼列表就先进内存）；`server.py` 少于 420 行、
没有 `if action == "`、没有 `def act_`、读 socket 只有 `_read_body` 一处。

**可复制命令与通过判据**：

```powershell
# 1. 动作表里每个动作都有实现、Api.handle 永远回 Response、没请求体的动作一次都不读 Body
python -m unittest tests.test_i3pro.TestApiLayerWithoutASocket tests.test_i3pro.TestStructureOfTheSplit -v
# -> Ran 16 tests / OK

# 3. 行为零变化：原先那些 HTTP 断言一条不减（九组 OverHttp 用例 + TestServer + TestSidecar）
python -m unittest tests.test_i3pro.TestServer tests.test_i3pro.TestBeaconEditingOverHttp tests.test_i3pro.TestBeaconUndoOverHttp tests.test_i3pro.TestSectionsOverHttp tests.test_i3pro.TestReportOverHttp tests.test_i3pro.TestNotesOverHttp tests.test_i3pro.TestGpsFixOverHttp tests.test_i3pro.TestHistogramOverHttp tests.test_i3pro.TestSpectrumOverHttp tests.test_i3pro.TestMathsOverHttp tests.test_i3pro.TestSidecar
# -> Ran 19 tests / OK

# 2. 结构本身（这两个数字是上面命令背后的事实）
rg -c "^def make_handler" src\i3pro\server.py      # -> 1
python -c "import sys; sys.path.insert(0, 'src'); from i3pro import api; print(len(api._Call.ACTIONS))"
# -> 17（动作数；与 api._Call.ACTIONS 的 17 条一一对应）
```

**实测数字**（改动前那份 `server.py` 从 `git show c7c72b8^:src/i3pro/server.py` 取出来数）：

| 事项 | 改动前 | 改动后 |
| --- | --- | --- |
| `server.py` 行数 | 1,274 | **401** |
| `make_handler` 行数 / 内嵌函数 | 767 / 25 | **136 / 12** |
| 动作分派 | `if action == "…"` 长链 | `api._Call.ACTIONS` 一张表（**17** 个动作，#45 起） |
| 不碰请求体的动作 | 也会读一次 `Body` | **一次都不读**（单测钉住） |
| 场次库 | 在 `server.py` 里 | `library.py`（**220** 行，不 import `api`/`server`） |

---

## A47 · `laps.py`：一个模块背四个概念 → 三个模块（ticket #25）

`laps.py` 原来 **1,140 行**，同时住着切圈算法、信标配置与编辑规则、距离轴换算与圈差、
圈表。四件事的改动理由完全不同（改切圈算法不该翻到信标改名那段），所以按概念拆开：

| 概念 | 现在住在哪 | 行数 |
| --- | --- | --- |
| 切圈（`detect_laps` / `gps_laps` / `winding_laps` / `figure8_laps` / `run_laps` / `lap_table`） | `laps.py` | **741**（改动前 1,140） |
| 信标与配置（`Beacon` / `LapConfig` / 改名与插入规则 / `undo_config` / 侧车读写） | `beacons.py`（新） | **379** |
| 距离轴换算与圈差（`time_at_distance` / `distance_on_master` / `overlay` / `time_delta`） | `axes.py`（新） | **133** |

老名字仍然可用，而且是**同一个对象**（`laps.LapConfig is beacons.LapConfig`、
`laps.Beacon is beacons.Beacon`、`laps.overlay is axes.overlay`、
`laps.time_at_distance is axes.time_at_distance`、`laps.load_config is beacons.load_config`），
`render.py` / `cli.py` / `server.py` / `tests` 一行都不用改——这是这一票"行为零变化"的判据之一。

**可复制命令与通过判据**：

```powershell
python -m unittest tests.test_i3pro.TestStructureOfTheSplit -v      # -> Ran 11 tests / OK
# 断言的是：beacons.py 里有 Beacon/LapConfig/reconcile_edits/check_new_crossings/undo_config/
# load_config/save_config/unique_name/merge_crossings；axes.py 里有 overlay/time_delta/
# time_at_distance/distance_on_master；laps.py 里这些定义**一个都没有**（只有 import）且少于
# 800 行；beacons.py / axes.py 运行时不 import laps（类型标注走 TYPE_CHECKING，不成环）。
```

行为不变由既有的功能用例保证：切圈 7 圈 / 26 段、圈速表、双圈叠加、圈差全部不变
（两份金标准快照的无头统计与拆分前逐字相同）。

---

## A48 · 数学通道的说明（`note`）不被面板保存抹掉（ticket #29）

`maths/global.json` 里的 `note` 是**手写在文件里的说明**（这条通道为什么这么算），
界面上没有编辑入口。面板保存走的是"整份替换"——前端把当前生效的定义收集成一个数组
PUT 上去。原先那次收集只带 `{name, expr, unit}`，于是**在面板里存一次，仓库里手写的
说明就永久没了**：真发生过，工作区那份 `global.json` 的 4 条说明全被抹掉。

规则只有两条，都住在 `viewer.html` 的 `mathsSaveList(draft)` 里：

| 存的是 | `note` 怎么办 |
| --- | --- |
| **正在编辑的那一条** | 带上它**原来**的说明（改表达式不该顺手删掉说明） |
| **别的定义** | 原样带走，一个字节都不动 |

**可复制命令与通过判据**：

```powershell
# 1. 渲染快照：快照里内嵌的就是这一步要验的那份 viewer.html
python -m i3pro snapshot --data i2pro_data --out out

# 2. 两份金标准快照都必须 PASS
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"

# 3. 变异检查：把那两处"带上 note"删掉，断言必须立刻变红
#    （一条永远不会红的断言等于没有断言）
$t = Get-Content -Raw "out\20260908-cjh 高避5圈.html"
$m = $t -replace 'unit: d\.unit \|\| "", note: d\.note \|\| ""', 'unit: d.unit || ""'
$m = $m -replace 'unit: draft\.unit,\s+note: \(original && original\.note\) \|\| draft\.note \|\| ""', 'unit: draft.unit'
Set-Content "out\_mut_note.html" -Value $m -NoNewline -Encoding UTF8
node tools\smoke_viewer.js out\_mut_note.html     # 期望：退出码 1，两条断言都红
```

**实测（2026-09-17，源码取 `baa9447`、快照由这一版渲染）**：

| 检查 | 结果 |
| --- | --- |
| 高避5圈 | `PASS`（1000083 线段 / 762112 点矩形 / 7 圈 / 471 通道行） |
| 耐久正赛 | `PASS`（1047584 线段 / 772137 点矩形 / 26 圈 / 377 通道行） |
| 变异检查 | 退出码 **1**，两条都红：`saving wiped the note of the definition being edited`、`saving another definition wiped a neighbour's note`；失败信息里打出的载荷只剩 `{"name":…,"expr":…,"unit":…}`，正好是"说明被抹掉"的那一份 |

断言在 `tools/smoke_viewer.js`（数学编辑器那一段，"说明文字（`note`）…"注释下面）：
先用 `{甲(带说明), 乙}` 验"改自己那条"，再用 `{甲, 乙}` → `{丙}` 验"存别人那条"，
两次都检查载荷里 `甲` 的说明还在。

---

## A49 · 工作表是一个文件：`worksheets/*.json`（ticket #30）

那 7 套工作表（分析 / 对比 / 动力 / 底盘 / 车手 / 仪表台 / 报表）原先硬编码在
`viewer.html` 里的一段 `presetDefinitions()`：加一套要改前端，**没人能把自己拖好的
布局存下来**，也发不给队友。现在一个 `.json` 一套，随仓库走。

规矩只有三条，都写进了 [`worksheets/README.md`](../worksheets/README.md)：

| 规矩 | 为什么 |
| --- | --- |
| **文件名是身份**（`analysis.json`）、`name` 是按钮上的字（`分析`） | 身份要能进 URL（#33 的增删改要用），按钮上的字要能是中文 |
| **挑通道一律走 `pick`**，不写死通道名 | 换一个场次通道可能叫别的名字或干脆没有。`pick` 是"我要这几类通道"，用本场次真实存在的通道去填；`{"limit": 5, "index": 4}` = 先收 5 条取第 5 条，一串规则 = 兜底链，找不到就是 `null`（**不乱指一条**） |
| **读不出来的文件只跳过它自己**，并且说清是哪个文件、下一步做什么 | 一份文件写坏不该让整个工作台空白 |

`src/i3pro/worksheets.py` 只管文件（找 / 读 / 校验 / 排序），**一个组件类型名都不写死**
——那张表在前端 `COMPONENT_TYPES` 里，是全仓库唯一的一份。所以"文件里写了一个不存在的
显示形式"这件事由前端在装配时报出来（`#sheetNote` 那一行字），不是在这里猜。

布局记忆也跟着改了：**按场次存**（`i3pro.worksheet.v2:<场次>`）。以前是一格全场共用，
在耐久赛里拖好的布局会跟着你走进高避。老键（`i3pro.components.v1`）只在某个场次还没有
自己那一份时当缺省读一次，**不再往里写**。

**可复制命令与通过判据**：

```powershell
# 1. 文件这一层（不需要数据文件）
python -m unittest tests.test_i3pro.TestWorksheets -v      # -> Ran 12 tests / OK

# 2. 界面这一层：两份金标准快照都必须 PASS
python -m i3pro render "i2pro_data\20260908-cjh 高避5圈.ld"
python -m i3pro render "i2pro_data\20260524-耐久正赛.ld"
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"

# 3. 真浏览器：按钮真的点得到（这一组 6 条）
python tools\verify_clicks.py

# 4. 想自己看一份工作表被解析成了什么（迁移时就是靠它比对搬前搬后的）
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html" --dump-worksheets
```

**实测（2026-09-17）**：

| 检查 | 结果 |
| --- | --- |
| 七份文件 | `analysis 分析 / compare 对比 / powertrain 动力 / chassis 底盘 / driver 车手 / dash 仪表台 / report 报表`，`order` 10…70，`problems = []` |
| **搬前搬后逐字段一致** | 高避那一场上 7 套工作表的**切过去之后的完整状态**（每个组件的类型 / 位置 / 尺寸 / config）与硬编码那版**逐个字段相同**——夹具 `tests/fixtures/worksheets_before.json` 是搬之前用 `--dump-worksheets` 倒出来的 |
| 高避5圈快照 | `PASS`（1083954 线段 / 894545 点矩形 / 7 圈 / 471 通道行） |
| 耐久正赛快照 | `PASS`（1142204 线段 / 942681 点矩形 / 26 圈 / 377 通道行） |
| 真浏览器（#30 六条） | 按钮 = 文件（7 个、顺序一致）· 工作表那一栏没有字 · 真点最后一套 → 换成那 3 个组件 · 记忆键 = `i3pro.worksheet.v2:20260908-cjh 高避5圈` · 记下来的是刚切过去的那套 · **不再往旧的全场共用键里写** · 切回第一套组件数不变 |
| 全量回归 | `Ran 300 tests` + `OK`；`verify_ld_vs_csv` PASS；两份 smoke PASS；`verify_clicks` **75 项检查：75 通过，0 失败** |

**一处要说明的数字变化**：无头驱动最后那行"线段 / 点矩形"是**从打开页面起累加**的计数器，
而 #30 的断言组会把这 7 套工作表逐个切一遍（这正是"每份文件都装得出来"的证据），所以
高避那行从 1000118 涨到 1083954。**行为没变**这件事由上面那条"搬前搬后逐字段一致"钉住，
不是由这行计数钉住的。

断言落在两处：文件层在 `tests/test_i3pro.py` 的 `TestWorksheets`（12 项），界面层在
`tools/smoke_viewer.js` 的「工作表（ticket #30）」那一组；"点得到"这件事在
`tools/verify_clicks.py` 的 `Checker.worksheets()`（真鼠标点按钮、真读 `localStorage`）。

顺手加的一个开发工具：`node tools/smoke_viewer.js <快照> --dump-worksheets` 把每套工作表
切过去之后的状态倒成 JSON——迁移时用它比对，以后改工作表格式也能用它看"到底变成了什么"。

---

## 全量回归

```powershell
python -m unittest discover -s tests -v      # 1. 单测：全部通过
python tools\verify_ld_vs_csv.py             # 2. 解析对照：0 channel(s) outside tolerance
node tools\smoke_viewer.js out\<场次>.html   # 3. 无头驱动前端：PASS
python tools\verify_clicks.py                # 4. 真 Edge 发真鼠标/键盘：全过（没有 Edge 的机器打印 SKIP，不算通过）
```

**通过判据**：`Ran 300 tests` + `OK`（无数据文件时相关用例自动 skip，不算失败）；
`PASS - 0 channel(s) outside tolerance`；`PASS - workbench ran headless ... interactions verified`；
`75 项检查：75 通过，0 失败`。**四条全绿才算改完**（AGENTS.md 规则 7）。

上面各条验收里出现的"当时的结果"（`Ran 232 tests` / `51 项检查` 之类）是**那一轮**的
实测记录，不是当前的数字；当前的数字只认这一节。每加一条验收条目就把这一节改一次。

**别并行跑两个测试进程**：`out/_test_data/` 是固定的，两边同时写同一份副本时
`shutil.copy2` 会撞上 `WinError 1224`（目标文件正被映射着）。实测过：两条
`python -m unittest` 同时起，第二条在导入阶段就报这个错；串行跑两次都是
`Ran 300 tests / OK`。这不是用例的错，是"副本目录只有一份"的代价。

**金标准场次先复制再读**：`tests/test_i3pro.py` 在导入时把两份金标准场次复制到
`out/_test_data/`（实测各 1 份、共 148.2 MB，复制 0.08 s；该目录每轮先清空），
测试只碰这份副本。侧车（信标 / 区段 / 注释 / GPS / 数学通道 / CSV 列映射）是
**用户资产**，就躺在 `i2pro_data` 里，队员在浏览器里给金标准场次改一次信标就会改变
测试读到的配置——实测 `20260908-cjh 高避5圈.laps.json` 里多一个 GPS 信标之后，
`TestSections.test_the_golden_hill_lap_splits_into_corners_and_straights` 与
`TestServer.test_http_api_end_to_end` 两条当场变红（后者 `/overlay` 报 500）。
复制之后两条都绿，且 HTTP 用例写的侧车也落在副本里，车队数据一个字节不动。

## A50 · DBC 解析器：读定义 + 按位解码（ticket #37）

原始 CAN 帧表要变成通道，先得有一份"这台车的 CAN 布局"。DBC 就是那份定义，
`src/i3pro/dbc.py` 把它读成报文 / 信号，并负责**按信号的位定义把一帧字节解成物理量**。
它是纯函数：读文本拿到库，拿库和字节拿到数值，不认识"日志"这个词。

为什么手写而不用 `cantools`：运行期只允许 numpy / pandas / pyarrow（规则 4），
而 `cantools` 会拉进 bitstruct 一串依赖。它在本项目里是**测试期的独立裁判**：
同一批真实帧、同一批随机载荷，两边逐信号对拍（规则 4 豁免测试工具）。

三处写错了不会报错的地方，都钉死了：

| 坑 | 判据 |
| --- | --- |
| **字节序** | `@0` 的起始位是最高位、字节内位号 0 是最低位，之后位号递减、到字节边界跳到下个字节的位 7；`@1` 的起始位是最低位、位号递增。合成用例里 16 位 `@0` 落在起始位 7 上，值必须是 `0x4A97`——**按位反转的 `0x52E9` 是错的**（实测两份 DBC 的 46 条信号全是 `@0`） |
| **DLC 不算数** | 用日志里那一帧**实际的字节数**。实测 `0x66D`：DBC 写 4、日志发 8。载荷短了要吵（"只有 2 字节"），长了照解 |
| **多路复用** | 信号带 `M` / `m<n>` 时要**明确报错**并给下一步，绝不按普通信号瞎解（那是"把别路的字节当成自己的值"） |

另外三件小事：`0xC0000000 VECTOR__INDEPENDENT_SIG_MSG` 是 Vector 的伪报文，
解析时就丢掉（实测盲解会多出 5 条永远为空的通道，而且它们会挂到上一条真实报文上）；
`VAL_` 值表带着自己的报文 ID（不能拿"当前报文"当归属）；标准帧与扩展帧按
`(extended, id)` 分开存，不会互相盖住。

**可复制的命令与判据**

```powershell
# 1. 解析器本身（13 项，含上面三个坑的合成用例）
python -m unittest tests.test_i3pro.TestDbc -v            # -> Ran 13 tests / OK

# 2. 独立裁判：cantools 解同一批真实帧 / 同一批随机载荷，逐信号一致
python -m unittest tests.test_i3pro.TestDbc.test_cantools_agrees_signal_by_signal_on_real_frames -v
python -m unittest tests.test_i3pro.TestDbc.test_cantools_agrees_on_random_payloads_for_every_signal -v

# 3. 没装 cantools 的机器上这两条自动 skip——那**不算通过**
```

**实测（2026-10-04）**

| 项 | 结果 |
| --- | --- |
| `Sensors.dbc` | 17 条报文 / 56 条信号 / 丢掉 1 条伪报文；全是标准帧、无复用 |
| E27 dashboard DBC | 63 条报文 / 180 条信号（175 条有符号）；**全是扩展 ID `0x9D22xxxx`** |
| 与 `cantools` 对拍（真实帧） | 11.3 万帧 / 45 条信号，最大差 **0.0** |
| 与 `cantools` 对拍（随机载荷） | 540 个信号值，0 处不一致 |
| 日志里真实存在的信号 | `Sensors.dbc` 的 14 条报文 / **46 条信号**（与 #38 解出的通道数一致） |

---

## A51 · 原始 CAN 帧表 → 场次（ticket #38）

记录仪写出来的 `.csv` 长这样（GBK、CRLF，第 2 列被 Excel 写坏成 `="17:54:13.761949`，
**相对时间在第 3 列**，数据列是 `x| 5a 64 46 23 55 00 7f 00`）：

```
序号,系统时间,时间标识,CAN通道,ID号,帧类型,帧格式,CAN类型,长度,数据
1000000,="17:54:13.761949,385.624895,ch1,0x27,数据帧,标准帧,CAN,8,x| 01 01 ...
```

`src/i3pro/canlog.py` 把它读成与 `.ld` **同形状**的场次：进侧边栏，能画图、散点、
直方图、频谱、加数学通道、导出。三条硬约束：

1. **导入时零阶保持到主时间基**（默认 100 Hz）。CAN 的更新率（11.04 / 16.8 / 29.85 /
   42.5 / 43.0 / 47.83 / 66.6 / 69.0 / 75.0 / 76.25 Hz）**没有一个能整除 100 Hz**，
   交给 `channels.hold_factor` 会把 69.03 Hz 当成 100 Hz（数据只铺满时间轴前 69%）、
   47.83 Hz 当成 50 Hz——静默错位，不报错。
2. **真实更新率进元数据**：`Channel.update_rate`，界面通道列表显示的是它（43.5 Hz 的
   胎温不写成 100 Hz），而 `sample_rate` 仍是主采样率（下游按它算保持因子）。
3. **DBC 用哪一份要能复核**：报告里列候选与各自覆盖的帧数、写进侧车的 DBC 名与
   sha256；没有 DBC 覆盖的 ID 一个都不生成字节通道。

入口只有一处：`csvlog.open_session()` 按**表头特征**分流——认得出是帧表就走这里，
认不出就当普通通道表。这样侧边栏、`i3pro info`、快照、导入预览全都自动支持。

**可复制的命令与判据**

```powershell
python -m unittest tests.test_i3pro.TestCanLog -v      # -> Ran 9 tests / OK

# 命令行看一眼（帧数 / 覆盖率 / 读不懂的 ID）
python -m i3pro info "can_data\2026_10_03_201147_ID0001.csv"

# 快照 + 无头断言（**有距离轴但没有圈**——见 A53：跑起来的 CAN 日志拿 Vx_KF
# 积出了距离轴。驱动按 hasLaps 让过"按圈算"的块，只断言它自己该有的东西）
python -c "from i3pro import csvlog, render; render.render_html(csvlog.open_session('can_data/2026_10_03_201147_ID0001.csv'), 'out/can.html', worksheets_dir='.')"
node tools\smoke_viewer.js out\can.html                # -> PASS（含 CAN 报告那一块）
```

**实测（2026-10-04）**

| 项 | 结果 |
| --- | --- |
| 单份 91 MB 帧表 | **1.3 s**（票面判据 ≤ 5 s；先按 11.4 s 写出来，把列下标提到循环外 + 只对候选 ID 解析 + 向量化解码之后降下来的） |
| 9 份（3,684,850 帧 / 43 个 ID） | **5.0 s** 解成 7 个场次（票面判据 ≤ 20 s） |
| 解出的通道 | **46 条**（`Sensors.dbc` 覆盖 24.6% 的帧 = 904,825 帧） |
| 未定义的帧 | 75.4%，报告里按 ID 列表（帧数 / 实测帧率 / 前 8 字节样例） |
| 诊断流量 | `0x7E0 0x7E2 0x7E3 0x7E4 0x7E6 0x7E7` 各自标为"诊断流量（OBD/UDS）" |
| 距离轴 | 当时（只有 `Sensors.dbc`）**没有**车速，所以没有距离轴：状态行与导入报告都写明，导出面板把"指定距离段 / 距离轴"禁用并回落到"全部 / 时间轴"。**这条已被 A53 取代**：13 份 DBC 取并集之后 `Vx_KF` 就是车速，跑起来的日志有距离轴 |
| 外部格式 | 列角色可被 `<场次>.can.json` 覆盖（换一个 CAN 工具导出时不用改代码） |
| 覆盖 0 帧的那份为什么 | 报告不只说"布局对不上"，还点出原因：那份 dashboard DBC **63 个报文全是扩展帧**（`0x9D220000–0x9D22003E`，按 DBC 文件里的写法印），而这批日志里**一帧扩展帧都没有**——不是布局不同，是记录仪没接那条总线（ticket #38 的验收点名了这条） |

---

## A52 · 连续记录并场 + 未定义 ID 的可见性（ticket #39）

记录器在**恰好 1,000,000 帧**处切文件，所以 9 份其实是 **7 次记录**。并场的判据是
**两个时钟同时接上**：墙钟差在 1 s 之内，且"相对时钟的增量"与它一致（50 ms 之内）。
只看墙钟会把相隔几分钟的两段并到一起；只看相对时钟会把"归零重开"的并到一起。

实测的两次合并：

```
2026_10_03_173936_ID0001.csv 接在 2026_10_03_173345_ID0001.csv 后面：墙钟差 0.026 ms
2026_10_03_175413_ID0001.csv 接在 2026_10_03_174748_ID0001.csv 后面：墙钟差 0.249 ms
```

场次库里**每次记录一个条目**（`…+1` 表示它并了后面 1 份），点进去看到的是合并后的
一场。界面上抬头多一个 **CAN 导入报告** 入口：帧数 / 覆盖率 / 用了哪份 DBC（含 sha256）/
候选 DBC 各自覆盖多少帧 / 并场依据 / 读不懂的 ID 表（ID、帧数、帧率、类型、样例字节）。
报告与界面读的是**同一份**数字（Python 算好放进 `DATA.meta.can`），不许两边各算一遍。

**可复制的命令与判据**

```powershell
python -m unittest tests.test_i3pro.TestCanLog -v      # 9 项里含并场与列表两条
python -m unittest tests.test_i3pro.TestCanLog.test_contiguous_recordings_merge_into_seven -v
python -m unittest tests.test_i3pro.TestCanLog.test_the_library_shows_one_entry_per_recording -v
node tools\smoke_viewer.js out\can.html                # 逐条断言报告 / 表格 / 关闭按钮
```

**实测（2026-10-04）**

| 项 | 结果 |
| --- | --- |
| 自动并场 | 9 份 → **7 个场次**（2 处各并 2 份），侧边栏 7 个条目（其中 2 个带 `+1`） |
| 关掉并场 | 一个文件一场 = **9 个场次**（同一批文件，逐份导入的帧数加起来仍是 3,684,850） |
| 非连续的几份硬拼 | **报错**并说下一步（墙钟差与相对时钟差对不上），不铺平线 |
| 报告与界面一致 | 表里的行数 = 报告里的条数（无头断言直接比这两个数） |
| 场次列表缓存 | 第一次 6.6 s（要解码 7 个 CAN 场次），第二次 **5 ms**（按文件 + 信标侧车的指纹缓存） |

---

## A53 · 多份 DBC 取并集 + 车速 → 距离轴（ticket #40）

这台车把 CAN 布局拆成了 **13 份小 DBC**（仪表一份、每个 ECU 一份），所以"一份日志
配好几份 DBC"是常态。原先的实现**只挑覆盖帧数最多的那一份**：它选 `Sensors.dbc`，
**46 条通道 / 覆盖 24.6% / 没有车速**——用户后来补的 11 份 DBC 一条都不参与解码，
而且界面上看不出少了什么。现在按**并集**解：91 条报文、**61 条通道**、覆盖
**1,600,064 / 3,684,850 帧 = 43.42%**。

并集必须回答"这条通道是从哪来的"：报告里每条通道带 `dbc` 字段，每份 DBC 带
文件名 + **sha256** + 覆盖帧数 + 归属 ID + 贡献通道数。同一条 ID 被两份定义成**不同
样子**时不静默合并——按各份的覆盖帧数选一份（帧数相同时按文件名定序，保证同样的输入
给同样的结果），并把冲突写进报告与界面；想固定成一份就把侧车的 `dbc` 写成文件名、
把 `dbc_mode` 设成 `"file"`（旧侧车里那条自动写的 `dbc` 不认，按并集解并在报告里说明）。

**距离轴是算出来的，不是写死的**：`0xC1 Throttle_INFO.Vx_KF`（`TH.dbc`，实测
24–62 km/h）就是 `.ld` 里那条 `Vx KF`。所以 CAN 场次现在**有距离轴**（速度积分，
实测 0.1–5546.8 m），切圈仍然靠手工信标（没有 GPS）。反过来说，只有几十秒、车没动的
日志里那条车速是**死的**（实测 `201147` 那份是 −0.05…0.00 kph），那种场次照旧没有
距离轴——报告分别说清"没有这条通道"与"通道在但没数据"。

顺带修掉一条**报错说错原因**的问题（同一场 CAN 暴露出来的）：`maths/global.json` 的
五条定义在那场 CAN 上全报"本场次没有 `G Force Lat`"，其中四条根本没引用它（缺的是
`FSD13 Distance1`、`G Force Long`）。根因是 `resolve_available` 把**第一条**失败的原因
发给了所有没解出来的定义。现在逐条再问一次，只有当它抱怨的通道**不是同一批里的另一条
定义**时才用逐条的解释——"绕成环"那种真·互相依赖仍然报"绕成一个圈"。

**可复制的命令与判据**

```powershell
python -m unittest tests.test_i3pro.TestDbcMerge -v   # 并集 / 相同定义 / 冲突 / 定序 / 扩展帧
python -m unittest tests.test_i3pro.TestCanLog -v      # 12 项：并集数字、并集通道数、距离轴、静止日志、固定一份、冲突
python -m unittest tests.test_i3pro.TestMaths -v       # 含"每条坏定义各报各的缺通道""环仍然是环"
node tools\smoke_viewer.js out\can.html                # 报告里的每份 DBC 贡献、每条通道的来源

# 工作台里要能看见它们：两个数据目录都要挂（启动.bat 现在同时挂这两个）
python -m i3pro serve --data i2pro_data --data can_data   # -> 23 个场次（16 个 .ld + 7 个 CAN）
```

**实测（2026-10-04）**

| 项 | 结果 |
| --- | --- |
| 13 份 DBC 的并集 | 91 条报文 / 61 条通道；只挑一份最多 46 条 |
| 9 份日志合计 | 3,684,850 帧 / 43 个 ID；覆盖 **1,600,064 帧 = 43.42%**（单份 `Sensors.dbc` 是 24.6%） |
| 第一场（2 份文件并起来） | 1,984,096 帧、覆盖率 **40.18%**、61 条通道 |
| 每份的贡献（第一场） | `Sensors.dbc` 46 条 / 447,463 帧、`TH.dbc` 5 条（`0xC1`）、`IMU_INFO.dbc` 4 条（`0xCB`）、`RodForce.dbc` / `Power_INFO2.dbc` / `Temp.dbc` 各 2 条；**7 份一条都没命中**（2023–2024 的 DBC 与 2026 的车上布局脱节），报告照实写出来 |
| 车速 → 距离轴 | 速度源 `Vx_KF`（kph）；距离轴 **0.1–5546.8 m**（第一场，69,945 点，单调不倒退） |
| 原地不动的日志 | `201147` 那份 `Vx_KF` = −0.05…0.00 kph → **没有距离轴**，报告写明原因（不是"没有这条通道"） |
| 读不懂的最大一块 | `0xCC` **139,050 帧**（9 份合计），6 个 OBD/UDS 诊断 ID 仍单独标出 |
| 冲突 | 真实 13 份之间**没有冲突**；合成的冲突用例（`0x660` 两种定义）按覆盖帧数选了 `Sensors.dbc`，冲突进报告，用的是赢的那份信号名 |
| 性能 | 9 份全解码 **6.12 s**（判据 ≤ 20 s）；单份 91 MB：`merge=False` **1.56–1.61 s**（单测走这条），**默认的 `merge=True` 3.22–3.30 s**（工作台走这条——它会先看一眼同目录的邻居，判断这份是不是被切开的记录）。两条都过判据 ≤ 5 s |
| 报错错因 | 三条各缺一条通道的定义分别报出自己的那条；互相引用仍报"绕成一个圈" |

---

## A54 · CSV / CAN 场次补齐：快照、启动器与导入文案（ticket #41）

`i3pro snapshot`（就是 `导出快照.bat`）过去只认 `*.ld`：它的清单是 `data.glob("*.ld")`，
而侧边栏用的是 `library.SessionLibrary`。于是**同一个场次，侧边栏里有、快照里没有**，
而且在一个只有 CSV / CAN 的目录上只打一句"没有 .ld 文件"就退出 1——"少了东西"和
"本来就没有"长得一模一样。现在两处共用同一处发现逻辑：`.ld`、i2 Pro 导出的 `.csv`、
原始 CAN 帧表都在里面，CAN 的并场（9 份文件 = 7 次记录）也照旧成立。

索引页的"场次"列因此改成**侧边栏那套名字**（普通场次 = 去扩展名的文件名；并起来的
CAN 记录带 `+1`），不再显示底层文件名——两处对不上时用户没法核对。

**可复制的命令与判据**

```powershell
# 1. 三件套：快照本身 / CAN 帧表 / 并场（缺 can_data 或 DBC 时自动 skip，那不算通过）
python -m unittest tests.test_i3pro.TestLaunchers -v          # -> Ran 5 tests / OK

# 2. CAN 目录整场导出：7 个快照 + 索引
python -m i3pro snapshot --data can_data --out out\_snap_can

# 3. 两个目录各来一份（= 导出快照.bat 现在干的事；启动.bat 早就这么挂）
python -m i3pro snapshot --data i2pro_data --data can_data --out out
```

**实测（2026-10-04）**

| 项 | 结果 |
| --- | --- |
| 修之前 | `python -m i3pro snapshot --data can_data --out out\_snapcheck` → `# can_data 里没有 .ld 文件`，退出码 1 |
| 修之后 | **7/7** 个 CAN 快照 + 索引 7 行，实测 **8.7 s** |
| 并场 | 两份连续文件只出一个 `2026_10_03_173345_ID0001+1.html`，索引里也只有一行 |
| 索引内容 | 设备 `CAN`、日期 `2026-10-03`、时长 18–699 s、通道 `62`（61 条 DBC 并集 + 全局数学通道 `速度kmh`） |
| 文案 | `导入数据.bat` 与 `importer.import_paths` 都写明接受 `.csv`；目录里一个都没有时给出的下一步是"把日志放进这个目录，或把文件拖到 导入数据.bat 上" |

**顺手修的一条（同一批命令暴露出来的）**：`tests/` 在 `out/` 下的临时目录用的是固定
名字，两个进程同时跑这套测试时一方会 `rmtree` 掉另一方正在读的目录。实测复现过两次
（同跑 `TestCanLog.test_the_library_shows_one_entry_per_recording`）：

```
FileNotFoundError: E:\桌面\i3pro\out\_can_library\2026_10_03_173345_ID0001.csv
FAILED (errors=1)                     # 另一次的表现是 listing() 两次长度不同：23 → 16
```

现在临时路径带进程号（`scratch()`），金标准副本目录 `STAGE` 退出时删掉（否则每跑一次
留 148 MB）。判据：**两个进程同跑上面那条用例，两条都 OK**（实测 23.2 s / 22.1 s）。

---

## A55 · 概览条认得出 CAN 的速度（ticket #38 / #40 的收尾）

同一个量在两条数据线上叫两个名字：`.ld` 里是 **`Vx KF`**，CAN 线（`TH.dbc` 的
`0xC1 Throttle_INFO`）解出来是 **`Vx_KF`**。距离轴那条解析早就认这两种写法
（`derive.speed_channel`，ticket #40 修的），但**概览条和轨迹着色用的是另一张候选表**
（`render.SPEED_FOR_COLORING`），只按原样比名字。后果实测如下：

```powershell
# serve 模式下，跑起来的 CAN 场次概览条是空的
python -m i3pro serve --data i2pro_data --data can_data
#   /api/session/2026_10_03_173345_ID0001+1/overview  ->  null
```

同一份数据导成快照却**有**概览条（那边退到了"第一条被选中的通道"）——所以这不是
"少画一条线"，而是**同一个场次离线看得见、联网看不见**，而且画的是哪条通道两种模式
都不同。现在两处共用 `render.overview_channel`（内部走
`derive.resolve_channel` 的同一套名字匹配），并且**有距离轴就必须画在速度上**。

顺带把没有圈时的报表提示改对：过去用的是区段模块那句"先放一个信标**再来分区段**"，
用户在**报表**上看到它会以为点错了地方；现在两种模式都说"先放一个信标，再来看报表"。

**可复制的命令与判据**

```powershell
python -m unittest tests.test_i3pro.TestSpeedChannelResolution -v   # 5 项
python -m unittest tests.test_i3pro.TestCanSessionSurface -v        # 2 项

# 快照那条路：跑起来的 CAN 场次必须过无头断言（新加的两条就在里面）
python -c "from i3pro import csvlog, render; render.render_html(csvlog.open_session('can_data/2026_10_03_173345_ID0001.csv'), 'out/can_moved.html', worksheets_dir='.')"
node tools\smoke_viewer.js out\can_moved.html
```

**实测（2026-10-04）**

| 项 | 结果 |
| --- | --- |
| 23 个场次逐个比对（16 `.ld` + 7 CAN） | 16 个 `.ld` 选的还是 `Vx KF`（**一个都没变**，含两份金标准）；7 个 CAN 从"没有概览条"变成 `Vx_KF` |
| 距离轴的判据没被带偏 | 原地不动的 `2026_10_03_201147_ID0001.csv`：概览条画 `Vx_KF`（平线），距离轴**仍然没有**（`derive.speed_channel` 的"要真的在动"那条只管距离轴） |
| 快照无头断言 | 修前：跑起来的 CAN 场次 **4 条红**（报表 / 按圈直方图窗口 / 导出选中圈 / 没有可测的通道名）；修后 PASS。断言点数（`rg -o "check\(" tools/smoke_viewer.js \| Measure-Object`）**492 → 497** |
| 变异测试 | 把 `display_speed_channel` 改回"只按原样比名字"，新加的那条立刻红（报"概览条画的不是速度通道，而是兜底的第一条通道：TH"）——证明断言不是空跑的 |
| 报表提示 | 没有圈的场次（7 个 CAN + `FSS_jhy_autox`）都说"本场还没有圈：先放一个信标，再来看报表"；有圈的场次一个都没变 |

**为什么三条断言要分开**：`hasDistance`（有没有距离轴）与 `hasLaps`（有没有圈）是
两件事——CAN 跑起来之后有距离轴但**一条圈都没有**（没有 GPS、没人放信标）。早先把
报表 / 按圈直方图 / 导出选中圈三块都写成 `if (hasDistance)`，于是"有距离轴但没圈"的
场次一进来就报红，而那种场次正是 CAN 数据**唯一有用**的形态。

---

## A56 · 导入 Excel（`.xlsx` 读成场次，ticket #31）

队友发来的是 Excel，不是 `.ld`。读进来的东西与 `.ld` / CSV **完全同一种场次**：
进侧边栏、能画图 / 切圈 / 比圈 / 报表 / 导出。

```powershell
# 命令行（拖到 导入数据.bat 上是同一件事）
.\i3pro.cmd import "D:\别人的表.xlsx" --data i2pro_data
.\i3pro.cmd import "D:\多页表.xlsx" --data i2pro_data --sheet "第二张" --map "原始列=TH"
.\i3pro.cmd info "i2pro_data\别人的表.xlsx"
```

**通过判据**（单测 `TestXlsxImport` 13 项 + `TestUploadEndpoint` 4 项）：

| 断言 | 说明 |
| --- | --- |
| **硬判据 round-trip** | 同一个导出请求写成 CSV 与写成 `.xlsx`，两条都读回来**逐点相同**（`atol=1e-6`，值最大到 100 km/h 量级）；再与 `.ld` 本身逐点对拍 |
| 两份金标准实跑 | 高避5圈 / 耐久正赛各导一段 20 s × 2 通道的 xlsx 再读回：**1001 行 × 2 通道**（17 KB / 15 KB），逐点一致 |
| 列名里的单位 | 我们自己的宽表把单位写在列名里（`Vx KF [km/h]`）；读回来拆成**名字 `Vx KF` + 单位 `km/h`**，否则速度、距离轴、对拍全都跟着失效 |
| 表头与单位行 | 与 CSV 共用同一套判定（`session_from_frame`）：列名走 原名 → 别名 → 手工覆盖，单位三处来源优先级一致 |
| 选哪张 sheet | 不给就**按顺序试**，用第一张能当通道表读的（自己的导出把「元数据」放第一张，所以不能按位置硬取）；`--sheet` 指定时那张读不出来要报清原因；选过的那张记进 `<场次>.map.json` |
| 日期时间列 | Excel 的日期单元格 / ISO 文本都折算成**相对秒**，起点写进场次的日期与时间（实测 20 行 100 Hz，首点 0.000000000 s、末点 0.190000 s） |
| 读不了的形态 | 宏（`vbaProject.bin`）/ 图表（`xl/charts/`）/ 外部链接（`xl/externalLinks/`）/ **公式没有缓存值** / 根本不是 zip：五种各自报错并说下一步，不静默读成空 |
| 没有时间列 | 拒绝，并说清改哪一列（`--map "原始列=Time"`），**不拿第一列冒充时间** |
| 同名不覆盖 | 走 `importer.unique_target()` 的 `-1/-2` 后缀 |
| 中间的空列/文字列 | 不再让后面的列整体左移（见下），跳过的那列进报告 |

**前端这一关怎么验**（规则 ①③）：导入用的那个页面是本地服务现生成的，无头驱动跑在
**快照 HTML** 上够不到它，所以这一票的交互断言分两处——真浏览器那关走整条导入流程，
无头驱动验"读进来的场次画得出图"：

```powershell
# 把一份 xlsx 场次渲染成快照，再过无头断言（437 通道 / 1001 行 / 1.5 MB）
python -c "import pathlib,sys; sys.path.insert(0,'src'); from i3pro import csvlog, export, ld, render; \
  log = ld.LogFile.read('i2pro_data/20260908-cjh 高避5圈.ld'); \
  out = pathlib.Path('out/xlsx_golden.xlsx'); \
  export.write(log, export.parse_request(log, {'channels':'all','format':'xlsx','rate':'50','from':'0','to':'20'}), out); \
  log.close(); render.render_html(csvlog.open_session(out), 'out/xlsx_viewer.html', worksheets_dir='.')"
node tools\smoke_viewer.js out\xlsx_viewer.html
#   PASS - workbench ran headless (324051 line segments, 341742 point rects, 466 channel rows …)

# 真浏览器：文件选择器收 .xlsx -> 上传 -> 列表出现 -> 点进去能画（6 条断言）
python tools\verify_clicks.py
#   #31 导入页的文件选择器收得到 .xlsx — .ld,.ldx,.csv,.xlsx
#   #31 导入前列表里没有这份 Excel
#   #31 用页面自己的文件选择器上传后，场次列表里出现了它
#   #31 这份 Excel 场次能当工作台打开
#   #31 列名里的 [单位] 拆回成了通道名 + 单位 — ['Vx KF', 'TH', '速度kmh']
#   #31 单位跟着列名一起读回来了 — {'name': 'Vx KF', 'unit': 'km/h', …}
```

**顺带修的一处**：无头驱动对抬头的断言原来只认 `.ld` / `.csv`，拿一份 Excel 场次的
快照去过就会红（"header was not populated"）——它现在也认 `.xlsx`。

**这一票顺带修掉两个真问题**

1. **浏览器上传一直是 500**（`upload` 里残留的 `self.rfile`）。`#26` 把请求闭包拆成
   `api.py` 那一层时，`upload` 还引用着闭包里的 `self.rfile`——这个名字在新的一层不存在。
   命令行的 `i3pro import` 不受影响，而且这个端点**一条用例都没有**，所以从那天起
   "拖进窗口导入"就一直失败（真浏览器那关现在会红）。现在请求体走 `Body.stream()`，
   按块读，不把上百 MB 的日志整份读进内存；并补了 4 条用例（落盘 / 同名 / 扩展名 / 中断不留 `.part`）。
2. **中间夹一个空列会让后面的列错位**：旧写法把"整列都是 NaN"的列 `dropna` 掉，再按
   位置取列。实测 `Time,A,Empty,B` 里，叫 `B` 的通道拿到 10/20/30，而 `Empty` 拿到了
   本该属于 B 的数字，报告里还写着 B「缺列」——一个错都不报，数值全配错了对。现在不
   按位置丢列，跳过的那列照实写进报告。文字列在旧写法里则是抛一句 pandas 的
   `could not convert string to float`，没有下一步。

**接口的接缝**：CSV 与 Excel 两条读取器共用 `csvlog.session_from_frame`（表头在哪、
单位在哪、哪一列是时间、列名怎么匹配、报告怎么写，只回答一次）。新加一种表格格式 =
写一个"表头几行 + 一张数据表"的 adapter。

---

## A57 · 导入 TXT / TSV（分隔文本 + 导入前预览，ticket #32）

队友发来的常常是一份 `.txt`：也许是 Tab 分隔，也许是分号，也许**根本没有时间列**
（一行就是一个采样点）。这一票把"怎么读"变成一件**看得见、改得了、记得住**的事。

```powershell
# 1) 命令行先只看读法：分隔符 / 编码 / 表头行 / 前 10 行，一样都不复制
.\i3pro.cmd import "D:\别人的表.txt" --data i2pro_data --preview
.\i3pro.cmd import "D:\别人的表.txt" --data i2pro_data --preview --delimiter ";" --header 2
.\i3pro.cmd import "D:\别人的表.txt" --data i2pro_data --preview --rate 100   # 没有时间列时生成

# 2) 真的导入（选择写进 <场次>.map.json，第二次自动套用）
.\i3pro.cmd import "D:\别人的表.txt" --data i2pro_data --delimiter ";" --rate 100
.\i3pro.cmd info "i2pro_data\别人的表.txt"

# 3) 导入页（serve 模式）里那一套：选文件 → 卡片给前 20 行 + 四个下拉框 → 真鼠标点「导入」
python tools\verify_clicks.py     # 95 项检查：95 通过，0 失败（其中 #32 有 14 条）
```

**通过判据**（单测 `TestTextImport` **19 项** + 真浏览器 12 条）：

| 断言 | 说明 |
| --- | --- |
| **四种分隔符 + 连续空白** | 逗号 / Tab / 分号 / 竖线 / 多空格都能读成场次，同一张表换个分隔符读出来**数值逐点相同** |
| **预览 = 导入** | 预览面板上的通道名、单位、采样率，就是点「导入」之后真正得到的东西——两边**同一条代码路径**（`txtlog.preview` → `csvlog.session_from_frame`），不是前端另猜一套 |
| **猜错看得见** | 分号表按逗号读 → 整份变一列 → 预览里当场说"多半是分隔符不对"，换一个下拉框就能读对（实测：`Vx KF` / `TH` 两列回来） |
| 侧车记住读法 | `<场次>.map.json` 里存 `delimiter` / `encoding` / `header` / `unit_row` / `generate_rate`；第二次导入自动套用，**列名覆盖（`--map`）不会把它冲掉**；传空值 = 改回自动 |
| 没有时间列 | 默认**拒绝**并说下一步（`--rate` 或预览里勾生成），**不拿第一列冒充时间**；选了生成之后时间列可复现（两次读 `array_equal`），报告里带 `generated` |
| 生成不盖真时间 | 表里本来就有时间列时，勾了"生成"也以**文件里的**为准（实测首三点 `0.00 / 0.01 / 0.02`） |
| 表头行 / 单位行 / 无表头 | 都能显式指定；指到不存在的行时说"只有几行"；`header=none` 时列名退化成 `列1` / `列2` |
| **CSV 也吃同一条路** | 同一张表存成 `.csv` 与 `.txt` 读回来**逐点相同**；分隔符不是逗号的 `.csv`（分号、制表符）同样按内容读对 |
| 导入的前后 | 暂存期间**不进场次列表**；提交后进列表、解析方式落到侧车；取消（真鼠标点）删掉暂存文件、列表里不留痕；过期的暂存编号给 404 + "重新选一次" |
| 坏选项 | 认不出的分隔符 / 编码 / 表头行 / 采样率各自 400，并**列出能用的值**（选了 Tab 却按逗号读是最难查的一类错） |
| 暂存不堆积 | 服务启动时扫掉一小时没动过的 `i3pro-import-*`（和导出临时目录同一套做法），一小时内的不动 |

**这一票撞出来的真问题：空格子的 GPS 被当成有效定位**

我们自己的导出在 `Auto` 模式下给慢通道**留空**。同一份数据导成 CSV 再读回来时，GPS
列有一半格子是空的（实测 23200 / 46400），而 `derive.gps_track` 里
`(np.abs(lat) <= 1e-3)` 对 NaN 是 `False`——**空格子既不算掉星也不算卫星不足，一路
混进了有效点**：轨迹里带 NaN 空洞，切圈从 **7 圈静默退化成 1 圈**（还是"不完整"的
那一圈）。修法是把 NaN 与 `(0,0)` 一样当"没有定位"（`no_fix |= ~isfinite(lat/lon)`），
于是同一份文件读回来是 **7 圈、其中 5 圈完整、起点 119.4 s**，与 `.ld` 逐圈对上
（170.6 / 211.1 / 262.3 / 311.3 / 351.7 / 392.7）。判据在
`TestTextImport.test_a_sparse_gps_column_does_not_hide_the_laps`。

**前端这一关怎么验**（规则 ①③）：导入页是本地服务现生成的，无头驱动跑在快照 HTML 上
够不到它，所以这一票和 A56 一样分两处——**真浏览器**走整条流程（选文件 → 卡片出 →
真键盘填 100 Hz → 真鼠标点「导入」/「取消」→ 列表与页头），**无头驱动**验"读进来的
文本场次画得出图、抬头写着读法"：

```powershell
# 把金标准导出成"别人的分号表"，走预览 → 导入 → 快照 → 无头断言
python tools\make_text_demo.py
node tools\smoke_viewer.js "out/_text_demo_html/金标准导出.html"
#   PASS - workbench ran headless (974224 line segments, 404159 point rects, 6614 labels,
#          7 lap rows, 11 channel rows, interactions verified)
#   （那份 1.5 MB 的分号表读成 6 通道 · 464 s · 文本；`parse_note` 那条断言只有在
#     快照带上读法时才跑——`.ld` 快照上它是空转的，所以专门造这一份）
node tools\smoke_viewer.js "out/20260908-cjh 高避5圈.html"      # PASS - 7 圈 / 471 通道行
node tools\smoke_viewer.js "out/20260524-耐久正赛.html"          # PASS - 26 圈 / 377 通道行
python tools\verify_clicks.py                                    # 95 项检查：95 通过，0 失败
#   #32 导入页的文件选择器收得到 .txt — .ld,.ldx,.csv,.xlsx,.txt,.tsv
#   #32 选一份 .txt 之后弹出预览卡片
#   #32 预览里说清了这份表没有时间列（下一步是填采样率）
#   #32 预览表按认出来的分隔符切成了多列
#   #32 填上 100 Hz 之后预览变绿并报出通道数 — ✓ 2 通道 · 表头第 1 行 · 共 2 列 · 100 Hz · 分号分隔 · UTF-8 · 时间列按 100 Hz 生成
#   #32 真点「导入」之后，场次列表里出现了它
#   #32 导入之后暂存目录被收掉 — 剩下了 []
#   #32 场次抬头写明了读法 — 分号分隔 · UTF-8 · 表头第 1 行 · 时间列按 100 Hz 生成
#   #32 生成的时间列就是 100 Hz — 99.99999999999991
#   #32 读法真的画在页头那一行上
#   #32 点「取消」之后卡片关掉、场次列表里没有它
#   #32 取消也要把暂存文件删掉
```

**全量回归**：`Ran 377 tests` + `OK`；`verify_ld_vs_csv` `PASS - 0 channel(s) outside
tolerance`；两份金标准快照各 `PASS`（7 圈 / 26 圈）；真浏览器 **95 项检查：95 通过，
0 失败**。

## A58 · 工作表增删改与进出（ticket #33）

到 A57 为止，`worksheets/*.json` 只能靠手写；工作台上那排按钮只做"切到哪一套"。
这一票补上"这一套本身"：**保存 / 另存为 / 新建 / 重命名 / 删除 / 导出 / 导入**，
改完立刻落到仓库里的文件。快照（离线 HTML）只有"看与切"，点了会说改用 serve 模式。

```powershell
# 1) 单测：文件层的全部规矩（16 条）
python -m unittest tests.test_i3pro.TestWorksheetEditing tests.test_i3pro.TestWorksheetEditingOverHttp -v

# 2) 真浏览器：真鼠标点那 7 个按钮、真键盘打名字，每一步回到磁盘核对
python tools\verify_clicks.py     # 127 项检查：127 通过，0 失败（其中 #33 有 32 条）

# 3) 无头：写回文件的形状（pick 不许被写死）+ 快照里点按钮只提示不发请求
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"
```

**通过判据**（`TestWorksheetEditing` + `TestWorksheetEditingOverHttp` **16 项** ＋
真浏览器 32 条 ＋ 无头 12 条）：

| 断言 | 说明 |
| --- | --- |
| 新建 / 另存为**真的多了文件** | 点完 `worksheets/` 里立刻多一个 `.json`，新那一套同时出现在按钮上（实测 `codex-probe.json`） |
| 同名另存为加后缀、不覆盖 | 再来一次同名 → `sheet-2.json` + 显示名 `… 2`；**旧文件字节一个都没变**（实测两次 `read_bytes()` 相等） |
| 保存只改那一份 | 真点组件 ✕ 去掉一个组件再点保存 → 文件里的组件数 6 → 5；**名字 / `order` / `hints` 留在文件里不动** |
| **没动过的通道名不写回文件** | 写回的图组件是 `{"type":"graph","config":{"mode":"tiled"},"pick":{...}}`——没有 `config.channels`。写死了换场次就空图（i2 Pro 用别名解决，见 #36）。**用户勾过的**则必须写回去，见 A74 |
| 改名会让**文件名**跟着变 | `codex-probe-2.json` → `codex-renamed.json`，旧文件删掉，按钮上也是新名字；目标名已被占用时报错，两份都不动 |
| 删除要先确认、删完说清去处 | 第一次点变成「确定删除？」；第二次才删。删的是正在用的那套时自动切到第一套：实测提示"已删除「codex-renamed 2」，现在切到「分析」" |
| 导出一份 = 一个文件 | 真点「导出」→ 下载目录里出现 `codex-renamed.json`；**字节与服务端那份文件完全相同**（`raw == (dir/"renamed.json").read_bytes()`） |
| 导入队友那份立刻可用 | 把导出的文件塞回文件选择器 → 多一套 `codex-renamed 2`（同名加后缀），**本地那份不动**；坏文件报"第 N 行第 M 列"并提示让队友点「导出」 |
| 快照只读 | 无头里逐个点那 7 个按钮：每个都提示"改用 serve 模式"，而且**一个请求都不发**（`/worksheets` 的调用数不变） |
| 身份不能出目录 | `/api/worksheets/..%2F..%2Fevil`、带空格的 id、Windows 保留名（`con`）一律 400；写文件走 `.json.part` → 改名，断电不留半份 |
| **没碰车队仓库** | 整条真浏览器流程跑在 `out\_verify_data_<端口>\_worksheets_root` 的副本上，跑完仓库里 `worksheets/` 每个文件的内容哈希逐一对得上 |

**这一票撞出来的两个真问题**

1. **清缓存不是函数。** `resetWorksheetCache` 只作为 `window.i3pro` 的一个属性存在，
   而 `applySheetState()` 里直接调用了这个名字——`ReferenceError` 被 `try/catch`
   吞进 toast 里，表现出来就是"文件真的建了、按钮一动不动"。第一次真浏览器跑就是
   这么红的（`#33 新那一套立刻上了按钮 — ['分析', …]`）。现在它是一个真正的函数，
   注册表里只做别名。
2. **`--worksheets` 只接了一半。** `server.session_page()` 忘了把
   `library.worksheets_root` 传给 `render.build_payload`，于是页面顶上那排按钮读的是
   **仓库根目录**、`/api/worksheets` 读的是 `--worksheets` 指的那个目录。真浏览器
   验收要跑在副本上，这条不修就等于"接口读 A、页面画 B"。判据是
   `TestWorksheetEditingOverHttp.test_页面载荷里的工作表按钮跟着文件走`。

**回归四项（本机实测）**：`Ran 393 tests` + `OK`；`verify_ld_vs_csv` `PASS - 0
channel(s) outside tolerance`；两份金标准快照各 `PASS`（7 圈 / 471 通道行、
26 圈 / 377 通道行）；真浏览器 **127 项检查：127 通过，0 失败**；
`smoke_viewer.js` 里的 `check(` 共 **512** 条。

**顺带**：`.txt` / `.tsv` 被加进场次库的发现范围（不加就等于导进来也看不见），
`.gitignore` 补了 `*.tsv`（`*.txt` 不加——`request/*.txt` 是**要进仓库**的需求文档，
全局忽略会让它们看起来像没被跟踪）。

## A59 · 缺失通道可见：三态分开、灰显、绝不自动剔除（ticket #34）

到 A58 为止，一份工作表换到没有那条通道的场次时，那条通道是**静默消失**的：服务端
`if log.has(...)` 跳过、前端再 `.filter(CHANNELS.has)` 滤一遍、零文案。用户看到的是
"这条图坏了"——所以这一票首先是**修 bug**，其次才是加显示。

```powershell
# 1) 单测：三态 + 报告 + 载荷 + 导出元数据（14 项）
python -m unittest tests.test_i3pro.TestChannelStates tests.test_i3pro.TestMissingChannelsOnExport -v

# 2) 真浏览器：导入一套引用本场没有通道的工作表，点进去看那张灰名单，再真导一次
python tools\verify_clicks.py     # 148 项检查：148 通过，0 失败（其中 #34 有 21 条）

# 3) 无头：三态判定只有一处 + 通道列表那一行 + 导出请求带 skip_missing=1
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"
```

**通过判据**（`TestChannelStates` 9 项 ＋ `TestMissingChannelsOnExport` 5 项 ＋
真浏览器 21 条 ＋ 无头 19 条）：

| 断言 | 说明 |
| --- | --- |
| **三态只有一处实现** | `channels.state()` / `channels.resolve()`（Python）与 `channelState()`（前端）是唯一的判定点；通道列表、组件抬头、页面抬头、通道报告、导出元数据、导出面板都问它，源码里 `.filter(CHANNELS.has)` 当"缺不缺"的写法已经清零 |
| 三种"没有"分得开 | `present` / `empty`（通道在、整段没有一个有效样本）/ `missing`（本场次根本没有）。实测：`Vx KF` → `present`、一条全 NaN 的数学通道 → `empty`、`FSD13 Distance1` 在高避场次 → `missing` |
| **本场没有的不许被自动剔除** | 真浏览器里那条通道仍留在 `comp.config.channels` 里（勾还勾着）；切回原来的场次，勾选与配置原样还在 |
| 通道列表里看得见 | 真 Edge 量出来的 `getComputedStyle(row).opacity` = **0.55**、`getBoundingClientRect()` = **289 × 22.84 px**、文字是 `本场没有的通道（验收样例）本场次没有` |
| 组件抬头写着缺几条 | 标题实测 `时间/距离图 · 2 通道 · 缺 1 条 · 分栏`；图上另有一行 `.lrow.lmissing`，实测 **1160 × 15 px**，文字 `本场次没有（不画）：…` |
| 页面抬头写着缺几条 | `#missingNotice` 实测 `· 本场次没有 1 条（…），它们没被从工作表里删掉`（快照里同样有——它跟着当前工作表算） |
| 通道报告分开报 | `channel_report()` 的 `missing` / `empty` 两组互不串门；合成数据里"空列"进 `empty`、"没这条"进 `missing` |
| 导出的元数据有 `excluded_missing` | 真浏览器真导出：下下来的 zip 里 `metadata.json` 的 `excluded_missing` = `['本场没有的通道（验收样例）']`；**空的时候这个键也在**（队友的脚本不该靠"有没有这个键"猜） |
| 缺的通道不许把整单打回 | 界面走的那条路带 `skip_missing=1`（实测 URL 里带这个参数），缺的那条不进数据列、也不进 `channels`；**命令行**默认仍然是硬报错，报错里写明"确实想跳过就加 `skip_missing=1`"——打错一个字就静默少一列是另一种坑 |

**这一票撞出来的两件事**

1. **`channel_index` 差点把页面载荷拖慢三分之一。** 首版用 `log.values(channel)`
   算"有没有有效样本"，实测耐久金标准 payload 从 **0.659 s → 0.881 s**——`values()`
   本身是整份数据的 `astype` 拷贝（342 条通道 0.168 s）。改成问会话的 `raw()`
   （`.ld` 是 mmap 视图，整数样本不可能是 NaN，直接跳过扫描）之后回到 **0.660 s**，
   与改动前持平（同一台机器、同一份数据、三次取最小值）。
2. **"整列空的通道"在导入路径上根本到不了界面。** `csvlog` 会把整列没有可用数值的
   列直接跳过（那是它该做的），所以 `empty` 这条状态真正的来源是**数学通道**
   （表达式算出一列 NaN）与没有报文的 CAN 信号列。单测与真浏览器验的都是这两条真路。

**一条副作用**：会话契约从"三样"变成"四样"——`channels.valid_count` 要问会话的
`raw(channel)`。没声明的会话会拿到一条带下一步的 `TypeError`，而不是悄悄退回慢路径。

**回归四项（本机实测）**：`Ran 407 tests` + `OK`；`verify_ld_vs_csv` `PASS - 0
channel(s) outside tolerance`；两份金标准快照各 `PASS`（7 圈 / 471 通道行、
26 圈 / 377 通道行）；真浏览器 **148 项检查：148 通过，0 失败**；
`smoke_viewer.js` 里的 `check(` 共 **531** 条。

## A60 · 组件显示：调色板 / 手选色 / 抬头字段（ticket #35）

通道一多，颜色按名字自动分配、抬头全挤在一行就都开始糊。这一票把三件事做成
**可以量、可以存**的：三套调色板、每通道手选色、抬头字段按组件开关——都跟着
工作表走。

```powershell
# 1) 单测：配色判据本身（8 项，纯函数 + 载荷）
python -m unittest tests.test_i3pro.TestPalettes -v

# 2) 真浏览器：真点色块选色、真点「显示」换调色板与抬头开关，再保存→切走→切回
python tools\verify_clicks.py     # 172 项检查：172 通过，0 失败（其中 #35 有 24 条）

# 3) 无头：换调色板真的换色、手选色优先且写回工作表、抬头那三格真的藏起来
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"
```

**通过判据**（`TestPalettes` 8 项 ＋ 真浏览器 24 条 ＋ 无头 27 条）：

| 断言 | 说明 |
| --- | --- |
| 三套调色板都在，每套 ≥ 8 色 | `default`（原来那 16 色，老工作表看起来不变）/ `colorblind`（Okabe–Ito）/ `contrast`（高对比）。颜色只写在 `palette.py` 一处，随页面载荷下发 |
| **同屏八条分得开**（可量） | 取每套的**前 8 色**（颜色按通道序号分配，所以八通道图用的就是这八个），两两 **CIE76 色差 ΔE ≥ 25**；实测最紧的一套是色盲调色板的 `#56b4e9` / `#0072b2` = **26.43** |
| 相邻两条分得开 | 序号挨着的两条最容易看混，ΔE ≥ 30；实测最紧 **55.80** |
| 深色底上看得见 | 每个颜色对面板底色 `#171a21` 的 **WCAG 对比度 ≥ 3.0**；实测最紧 **3.36**（同样是色盲那套的深蓝） |
| "缺失"灰不与调色板冲突 | 灰 `#4a5160` 不在任何一套里，且与**所有**调色板颜色 ΔE ≥ 25；实测最近 **33.96** |
| 手选色优先、且**跟着工作表存** | 真浏览器：点色块 → 选色 → 点「保存」→ 切到「对比」再切回「分析」→ 颜色还在。落盘的 `worksheets/analysis.json` 里实测 `"colors": {"Vx KF": "#b6ff00"}` |
| 换调色板不动手选色 | 手选的那条通道在换整套调色板之后颜色不变（那正是手选的意义） |
| 抬头字段按组件开关 | 六个字段各有各的开关（光标值 / Δ / Min / Max / Avg / 单位）。真点「显示」里的 `Avg` → `{"cursor":true,"delta":true,"min":true,"max":true,"avg":false,"unit":true}`：**只有那一格**收起来（`display:none`），而且同样存进工作表、切走切回还在 |
| 尺子本身是对的 | `delta_e(#000,#fff)` = 100.0、`contrast_ratio(#000,#fff)` = 21.0、同色 = 0 / 1.0（这两把尺子被改坏就红） |

**这一票撞出来的一个真问题**：换色只清了画布缓存（`cacheKey`），漏了抬头那一排色块
（`legendKey`）——于是**线换色了、图例还是旧色**。第一次真浏览器跑就是这条红的
（`#35 图例上的色块画出来也是手选色 — rgb(76, 194, 255)`），现在两个缓存一起失效。

**顺手补的两处**：假 DOM 缺 `removeEventListener` / `removeChild` / `parentNode`
（小弹窗关不掉了），以及调色板选择器用按钮而不是原生 `<select>`——原生下拉是系统
弹层，真鼠标验收点不开。

**回归四项（本机实测）**：`Ran 415 tests` + `OK`；`verify_ld_vs_csv` `PASS - 0
channel(s) outside tolerance`；两份金标准快照各 `PASS`（7 圈 / 471 通道行、
26 圈 / 377 通道行）；真浏览器 **172 项检查：172 通过，0 失败**；
`smoke_viewer.js` 里的 `check(` 共 **557** 条。

## A61 · 通道别名与状态视图：同一套工作表跨场次能用（ticket #36）

到 A59 为止，"这条通道本场次没有"已经看得见了——但它只是**说得清**，不能让那张图
**变得对**。这一票补上 i2 Pro 的 Channel Aliases：一条别名 = **有序候选通道名**，
解析取**第一条在本场次存在的**；别名存在工作表里，所以同一套表换一份数据还能用。

真实场景就摆在这两份金标准上：**左后轮速在耐久那台车上叫 `SpeedRL`，高避那台没有
这条通道**（只有 `Gear Speed1`）。写死哪一个名字都会让另一场空着。

```powershell
# 1) 单测：规则、形状、文件格式、真数据 + HTTP（15 项）
python -m unittest tests.test_i3pro.TestAliases tests.test_i3pro.TestAliasesOverHttp -v

# 2) 真浏览器：导入一套写 @左后轮速 的工作表 → 看它落到 Gear Speed1 → 真点着改候选、调顺序 → 保存 → 回磁盘核对
python tools\verify_clicks.py     # 188 项检查：188 通过，0 失败（其中 #36 有 16 条）

# 3) 无头：只查表不重写规则、落不到就留着引用交给 #34、保存写回引用而不是真名
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"
```

**通过判据**（`TestAliases` 14 项 ＋ `TestAliasesOverHttp` 2 项 ＋ 真浏览器 16 条 ＋
无头 18 条）：

| 断言 | 说明 |
| --- | --- |
| **规则只有一处实现** | `aliases.landing`（Python）= "有序候选取第一条存在的"。页面载荷与 `/api/worksheets` 把结果贴成 `alias_landing` 一张表，前端**只查表**（`channelState("@车速")` → 查表 → present / missing） |
| **同一套别名在两份金标准上都落地、而且落到不同通道** | 实测：`{"name":"左后轮速","candidates":["SpeedRL","Gear Speed1","GPS Speed"]}` 在高避 → `Gear Speed1`、在耐久 → `SpeedRL`（用例里还有一条断言专门要求两者**不相等**——落到同一条的话这条用例什么也没证明） |
| 一条都不在就明确标记 | `status()` 的 `channel` 是 `None`，候选原样带出去（界面要能说清试过哪几条）；不是引用、或别名表里没这个名字，也都不去猜 |
| 与"缺失通道"是**同一条缝** | 落不到的引用**留在配置里**，`channelState` 判它 `missing` → ticket #34 那套灰显 / 抬头 / 导出 `excluded_missing` 全都自动生效，没有第二套"别名失败了"的说法 |
| 编辑：增删候选、调顺序、增删别名 | 真浏览器真点：`＋ 别名`（真键盘打字）、候选上的 `↑` 连点两下把它顶到第一位、保存后回磁盘核对候选顺序 = `["GPS Speed","SpeedRL","Gear Speed1"]` |
| **保存写回的是引用，不是真通道名** | 落盘的 `worksheets/sheet-1.json` 里 `"channels": ["@左后轮速"]`——写成 `Gear Speed1` 的话换场次就又空了。用户真改过那一格才按他写的存 |
| 落点跟着新顺序走 | 保存回来之后 `alias_landing` 变成 `GPS Speed`（新顺序的第一条），图里画的那条通道也跟着换 |
| 别名随工作表进出 | `aliases` 是工作表文件里的一个键；导出 → 导入实测逐字段相同（`_test_导出的那份能原样导入回来_别名也带着`），`.ld` 一律只读 |
| 一条都没落地的别名写得清 | 刚建好还没加候选（或候选全不在）的别名，状态视图写"本场次一条都没落地"；改过还没保存的写"改过了 · 保存后重新判定" |
| 坏形状说下一步 | 没写名字 / 重名 / 名字带 `@` / `candidates` 不是一列 / 候选里又写别名 —— 各自一条中文报错；`POST` 坏别名 400 且带原文 |

**两处刻意的取舍**

* **空候选是合法的**。界面上"新建一条别名"和"给它加候选"是两个动作，中间那一瞬
  就是空的。第一版把它当坏文件挡掉，结果真浏览器验收直接撞上"保存失败"——比报错
  更难懂的是"我明明只是还没填完"。现在状态视图会把这种别名显示成"本场次一条都没
  落地"（文件名层面的坏形状依旧照挡）。
* **别名不参与 `pick` 规则**。`pick` 是"按提示词挑几条"，别名是"这条通道在别的车上
  叫什么"，两件事各自解决各自的问题；混在一起会让"为什么这条图上是这个通道"变成
  一道要读两套规则的题。

**回归四项（本机实测）**：`Ran 431 tests` + `OK`；`verify_ld_vs_csv` `PASS - 0
channel(s) outside tolerance`；两份金标准快照各 `PASS`（7 圈 / 471 通道行、
26 圈 / 377 通道行）；真浏览器 **188 项检查：188 通过，0 失败**；
`smoke_viewer.js` 里的 `check(` 共 **577** 条。

## A62 · DBC 目录递归 + 经纬度配对（ticket #42）

车队把新 DBC 按批次放在**子目录**里（`i2pro_data/dbc/261004/`），而 `load_databases`
只 `glob("*.dbc")` 顶层——新加的 DBC **一条都不参与解码**，而且界面上看不出少了什么。
同一票还把经纬度配对放宽：CAN 线解出来的叫 `latitude_MTI` / `longitude_MTI`，
旧版只认三个写死的全名。

```powershell
# 1) 发现规则与配对规则（合成数据，任何机器都跑）
python -m unittest tests.test_i3pro.TestDbcDiscovery tests.test_i3pro.TestGpsPair -v

# 2) 实测覆盖：同一批 9 份日志，DBC 集合变了之后解出多少（数字见下表）
python -m unittest tests.test_i3pro.TestCanLog -v

# 3) 真浏览器那关照旧
python tools\verify_clicks.py
```

**通过判据**（`TestDbcDiscovery` 4 项 ＋ `TestGpsPair` 4 项 ＋ `TestCanLog` 里的实测数字）：

| 断言 | 实测 |
| --- | --- |
| **子目录里的 DBC 也算** | 同一批 9 份日志（3,684,850 帧）：DBC 从 **13 份 → 16 份**、报文 91 → **127**、通道 61 → **124**（2026-10-05 车队又加了 `IMU.dbc` / `IVT.dbc`，份数变 18、报文 137，**通道数与覆盖率不变**——那两份的 ID 不在这批日志里） |
| 覆盖率 | **1,600,064 帧 = 43.42% → 3,013,146 帧 = 81.77%**（第一场 82.34%）；只挑一份 `Sensors.dbc` 仍是 46 条 / 24.6% |
| 多出来的是什么 | 子目录那 4 份贡献 **63 条通道**：S-Motion 的 `Vel`/`VelX`/`VelY`（光学地面速度 50 Hz）、Xsens MTi 的 `latitude_MTI`/`longitude_MTI`/`accX_MTI`/`gyrX_MTI`/`yaw_MTI`…（100 Hz）、`sw260425` 的 `LWS_ANGLE`/`LWS_SPEED`（方向盘转角） |
| 同一个目录里逐字节相同的只留一份 | `Sensors.dbc` 与 `261004/Sensors10.4.dbc` 同 sha256、`261004/ECU_To_MoTeC/*` 与顶层那 11 份同 sha256 → 报告里不会出现两份一样的贡献 |
| 目录之间仍是"第一个有 DBC 的说了算" | 显式给一个 `dbc_dir` 能隔离出一套 DBC（"固定用某一份"和测试里造冲突都靠它）；要找子目录靠递归，不靠多给目录 |
| 点名一份时会进子目录去找 | `load_databases(root, only="two.dbc")` → `261004/two.dbc` |
| 经纬度配对放宽到"同一后缀的一对" | `latitude_MTI`/`longitude_MTI` 认得出；`Lateral`/`Longitudinal` 这种"只是像"的不认；金标准 `.ld` 仍是 `GPS Latitude`/`GPS Longitude` |
| CAN 场次端到端 | `2026_10_03_173345` 那份日志：`gps_pair` 给 `("latitude_MTI", "longitude_MTI")`，`gps_track` 算得出 x/y，且这一对确实来自 `261004/Xsens_MTi_600_series.dbc` |

**两件必须如实说明的事**

1. **0x7E0–0x7E8 以前被当成"诊断流量"，那是误判。** 它们是 S-Motion Correvit 传感器
   （`BO_ 2016 Vel_Angle` 等 9 条报文），这份 DBC 一进来就解出 21 条通道。报告与 CLI
   里那句判语现在改成"**可能**是诊断流量"——没有 DBC 时它只是猜测，真正的判据永远是
   "有没有 DBC 解得开"。
2. **MTi 的经纬度能解出来，但定位是冻住的。** 15 份日志实测：坐标跨度**最大 49 m**，
   而同一段车跑了 **4 km 以上**（`2026_10_03_173345`：`∫|Vx_KF|dt ≈ 20.0 km`，
   距离轴末值 5,546.8 m）。所以 CAN 场次现在"有经纬度通道、配对也认得出"，
   但**画不出真轨迹**——轨迹会是一小团（约 45 m × 22 m）。这条只钉"认得出、算得出"，
   不断言轨迹形状：MTi 真跟上车之后，同一条用例自动变成一条真轨迹。要查的是车上
   那台 Xsens 的 GNSS（天线 / 是否进了 GNSS 模式），不是解析。

**回归四项（本机实测）**：`Ran 440 tests` + `OK`；`verify_ld_vs_csv` `PASS - 0 channel(s)
outside tolerance`；两份金标准快照各 `PASS`（7 圈 / 471 通道行、26 圈 / 377 通道行）；
真浏览器 **188 项检查：188 通过，0 失败**。

---

## A63 · 多路复用报文（`M` / `m<n>`）解码（ticket #43）

车上的 IMU 用**双帧报文**发数据：`i2pro_data/dbc/IMU.dbc` 的 0x780–0x783 里，
byte0 是帧类型（`0x01` 加速度 / `0x02` 角速度），后 6 字节按帧类型分别是三轴加速度
或三轴角速度——DBC 里就是标准的 `M` + `m1`/`m2` 多路复用。旧版一遇到复用就**整场拒绝**
（"本轮不支持…把它拆成不带复用的 DBC 再导"），于是这批 IMU 日志**一条都导不进来**。

```powershell
# 1) 合成用例 + 与 cantools 逐帧对拍（含"这一帧该不该有这条信号"）
python -m unittest tests.test_i3pro.TestDbc -v

# 2) 真数据：双帧报文那个 ID 解成了两路
python -m unittest tests.test_i3pro.TestCanLog.test_多路复用的_IMU_报文解成了两条通道 -v

# 3) 命令行肉眼复核（两条通道各自的真实更新率）
.\i3pro.cmd info "i2pro_data\2026_10_05_164008_ID0001.csv" | Select-String "IMU_RawData"
```

**通过判据**（`TestDbc` 合成 2 项 + `TestCanLog` 真数据 1 项 + 与 cantools 逐帧对拍）：

| 断言 | 实测 |
| --- | --- |
| 按选择子只解这一路 | 选择子 1 → 只有 `ACC_X/ACC_Y` + `FrameType`；选择子 2 → 只有 `GYR_X/GYR_Y` + `FrameType`；DBC 里没有的取值 → 只剩 `FrameType` |
| `m0` 是合法的一路 | 选择子取 0 时 `On_Zero` 在；不能把 0 当成"没有选择子" |
| **与 cantools 逐帧一致（含信号集合）** | `2026_10_05_164008` 的 **40,338 帧**（0x781×5,436 + 0x782×34,902）、7 条信号：数值差 0，**"该不该有这条信号"也 0 处不一致** |
| 两路各自的帧数与更新率 | 0x781 → ACC/GYR **各 2,718 帧**、0x782 → **各 17,451 帧**；两路都是 **47.67 Hz**（报文本身约 95 Hz） |
| 两路是**不同的数** | `ACC_X` 与 `GYR_X` 的列不相等——按普通信号解会把角速度的字节当成加速度 |
| 选择子列也留着 | `IMU_RawData_Copy_1.FrameType` 实测取值集合 = {1, 2} |
| 不认识的分支不拖累别人 | DBC 里没有的取值只**跳过那几帧**并计数（cantools 在这种情况下直接抛 `DecodeError`，会连累整条报文的所有分支）；报告里写"哪条报文多少帧的选择子不在 DBC 里" |
| 这一路这次一帧都没有 | 不出通道，但报告写明"是这条报文没发那一路，不是解码失败" |

**一处与 cantools 故意不同**：选择子取到 DBC 未定义的分支时，cantools 抛
`DecodeError`；我们返回"只有选择子"的字典并计数。理由是调用方是导入管线——
一条坏帧不该让**这条报文的所有分支**都导不进来（实测车上会发 DBC 里还没有的帧类型）。
这条差异写在 `dbc.decode` 的说明里，也钉在 `TestDbc` 的合成用例上。

**回归四项（本机实测）**：`Ran 443 tests` + `OK`；`verify_ld_vs_csv` `PASS - 0 channel(s)
outside tolerance`；两份金标准快照各 `PASS`（7 圈 / 471 通道行、26 圈 / 377 通道行）；
真浏览器 **188 项检查：188 通过，0 失败**。

---

## A64 · 总线进报告（ch1/ch2/ch3）+ IVT（ticket #44）

2026-10-05 16:00 之后的日志多了**第三条总线 ch3**（`0x511` + `0x521–0x526`），
`i2pro_data/dbc/` 里也多了 `IVT.dbc`。实测两件事：

- **IVT 用上了**：`IVT.dbc`（6 条报文 / 24 条信号）正好定义 ch3 上的 0x521–0x526，
  实测贡献 **24 条通道**——`I_Data` 电流 8,684 帧（29.13 Hz）、`U1/U2/U3_Data`
  电压 2,907 / 3,073 / 3,065 帧、`T_Data` 温度 2,151 帧、`P_Data` 功率 6,740 帧。
- **ch3 的数据本来就进得来**（导入从不按总线过滤），但 `CAN通道` 这一列**只解析、
  没派用场**：报告里看不出哪条 ID 来自哪条总线，而 DBC 只按 ID 认报文——同一条 ID
  出现在两条总线上时会把两边当成同一条报文。这批数据实测**没有**同号跨总线的情况，
  但记录仪已经挂着三条总线，所以这一票把它变成看得见的事实。

```powershell
# 合成双总线 + 真 ch3/IVT 两条用例
python -m unittest tests.test_i3pro.TestCanLog -v

# 命令行肉眼复核：未解码的 ID 旁边现在写着它在哪条总线上
.\i3pro.cmd info "i2pro_data\2026_10_05_174142_ID0001.csv"
```

**通过判据**（`TestCanLog` 里 2 条新用例 + 命令行实测）：

| 断言 | 实测 |
| --- | --- |
| 每条通道都带总线 | `IVT.dbc` 那 24 条通道全部 `bus=ch3` |
| 未解码的 ID 也带总线 | 这份日志 `0x527×226`、`0x511×1` 都标着 **ch3**（也就是"IVT.dbc 少一条 0x527"） |
| 总线汇总写一行 | `ch1 761,143 帧 / ch3 26,847 帧 / ch2 5,243 帧` |
| **同号跨总线要吵** | 合成一份"0x660 同时在 ch1 与 ch3"的帧表：报告写"有 1 条 ID 出现在**不止一条总线**上"，通道行的 `bus` 是 `ch1/ch3` |
| 热循环的代价 | 总线记账用整数座位号键，实测 **+0.2 s / 100 万帧**（1.31 s vs 1.11 s） |
| 单份 91 MB 的耗时 | **2.6 s**（`merge=False`）；工作台那条（`merge=True`，把两卷并起来）**5.2 s**——DBC 覆盖 43%→82% 之后本来就要多解一倍的帧，门槛从 5 s 改成 8 s |

**顺带说明**：车队 2026-10-06 把那份 63 条报文的 dashboard DBC 从 `i2pro_data/dbc/`
里删掉了，于是 A53 里点名它的三条断言改成"这份文件在才查"。同一批 DBC 目录里
`IMU.dbc` / `IVT.dbc` 是新加的——`TestCanLog` 里"DBC 份数与报文数"不再写死，
改成**从目录现算**（实测下限：≥124 条通道、≥82% 覆盖）。

**回归四项（2026-10-06 重测，金标已放回）**：

```text
python -m unittest discover -s tests  → Ran 445 tests，1 error
                                         （TestServer.test_http_api_end_to_end：
                                          /api/sessions 在 41 场数据下要 32.3 s，超了 30 s 的客户端超时）
python tools\verify_ld_vs_csv.py       → PASS - 0 channel(s) outside tolerance
两份金标准快照 + node tools\smoke_viewer.js → 均 PASS（7 圈 / 471 行、26 圈 / 377 行）
python tools\verify_clicks.py          → 188 项检查：188 通过，0 失败
```

那条超时是**新发现的真问题**（不是本轮改动引入）：侧边栏列表要为每一场算摘要，
CAN 场次等于整场重解码一次。实测 `SessionLibrary.listing()` **32.3 s / 41 场**，
第二次走内存缓存 0.01 s。修法（下一票）：导入时把摘要写进 `<场次>.can.json`，
列表读侧车、按 DBC 指纹判断失效——**已由 A66 做掉**（ticket #45）。

---

## A65 · CAN 解码 vs MoTeC 同场记录（对照分析，2026-10-06）

车队把**同一次跑车**的 MoTeC `.ld` 与 CAN 帧表都放进来了。两边记的是**同一批 CAN
帧**——`.ld` 是 MoTeC 自己解的，我们是自己解的——所以这是一位独立裁判（和
`verify_ld_vs_csv` 那条一个道理，只是裁判从"MoTeC 导出的 CSV"换成了"MoTeC 记的 `.ld`"）。

**先配对**：按墙钟猜是不行的（实测差 100 s 级）。用 1 Hz 的 `Vx` 互相关粗搜
（±40 min）再细搜（±3 s），相关系数才算得出来：

| CAN 帧表 | MoTeC `.ld` | 相关系数 | 时移 |
| --- | --- | --- | --- |
| `2026_10_05_152948` + `153603` | `20261005-jhy高避烧保险掉高压.ld` | **1.000** | −21.99 s |
| `2026_10_05_164008` | `20261005-底马八字前动态练习.ld` | **0.994** | −295 s |
| `2026_10_05_174142` | `cjh阶跃+脉冲.ld` | **1.000** | −21 s |

（`2026_10_05_112317`/`112945` 与任何一份 10-05 的 `.ld` 都对不上：最好的也只有
0.80，多半那一段车上没同时开 MoTeC。）

**再逐通道比**（第一对，时移 −21.99 s，45 601 点）：

| 通道 | 中位差 | 95 分位 | 相关 |
| --- | --- | --- | --- |
| `gyrZ_MTI` | 0.0020 rad/s | 0.0064 | **1.0000** |
| `accY_MTI` | 0.0112 m/s² | 0.0761 | **1.0000** |
| `accX_MTI` | 0.0119 m/s² | 0.0428 | **0.9999** |
| `Timestamp_MTI` | 3 µs | 97 µs | 0.9999 |
| `roll_MTI` | 0.0069° | 0.0141 | 0.9995 |
| `FL_Aero_Ride_Height` | 0.0401 cm | 0.0698 | 0.9993 |
| `FR_Aero_Ride_Height` | 0.0509 cm | 0.0795 | 0.9991 |
| `yaw_MTI` | 0.78°（360° 环绕） | 0.79° | 0.9988 |
| `velY_MTI` | 0.0156 m/s | 2.98 | 0.9935 |

**结论**：DBC 解析 → 按位提取 → factor/offset → 落到主时间基这条链，**和 MoTeC 自己
的解逐点一致**，差值都在显示精度量级（`acc` 那 0.01 是 float32 的量化）。

**同名字但不是一个源的通道**（也一样有用——别拿它们互相对账）：

| 通道 | 现象 | 判断 |
| --- | --- | --- |
| `FR/RR_Brake_Disc_Temperature` | 中位差 29.4 / 25.6 °C，`FL/RL` 两边都恒 0.0034 | 两边不是同一个传感器（`.ld` 里 FR/RR 是另一路；FL/RL 是死的） |
| `LV` | 中位差 2.23 V，相关 −0.35 | `.ld` 那个 `LV` 不是 ECU 的 `0x?` 那条 |
| `RR/RL_Aero_Ride_Height` | 中位差 0.05 cm 但相关 0.39/0.23 | 数值接近、不同步——有一边是卡住的 |
| `latitude/longitude_MTI` | 中位差比量程（0.002°）还大 | MTi 定位本来就是冻住的（见 A62） |
| 四条 `*_Suspension_Load` | 中位差 0.10–0.16 V / 量程 0.85 V，相关 ≈0 | 同名不同源（量程都对不上） |

**这一票没做什么**：没有把这条对照做成命令（`tools/verify_can_vs_ld.py`）——配对与
时移都还是探针（`out\_probe_matrix.py` / `_probe_compare2.py`）。要做成常驻验收，
下一票把它固化成工具 + 判据（"相关 ≥ 0.999 且中位差在显示精度内"）。

---

## A66 · 侧边栏列表不再为每一场重算（ticket #45）

**问题**：`/api/sessions`（侧边栏那一列）要为每一场算摘要。`.ld` 那部分约
**0.18 s/场**（打开 + 数学通道 + 切圈），CAN 场次更贵——**整场解码一次**。实测 41 场
数据下 `SessionLibrary.listing()` **32.3 s**（第二次走内存缓存 0.01 s），于是
`TestServer.test_http_api_end_to_end` 那条 30 s 客户端超时先红了出来。

**做法**（两层，都不往车队数据目录里塞新文件）：

1. **CAN 场次**：导入时把摘要写进 `<场次>.can.json`（`canlog.cache_summary`）。
   指纹 = 源文件（含分卷）+ DBC 目录的**内容 sha256** + 圈侧车；三样都对才用。
2. **整张列表**：落到磁盘缓存（默认 `%TEMP%\i3pro-listing-cache.json`，
   `SessionLibrary(index_cache=…)` 可换）。钥匙是"每一场（文件 + 圈侧车）+ DBC 目录"
   的指纹——文件动一下、DBC 改一行、圈侧车写一次，都会让它失效。

**实测**（41 场，同一份数据）：

| | 耗时 |
| --- | --- |
| 改之前，冷启动 | **36.3 s** |
| 只有第 1 层（CAN 侧车摘要） | 13.6 s |
| 两层都有，新实例（模拟重启服务） | **0.04 s** |
| 内存缓存（同进程第二次） | 0.01 s |

**判据**（`TestListingCache` 1 项 + `TestCanAgainstMoTeC` 里 1 项）：

| 断言 | 实测 |
| --- | --- |
| 冷启动读的是磁盘缓存 | 新实例里把 `summary()` patch 成抛异常，`listing()` 仍给出**同样的行**——证明一次都没重算 |
| 数据动过就失效 | `os.utime` 改一下 `.ld` 的 mtime → 指纹变 → 重算（patch 成抛异常时列表里出现 error 行） |
| DBC 内容动过就失效 | 往 DBC 追加一行（内容 sha256 变）→ `cached_summary` 返回 `None` |
| 圈侧车动过就失效 | 写一次 `<场次>.laps.json` → 同上 |
| 缓存写不进去也不吵 | 只读目录 / 权限不够时 `cache_summary` 吞掉 `OSError`：每次重算，列表照常 |

**回归四项（本机实测）**：`Ran 447 tests` + `OK`；`verify_ld_vs_csv` `PASS - 0 channel(s)
outside tolerance`；两份金标准快照各 `PASS`（7 圈 / 471 通道行、26 圈 / 377 通道行）；
真浏览器 **188 项检查：188 通过，0 失败**。

---

## A67 · 通道引用：五套规则收成一个 module（ticket #46）

「用户写的这个通道名字，本场次指哪条通道」这件事，到 #45 为止被写了**五遍**：
导入 CSV 的改名表（`csvlog`）、去空格匹配（`derive.resolve_channel`）、同后缀配经纬度
（`derive.gps_pair`）、数学通道表达式里再匹配一次（`maths._canonical_name`）、
工作表别名候选链（`aliases`）。它们互相绊倒过：`derive.resolve_channel` 的说明里
记着那次事故——往 `SPEED_CANDIDATES` 多写一个"同一化的写法"（`Vx_KF`），一张列叫
`Vx KF` 的表**导入时会被改名**。

```powershell
# 1) 新 module 的规则（合成数据，任何机器都跑）
python -m unittest tests.test_i3pro.TestChannelReference -v

# 2) 五条老路径照旧：导入 / 数学通道 / 别名 / 速度 / GPS
python -m unittest tests.test_i3pro.TestCsvSession tests.test_i3pro.TestMaths `
  tests.test_i3pro.TestAliases tests.test_i3pro.TestSpeedChannelResolution -v
```

**通过判据**（新 module 7 项 ＋ 五条老路径 96 项照旧全过）：

| 断言 | 实测 |
| --- | --- |
| 规范化只有一处 | `Vx KF` / `Vx_KF` / `vx-kf` / ` Vx.KF ` 都给 `vxkf`；空名字给空串 |
| 两种歧义策略**都有**、都说得清 | `lookup` 撞名取排序第一条（确定性优先，"这场用了哪条速度"不能随字典顺序漂移）；`lookup_unique` 撞名给 `None`（数学通道用它，宁可不猜） |
| 同后缀才配一对 | `latitude_MTI` / `longitude_MTI` 配得上；`Lateral` / `Longitudinal` 配不上 |
| 有序候选取第一条存在的 | `["A","B","C"]` + 本场有 `{B,C}` → `B` |
| **导入归一表撞名时结果固定** | 名单里同时有 `Vx KF` 与 `Vx_KF` 时取排序第一条；把名单顺序倒过来结果一样（以前是"字典最后写进去的赢"，那正是上面那次改名事故的机制） |
| **数学通道也算可引用的通道** | `channelref.known_names()` = 原生 + 数学通道；别名候选 `["没有的", "速度kmh"]` 现在能落到数学通道 `速度kmh` 上（以前只认原生通道，永远落不到） |
| 五条老路径行为不变 | 96 条用例全过：CSV 列名匹配 / 数学通道 `FSD13Distance1`、`Vx KF`、`vx kf`、`FSD13-Distance1` / 别名落地 / 速度通道挑选 / GPS 配对 |

**这一票修掉的两处不一致**（都是"同一件事两份规则"的后果）：

1. 别名候选池原来只有原生通道（`{ch.name for ch in log.channels}`），而 `derive` 那边
   用的是"原生 + 数学通道"。CONTEXT.md 写的是数学通道"除此之外与原生通道完全一样"，
   所以统一到 `channelref.known_names()`——别名现在也能落到数学通道上。
2. `csvlog._normalise` 是自己的第二套规范化（把非字母数字换成空格），`ALIASES` 的键
   是按它写的字面量。统一到 `channelref.normalise` 之后，字面量不用跟着规则变形：
   键在导入时现算（`_ALIAS_WORDS` → `ALIASES`）。

**回归四项（本机实测）**：`Ran 454 tests` + `OK`；`verify_ld_vs_csv` `PASS - 0 channel(s)
outside tolerance`；两份金标准快照各 `PASS`（7 圈 / 471 通道行、26 圈 / 377 通道行）；
真浏览器 **188 项检查：188 通过，0 失败**。

---

## A68 · `read_can_session` 拆成编排 + 帮手（ticket #47）

到 #45 为止，`canlog.read_can_session` 是**一个 424 行的函数**（全仓库最长，第二名 185），
体内依次做八件事：读侧车/列角色 → 找 DBC 目录并取并集 → 扫帧（计数 / 总线 / 样例）→
每条报文解码（含多路复用的分支）→ 落主时间基并生成通道行 → 拼报告与注释 → 读写摘要缓存
→ 构造场次。最近五个提交里 `canlog.py` 被改了 **4 次**（#42 递归、#43 复用、#44 总线、
#45 摘要缓存），每一次都在同一段 400 行的编排里找位置。

```powershell
# 1) 编排函数的行数（改之前 424）
python -c "import ast,pathlib;t=ast.parse(pathlib.Path('src/i3pro/canlog.py').read_text(encoding='utf-8'));f=next(n for n in ast.walk(t) if isinstance(n,ast.FunctionDef) and n.name=='read_can_session');print(f.end_lineno-f.lineno+1)"

# 2) 新缝的单测（4 项）
python -m unittest tests.test_i3pro.TestCanSeams -v

# 3) CAN 那 14 条端到端用例照旧
python -m unittest tests.test_i3pro.TestCanLog tests.test_i3pro.TestCanSessionSurface -v
```

**通过判据**：

| 断言 | 实测 |
| --- | --- |
| 编排函数短下来 | **424 → 220 行**；拆出来的帮手：`_decode_channels` 111、`_report_notes` 107、`_scan` 71、其余四个各 < 30 |
| 对外 interface 不变 | `read_can_session(paths, …)` 签名与返回值一字未动；14 条 CAN 端到端用例（报告里每一项都断言过）全过 |
| **新缝本身能单测** | 新增 `TestCanSeams` 4 项：总线归属 / 总线帧数汇总 / 未解码表（排序·帧率·"可能是诊断"提示）/ 报告注释 / `_decode_channels` 直接喂合成帧 |
| 以前测不到的分支现在有了 | "同一条 ID 出现在**两条总线**上"真数据里没有（实测 ch1/ch2/ch3 各管各的），所以那条警告以前完全没被测过；现在用合成计数直接触发并断言报告里写出来 |
| 多路复用分路可单测 | 直接给 `_decode_channels` 三帧加速度 + 两帧角速度：两条通道各自的帧数（3 / 2）、分支号（1 / 2）、值（`0x1000 × 0.001795…`）都对，且两路的值不相等 |
| 行为零变化 | 除上面的新用例，**没有一条老用例被改动**；四道回归全绿 |

**回归四项（本机实测）**：`Ran 458 tests` + `OK`；`verify_ld_vs_csv` `PASS - 0 channel(s)
outside tolerance`；两份金标准快照各 `PASS`（7 圈 / 471 通道行、26 圈 / 377 通道行）；
真浏览器 **188 项检查：188 通过，0 失败**。

---

## A69 · 命令行与界面走同一条"取数路"（ticket #48）

到 #47 为止，"打开一场、挂上数学通道"有**三个各自为政的实现**：场次库那条（缓存 +
DBC 目录 + 失效戳）、命令行里手写的三处（`convert` / `export` / `render`）、以及另外
**八条命令根本不挂**（`info` / `channels` / `laps` / `delta` / `track` / `report` …）。
症状可复现——同一个场次、同一条通道：

```
i3pro report … --table channels --channels 测试通道   →  0 行 · 通道 （无）
i3pro export … --channels 测试通道 --estimate         →  rows: 46400
```

顺带还有一个**旗标说谎**：`--maths-file` 的说明写着"额外的数学通道定义文件"，实现却把
它当根目录用（`<它>/maths/global.json`），所以传文件时**静默不生效**（定义里那条通道
在 `export` 里报"本场次没有"）。

```powershell
# 1) 命令行真的看得见数学通道（就是上面那条症状）
python -m unittest tests.test_i3pro.TestSessionForAnalysis -v

# 2) 手动复核（把金标准复制一份、旁边放一份 <场次>.maths.json）
.\i3pro.cmd report "<副本>.ld" --table channels --channels 测试通道 --limit 2
.\i3pro.cmd export "<副本>.ld" --channels 额外通道 --estimate --maths-file extra.json
```

**通过判据**：

| 断言 | 实测 |
| --- | --- |
| `report` 看得见数学通道 | 改动前 `0 行 · 通道 （无）`；现在 `通道报告：按圈分组 · 7 行 · 通道 测试通道` |
| `--maths-file` 传**文件**算数 | 改动前 `导出不了：本场次没有 '额外通道' 这条通道`；现在 `"rows": 46400` |
| `--maths-file` 传**目录**也照样认 | 当"全局定义的根"读 `<目录>/maths/global.json`（两种写法都测了） |
| 同名不覆盖、而且**报出来** | 额外文件里与本地/全局同名的条目被跳过，返回一行"不覆盖它（要改请改本地/全局定义）" |
| 死路要说下一步 | 给一个不存在的路径 → "既不是文件也不是目录；--maths-file 要指向一份定义文件，或一个含 maths/global.json 的根目录" |
| `info` 的通道数**不受影响** | 仍是 442（原生通道数；数学通道是另算的派生列，不算进 `metadata()["channels"]`） |
| 命令行与服务端同一个答案 | 两边都走 `library.open_for_analysis()`；`SessionLibrary.get()` 也改成走 `library.open_session()` |

**写这一版时踩到并修掉的坑**：`maths.attach()` 开头会 `detach()`（"定义一变整批清空
重算"是它的规矩），所以"先挂本地+全局、再挂额外文件"会把第一批**清掉**——实测第一批
只剩额外文件那几条。现在额外定义**并进定义表、一次挂完**（`TestSessionForAnalysis`
里那条"额外定义：文件与目录都认、同名不覆盖"就是它的回归）。

**回归四项（本机实测）**：`Ran 462 tests` + `OK`；`verify_ld_vs_csv` `PASS - 0 channel(s)
outside tolerance`；两份金标准快照各 `PASS`（7 圈 / 471 通道行、26 圈 / 377 通道行）；
真浏览器 **188 项检查：188 通过，0 失败**。

---

## A70 · 缓存与指纹收成一个 module（ticket #49）

#45 把侧边栏从 36 s 压回 0.04 s，代价是**四处缓存各写一遍**"什么算变了、缓存坏了
怎么办"：场次对象（内存）、数学通道挂载（本地/全局定义文件的 mtime）、侧边栏列表
（内存 + `%TEMP%` 里那份磁盘缓存）、CAN 摘要（写进 `<场次>.can.json` 侧车）。
指纹于是有**三种形状**（`(存在, mtime, size)` / `[名字, size, mtime]` / 内容 sha256），
"坏了当没有"写了两遍，而**最容易踩的那一脚没有任何地方写着**：

> 指纹要能写进 JSON 再比回来，所以形状必须是 `list` —— `tuple` 一进 JSON 就变 `list`，
> 比回来永远不等，缓存于是**每次都判"过期"**。不报错，只是白算（列表那次就是 36 秒）。

```powershell
# 1) 指纹与缓存策略（合成数据，任何机器都跑）
python -m unittest tests.test_i3pro.TestCacheStamps -v

# 2) 列表缓存真的会失效（改了文件 / 改了 DBC / 缓存写坏）
python -m unittest tests.test_i3pro.TestCacheStamps.test_改一个文件或一份_DBC_都会让列表缓存失效 -v
```

新增 `src/i3pro/cache.py`：`file_stamp`（`[名字, 字节数, mtime_ns]`）、`content_stamp`
（`[名字, sha256]`，给 DBC 这种"内容决定解码"的文件）、`tree_stamp`（目录树）、
`read_json` / `write_json`（指纹对得上才作数、原子写、坏缓存当没有）。四处缓存都改成
它的 caller，`library._file_stamp` 与 `canlog._stamp` 两处私有实现删掉。

**通过判据**：

| 断言 | 实测 |
| --- | --- |
| 文件指纹是 JSON 存得下来的形状 | `json.loads(json.dumps(stamp)) == stamp`；文件不在给 `-1`；内容一变指纹就变 |
| 目录指纹递归、顺序稳定 | 先浅后深、同层按路径（与 `canlog._dbc_files` 取文件同序）；目录不在给 `None` |
| 内容指纹只看内容 | 只改 mtime（`os.utime`）内容指纹不变、文件指纹变；sha256 长度 64 |
| **坏缓存当没有** | 没有 / 坏 JSON / 结构不对 / 指纹对不上，四种都返回 `None`，**都不抛** |
| 写不进去不吵 | 父路径是个文件 → 返回 `False`，**不留 `.part`**（缓存不是数据） |
| 改一个文件就让列表缓存失效 | 场次文件 `utime` 一动，那一场的钥匙就变（旧行不作数）。A87 把钥匙从"整份清单一个"拆成"一场一个"，`_index_stamp` 随之改成 `_row_stamps` + `_dbc_stamp` |
| 改一份 DBC 也让列表缓存失效 | `dbc/` 里加一份 → 钥匙变（"改了 DBC 通道数会变"那条） |
| 缓存写坏照常出结果 | 把 `index_cache` 写成半截 JSON → 新建的 `SessionLibrary` 照常列出同样的场次 |

**回归四项（本机实测）**：`Ran 467 tests` + `OK`；`verify_ld_vs_csv` `PASS - 0 channel(s)
outside tolerance`；两份金标准快照各 `PASS`（7 圈 / 471 通道行、26 圈 / 377 通道行）；
真浏览器 **188 项检查：188 通过，0 失败**。

---

## A71 · 跨天的墙钟不再把同一天的分卷切开（本次修复）

`group_recordings` 把"记录器在 100 万帧处切开"的两卷并回一次记录，判据是**两个时钟
同时接上**（墙钟差在窗口内 + 相对时钟的增量与它对得上）。但它排序用的 `first_wall`
是**一天中的时刻**（`17:47:48.137053`）——**没有日期**。数据一长就出事：

```
17:39:36 (10-03)  173936
17:41:42 (10-05)  174142   ← 别的日期的日志插进来了
17:47:48 (10-03)  174748
17:52:26 (10-05)  175226   ← 正好插在 174748 与 175413 之间
17:54:13 (10-03)  175413
```

于是 `174748 → 175226`（跨天，"间隔"4.6 分钟）与 `175226 → 175413` 都不满足判据，
**本该并成一场的两卷被切开**——侧边栏里多出一场、快照也变成两份半截。实测触发它的
是 2026-10-05 新增的那批日志（`174142` / `175226`），而这两卷从 ticket #39 起一直是
并着的。

```powershell
# 合成帧表复现：同一天的两卷 + 一份"时刻正好夹在中间"的别的日期的日志
python -m unittest tests.test_i3pro.TestCanLog.test_跨天的墙钟不会把同一天的分卷切开 -v

# 真数据：这两卷要还是 `+1` 那一场
python -m unittest tests.test_i3pro.TestCanLog -v
```

**通过判据**：

| 断言 | 实测 |
| --- | --- |
| 同一天的两卷照旧并起来 | 合成用例：`2026_10_03_174748` + `2026_10_03_175413` 仍是一组（墙钟差 0.249 ms、相对时钟对得上） |
| 别的日期的日志自成一场 | 同一批里的 `2026_10_05_175226` 单独一组，不再插进上面那两卷之间 |
| 真数据 | `SessionLibrary.names()` 里那两份回到 `2026_10_03_174748_ID0001+1`（改动前是两份） |
| 认不出日期的文件不受影响 | 名字里没有日期时退回原来的判据（只看时钟），不会因为"认不出"就不并 |
| 9 份金标准仍是 7 场 | `TestCanLog.test_contiguous_recordings_merge_into_seven` 照旧（0.026 ms / 0.249 ms 两条实测证据还在） |

修法：新增 `canlog._wall_day(path)`——日期从**文件名**拿（记录仪自己就是这么命名的），
排序键变成 `(日期, 时刻)`，连续性判据加上"两边日期相同（或有一边认不出）"。

**回归四项（本机实测）**：`Ran 468 tests` + `OK`；`verify_ld_vs_csv` `PASS - 0 channel(s)
outside tolerance`；两份金标准快照各 `PASS`（7 圈 / 471 通道行、26 圈 / 377 通道行）；
真浏览器 **188 项检查：188 通过，0 失败**。

---

## A72 · 新机器 clone 下来能不能跑（本次新增）

README 一直写着"`git clone` 下来就能跑"，但那句话只在**机器上已经有那四个包**时成立。
拿一个什么都没装的解释器实跑一次，暴露出三件事：

1. `i3pro.cmd` 用 `python -m i3pro --help` 当探针找解释器。缺 `numpy` 时它自己也失败，
   于是**"缺包"被报成"找不到 Python 3"**——照着提示去重装 Python，装完还是跑不起来。
2. 仓库里**没有 `requirements.txt`**，也没有一句话说该装什么。`pyproject.toml` 里有，
   但没有任何东西保证它和实际 import 对得上。
3. 三个 bat 的**非正常分支是坏的**：`启动.bat` 的报错分支里 `echo` 带一个没转义的
   `)`（藏在 `.xlsx)` 里），提前关掉 `if (` 块，cmd 直接 `. was unexpected at this
   time.` 一个字都不打印；`导入数据.bat` 的用法分支同一处毛病。而"没数据 / 缺依赖"
   正是新机器上必走的那一支。

```powershell
# 1) 干净解释器：一个什么都没装的 venv，等价于"队友刚装完 Python 的机器"
python -m venv $env:TEMP\i3pro-clean
& "$env:TEMP\i3pro-clean\Scripts\python.exe" -m pip list     # 只有 pip

# 2) 不装依赖直接跑：必须把该敲的命令打出来，不是"找不到 Python"
$env:PATH = "$env:TEMP\i3pro-clean\Scripts;$env:PATH"
.\i3pro.cmd --help

# 3) 一键装依赖：双击 安装依赖.bat 的等价命令行
cmd /c "安装依赖.bat < nul"

# 4) 再跑一次：这次要出来中文帮助
.\i3pro.cmd --help

# 5) 清单不许漂移 + 三条 bat 的解析 + 第三方 import 全覆盖
python -m unittest tests.test_i3pro.TestBootstrap -v
```

**通过判据（本机实测）**：

| 步骤 | 实测 |
| --- | --- |
| 干净 venv | `pip list` 只有 `pip 24.2`（Python 3.13.0） |
| 缺包时 `i3pro.cmd --help` | 打印 `[ERROR] Python python found, but the runtime packages are missing.` + `python -m pip install -r requirements.txt`，**退出码 9009**（不再说"找不到 Python"） |
| `安装依赖.bat` | `Successfully installed … numpy-2.5.3 … pandas-3.0.6 … pyarrow-25.0.1 … openpyxl-3.1.5`，随后自己念出版本号；**退出码 0** |
| 装完 `i3pro.cmd --help` | 中文帮助正常打印 `MoTeC i2 Pro 数据工具链 (i3pro)` + 14 个命令 |
| `TestBootstrap` | **6 项全过**（三条 bat 的解析、ASCII 清单、清单一致性、第三方 import 全覆盖、安装脚本指向同一份清单） |
| 全新 clone 的场次数 | `SessionLibrary(['i2pro_data']).names() == []`——**一个场次都没有是正常的**，界面写"没有找到任何日志文件" |

顺带修掉一个**当场踩到**的坑：`requirements.txt` 第一版里有一句中文注释，
`安装依赖.bat` 直接死在
`UnicodeDecodeError: 'gbk' codec can't decode byte 0xa3 in position 124`——
pip 在没有 BOM 时按**本机 locale**（中文 Windows 是 cp936）解码 requirements 文件，
一个非 ASCII 字节就让整个安装停在下载之前。所以那份清单**必须全是 ASCII**，由
`TestBootstrap.test_requirements_txt_lists_exactly_the_pyproject_dependencies` 盯着。

**回归四项（本机实测）**：`Ran 474 tests` + `OK`；`verify_ld_vs_csv` `PASS - 0 channel(s)
outside tolerance`；两份金标准快照各 `PASS`；真浏览器 **188 项检查：188 通过，0 失败**。

---

## A73 · 侧边栏的 `+N` 与"点开读到哪几份"出自同一处（本次修复）

用户报的现象：**两份时间相接的 CSV，单独看各自都有数据，合并成 `+1` / `+2` 之后数据就少了。**

实测根因**不是合并丢数据**，而是"这场由哪几份文件组成"这件事被**两个地方各判了一遍**，
而两边规则不一样：

* 侧边栏算名字时（`library._entries()`）是**跨所有数据根**一起分组
  （`_groups(frames)`）——而 `启动.bat` 正好把 `i2pro_data` 与 `can_data` 都当数据根；
* 打开时（`read_can_session`）只扫**主文件自己那个目录**的邻居
  （`first.parent.glob("*.csv")`）。

于是：一份在 `i2pro_data/`、另一份在 `can_data/` 时，侧边栏写着 `recA_ID0001+1`，
点开**只有 `recA` 一份**。最小复现（两卷各 1,000 帧、分别放在两个数据根）：

| | 侧边栏 | 点开这一场 |
| --- | --- | --- |
| 修之前 | `['recA_ID0001+1']` | 帧 **1000** · 来源 `['recA_ID0001.csv']` · 9.990 s |
| 修之后 | `['recA_ID0001+1']` | 帧 **2000** · 来源 `['recA_ID0001.csv','recB_ID0001.csv']` · **19.990 s** |

修法：**分组只判一次，判完把成员表交下去**。

* `library` 新增 `_entries()`（名字 → 主文件 + **全部成员**），`_paths()` 由它派生，
  并新增 `members_of(name)`；
* `SessionLibrary.get(name)` 把成员表交给 `library.open_session(..., sources=...)`
  → `csvlog.open_session(..., sources=...)` → `canlog.read_can_session(given, discover=False)`；
* `read_can_session` 新增 `discover`：给了成员表（哪怕只有一份）就**不许**再回邻居里扩——
  "一份也被扩成两份"是同一个坑从后门回来（实测：`152948` / `153603` 这两个条目）。

顺带把上一轮查出来的另一半补上：**日志不是从记录起点开始时要说出来**。

```powershell
# 两卷跨两个数据根：名字说 +1、内容必须也是两份
python -m unittest tests.test_i3pro.TestSplitSessionsAreWhole -v

# 真数据：侧边栏每个 +N 都要与成员表、字节数对上
python -m unittest tests.test_i3pro.TestSplitSessionsAreWhole.test_the_real_sidebar_never_promises_more_files_than_it_loads -v

# 首帧相对时钟不是 0 -> 报告里必须明说"前面还有卷，不在这儿"
python -m unittest tests.test_i3pro.TestSplitSessionsAreWhole.test_a_log_that_starts_mid_recording_says_so -v
```

**通过判据（本机实测）**：

| 断言 | 实测 |
| --- | --- |
| 跨数据根的分卷整场读进来 | `recA_ID0001+1`：2 份来源 / 2,000 帧 / 19.990 s，字节数 = 两份文件之和 |
| 同一对放进同一个目录结论相同 | 2 份 / 2,000 帧（规则只有一条） |
| 只声明一份时不再自己扩 | `discover=False` 下 `sources == ['recB_ID0001.csv']`、1,000 帧 |
| 真数据不再有名实不符 | 侧边栏 **74 个条目 / 12 个 `+N` 分卷场 / 0 个不一致** |
| 起点不在 0 的日志会出声 | 10-07 那场：`head_gap_s = 585.700522`、`head_seq = 2,000,000`，报告里写着「前面约 586 秒不在这批文件里——不是 i3pro 丢的」 |
| 起点就是 0 的日志不啰嗦 | `head_gap_s == 0.0`，报告里没有那句话 |

**回归四项（本机实测）**：`Ran 480 tests` + `OK`；`verify_ld_vs_csv` `PASS - 0 channel(s)
outside tolerance`；两份金标准快照各 `PASS`；真浏览器 **188 项检查：188 通过，0 失败**。

---

## A74 · 用户勾过的通道保存之后不许丢（本次修复）

用户报的现象：**新建工作表，勾好通道保存，再进来又变回默认通道。**

修前实测（高避，拿 `worksheets/sheet-3.json` 的原文复刻）：

| 步骤 | 修之前 | 修之后 |
| --- | --- | --- |
| 进入工作表（规则挑出） | 本场前 8 条 `G Force Lat … Aceinna GyroZ` | 同左 |
| 左栏勾 `Σ力` / `ST torque` | 两条都在图上 | 同左 |
| 「保存」写回文件 | `config` 里**没有** `channels`（只剩 `pick`） | `config.channels = ["Σ力","ST torque"]` |
| 拿落盘那份重新进入 | 又变回那默认 8 条 | 还是 `Σ力` / `ST torque` |

根因是"规则填过的键"被记成了**单向闩**：`applyPicks()` 把键名记进 `comp.picked`，
`sheetComponent()` 保存时跳过这些键，而**没有任何地方**在用户改动之后松闩。于是凡是被
`pick` 填过的通道槽（图 / 仪表 / 通道报告 / 直方图 / 散点 / 轨迹）都再也进不了文件；
自己新建的组件（`config.channels` 是显式的、没有 `pick`）反而存得住——仓库里
`worksheets/sheet-1.json` 第一个图和它下面两个图"一半能存一半不能"就是这个。

修法：判据从"填过没填过"换成**"规则当场还算不算得出屏幕上这个值"**
（`viewer.html` 的 `ruleStillFills`），保存那一刻现算，没有要维护的状态：

* 还算得出 → 不写进文件（没动过的图换场次照样自己挑通道）；
* 算不出 → 用户动过它，照实写回 `config`；文件里那条 `pick` 一律留着。

顺带修掉同一类的第二个隐患：**带 `index` 的规则只挑一条通道**，塞进散点的 `Y`
（声明里是一排）会让 `(comp.config.y || []).forEach` 抛异常——**那一套工作表进都进不去**。
现在规则填出来的值统一按组件自己 `defaults()` 声明的形状对齐（`shapePickValue`）：
一排的键永远拿一排，单值槽拿到一排只留第一条。

```powershell
# 格式那一半：显式通道与 pick 并存时读 / 写 / 导出三路都保住两份
python -m unittest tests.test_i3pro.TestWorksheets -v

# 无头：未动过不写死、动过必须写回、散点 Y 不再炸
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"

# 真浏览器真鼠标：勾一条 -> 保存 -> 切走切回，磁盘上那份文件里得有它
python tools\verify_clicks.py
```

**通过判据（本机实测）**：

| 断言 | 实测 |
| --- | --- |
| 没动过的键不写死（无头） | 规则挑的 12 条原样不进 `config`，`pick` 还在 |
| 动过的键写回（无头） | 删到只剩 2 条 → 写回那 2 条；整排换成别的 → 写回那 3 条 |
| 拿落盘那份重新进入（无头） | 还是用户那 3 条，不是规则的 12 条 |
| 通道报告走同一个入口（无头） | 勾 `Σ力` / `ST torque` → 落盘 `config.channels` 就是这两条 |
| 散点 `Y` 被带 `index` 的规则填（无头） | 进入不抛异常：`x="G Force Lat"`、`y=["G Force Lat"]`（一排） |
| 真鼠标端到端（Edge） | 勾 `amk trqlimit` → 保存 → 副本 `worksheets/codex-probe.json` 里有它 → 切「分析」再切回，图上还有它 |

**回归四项（本机实测）**：`Ran 481 tests` + `OK`（比 A73 多 1 条，就是新加的那条）；
`verify_ld_vs_csv` `PASS - 0 channel(s) outside tolerance`；两份金标准快照各 `PASS`；
真浏览器 **193 项检查：193 通过，0 失败**（比 A73 多 5 条，就是 #74 那几条）。

---

## A75 · 通道处理：低通 / 增益偏置 / 光标归零（本次新增）

用户问的是"能不能像 i2 Pro 那样对每个通道做 Filter 与 Scale/Offset"。i2 Pro 的帮助
（`Maths Overview`）里那条语义是：**生成一条同名的通道覆盖原通道**——原文
"create a new channel (of the same name) that overrides the original channel"，
快捷键 `Ctrl+Shift+F`（滤波）/ `Ctrl+Shift+O`（增益偏置）/ `Ctrl+Shift+Z`（用光标处归零）。
这一票照搬这三件事，实现落在已经有的数学通道上，`.ld` 仍然只读。

**真正的技术点**：同名自引用在引擎里被判成"绕成一个圈"，所以那条路原来根本走不通——
实测把 `filter_lp('G Force Long', 20)` 存成名为 `G Force Long` 的定义会报
「数学通道绕成一个圈了：G Force Long -> G Force Long」。改法只有一句话：`resolve_all`
里"被引用的名字 == 正在求值的定义名、且本场次真有一条同名原生通道"时解析到**原生列**；
没有同名原生通道的照旧报环（回归里两条都在）。

| 改/加 | 内容 |
| --- | --- |
| 引擎 | 同名自引用回退到原生列；求值**之前**先 `detach`（上一轮的覆盖层还留着会把滤完的值再滤一遍，而且数字看着仍然像模像样） |
| 新函数 | `filter_lp0(x, 截止Hz)` / `filter_hp0(x, 截止Hz)`：零相位（正反各跑一遍）。旧 `filter_lp` / `filter_hp` 一字不动 |
| 定义 | 多一个可选 `setup`（四栏参数）：面板生成的定义能原样读回；手写的定义不会凭空多这个键，面板也不猜、不盖 |
| 界面 | 通道列表每行 ⚙ + `Ctrl+Shift+F/O/Z`；四栏固定顺序 滤波 → 增益 → 偏置 → 归零；处理过的通道在列表与图上带 ƒ 角标；导出元数据多一条 `transformed` |
| 顺带 | `docs/deploy-check.md`：把 i3pro 放到公网要查什么、要定什么、缺什么（用户问的第二个问题，只交清单不动部署） |

```powershell
# 1) 引擎与格式：同名覆盖、不递增、零相位、setup 读回、导出元数据
python -m unittest tests.test_i3pro.TestMaths tests.test_i3pro.TestExportFiles -v
python -m unittest tests.test_i3pro.TestMathsOverHttp -v

# 2) 无头：四栏拼出来的式子、快捷键、面板读回、ƒ 角标
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"

# 3) 真浏览器真鼠标：真点 ⚙ -> 敲 20 -> 应用 -> 磁盘上有同名定义 -> Ctrl+Shift+Z 归零 -> 删除
python tools\verify_clicks.py
```

**通过判据（本机实测）**：

| 断言 | 实测 |
| --- | --- |
| 同名覆盖算得出来、而且真的换了值 | 高避 `G Force Long` 前 3 点：原生 `-0.03` → 覆盖后 `-0.0062`；`is_derived` 为真 |
| 算两遍一模一样（不递增） | 连跑两次 `apply_to_session` 逐点 `allclose`；侧车在磁盘上那一遍也过了 |
| 没有同名原生通道仍然是环 | `凭空来的 = filter_lp('凭空来的', 20)` 报「绕成一个圈了」 |
| 零相位 | 脉冲滤完峰在原位、左右对称；单程那条峰往后跑（对照）；`filter_lp0` + `filter_hp0` = 原信号 |
| 空档 | `filter_lp0` 原样留 NaN；旧 `filter_lp` 仍按 0 处理（行为不动） |
| 性能 | 46,400 样本：单程 0.068 s、零相位 0.137 s |
| `setup` 往返 | `as_dict` → `from_dict` 逐字段相同；手写定义不带这个键 |
| 导出元数据 | `transformed: ["Vx KF"]`；Excel 元数据 sheet 多一行「已处理的通道」 |
| 真鼠标端到端 | 点 ⚙ → 低通（零相位）20 Hz → `worksheets/<场次>.maths.json` 里出现 `filter_lp0('Vx KF Init', 20)`；`Ctrl+Shift+Z` 把光标处的 `51.75334` 变成 `4e-05`；切走切回还在；「删除处理」之后侧车与角标一起消失 |

**回归四项（本机实测，当时是 492 项；A76 之后是 508 项，见下）**：`Ran 492 tests` + `OK`；
`verify_ld_vs_csv` `PASS - 0 channel(s) outside tolerance`；两份金标准快照各 `PASS`；
真浏览器 **212 项检查：212 通过，0 失败**
（比 A74 的 193 多 19 条，就是 #75 那几组）。

**已知取舍**：覆盖语义下**原值只能靠「删除处理」拿回**（不做"同时看原值"的第二条通道）；
处理在算距离轴的那条车速会连带改变里程与圈速（面板会提示，那是语义不是 bug）；
切比雪夫滤波仍然不做（要 scipy，违反依赖规则，用 `filter_lp0` 代替）。

---

## A76 · CAN 帧表对到 `.ld` 场次并合并通道（`i3pro pair`，本次新增）

用户要的是："把 `10.5湾区` 里的 `.ld` 和记录仪帧表对齐，把没有的通道加到同一份数据里，
整理成 i3pro 能用的格式"。这件事本来做不了：**C125 与记录仪记的东西从来不在同一份
文件里**——`.ld` 有 459–474 条通道（电机 / AMK / MTI / GPS）但没有 IVT；帧表解出来
只有 114–138 条（`Vx_KF`、`latitude_MTI`、`I_Data`…）但没有电机通道。所以母线电流
与电机电流从来不同框，上一轮那个"电机电流推母线电流"就卡在这里。

**两条判据，各挡一类错**：

* **墙钟只管挑候选**：`.ld` 头里的 `log_time` 是记录**结束**时刻（`begin = log_time − duration`，
  实测 9 份 10.5 数据全部吻合），帧表那头用文件名里的日期 + 首/末墙钟；重叠 ≥ 20 s 才是候选。
* **偏移要在相对时钟上用数据求**：两边的钟各有各的零点，所以用两边都有的
  `Vx KF` / `Vx_KF` 互相关。**一个通道不够**——速度在短窗口上会在错的偏移上撞出 `r=1.0000`
  （实测两例），所以再拿加速度复核同一条偏移。搜索范围给到墙钟估计 **±900 s**：记录仪
  自己的钟实测能差到几分钟（10.7 湾区同一天在 19 s 到 311 s 之间跳），把搜索卡在 ±120 s
  会把 `jhy绕桩2`（峰在墙钟外 311 s）、`jhy高避`（146 s）当成"不配对"。

**第三判据：比够长才算数（2026-10-13 补，`MIN_COMPARE_POINTS = 60`）**。放宽搜索之后
冒出一类新错：墙钟窗口夹不住的那一头只剩几个到二十几个比较点，一段平滑的速度曲线在上面
随手就是 `r=1.000000`，抢走了 4 个真峰。实测这 4 例（互相关面逐点扫出来的）：

| 场次 | 假峰 | 真峰 |
| --- | --- | --- |
| `80定速滑行` | +131.67 s `r=1.000000` n=23 | −649.93 s `r=0.999929` n=276 |
| `cjh直线` | +15.07 s `r=1.000000` n=6 | −223.93 s `r=0.999614` n=197 |
| `cjh高避油门线松了` | +495.87 s `r=1.000000` n=11 | +175.07 s `r=0.999703` n=300 |
| `YJW修改过流后跑车` | −605.93 s `r=1.000000` n=6 | −932.93 s `r=0.999072` n=276 |

所以互相关至少要落在 **60 个点**上（比较窗是 1 Hz，就是 60 s）；真配对实测最少 132 个点
（`cjh高避TV1`），假峰最多 53 个（`cjh阶跃+脉冲` 对 `175226`）。不足 60 个点的判定语
是「能拿来比的时段都不到 60 个点，认不了」。

**加速度复核取三根轴的中位数**（`ACCEL_AXES`，2026-10-13 补）：只看 `accY MTI` 会误杀真配对。
实测 10.7 `jhy耐久`：速度 `r=1.0000`、`accZ` 0.970、`accX` 0.832，`accY` 只有 **0.359**
（那一段记录仪的 `accY_MTI` 自己就是死的）；三根轴的中位数是 0.832。反过来，假配对的三根轴
中位数仍在 0 附近（`cjh直线` 对上 `094537` 那一次：−0.058 / +0.121 / +0.106，中位数 0.106）。

**合并的规矩**：时间轴以 `.ld` 为准（时长 / 零点 / 采样率都是它的），CAN 通道按零阶保持
重采样上去、覆盖不到的地方留空；同名只留一份（`.ld` 的写法赢，`Vx KF` / `Vx_KF` 算同一条）；
**C125 里那些整段为 0 的 IVT 模板通道（`I Data` / `U1 Data`）会被帧表里真的有数的替换掉**
——不换的话用户拿到的是一条全 0 的母线电流，正是他要的那条反而没了。

**一个 `.ld` 对上不止一次记录时并进同一场**（2026-10-13 补）。实测 `10.4湾区测试`
2026-10-04 那天记录稀疏、每段很短，`CJH高避第三圈掉电_TV1` 的两段分别被 `135947` 与
`140346` 两次记录录到；分开写会得到 `<名>.csv` 与 `<名>-1.csv` 两份，看不出一场到底
该看哪一份。现在匹配上的全部并进同一个合并场次，各自只补自己的那一段（先来的先填、
后来的补空档，同一时刻不互相盖）。

| 改/加 | 内容 |
| --- | --- |
| 新模块 | `src/i3pro/pairing.py`：纯函数（时间窗 / 候选 / 对齐 / 合并列 / 写宽表），不 import 服务层 |
| 新子命令 | `i3pro pair <数据目录> [--ld …] [--list\|--apply] [--copy --source …] [--out …] [--min-overlap …] [--max-lag …]`；`--list` 是默认值，**一个字节都不写** |
| 侧车 | 每份帧表的 `.can.json` 里多记 `paired_ld` / `pair_offset_s` / `pair_overlap_s` / `pair_vx_r`；原始帧表与 `.ld` 一个字节不动 |

```powershell
# 1) 纯函数层：时间窗 / 候选 / 偏移 / 同名 / 空占位列 / 写出来的宽表能读回
python -m unittest tests.test_i3pro.TestPairing -v

# 2) 真数据上先只看配对表（不写任何文件）
python -m i3pro pair i2pro_data --list

# 3) 真要文件：把两边通道合到一个场次（合并产物写在数据目录里，
#    帧表保持记录仪原名——自动并卷靠它）
python -m i3pro pair i2pro_data --apply
```

**通过判据（本机实测，10.5 湾区，`out/_pair_105` = 共享目录的原样拷贝）**：

| 断言 | 实测 |
| --- | --- |
| 候选：9 场 `.ld` / 64 份帧表 / 50 次记录 | 全部时间窗按 `log_time − duration` 对上 |
| 配对成功 | `底马八字`（`165220` 4 卷，重叠 1276.0 s，r=1.0000）、`传杰八字`（`171439` 3 卷，890.0 s，1.0000）、`jhy高避烧保险掉高压`（`152948` 2 卷，474.9 s，1.0000）、`cjh高避BSPD`（`102857` 4 卷，382.3 s，1.0000）、`底马八字前动态练习`（`164008`，354.9 s，1.0000）、`右前逆变器温度异常`（`111646` 3 卷，451.9 s，1.0000） |
| 假配对被加速度挡住 | `101241`/`102623`/`102717`/`100742`/`181315` 速度都给出 `r=1.0000`，加速度复核 `-0.069 / -0.039 / -0.145 / -0.393 / 0.112` → 不配对（错偏移上速度像、别的通道不像） |
| 墙钟与真偏移确实不同 | `102857`：墙钟估 `-200.1 s`，互相关给 `-241.0 s`（差 41 s）；`152948`：墙钟 `-3.1 s`，实测 `-22.0 s`。所以"只信墙钟"会配错 |
| 合并后的场次 | `20261005-底马八字.csv` = **127,600 行 × 555 列 / 205.7 MB**；`csvlog` 读回 **287 条**通道 / 1275.99 s / 100 Hz；`looks_like_frames` 为假（不会被当成帧表）。列数比读回的通道数多，是因为 `.ld` 里 267 条整段为 0 的 C125 空模板（`Aceinna *`、`PosLat`、`AccelX`… 实测在 `.ld` 里 25520 / 127600 个样本全是 0）在合并场次里是空的，读的时候按"非数值列"跳过——**不是丢数据**（2026-10-13 逐列核过：整列为空的通道，源头也整列为 0） |
| 两边通道真的同框 | 该场次里 `I Data`（A，−84.12…0.01，126,001 个有效样本）与 `MCU1 FR TorqueCurrent` **同时存在** —— 这就是上一轮做不下去的那个分析的两个输入 |
| 空占位通道被替换 | `.ld` 里 `I Data` / `U1 Data` 整段为 0；合并后带的是帧表的真值（`I_Data` min −84.12 A、`U1_Data` 510.65–557.55 V） |
| 占位通道不是**精确**的 0 | 实测 `传杰八字` 的 `.ld` `I Data` 是 `-0.0015 … 0`（记录仪自己的量化底噪）。判"空列"的容差因此是 `|v| < 0.01`（IVT 量程 1500 A / 分辨率 1 mA，10 mA 以下等于没有）；严格 `== 0` 会让该场次合并出一个全 0 的母线电流。两侧**都真的有数**时照旧不吃掉对面（回归里两条都在） |
| 名字归并有分寸 | `Vx KF`↔`Vx_KF`、`accX MTI`↔`accX_MTI` 算同一条（保留 `.ld` 的写法）；`AngVelX_body` 与 `AngVelX_hor` 是**两条**，都被留下 |
| `--list` 不写盘 | 空目录跑一次：退出码 1、打出下一步、目录里一个文件都没有 |

**10.4 湾区测试（2026-10-13 实跑）**：11 场 `.ld` × 22 份帧表，配出 3 对；
`CJH高避第三圈掉电_TV1` 一场并进**两次记录**（`140346` 偏移 −269.985 s + `135947`
偏移 −54.971 s，各 `r=1.0000`）；`jhy高避第第二圈掉电_TV2_瞬态factor1.8` 被拒
（速度 `r=1.0000` 但加速度只有 −0.132 → 不是同一段驾驶）。合并产物 10 份、约 1.14 GB。
合并完把原始 `.ld` / `.ldx` / 帧表从数据目录里清掉（原始数据留在共享目录，一个字节没动），
清掉 43 份 / 1.27 GB，只留合并场次。

**10.7 湾区 + 10.6 中庭（2026-10-13 实跑）**：10.7 那份 14 场 `.ld` × 67 份帧表、
10.6 那份 2 场 `.ld` × 6 份帧表，配出 **12 对**，另在 10.5 上多认出 1 对
（`cjh高避TV1` 对 `095457` 2 卷，重叠 616 s，偏移 −251.997 s，`r=1.0000`）。合并产物 13 份、
1.97 GB（13 份全部 100 Hz、时间轴与 `.ld` 一致；其中 10 份同时带 `I Data`/`U1 Data`
与四条 `MCU* TorqueCurrent`）。每份的行数 / 列数 / MB 可以用
`python out\_final_106_107.py i2pro_data` 复现——脚本默认看的 `out/_merged_106_107`
是合并时的暂存，拷进数据目录与共享之后就收掉了。
没配上的 3 场（`庞仙耐久两圈`、`传杰最小半径绕圆`、`cjh100限速直线`）在记录仪那头没有
能对上的时段：前两场没有任何候选，最后一场三段候选都卡在别的判据上（最近的一段
`100816` 速度 `r=0.9999`，但加速度三轴中位数只有 0.074）。

合并完按用户要求清数据目录里的原始文件：**只删共享里拿得回来的**——本地文件去掉
`-1/-2` 与 `.can/.map` 这类侧车后缀之后的键，能在 `10.7湾区` / `10.6中庭` 里找到同名原件
才删——两处合计 199 份 / 5.82 GB：10.6/10.7 那两份 179 份（16 场 `.ld` + 15 份 `.ldx`
+ 77 份帧表 + 71 份侧车），10.5 那批 20 份（10 场 `.ld` + 10 份 `.ldx`，帧表留下——
里面是 IVT 原始数据）。删除脚本
`out/_clean_106_107.py` 有一条硬规则：只删文件名匹配记录仪命名 `^\d{4}_\d{2}_\d{2}_\d{6}_ID\d+`
的 `.csv`，因为合并出来的宽表叫 `cjh直线.csv`，去掉后缀之后和共享里的 `.ld` 是同一个键，
光看键会把刚合出来的产物一起删掉。

**第二轮：把四个日期目录重扫一遍（2026-10-13 晚）**。用户指出两件事：`20261008-传杰最小
半径绕圆.ld` 其实是 10/07 那天录的（`.ld` 头里的绝对时间不可信），而且"理论上每个 `.ld`
都有对应的 csv"。照这句话把没配上的三场重新查了一遍，查出**两个真结论**和**一个假象**：

* **`.ld` 头里的绝对时间不可靠**：实测同一天差 19–505 s 而且会跳（记录仪自己的
  `系统时间` 反倒是准的），那个文件的日期甚至差了一天。所以墙钟只能当先验——
  **候选按 `MAX_LAG_S` 放宽**（两个窗口之间隔上百来秒也算候选），偏移由数据定。
* **比较窗试两种，先过判据的算**：老窗（锚在墙钟那段重叠上）钟差小时最稳，新窗（跟着偏移
  走）钟差大时才有内容可比。实测 `cjh100限速直线` 对 `100816`：老窗只盖住 86 个点、那一段
  记录仪的 MTI 恰好是死的，速度 `r=0.9999` 却判成不配对；新窗盖住 283 个点，同一条偏移上
  加速度复核 0.941 → 配对。**老窗那一路一行都没动**，所以已经合出来的场次一个字节没变。
* **偏移要用加速度钉准**：速度那条曲线太平，`r` 在半个秒里几乎不变（`庞仙耐久两圈` 对
  `112831`：Vx 峰与加速度峰差 0.47 s，复核因此从 0.96 掉到 0.22）。所以在 Vx 认下来的峰
  附近 ±1 s 里用加速度中位数重新定一次偏移，**Vx 的判据在那条偏移上仍然要过**。
* **相关系数门槛 0.9 → 0.99**：真配对实测都在 0.9927 以上，而放宽搜索之后冒出来的假配对在
  0.906–0.908（`cjh直线` 对 `100816`、`cjh100限速直线` 对 `100027`，两边的墙钟窗口压根
  没重叠）。0.99 卡在中间。
* **同一场 `.ld` 上认下来的几次记录，隐含钟差要挨在一起**（`E_TOLERANCE_S = 150 s`）：
  `jhy耐久`（耐久，一圈一圈跑）除了自己那一次记录，还认下了 19 分钟前的那一次——两者推出的
  钟差差 308 s，而那次记录在 `.ld` 真正的时间窗里**还没开始**。这条把它剔掉了。

**重扫结果（36 场 `.ld` / 159 份帧表）**：已交付的 23 条**一条没变**（偏移逐条对得上），
**新配出 6 条 / 4 场**：

| 场次 | 记录 | 重叠 | 偏移 | r | 加速度中位数 |
| --- | --- | --- | --- | --- | --- |
| `20261005-BSPD第一次掉电` | `101241` | +22.4 s | −116.994 s | 1.0000 | 1.000 |
| `cjh阶跃+脉冲` | `175226` 3 卷 | +1026.6 s | −317.849 s | 0.9997 | 0.871 |
| `20261007-庞仙耐久两圈` | `112831` 2 卷 | −23.6 s | −174.998 s | 1.0000 | 0.999 |
| 同上 | `113516` | +176.3 s | −478.995 s | 1.0000 | 0.999 |
| `cjh100限速直线` | `100816` | +105.5 s | −19.997 s | 1.0000 | 0.941 |
| 同上 | `101451` | +150.4 s | −299.996 s | 1.0000 | 0.959 |

合并产物 4 份、约 477 MB（`20261005-BSPD第一次掉电.csv` 27,500 行 × 493 列 34.9 MB；
`cjh阶跃+脉冲.csv` 135,500 × 554，229.4 MB；`20261007-庞仙耐久两圈.csv` 66,000 × 556
（并入 2 次记录）115.9 MB；`cjh100限速直线.csv` 56,200 × 556（并入 2 次记录）96.9 MB），
共享目录与 `i2pro_data/` 各一份。

**`20261008-传杰最小半径绕圆` 没配上，而且不是时间的问题**：把 10.7 那天全部 43 次记录
（不设墙钟约束、偏移在 [−416 s, 记录时长] 里全扫一遍）逐个对过，最高的几个峰速度都能到
`r=1.0000`，加速度中位数却都在 0 附近（−0.085 / 0.009 / −0.012 / −0.002 / −0.053）——
一个都不像。共享上**根本没有 10/08 的帧表**（递归找 `2026_10_08*` 为空），而 10.7 那天
记录仪在 15:22:52–15:30:32 之间本来就没录（唯一挨得着的 `151458` 2 卷只盖到 15:22:52）。
所以这一场是"记录仪没录到"，不是"没找对"。

**已知边界**（写下来免得下次再撞）：`cjh阶跃+脉冲` 的另一个片段 `174142` 速度 `r=0.9986`、
加速度中位数 0.566——差一点没过 0.6，所以只并进了 `175226` 那一半（`.ld` 的 [318, 1355] s
有 CAN 数据）；要收进来得先看那一段记录仪的 MTI 是不是又一个坏的。周期性动作（耐久一圈
一圈跑）在内容上区分不出"整圈错位"的配对，只能靠隐含钟差那条规则兜底，而一场 `.ld` 只有
一个候选时就没有兜底。老窗那一路的偏移是按当时的代码测的：现在重算 10.4 的两个片段会落在
相邻的 0.01 s 格上（100 Hz 上就是一个采样点），已交付的文件没有重算。

**回归四项（本机实测）**：`Ran 513 tests` + `OK`；`verify_ld_vs_csv` `PASS - 0 channel(s)
outside tolerance`；两份金标准快照各 `PASS`；真浏览器 **212 项检查：212 通过，0 失败**。

**无头断言为什么没加**：这条命令是 CLI，没有界面交互（`--list` 的输出是文本表格，
`--apply` 的产物是磁盘上的 CSV）。按 AGENTS.md 第 1 条，"无头交互断言"是给**前端 / 交互类**
改动的；这里对应的是 `TestPairing` 里那 18 项单测 + `csvlog` 读回合并产物那一条
（`test_merged_output_reads_back_as_one_session`），所以 `tools/smoke_viewer.js` 不加断言。

**已知取舍**：这个偏移是"同一段驾驶"的对齐，不是逐样本的时间同步（`r=1.0000` 已是
1 Hz 上互相关的上限）；记录仪没覆盖到的时段在合并场次里是空的（零阶保持不补、不外推）；
`--apply` 只处理**配对成功**的那些（不成功的照旧留在侧边栏，用 `.ld` 与帧表两份分别看）。

---

## A77 · CSV 场次上的同名覆盖把原生列删了（"未知场次: <通道名>"，本次修复）

用户报障原文：**"设置完滤波参数以后，会提示数学通道未找到，未知场次"**。

现场是 CAN 帧表那类场次（`cjh阶跃+脉冲`，通道 `IMU_RawData_Copy_1.ACC_Y`，侧车在
2026-10-14 09:06 已经写好了，说明**第一次应用是成功的**）。真 Edge 里复现出来的
那一步是**第二次**应用：

```
PUT /api/session/cjh阶跃+脉冲/maths?scope=local  ->  404  {"error": "未知场次: IMU_RawData_Copy_1.ACC_Y"}
界面 toast：数学通道保存失败：未知场次: IMU_RawData_Copy_1.ACC_Y
```

**一个 bug，两条错**：

1. **CSV 场次的派生列和原生列同住一本字典**（`CsvSession.derived_target` 返回 `self.columns`）。
   同名覆盖是"把算出来的那列盖在原通道上"，于是 `attach` 把 `columns['IMU_RawData_Copy_1.ACC_Y']`
   原地**顶掉**；下一轮 `apply_to_session` 开头的 `detach` → `channels.clear()` 再把它
   **pop 掉**。原生列从此整条没了，求值走 `_session_series` → `CsvSession.raw()` 直接
   `KeyError`。`.ld` 踩不到：`LogFile` 的派生列一直住在**另一个**字典 `derived` 里。
2. `Api.handle` 那条 `except KeyError` 把**任何** KeyError 都说成"未知场次"，
   于是"字典里少了一个键"在界面上变成"这个场次不存在"，连报错指向的层都是错的。

| 改/加 | 内容 |
| --- | --- |
| 存储 | `CsvSession` 多一本 `derived` 字典（与 `LogFile.derived` 对称）：`derived_target` 指它，`values()` 优先读它，`raw()` 明确拒绝（"是数学通道算出来的"）。**原生列不再被顶掉、也不再被 pop** |
| 合并 | `pairing.merge_columns` 改走 `session.values()`：列分两本之后按名字查 `columns` 会漏掉本场次算出来的数学通道 |
| 报错分层 | `library.UnknownSession`（`KeyError` 的子类）：`library.get` / `maths_state` 抛它，API 层**只**把它翻成 404「未知场次」；其余 KeyError 落到 500 并写清"这是 i3pro 的 bug，不是你的数据/名字" |
| 顺带 | 页面地址里的场次名对不上时给 **404 页面**（以前是 500 `KeyError: '名字'`），并列出名字相近的场次 + 回列表的链接（`indexpage.unknown_session_page`） |

**为什么四道回归原来没拦住**：金标准两份都是 `.ld`（`LogFile.derived`），而 CSV 场次
这条路当时只有"挂一次"的用例（`test_a_csv_session_uses_the_same_seam`），
**第二次**应用这个触发条件谁都没走过。

```powershell
# 1) 单测：CSV 场次的同名覆盖（接缝 + 用户那两步）
python -m unittest tests.test_i3pro.TestChannelSeam -v
python -m unittest tests.test_i3pro.TestMathsOverHttp -v
python -m unittest tests.test_i3pro.TestServer -v

# 2) 真浏览器真鼠标：#77 在**导入进来的 CSV/文本场次**上真点两次「应用」
python tools\verify_clicks.py
```

**通过判据（本机实测）**：

| 断言 | 修之前 | 修之后 |
| --- | --- | --- |
| 用户那一步：第二次「应用」 | `404 unknown_session`，toast 是「数学通道保存失败：未知场次: IMU_RawData_Copy_1.ACC_Y」 | `200`，toast 是「已给「IMU_RawData_Copy_1.ACC_Y」加上处理：低通 20 Hz（本场次）」；随后 `/info` 与 `/trace` 都是 200 |
| 接缝：连跑 3 轮 `detach -> resolve -> attach` | 第 2 轮 `KeyError: 'GPS Speed'` | 3 轮逐点 `allclose`；摘掉覆盖层之后原生列与文件里的原值**逐点相同** |
| 真鼠标 #77（真点 ⚙ → 低通 20 Hz → 应用 → 再应用 → 删除处理） | — | 侧车 `filter_lp('TH', 20)` 一条；两次应用都 200；第二次之后光标处 `394.0535` 读得出来；**删除处理**之后回到文件里的 `396`（原生列还在，而且两数不同 = 覆盖真的生效过） |
| 未知场次的两条路 | `/api/session/nope/trace` 404（本来就是对的）；`/session/没有这个场次` **500** `KeyError` | 前者仍 404「未知场次」；后者 **404 页面**「没有这个场次 … 最接近的几条 … ← 回到场次列表重新点一个」 |

**回归四项（本机实测）**：`Ran 516 tests` + `OK`；`verify_ld_vs_csv`
`PASS - 0 channel(s) outside tolerance`；两份金标准快照各 `PASS`；真浏览器
**229 项检查：229 通过，0 失败**（比 A76 的 212 多 17 条，就是 #77 那一组）。

**五件套的落点**：① 实现 `csvlog` / `pairing` / `library` / `api` / `server` / `indexpage`；
② 单测 3 条（`TestChannelSeam` 接缝 3 轮、`TestMathsOverHttp` 两次应用、
`TestServer` 未知场次页面 404）；③ 无头交互断言 = `verify_clicks.py` 的 #77（**真鼠标**；
`tools/smoke_viewer.js` 不加——这一票没动 `viewer.html` 的界面）；④ 本条目；
⑤ 金标准两份照旧全绿，另外 CSV 那条由 `20260522-yjw第二次直线3.72.csv` 与
`verify_clicks` 导入的文本场次实跑。

**已知取舍**：CSV 场次的"原生列"与"派生列"现在真的分两本字典，所以
`session.columns` **只装文件里的列**（外部代码要按名字取一条通道时一律走
`session.values(name)`；`tests` 里两条断言跟着搬到了 `session.derived`）。
"应用一次之后左边栏变长、面板按钮会被挤出视口"是布局的现成行为（侧栏可滚动，
`verify_clicks` 里显式滚一次再点）——这一票不改布局。

---

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
| `TestMaths` | 数学通道引擎（44 项）：白名单与 AST 断言、函数集、区间统计的条件与复位、微分积分、平滑与低通、成环与前向引用、一条坏了不拖累其它、**每条坏定义各报各的缺哪条通道（#40 修）**、本地覆盖全局、缓存命中与失效、派生列在下游等价于原生通道、通道名的识别（少空格/换分隔符/大小写/歧义不猜） |
| `TestSections` | 赛道区段（19 项）：切分覆盖整圈不重不漏、弯切在该在的位置、灵敏度单调、测度整条平线时不造弯、最短段长决定"尖峰算不算弯"、手工编辑的排序/夹紧/补齐、名字去重、同一份不算改过、侧车往返与坏文件、真数据的份数与里程、每条圈的边界时刻（含"换了参考圈要提醒"）、**落在哪一段**（边界归后一段、空档不算、最后一段含终点、坏输入不猜）与**一段的时间窗口**（零宽度不给窗口） |
| `TestHistogram` | 直方图（10 项）：计数与格边界、门槛三种模式、着色取箱内均值、常量通道的区间撑开、NaN 分开报、格数夹取并说明、门槛可以是通道名或数学表达式、四种坏输入的下一步、金标准窗口统计 |
| `TestHistogramOverHttp` | `/histogram` 端点（1 项）：缺 `channel=` 400、通道不存在 400、半开区间、格数夹过带 `notice` |
| `TestSpectrum` | 频谱（12 项）：正弦落格、Parseval 与方差对上、hann/blackman 压泄漏、点数吸附到 2 的幂并说明、短数据补零并说明、50 % 重叠段数 10 → 19、`amplitude` 就是有效值、平滑压低峰但功率守恒、NaN 补齐并报数、坏输入的下一步、金标准按**通道自己的采样率**、快照只内嵌勾选的那些 |
| `TestSpectrumOverHttp` | `/spectrum` 端点（1 项）：参数一个不少、单位与采样率、400 带下一步 |
| `TestSectionsOverHttp` | 赛道区段走到 HTTP：GET 不落盘、重切落盘、手工改名字与边界、`edited` 立起来、被挡住的重切 400 + `needs_force` 且侧车不动、带 `force` 才覆盖、坏请求的下一步、`.ld` 字节不变 |
| `TestMathsOverHttp` | 数学通道走到 HTTP：存本地 / 全局、侧车落盘、坏表达式 400 且不动已存侧车、同名拦截、`shadowed`、试算接口、函数表、**同名覆盖在 CSV 场次上连按两次「应用」都是 200**（A77） |
| `TestRender` | 静态/服务两种 payload、自包含性 |
| `TestServer` | HTTP 端到端：场次列表、工作台页、通道、时间窗、散点、概览、对比圈、赛道、404；**未知场次的页面给 404 页面并说下一步**（A77；以前是 500 `KeyError`） |
| `TestIndependentParsers` | 第二套实现交叉验证、213 通道 CSV 全量对照 |
| `TestBeaconUndo` | 撤销的纯函数层：什么是"同一版"、什么时候没有可撤销的一步、交回去的是上一版本身 |
| `TestBeaconUndoOverHttp` | 撤销走真实 `PUT`：改名 / 插入 / 删除各自一步回到原样、`trusted` 迁移、落盘、一次无改动的保存不吃掉上一步、没有可撤销的一步时 400 并说明下一步、页面注入的 `laps_can_undo` 三态 |
| `TestViewerScript` | 无头驱动前端：脚本里 **840 个 `check(...)` 断言点**（`rg -o "check\(" tools/smoke_viewer.js | Measure-Object`；跑起来实际执行 **901 项**，循环里的断言点会各算一次）+ 时间轴 / 双圈两条渲染路径 + 直接打开模板的提示 |
| `TestAliases` / `TestAliasesOverHttp` | 通道别名（#36，16 项）：有序候选取第一条存在的、重复候选去掉、九种坏形状各说下一步、空候选合法、不是引用就不去别名表里翻、**同一套别名在两份金标准上落到不同通道**（`Gear Speed1` / `SpeedRL`）、`annotate` 贴出落点表、别名随文件往返、**导出 → 导入逐字段相同**、保存时给不给 aliases 的两种行为、页面载荷带着落点；HTTP：保存别名与落点、坏别名 400 带下一步 |
| `TestPalettes` | 波形配色（#35，8 项）：三套调色板齐、每套 ≥ 8 色、前八色两两 ΔE ≥ 25、相邻 ≥ 30、对底色对比度 ≥ 3.0、"缺失"灰不属于任何一套且与所有颜色 ΔE ≥ 25、色差与对比度这两把尺子本身对拍（黑/白 = 100 / 21）、页面载荷带着三套与缺失灰、不认识的调色板名退回默认 |
| `TestWorksheets` | 工作表（#30，13 项）：仓库里那七份文件全都读得出来（名字 / 身份 / 顺序 / 组件类型）、随版本发布的那几份**挑通道的规则不许被写没**（选了通道就要有 `pick` 兜着，A74 之后 `config` 里有通道名是允许的）、用户勾过的通道与 `pick` 并存时读 / 写 / 导出三路都保住两份、坏文件不连累别人、七种坏法各自的下一步（坏 JSON / schema 不认 / 键打错 / 尺寸非法 / 空组件列 / 空规则 / 规则键打错）、重名被挡、目录不存在与目录为空、载荷在快照与本地服务两条路上都带工作表（含 `--worksheets` 换目录只影响那一个载荷）、**搬进文件之后与搬之前逐字段一致**（夹具是硬编码那版倒出来的） |
| `TestExportRangeResolution` | 导出的范围解析（#23，4 项）：裸时钟沿用场次那天、范围左闭右闭、颠倒/越界各自说下一步、距离段填的是米而 `t_start` 才是秒 |
| `TestExportRanges` / `TestExportSampling` / `TestExportFiles` / `TestExportErrors` / `TestExportPerformance` | 导出的数据面（#23，17 项）：auto 保留原始采样点且含起止点、绝对时间与相对秒逐字节相同、距离轴不倒退、统一采样率不留空格、`linear`/`hold`/`mean` 各给各的数、`plan` 行数 = 实际行数、CSV 带 BOM、zip 里的 `metadata.json`、长表只写有值的格子、xlsx 的 openpyxl 往返、五类坏输入的下一步、分块写的峰值内存守门 |
| `TestExportEstimate` | 预估对得上真文件（#23，1 项）：真写一次 CSV，把"预估 ÷ 实际"锁在 0.5×–2×，并断言预估行数 = 实际行数（标定常数被改坏就红） |
| `TestExportTimestampIndex` | 绝对时间主索引（#27，7 项）：时间戳列 = 场次起点 + 相对秒（与 `epoch_of` 对拍）、长表首列逐字是 `timestamp`、元数据写明"起点精确到秒"且不选它时不出现、距离轴配时间戳是 400、时间轴配 `distance_m` 是 400、没有日期时间的场次说下一步、Excel 里是文本时间戳而不是数字 |
| `TestXlsxWriter` | 标准库写的 `.xlsx`（#23，5 项）：列名、单元格往返（空值 / NaN / 尖括号与和号）、`MAX_ROWS` 调小后真分表（99+99+99+3，不丢不重）、关掉分表要大声报错、超列数说改用 CSV |
| `TestApiLayerWithoutASocket` | API 层（#26，5 项）：不起 socket 直接调一个动作、路由表每行都有实现、动作清单就是那 17 个、每个请求都拿得到回复、请求体按需读 |
| `TestUploadEndpoint` | 浏览器上传（#31，4 项）：上传一份 `.xlsx` 落盘 + 进侧边栏 + 概览（通道数 / 设备 / URL）、同名加 `-1`、扩展名不认就 400、上传中断要吵且不留半截文件。**这个端点在这之前一条用例都没有**，所以 #26 那次重构把 `upload` 里的 `self.rfile` 留在已删除的闭包里后，浏览器上传一直 500 没人发现 |
| `TestXlsxImport` | 读 Excel（#31，13 项）：普通工作簿读成场次、**同一次导出的 CSV 与 xlsx 读回来逐点相同**、两份金标准各实跑一次（1001 行 × 2 通道）、列名里的 `[单位]` 拆成名字 + 单位、默认挑第一张能当表读的 sheet 且记得住选择、日期时间列折成相对秒并带出场次日期、没有时间列 / 宏 / 图表 / 外链 / 公式无缓存 / 不是 zip 各自报错并给下一步、同名导入不覆盖、中间的空列与文字列不错位 |
| `TestStructureOfTheSplit` | 结构性重构的守卫（#23 / #24 / #25 / #26，11 项）：`server.py` 只剩 HTTP 管道、动作分派只有一张表、库不反向依赖服务、老名字仍可用、信标住 `beacons.py`、距离轴住 `axes.py`、`laps.py` 只剩切圈、三模块共用同一份对象、**HTTP 用例共用一个夹具**（全场只有一处起服务） |
| `TestComponentRegistry` | 组件类型注册表（#17 / #19 / #20 / #21，6 项）：每种显示形式都在表里、分派只剩"哪个是图"与"哪一列是文字列"（实测 10 处）、每种形式都声明了怎么画与怎么取数、取数只有一条路（六个端点不许自己 fetch）、自检形式只挂在无头驱动的标记上 |
| `TestChannelSeam` | 通道接缝（#18，11 项）：那条规则只准写在一个模块里（扫源码）、会话必须显式声明三样、原生通道保留自己的采样率与单位、同名覆盖时保持因子/采样率/单位、Parquet 写出的是派生列本身、挂载与卸载走声明、**CSV 场次的派生列住 `derived`、同名覆盖连跑三轮都不许把原生列弄丢**（A77）、金标准 437 条通道逐条与旧公式一致（无数学通道时逐点不变） |
| `TestNotes` / `TestNotesOverHttp` | 注释（#15，15 项）：文字折行与截断、时刻校验的下一步、增删改不改原表、距离在主采样上插值、轨迹取最近抽稀点、越界不猜位置、侧车往返与坏文件、**注释不动圈速表**、HTTP 的 PUT 落盘 / 400 说明下一步 / `.ld` 字节不变 |
| `TestGpsFix` / `TestGpsFixOverHttp` | GPS 校正（#14，15 项）：`(0,0)` 只计数不进轨迹、跳点与空档各自断开、跳变两端都算坏点、**关掉校正逐点不变**、按秒与按更新周期两种偏移、分段插值绝不跨空档、路径里程跳过跳变、距离轴作用域的开关、抽稀后断点必须落在**跨着跳变的那一段**上、参数校验的中文下一步、侧车往返与坏文件、金标准（耐久 1 个 214.5 m 跳点且断的就是那 214 m 幽灵线 / 高避 0 跳点 638 个空定位）；HTTP 的 GET / PUT / 落盘 / 400 不动侧车 / `.ld` 字节不变 |
| `TestLaunchers` | 一键启动（5 项）：快照批量导出 + 索引页、**原始 CAN 帧表也导得出快照**（A54）、**两份连续记录只出一个 `+1` 快照**、缺数据目录的报错、端口占用自动换端口 |
| `TestBootstrap` | 新机器怎么装（6 项，A72）：`requirements.txt` 与 `pyproject.toml` 逐包相同、清单必须全是 ASCII（pip 按本机 locale 解码，一个汉字就装不上）、`src/` 里 import 的每个第三方包都声明过、`i3pro.cmd` 把"没 Python"与"缺包"分成两句并给出 pip 命令、安装脚本引用同一份清单、**三条 bat 的非正常分支真能打印**（`启动.bat` 的报错分支曾被一个没转义的 `)` 整死） |
| `TestSplitSessionsAreWhole` | 分卷场次的"名字 = 内容"（6 项，A73）：**跨两个数据根的分卷整场读进来**（修之前侧边栏写 `+1`、点开只有一份）、同一对放进同一个目录结论一致、只声明一份时不再回邻居里扩（`discover=False`）、首帧相对时钟不是 0 时报告里明说"前面还有卷"、起点就是 0 时不啰嗦、真数据 74 个条目 / 12 个 `+N` 场零不一致 |
| `TestTimebase` | 主时间基（#22，5 项）：那条公式在 `src` 里只剩 `timebase.py` 一处、`tests` 里 0 处、两份金标准的轴与改动前逐点相同（长度 / 首末点 / 和）、Parquet 与报表取的是同一条轴、`--rate` 换的是同一个答案 |
| `TestSidecar` | 侧车文件（#16，7 项）：六个领域模块里不再有 `read_text`/`write_text`/`json.load`/`json.dump`、六种侧车都在一处登记（含 `.ld`/`.csv`/名字带点的场次）、缺失=空、读坏=报错且**文件原样留着**（坏 JSON 与顶层形状两种）、写=原子替换且不留临时文件、新加一种侧车只要一条登记、快照/serve/命令行三条路径读到的侧车逐字节一致 |
| `TestDbc` | DBC 解析（#37，15 项）：`@0` 锯齿位序（`0x4A97` 而不是按位反转的 `0x52E9`）、`@1` 小端、有符号、factor/offset、`VAL_` 值表、标准帧与扩展帧分开、伪报文跳过（信号不许挂到上一条报文上）、**多路复用按选择子分路**（`m0` 也是一路；与 cantools 逐帧比数值**和**"这一帧该不该有这条信号"）、DLC 与实际载荷不一致时以载荷为准、两份真 DBC 的报文/信号数、14 条报文解出 46 条信号、与 `cantools` 对拍（真实帧 11.3 万帧 / 随机载荷 540 个值） |
| `TestDbcMerge` | 多份 DBC 取并集（#40，5 项）：各管各的 ID 都进并集、逐字相同的定义**不算冲突**、同一条 ID 两种定义按覆盖帧数选并写进冲突表、帧数相同时按文件名定序（可复现）、标准帧与扩展帧同号不互相盖住 |
| `TestCanLog` | 原始 CAN 帧表（#38 / #39 / #40 / #42 / #43 / #44，12 项）：帧表不被当成通道表、会话落在主时间基上且真实更新率另行记录、零阶保持与按帧时间戳重算逐点一致、**并集**后的帧数/ID 数/覆盖率（≥124 条通道、≥82%，份数从目录现算）/每条通道的来源 DBC、跑起来的日志有距离轴而原地不动的没有、固定一份 DBC 仍可用、合成的 ID 冲突进报告、侧车记下角色与 `dbc_mode` 并可用角色覆盖、找不到 DBC 时说下一步、9 份并成 7 场（含实测的 26 µs 与 249 µs 依据）与关掉并场时的 9 场、场次库每个记录一个条目、单份 91 MB ≤ 8 s、CAN 日志认得出 MTi 经纬度、**多路复用的双帧报文解成两条通道**、**同一条 ID 跨两条总线要吵出来**、**ch3 上的 IVT 真的进到场次里** |
| `TestTextImport` | 读分隔文本（#32，20 项）：四种分隔符（逗号 / Tab / 分号 / 竖线）与**连续空白**都读成同一个场次**且数值逐点相同**、预览说出的通道名与真正导入的一致（预览=导入）、分隔符猜错时预览里看得见并能改对、侧车记住解析方式（并且不被列名覆盖冲掉、空值=改回自动）、没有时间列时拒绝并给下一步、按固定采样率生成的时间列可复现（两次读逐点相同、报告里带 `generated`）、**表里本来就有时间列时生成选项不许盖掉它**、表头行指错会说清有几行、**单位行末尾多一个分隔符时不算多一列**（#64）、没有表头行时列名退化成 `列1/列2`、同一张表的 CSV 与 TXT 读回来逐点相同（含"分隔符不是逗号的 `.csv`"）、导入页带齐四个下拉框、API 暂存→预览→提交→侧车、取消会删掉暂存文件（过期的编号给 404 + 下一步）、坏选项给 400 且列出能用的值、命令行 `--preview` 不动文件且 `--rate` 落到侧车、暂存目录只清一小时前的、**稀疏 GPS 列不许把圈吃掉**（导出→读回仍是同样的圈数） |
| `TestChannelStates` | 缺失通道的三态（#34，9 项）：present / empty / missing 分得开、不知道的名字与空名字都算缺、`resolve` 保序去重、`valid_count` 与 `sample_count` 是两件事、通道索引带 `has_data`、页面载荷把缺的**带出去**而不是静默丢掉、通道报告把 missing 与 empty 分两组、金标准 437 条通道里没有空列 |
| `TestMissingChannelsOnExport` | 缺失通道走到出口（#34，5 项）：默认硬报错并说"确实想跳过就加 `skip_missing=1`"、明说跳过时名单进 `excluded_missing`（**空的时候键也在**）、Excel 元数据 sheet 里也写着这一行、一条都不剩时说下一步、导出的 CSV 里真的没有那一列 |
| `TestCanAgainstMoTeC` | CAN 侧的三条交叉检查（#42/#43/#44，4 项）：同一条 ID 跨两条总线要吵出来（合成帧表）、ch3 上的 IVT 真的进到场次里、`.can.json` 的摘要按指纹失效（圈侧车 / DBC 内容）、CAN 日志认得出 MTi 经纬度 |
| `TestListingCache` | 侧边栏列表的磁盘缓存（#45，1 项）：冷启动读磁盘缓存（把 `summary()` patch 成抛异常仍给同样的行）、数据动过（mtime）就失效重算 |
| `TestPairing` | CAN 帧表对到 `.ld` 并合并通道（A76，16 项）：`log_time` 是**结束**时刻、帧表日期从文件名拿、墙钟偏移的正负、候选按重叠筛（含"文件名不是那个形状就认不出日期"）、互相关认出的是**数据**而不是墙钟、墙钟估错的偏移照样找回来、**速度像但加速度不像要拒绝**（实测假配对的形状）、两次不相干的驾驶不配对、重叠太短说"太短"而不是猜、同名只留一份且 `.ld` 的写法赢、**整段为 0 的占位通道被真值替换**、`AngVelX_body` 与 `AngVelX_hor` 是两条都要留、新加的 CAN 通道接在 `.ld` 通道后面、写出来的宽表 `csvlog` 读得回、`--list` 一个字节都不写 |
---

## A78 · 侧边栏快速跳转到哪一栏（本次新增）

左边那一栏是"工作表 / 圈速 / 区段 / 注释 / GPS / 光标值 / 通道 / 别名 / 数学通道 /
通道处理"十块竖着排下来的，屏幕上一次只看得见两三块。找"数学通道"要滚半天，还得记住
它在谁下面。这一票在 `aside` 顶上钉一排按钮：点一下就滚到那一栏，顺手把该聚焦的控件
聚焦（找通道时直接落在搜索框上）；`Ctrl+K` 把光标放到这一排上，方向键换、回车跳。

| 改/加 | 内容 |
| --- | --- |
| 一份清单 | `JUMP_TARGETS`（`viewer.html`）：按钮、跳转落点、滚动时的高亮三处共用它。加一个模块只加一行；单测按源码核对"清单里的每一栏都存在、侧边栏里每一栏都进了清单" |
| 落点 | `jumpTo(id)` 把目标那一栏的标题对齐到**按钮排的下沿**（不是固定 34 px——按钮排自己 108 px 高，按固定数会让目标藏在它下面） |
| 收起来的模块 | 「光标值」默认收着（i2 Pro 的 Values 窗口也要按 V）：跳过去先把它打开。不打开的话隐藏元素的 rect 全是 0，跳了等于没跳 |
| 高亮 | 滚动时按"哪一栏的顶边过了按钮排"点亮对应的按钮（`syncJumpActive`）。**只算看得见的模块**：收起来的模块 rect 全是 0，不能算"已经滚过它"（2026-10-14 评审修掉"高亮一直停在收起来的「光标值」上"，见下） |
| 键盘 | `Ctrl+K` 聚焦这一排（当前那一栏优先）→ 左右方向键换 → 回车跳。它排在"光标在输入框里就不管"那条**之前**：带修饰键，不会和单字母快捷键撞，而"正落在通道搜索框里"恰恰是最想换一栏的时刻。这一排里的左右键**不冒泡**（`stopPropagation`）——冒上去会被全局那把当成"挪测量光标"（2026-10-14 评审修掉，见下） |

**真浏览器抓到的两个 bug**（无头那条抓不到，因为它没有 scroll 事件、也没有真实高度）：

1. `addEventListener("scroll", syncJumpActive)` 会把 **Event** 当第一个参数传进去，
   于是"要亮的那一栏"变成了那个事件对象——所有按钮全灭。改成包一层箭头函数。
2. 跳到隐藏的「光标值」是空动作（该组 `display:none`，rect 全 0）。加 `reveal` 一行，
   跳之前先 `state.show.values = true`。

**2026-10-14 评审（`/code-review` 的 Spec 轴）又抓到两条**，两条都补进真浏览器验收：

3. **高亮停在收起来的「光标值」上**。`syncJumpActive` 取的是"最后一个顶边过了按钮排的
   一栏"，而 `display:none` 的 `rect.top` 恒为 0（永远"过了"）——只要它排在别的候选
   后面就赢。实测：`scrollTop = 0` 与 `scrollTop = 300` 亮着的都是 `valuesGroup`；按
   `V` 把它展开之后才恢复正常。修法：rect 高度 ≤ 0 的模块直接跳过（它不在页面上）。
   假 DOM 也一并修了——它以前不看 `style.display`，"收起来的模块"量出来照样 150 px
   高，这一整类 bug 在无头那关是隐形的；现在假 DOM 认 `display:none`，无头那关也多了
   一条断言盯着它。
4. **这一排里按方向键会顺手挪测量光标**。按钮上的 `keydown` 没有 `stopPropagation`，
   事件冒到 `onKeyDown` 里的 `ArrowLeft/ArrowRight → moveCursor`。实测：焦点从
   `channelGroup` 换到 `aliasGroup` 的同时 `state.cursor` 由 `null` 变成 `0.01`。
   修法：这一排处理完 `preventDefault` 之后再 `stopPropagation`。

连带修掉的两条**判定空转**（不是产品 bug，是"这条断言其实什么都没证明"）：
「回车跳得动」原先先在「通道」上点一下、再在「通道」上按回车，前后都是 `channelGroup`；
而且 `verify_clicks.key_named` 没带 `text`，回车根本到不了页面。现在改成"先右方向键换到
「别名」、再回车、看高亮真的从 `channelGroup` 换成 `aliasGroup`"。

```powershell
# 1) 单测：清单与页面上的栏目一一对应（漏一栏 = 点了没反应）
python -m unittest tests.test_i3pro.TestViewerScript -v

# 2) 无头：按钮接线、点一下高亮跟着走、跳到收起来的模块会打开它、收起来的模块不拦高亮、
#    Ctrl+K 落到那一排
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"

# 3) 真浏览器真鼠标真键盘：滚动条真的动了、标题真的贴在按钮排下面
python tools\verify_clicks.py
```

**通过判据（本机实测，Edge 1600×1000）**：

| 断言 | 实测 |
| --- | --- |
| 按钮排 | 10 枚按钮，与侧边栏 10 个 `.group` 一一对应（单测按源码比对，多一个少一个都红） |
| 按钮排占的高度 | 108.3 px（会换行，所以落点必须按它的实际高度算） |
| 点最上面那一栏 | `scrollTop = 4`（滚条在顶） |
| 点「光标值」（默认收着） | 跳之前 `height = 0`；跳之后 `height > 0`，`scrollTop 4 → 1424`，标题落在按钮排下面 **6.1 px**（2026-10-14 重测；滚动的绝对距离随那一栏里有多少内容变，判据是那个 6.1 px 的关系，不是 1424 这个数） |
| 点「通道处理」（最后一栏） | 到底了滚不到顶（正常）：标题 `top = 584.5` 落进视野（视野底 906），按钮排上仍有一栏亮着（同日重测） |
| 点「通道」 | `document.activeElement.id === "chSearch"` |
| 真按 `Ctrl+K` | 焦点落到 `button[data-jump]` 上（实测值 `channelGroup`）；右方向键换到 `aliasGroup` 且 `state.cursor` **125 → 125 没动**；回车之后高亮真的换成 `aliasGroup` |
| 修之前（高亮那条 bug） | `scrollTop = 0` 与 `scrollTop = 300` 亮着的都是收起来的 `valuesGroup`；展开它才恢复正常 |
| 修之前（方向键那条） | 焦点换一栏的同时 `state.cursor` 由 `null` 变成 `0.01` |
| 修之前（滚动监听那个 bug） | 滚一下侧边栏，10 枚按钮**全部熄灭**（`jumpActive()` 返回空） |

**五件套的落点**：① 实现 `viewer.html` 的 `JUMP_TARGETS` / `jumpTo` / `syncJumpActive` /
`focusJumpRail`（都在同一处，没有第二份模块清单）；② 单测
`TestViewerScript.test_侧边栏快速跳转的清单与那一栏一一对应`；③ 无头断言
`smoke_viewer.js` 第 34 段（2026-10-14 起还钉"收起来的模块不拦高亮"）；④ 本条目；
⑤ 两份金标准快照照旧全绿（这一票没动取数）。

**回归四项（本机实测，与 A79 同一轮）**：`Ran 525 tests in 348.614s` + `OK`；
`verify_ld_vs_csv` `PASS - 0 channel(s) outside tolerance`；两份金标准快照各 `PASS`
（高避 466 通道行 / 7 圈，耐久 372 通道行 / 26 圈）；真浏览器
**251 项检查：251 通过，0 失败**——比 A77 的 229 多 22 条，就是 A78 的 11 条 + A79 的 11 条。

**2026-10-14 评审修完之后重跑的回归四项（本机实测）**：`Ran 532 tests in 353.560s` + `OK`；
`verify_ld_vs_csv` `PASS - 0 channel(s) outside tolerance`（两条各 213/213，最差
`AccelX` 0.00498 / `Aceinna GyroX` 0.4）；两份金标准快照各 `PASS`；真浏览器
**254 项检查：254 通过，0 失败**（比 251 多 3 条：收起来的模块不拦高亮、方向键不挪光标、
回车真的跳）。

**同一份验收跑在"修之前"的代码上（把这两处改动临时撤掉再跑一次）**：
`254 项检查：252 通过，2 失败`——红的正是那两条，而且红得和评审量到的一模一样：
`收着=True · 高亮=valuesGroup`、`125 -> 125.01`。这两条断言不是跟着实现写出来的。

---

## A79 · 曲线连接：i2 Pro 的 Channel Up-sampling（本次新增）

用户的原话：**"我觉得 i2 Pro 里的曲线会更丝滑，是因为采样点的问题吗，还是 i2 Pro 有插值？"**

**调研结论：是"点与点之间怎么取值"的问题，不是采样点数量的问题。** i2 Pro 帮助文件
《Log Files → Channel Properties》里有一条 **Up-sampling** 属性，原文：

> "The up-sampling property specifies how to calculate the channel value when a value is
> required that lies between sample points. **This is used when displaying a graph to 'join
> the dots'.**"  —— 取值：Linear（线性插值）/ Closest（最近点）/ Previous（保持前值）/ Next（下一个点）

我们这边**画图的取数路一直是"保持前值"**（`derive.hold_to_master` 把每个原始采样重复
k 次拉到 100 Hz 主时间基上），于是慢通道画出来是一排平顶加竖直跳变。实测高避金标准：
437 条通道里 **147 条**比 50 Hz 慢（100 Hz ×268 / 10 Hz ×90 / 1 Hz ×37 / 50 Hz ×22 /
2 Hz ×10 / 20 Hz ×7 / 5 Hz ×2 / 25 Hz ×1）。10 Hz 的 `LF Tire Temperature2` 在
2 秒窗口里就是"每 0.1 秒一个平顶"，而线性插值下同样这 10 个点是 36.64 → 37.73 的斜线。

**这两个数（437 / 147）连着那张直方图都是跑出来的，不是抄来的**——评审发现这里当初只写了
结论、没写命令（AGENTS 规则 2）。现在有命令了，输出就是上面那一行：

```powershell
python -m unittest tests.test_i3pro.TestUpsample.test_金标准上慢通道有多少条 -v
# [A79 实测] 高避金标准：437 条通道，其中比 50 Hz 慢的 147 条
# （100 Hz ×268 / 10 Hz ×90 / 1 Hz ×37 / 50 Hz ×22 / 2 Hz ×10 / 20 Hz ×7 / 5 Hz ×2 / 25 Hz ×1）
```

这一票加的就是这条属性：**画图默认线性**，`显示 → 曲线连接` 里可以换回阶梯保持；
分析路径（距离轴 / 切圈 / 报告 / 直方图 / 导出）**一个字都没改**，仍然是保持前值。

| 改/加 | 内容 |
| --- | --- |
| 引擎 | `derive.upsample_to_master(log, name, mode)`：四种取值与 i2 Pro 一一对应；`previous` 就是原来的 `hold_to_master`（分析路径照旧调它）；不认识的取值报中文错并列出可选项 |
| 画图 | `render.trace(..., upsample=)`，默认 `render.DEFAULT_UPSAMPLE = "linear"`；载荷里带回 `upsample` 字段（界面据此说清"这一屏取的是哪一份"）。抽稀（Min/Max 留尖峰）与两条路共用，**没有改** |
| HTTP | `/trace?upsample=linear\|previous\|closest\|next`：默认线性；打错的取值 400 + 一句能照做的中文（**在取数之前**就校验，空通道列表也不会让它蒙混过去） |
| 界面 | 组件标题栏的「显示」菜单里多一排「曲线连接」四枚按钮；换了它窗口缓存作废并重新取数；组件标题写明「曲线：阶梯保持」；跟布局一起记住（localStorage）。快照文件（没有服务端）里按钮**灰掉**并说明"导出时烘进去的，改不了" |
| 顺带 | 折线改用圆角连接（`lineJoin/lineCap = round`）：同样是那些点，尖角少一截 |

**已知边界（写清楚，不含糊）**：CSV / CAN 场次的列在**导入时**就已经零阶保持到主时间基了
（`update_rate` 只是记着"报文多久发一次"），原始采样时刻没留下来，所以那些场次的曲线
换不回斜线——这一票只对 `.ld` 的原生慢通道有效。

```powershell
# 1) 单测：四种取值 / 中文报错 / 数学通道不被再拉一遍 / 金标准上两条线确实不同
python -m unittest tests.test_i3pro.TestUpsample -v

# 2) HTTP：默认线性、显式 previous 是平顶、打错的取值 400
python -m unittest tests.test_i3pro.TestApiLayerWithoutASocket -v

# 3) 无头：四种取值都在、换了会重新取数（请求里带 upsample=）、快照里改不了但说清为什么
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"

# 4) 真浏览器真鼠标：点「显示 → 阶梯保持」之后**取回来的值真的变了**，点回线性又变回去
python tools\verify_clicks.py
```

**通过判据（本机实测）**：

| 断言 | 实测 |
| --- | --- |
| 四种取值 | `linear / previous / closest / next`；`previous` 与 `hold_to_master` **逐点相同**（`array_equal`） |
| 线性就是线性 | 10 Hz 通道在 100 Hz 主时间基上：连续 10 个点等差（0 → 10 每步 +1.0，误差 < 1e-9） |
| 极值不虚增 | 线性只在相邻两点之间取值：`line.min() == hold.min()`、`line.max() == hold.max()`（逐点比较通过） |
| 数学通道 | 同名覆盖出来的列本来就在主时间基上：四种取值都**不做二次拉伸**（逐点相同，ticket #18 那条规则没被绕过） |
| 默认值只在一处 | `render.DEFAULT_UPSAMPLE == derive.UPSAMPLE_MODES[0] == "linear"`，前端 `state.upsample: "linear"` 由单测按源码比对 |
| 真浏览器（`MCU1 FR TempMotor`，10 Hz，2 秒窗口） | 线性 `[57.9, 57.91, 57.92, 57.93, …]`（相邻点互不相同）→ 点「阶梯保持」后 `[57.9, 57.9, 57.9, 57.9, …]`（与服务端直接问 `upsample=previous` 的那一份**逐点相同**）→ 点回线性又变回斜线 |
| 真浏览器：标题 | `时间/距离图 · 1 通道 · 分栏 · 曲线：阶梯保持` |
| 快照文件大小 | 高避 `1,629,655 → 1,646,743` 字节（+1.05%）、耐久 `2,050,304 → 2,062,972`（+0.62%）——线性之下重复值变少，文本稍大一点（A79 当时的实测；2026-10-14 重测同一份快照：高避 `1,649,203`、耐久 `2,065,432` 字节） |

**2026-10-14 评审（`/code-review` 的两条轴）修了四处**——四处都不是"功能不对"，是"以后会被
抄错、而且抄错了没人红"：

| 修掉 | 原来什么样 | 现在 |
| --- | --- | --- |
| 四档取值抄在四处 | `derive.UPSAMPLE_MODES` / `render.DEFAULT_UPSAMPLE` / `viewer.html` 的 `UPSAMPLE_MODES` + `UPSAMPLE_LABELS` + `UPSAMPLE_HINT`，单测只钉了默认值那一个词 | 单测按源码比对**整张表**（`TestViewerScript.test_曲线连接的四档与界面上那四枚按钮出自同一张表`）。漂开的后果是请求里带一个服务端不认的词 = 400，而两边测试全绿 |
| 同一句报错写了两遍 | `api.act_trace` 把 `derive.upsample_to_master` 的中文错又抄了一遍，注释还写着"由 derive 报" | 新增 `derive.check_upsample(mode)`，文案只有这一处，`api` 调它。单测 `TestUpsample.test_不认识取值的报错只有一处` 扫 `src/i3pro/**/*.py`，出现两处就红 |
| 换曲线连接会连轨迹图一起重取 | `windowKey()` 里混着 `upsample`，而它同时是 `trackData.key`（`/track` 的缓存键） | 拆成 `windowKey()`（只说"看的是哪一段"）与 `traceKey()`（= windowKey + 连接方式，只有 `/trace` 用它）。无头两条断言分别盯"换了档 `traceKey` 要变"与"`windowKey` 不许跟着变"；单测另按源码钉住 `windowKey` 里不许再出现 `upsample` |
| 三个没人调的导出 | `upsampleLabels` / `buildJumpRail` / `focusJumpRail` 挂在 `window.i3pro` 上，全仓库没有调用者 | 删掉这三个导出（函数本身还在原地）。留在导出表上的都有真实调用者 |

**五件套的落点**：① 实现 `derive.upsample_to_master` + `derive.check_upsample` +
`render.trace` + `api.act_trace` + `viewer.html` 的「曲线连接」；② 单测 `TestUpsample` 8 条
（含"报错只有一处"与金标准慢通道那张直方图）+ `TestApiLayerWithoutASocket` 1 条 +
`TestViewerScript` 3 条（默认值 / 四档 / 缓存键）；③ 无头断言 `smoke_viewer.js` 第 35 段
（含"换档要重新取数"与"窗口键不许跟着变"）；④ 本条目；⑤ 两份金标准快照照旧全绿
（`verify_ld_vs_csv` 不受影响——那条路只读原始样本）。

**回归四项（本机实测，2026-10-14 评审后重跑）**：`Ran 532 tests in 353.560s` + `OK`；
`verify_ld_vs_csv` `PASS - 0 channel(s) outside tolerance`；两份金标准快照各 `PASS`；
真浏览器 **254 项检查：254 通过，0 失败**。无头驱动里 `check(` 共 **628** 条（A77 那会儿 609）。

---

## A80 · 合并出来的 csv 里没有 GPS（"GPS never got a usable fix"，本次修复）

用户报的现象：`i3pro pair` 合出来的 csv 导进 i3pro 之后**没有 GPS 轨迹**（原始 `.ld`
是有的）。查下来不是数据丢了——`GPS Latitude` / `GPS Longitude` 都好好地写在合并文件里，
`derive.gps_pair` 也认得出这一对；是 **`derive.gps_track` 把它们整条否掉了**。

**根因：一条本来就写错的判据，被"合并成一条时间轴"这件事踩响了。**

`gps_track` 拿卫星数通道判"没定位"：

```python
low_sats = np.zeros(lat.size, dtype=bool)
if log.has(sats_channel):                      # sats_channel = "GPS Sats Used"
    sats = log.values(sats_channel)
    if sats.size == lat.size:                  # <- 这一条
        low_sats = sats < 4
```

`sats.size == lat.size` 是在拿**长度**当"这条通道靠不靠得住"的替身。实测两份数据的形状：

| | 经纬度 | `GPS Sats Used` | 那条判据 |
| --- | --- | --- | --- |
| 原始 `.ld` | 50 Hz（63,800 点） | 20 Hz（25,520 点） | 长度不等 → **从来没生效** |
| 合并后的 csv | 100 Hz（127,600 点） | 100 Hz（127,600 点） | 长度相等 → 生效 |

而本车那个卫星数通道是**死的**——`20261005-底马八字.ld` 里 25,520 个样本全是 `0`；
`20261007-jhy绕桩.ld` / `cjh100限速直线.ld` / `20261007-庞仙耐久两圈.ld` 里全是 `0` 与
`-1`（非零的只有 13 / 27 / 30 个）。于是 `sats < 4` 把所有点都判成"没有定位"，
`valid.sum() < 10` 成立，轨迹直接抛 `GPS never got a usable fix`。

**改法**（`derive.gps_track`）：不再看长度，而是

1. 把卫星数按**零阶保持**搬到经纬度那条时间轴上再逐点比（`.ld` 里两者采样率不同，
   这一步本来就得做）；
2. **只在它真的报过数时**才拿它当判据——`np.nanmax(sats) >= derive.SATS_MIN`（4）。
   一次都没报过 ≥ 4 的卫星数通道没有任何信息，不能拿它去否掉定位。
   判据没生效时 `dropped["low_sats"]` 记 0，界面上那句"卫星数不足 N 点"自然也不出现
   （以前那条判据在 `.ld` 上就是靠"长度不等"偶然躲过去的，现在是明写的规则）。

零阶保持这一份实现搬到了 `timebase.hold_sample`（`pairing` 原来那个 `hold_sample`
改成调它），两处不再各写一遍。

**判据有没有真的变**（同一份合成数据，老判据照抄出来并排算）：

| 情形 | 老判据 | 新判据 |
| --- | --- | --- |
| 死通道（整份只有 0 与 −1） | 能用的点 **0** 个 → 抛 `never got a usable fix` | 能用的点 **170** 个，`low_sats = 0` → 画得出 |
| 活的卫星数通道（0~3 颗那 20 个采样） | 能用 150 个，`low_sats = 20` | 能用 **150** 个，`low_sats = 20`（**没被改松**） |
| 卫星数比经纬度慢（40 个采样 vs 200 个） | 长度不等 → 判据不生效，`low_sats = 0` | 搬上同一条轴，`low_sats = 135` |

**真数据实跑**：把 `i2pro_data` 里**全部 26 份合并场次**过一遍（`out\_gps_prefix_count.py`
数改动前会被这条判据打掉的有多少、`out\_gps_sweep.py` 数改动后画得出来多少）：

| 26 份合并场次 | 份数 | 说明 |
| --- | --- | --- |
| 改动前会被否掉 | **22** | `GPS Sats Used` 被读成数值列，且 99.98% 的样本 < 4（如 `jhy高避` 77,500 个样本里 77,495 个） |
| 改动前侥幸躲过 | 2 | `20261005-底马八字` / `cjh高避油门线松了`——那两场的占位列在合并时恰好被写成空列、读的时候整列丢掉，老判据就没得踩 |

改动后 **26 份全部画得出轨迹、0 出错**。例：`20261005-底马八字.csv` 127,600 行 →
124,406 个定位点（x −50.3…0.1 m、y −31.0…9.5 m），`cjh阶跃+脉冲.csv` → 128,276 个定位点，
`jhy高避.csv` → 65,846 个。**不需要重新合并**：毛病在读的一侧，已经合出来的 csv 现在就能看。

```powershell
# 1) 纯函数层：死通道不否定位 / 活通道照旧否 / 慢通道照样搬
python -m unittest tests.test_i3pro.TestGpsFix -v

# 2) 真数据：每一份合并场次都要画得出轨迹（26 份、0 出错）
python out\_gps_sweep.py
```

**五件套的落点**：① 实现 `derive.gps_track` + `timebase.hold_sample`；② 单测
`TestGpsFix` 新增 3 条（死通道 / 活通道 / 慢通道）；③ 无头断言**不加**——这条改的是
服务端算轨迹的判据，没有新的界面状态，`smoke_viewer.js` 里那份 GPS 汇总载荷本来就是注入
的静态数据（改动前后逐字相同）；④ 本条目；⑤ 两份金标准快照照旧全绿。

**回归四项（本机实测）**：`Ran 532 tests in 353.560s` + `OK`（A80 当时是 528；
2026-10-14 评审又加了 4 条）；`verify_ld_vs_csv` `PASS - 0 channel(s) outside tolerance`；
两份金标准快照各 `PASS`；真浏览器 **254 项检查：254 通过，0 失败**（当时是 251）。

**跑测试要先看磁盘**：这套回归会把 `i2pro_data` 拷进 `%TEMP%`（`verify_clicks.py` 那条，
以及 `TestLaunchers` / `TestBootstrap` 那几条），合并场次进来之后这一份拷贝要 ~7 GB。
本机 `C:` 只剩 0 GB 时这四条会以 `OSError: [Errno 28] No space left on device` 成片报错
——那是磁盘不是代码。把 `%TEMP%` 指到有空间的分区再跑即可（本次实测：
`$env:TEMP="E:\桌面\i3pro\out\_temp"; $env:TMP=$env:TEMP`）。

---

## A81 · 「这一场有哪些圈」只有一个入口（本次修复）

2026-10-15 的架构评审量出来的：**同一个 `.ld` 会给出两个圈速答案**。
`laps.detect_laps` 是**算法**入口（只看算法，不看侧车），`laps.detect_from_config` 才是
"这一场有哪些圈"（读用户选的信标 / 模式 / 可信圈）。界面与侧边栏走的是后者，
而 track / delta / render 摘要 / 快照索引四条命令行走了前者。

| 改/加 | 内容 |
| --- | --- |
| 单源 | `laps.lap_table(log)` 不给 laps 时问的就是 `detect_from_config`（以前默认走自动路） |
| 四处调用点 | `cli.py` 的 `cmd_track` / `cmd_delta` / `cmd_render` 摘要 / `cmd_snapshot` 索引改走 `detect_from_config` |
| 说清楚 | `laps.py` 的模块文档把两个入口的差别写死：一个是算法，一个是这一场的答案 |
| 守卫 | `TestLaps.test_这一场的圈只有一个入口`：按源码钉住 `src/i3pro` 里除 `laps.py` 不许再出现 `detect_laps(` |

```powershell
python -m unittest tests.test_i3pro.TestLaps tests.test_i3pro.TestLapModes -v
python -m i3pro track "i2pro_data\20260912-TV0.ld"        # 侧车写着 mode=figure8
```

**通过判据（本机实测，2026-10-15）**：

| 断言 | 实测 |
| --- | --- |
| `20260912-TV0` 的圈数 | 修之前 **16 圈 / 0 完整 / 最快 无**（16 行全挂 `(进出场/泊车/异常段)`）；修之后 **20 圈 / 16 完整 / 最快 10.980 s**，与侧边栏同一个数 |
| `20260908-jhy高避2圈`（侧车 `mode=run`） | 1 圈 / 0 完整 → **5 圈 / 2 完整（最快 35.120 s）** |
| 侧车说了算 | 往金标准副本写 `{"mode":"run"}` 之后 `lap_table(log)` 与 `detect_from_config` **逐行相同**，且与不看侧车的自动路**不同** |
| 源码守卫 | `src/i3pro` 里 `detect_laps(` 只出现在 `laps.py`（那 4 处调用点清零） |

**五件套的落点**：① `laps.lap_table` 的默认 + `cli.py` 四处 + `laps.py` 的文档；
② 单测 `TestLaps` 新增 2 条；③ 无头断言不加——这条改的是"取哪一份圈"，页面载荷的形状
一个字没动（`render.detect` 本来就是对的）；④ 本条目；⑤ 两份金标准快照照旧全绿。

---

## A82 · 默认比哪两条圈只有一处规则（本次修复）

同一轮评审量出来的：**`i3pro delta` 从来没跑到底过**。默认参数下
`cli.py` 里的 `args.ref_index()` 直接抛 `TypeError: 'int' object is not callable`
（解析器把 `--ref-index` 定义成 `type=int`），而"默认看哪两条圈"这件事还写了两遍
（`render.default_lap_pair` 与 `cmd_delta` 各挑一次，兜底规则不一样）。

| 改/加 | 内容 |
| --- | --- |
| 单源 | 规则搬进 `axes.default_pair(laps, ref, cmp)`（"距离轴与圈差"那一层）；`render.default_lap_pair` 只剩形状适配，`cmd_delta` 调同一个 |
| 命令能跑 | 删掉从来没工作过的 `--ref-index` / `--cmp-index`；`--ref` / `--cmp` 按**标签**点名 |
| 不静默 | 点名的标签不在这一场里时报错并列出可选标签（以前会静默换成"最快那条"） |
| 边界 | 只有一条圈时明说"没有可比的那一条"，不拿它自己比自己（差值恒为 0 的图会骗人） |

```powershell
python -m unittest tests.test_i3pro.TestDefaultPair -v
python -m i3pro delta "i2pro_data\20260908-cjh 高避5圈.ld"          # 不给任何参数
python -m i3pro delta "i2pro_data\20260908-cjh 高避5圈.ld" --ref 99  # 打错的标签
```

**通过判据（本机实测，2026-10-15）**：

| 断言 | 实测 |
| --- | --- |
| 默认参数 | `lap 左环 5 (40.440s) vs lap 左环 2 (40.560s), lap-time difference +0.120s`，并写出 `.delta.json` |
| 打错的标签 | `# --ref '99' 不是这一场的圈标签（可选: 左环 1, …, 左环 7）`，退出码 1，**不落文件** |
| 与载荷同源 | `render.default_lap_pair` 与 `axes.default_pair` 三种入参（默认 / 只给 ref / 同时给）逐个相等 |
| 命令真的跑完 | `cli.main(["delta", HILL, "--out", …])` 返回 0，写出的 `reference`/`compare` 就是 `axes.default_pair` 挑的那两条 |

**五件套的落点**：① `axes.default_pair` + `render.default_lap_pair` 转调 + `cli.cmd_delta`；
② 单测 `TestDefaultPair` 6 条（含两条端到端）；③ 无头断言不加——命令行不在页面上，
这条的判据就是命令本身；④ 本条目；⑤ 两份金标准照旧全绿（`delta` 在金标准上跑出上面那行）。

---

## A83 · 写路径只有一条缝（本次修复）

`viewer.html` 的注释里写着 `apiUrl` 是"请求地址的唯一出处"、`apiGet` 是"唯一的请求出口"，
但**只有读路径走它们**：十处写路径各自 `fetch(DATA.api + "/session/" + …)`，失败也各解一遍
（五种写法），服务端写好的中文原因在其中一种里被换成 `HTTP 400`——AGENTS 规则 6 要的是
"报错说下一步做什么"。

| 改/加 | 内容 |
| --- | --- |
| 出口 | 新增 `sessionRequest(route, {method, params, body})`：URL 走 `apiUrl`，失败解出服务端那句中文并挂在 Error 上（`body` / `status` 一起带上） |
| 十处调用点 | 信标 / 注释 / GPS（读写）/ 数学通道（读、保存、试算）/ 区段（读、保存）/ 通道索引 |
| 不留第二只手 | 单测按源码钉住 `DATA.api + "/session/"` 在 `viewer.html` 里只出现一次（就是 `apiUrl` 自己那行） |
| 只在场的 | 工作表那条路由不在 `/session/<场次>` 前缀下，仍走自己的 `fetch`（`apiUrl` 只认场次路由） |

```powershell
python -m unittest tests.test_i3pro.TestViewerScript -v
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"
python tools\verify_clicks.py
```

**通过判据（本机实测，2026-10-15）**：

| 断言 | 实测 |
| --- | --- |
| 手拼 URL | `viewer.html` 里 `fetch(` 共 5 行（`apiGet`、`sessionRequest`、导出下载 2 处、工作表路由 1 处）；`DATA.api + "/session/"` **只剩 1 处**（`apiUrl` 里面那行） |
| 服务端原因没被吞 | 真浏览器：让服务端拒一条注释（时刻落在这一场外），toast 是服务端那句中文、**不含 `HTTP 400`**，页面走的是 `sessionRequest` |
| 写路径仍然能写 | `verify_clicks.py` 里信标改名 / 插穿越 / 删信标 / 注释 / GPS / 区段 / 通道处理 / 工作表那几段全过（写路径一条没坏） |
| 快照模式 | 各写入方原有的"快照模式不能改…"提示一字未动（那些判断留在了各自的领域里） |

**五件套的落点**：① `readJson` / `failureOf` / `sessionRequest` + 十处调用点；
② 单测 `TestViewerScript.test_写路径只有一个出口`；③ 无头断言：假 DOM 里 fetch 永远 reject，
这一条归真浏览器（`verify_clicks.py` 的 `write_seam`，2 条）；④ 本条目；
⑤ 两份金标准快照照旧 PASS。

---

## A84 · 转义只有一处（本次修复）

`esc`（72 处调用）与 `escapeText`（8 处调用）**行为逐字节相同**、隔着 3800 行各活一份，
于是"转不转义"成了每个调用点的自觉——实测已经漏了一处：通道列表把通道名原样写进
`innerHTML`（`<span class="nm">` + name），而三行之隔的 `processedTitle` 却转义了。
通道名来自用户文件（`.ld` / CSV 表头），这是现成的注入点。

| 改/加 | 内容 |
| --- | --- |
| 一个转义器 | 删掉 `escapeText`，8 处调用改 `esc` |
| 补上漏的那处 | 通道列表两处行模板（在册通道 / 本场次没有）都改成 `esc(name)` |
| 守卫 | `TestViewerScript.test_转义只有一个实现`：按源码钉住 `function esc(` 只有一处、`escapeText` 不再出现 |
| 无头断言 | smoke 里往通道表里塞一条名字带尖括号的通道，断言它画出来是 `&lt;img src=x&gt;` 而不是标记 |

```powershell
python -m unittest tests.test_i3pro.TestViewerScript -v
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"
```

**通过判据（本机实测，2026-10-15）**：

| 断言 | 实测 |
| --- | --- |
| 转义器数量 | `function esc(` = 1，`escapeText` 出现 0 次 |
| 注入点 | 名字 `<img src=x>` 的通道在通道列表里渲染成 `&lt;img src=x&gt;`（无头断言；同一份断言在改之前是红的） |
| 其它转义点没坏 | 数学通道名 / 表达式 / 报错 / 函数说明 / 注释文字 / 通道处理角标全部仍走同一个 `esc` |
| 金标准快照 | 高避 466 通道行 / 7 圈，耐久 372 通道行 / 26 圈（与改动前逐行相同） |

**五件套的落点**：① `viewer.html`（删 `escapeText`、8 处改 `esc`、补通道名那两处转义）；
② 单测 `TestViewerScript.test_转义只有一个实现`；③ 无头断言：`smoke_viewer.js` 里那条
"通道名里的尖括号要说清是文本"；④ 本条目；⑤ 两份金标准快照照旧 PASS。

---

## A85 · 前端导出表按消费者收窄（本次修复）

`window.i3pro` 是前端唯一的测试缝（两个驱动 `smoke_viewer.js` / `verify_clicks.py` 都从它进来），
但它只增不减：实测导出 **207 个名字，其中 68 个在 `tools/` 与 `tests/` 里一次都没出现**
（2026-10-14 评审删掉的 3 个死导出只是这个类的零头）。缝宽了，"哪条边才是可测的"就淹掉了。

| 改/加 | 内容 |
| --- | --- |
| 收窄 | 删掉 68 个没有消费者的导出（函数本身都还在原地，只是不再挂到 `window.i3pro` 上） |
| 留痕 | 导出表顶上写明"只导出两个驱动真的用得到的名字" |
| 守卫 | `TestViewerScript.test_导出表里每个名字都有人过`：导出表里每个名字都必须在 `tools/` 或 `tests/` 里出现过（粗判，挡的是整批死导出长回来） |

```powershell
python -m unittest tests.test_i3pro.TestViewerScript -v
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"
python tools\verify_clicks.py
```

**通过判据（本机实测，2026-10-15）**：

| 断言 | 实测 |
| --- | --- |
| 导出名字数 | 207 → **139**（68 个删除） |
| 没人用的名字 | **0**（同一份扫描：`rg -w <名字>` 在 `tools/ tests/ src/`（除 viewer.html）里都能找到消费者） |
| 两个驱动没坏 | smoke 全绿（466 通道行 / 7 圈）；真浏览器全绿（写路径、信标、别名、工作表、导出那几段全过） |

**五件套的落点**：① `viewer.html` 的导出表；② 单测 `TestViewerScript.test_导出表里每个名字都有人过`；
③ 无头断言不加——这 68 个名字本来就没有无头消费者（加不了），这一条本身就是"按消费者收窄"；
④ 本条目；⑤ 两份金标准快照照旧 PASS。

---

## A86 · 通道名表搬出载荷层（本次修复）

`csvlog.canonical_names()`（**读取层 L1**）要拿三张"哪几类通道名"的表去认列名，而这三张表
住在 `render.py`（**载荷层 L3**）——于是全包唯一一处 L1 → L3 的反向依赖。表本身与
"怎么搭页面"无关，跟着载荷走是历史原因。

| 改/加 | 内容 |
| --- | --- |
| 搬家 | `DEFAULT_CHANNEL_PRIORITY` / `OVERLAY_PRIORITY` / `SPEED_FOR_COLORING` 搬进 `channelpick.py`；`render` 与 `csvlog` 都只 import 它 |
| 说清楚 | `channelpick.py` 的模块文档写明"这里只放名字表"：通道引用（写法差异）在 `channelref`，"能不能积分出距离轴"在 `derive.SPEED_CANDIDATES`（**顺序有用，别合并**） |
| 守卫 | `TestStructureOfTheSplit.test_读取层不再反向依赖载荷层`：六个读取器都不许 import 载荷层（`render` / `api` / `server` / `library` …） |

```powershell
python -m unittest tests.test_i3pro.TestStructureOfTheSplit -v
python -m unittest tests.test_i3pro.TestCanSessionSurface tests.test_i3pro.TestCsvReader -v
```

**通过判据（本机实测，2026-10-15）**：

| 断言 | 实测 |
| --- | --- |
| 反向依赖 | `csvlog.py` 里 `import render` **0 处**（改之前是第 33 行），`import channelpick` 1 处 |
| 六个读取器 | `ld` / `motec_csv` / `csvlog` / `txtlog` / `xlslog` / `canlog` / `dbc` 一个都不 import 载荷层 |
| 行为没变 | 概览条那一套用例（`TestSpeedChannelResolution`）全过：`Vx_KF` 的识别、空候选表的回落、金标准上仍是 `Vx KF` |

**五件套的落点**：① `channelpick.py`（新）+ `render.py` / `csvlog.py` 的引用；
② 单测 `TestStructureOfTheSplit.test_读取层不再反向依赖载荷层`（另：概览条那条用例改成
monkeypatch `channelpick`，因为它要模拟"速度候选表空了"）；③ 无头断言不加——这是 import 方向，
页面看不到；④ 本条目；⑤ 两份金标准快照照旧 PASS。

---

**A81–A86 的回归四项（本机实测，2026-10-15）**：`Ran 544 tests in 365.358s` + `OK`
（A80 时是 532，这六条加了 12 条单测）；`verify_ld_vs_csv` `PASS - 0 channel(s) outside
tolerance`（两条各 213/213）；两份金标准快照各 `PASS`（高避 466 通道行 / 7 圈，耐久
372 通道行 / 26 圈）；真浏览器 **256 项检查：256 通过，0 失败**（A83 那两条是新加的）。

**六条改动的实测锚点**（每一条都能在下面各自的段落里找到命令）：

| 条目 | 一句话 | 实测 |
| --- | --- | --- |
| A81 | 圈数只有一个入口 | TV0：16 圈/0 完整 → **20 圈/16 完整/10.980 s** |
| A82 | 默认双圈只有一处规则 | `i3pro delta <文件>` 从 `TypeError` → **跑完并写出 JSON** |
| A83 | 写路径只有一条缝 | 手拼 URL 11 处 → **1 处**（`apiUrl` 自己）；服务端原因原样到 toast |
| A84 | 转义只有一处 | `escapeText` 从 8 处调用 → **删除**；通道名里的尖括号渲染成文本 |
| A85 | 导出表按消费者收窄 | 207 → **139 个名字**，没人用的 **0** |
| A86 | 通道名表搬出载荷层 | `csvlog → render` 反向依赖 **0 处** |

---

## A87 · 首屏不再为"一份新日志"重算整份列表（本次修复）

用户报的现象：点 `启动.bat` 之后**浏览器一直转圈**，首页/工作台要等一分钟以上才出来。

量出来的根因（2026-10-15，110 个场次）：

| 环节 | 实测 |
| --- | --- |
| 首页（场次列表）第一次 | **68.91 s**；同一份列表第二次 **0.03 s**（内存/磁盘缓存命中） |
| 工作台页面、`/info`、`/trace`、`/laps`… | 0.03–0.12 s（都不是慢的那个） |
| 110 场里最贵的 | 93 个 CSV 场次合计 **70.6 s**；17 个 `.ld` 只占 1.3 s |
| 一份 CSV 的摘要 | 3.63 s，**其中整份解析 3.63 s、切圈 0.05 s、挂数学通道 0.04 s** |

于是"列表本身很贵"不是 bug（它是每个场次的圈数/最快圈），**bug 是失效粒度**：
整份列表只共用**一个**指纹，导入一份日志（或改一个圈侧车）会把 110 场的摘要
全部作废——用户看到的就是"每次启动都等一分钟"。

| 改/加 | 内容 |
| --- | --- |
| 一场一把钥匙 | 磁盘缓存从"一整份行 + 一个全局指纹"改成 `{"dbc": …, "sessions": {名: {钥匙, 行}}}`（`library.SessionLibrary._row_stamps` / `_dbc_stamp`） |
| 只重算变了的 | `listing()` 逐场比对：钥匙对上就用旧行，只有变了的场次才 `summary()` |
| DBC 单独一条 | DBC 树一变，CAN 场次的通道会变 → 那些行**全部**作废（一条指纹管全部，不逐行抄） |
| 形状版本 | `LISTING_CACHE_SHAPE = 2`：形状改了就把旧文件当没有，不去猜上一版长什么样 |

```powershell
python -m unittest tests.test_i3pro.TestListingCache -v

# 真数据（110 场）：先删掉缓存量一次，再模拟"导入一份 csv"
python out\_probes\time_home.py "第一次（重建）" "第二次（命中）"
```

**通过判据（本机实测，2026-10-15）**：

| 断言 | 实测 |
| --- | --- |
| 重建一次（缓存形状变了/清过 TEMP） | 首页 **68.91 s**（一次性，之后不再发生） |
| 缓存命中 | **0.03 s** |
| 新导入一份 csv 之后第一次 | **4.25 s**（只算了那一场，不再重算 110 场） |
| 把那份挪走之后第一次 | **0.12 s**（只丢掉那一行） |
| 单测 | `TestListingCache`：动一份文件只调用一次 `summary`、且是动过的那一场；新实例读磁盘缓存时一次都不算 |

**已知边界（写清楚）**：缓存形状升级后、或 `%TEMP%` 被清过 / 换机器时，第一次
仍要重建一次（69 s）——那是**一次**，不是每次；这一条不做"边算边给"的渐进式列表。

**五件套的落点**：① `library.py`（`listing` / `_row_stamps` / `_dbc_stamp` /
`_read_index_cache` / `_write_index_cache` + `LISTING_CACHE_SHAPE`）；② 单测
`TestListingCache.test_只重算钥匙变了的那个场次`（同类里那条冷启动用例照旧）；
③ 无头断言不加——这是服务端的缓存粒度，页面载荷的形状一个字没动；
④ 本条目；⑤ 两份金标准快照照旧 `PASS`。

## A88 · 姿态引擎：三个角度 → 可画的姿态（本次新增，ticket #42）

Marple Insight 有一个立体姿态图（`ATTITUDE`）和一块 PFD。它在 Aurora 的账号上被 license
挡住（`Add Plot` 的选项按 `license.features` 过滤，`Aerospace` 组不在里面），所以这一票
只能照**静态包**的行为对齐（`FigureAttitude-*.js`）：roll / pitch / yaw 三条通道 +
角度单位 + **每通道倍数**。本轮先落**算术那一半**，画布与光标幽灵在 ticket #43
（组件的判据接着补进这一条）。

数据不用新采：两份金标准都带 `Aceinna Roll` / `Aceinna Pitch` / `AngleLocalYaw`
（deg、100 Hz、2 位小数；高避 **46,400** 点 / 耐久 **194,300** 点）。

| 改/加 | 内容 |
| --- | --- |
| 约定写死，不做配置 | 世界系 x 前 / y 左 / z 上；**正 roll 抬左、正 pitch 抬头、正 yaw 左转**；`R = Rz(yaw)·Ry(pitch)·Rx(roll)`（3-2-1）；屏幕 y 向下 |
| `attitude.normalize_angle` | 单位只认 `deg`/`rad`（别的**报错并说下一步**）；每通道倍数；归一化到 `[-π, π)`；**NaN 原样返回**（画法要跳过那一帧，不许悄悄给 0） |
| `attitude.rotation_matrix` / `project` / `pfd_scales` | 纯函数、无第三方依赖；相机先偏航后俯仰、`scale` 是像素/单位；刻度每 10° |
| `attitude.pick_channels` | 三级：**实测名字**（`channelpick.ATTITUDE_*`，写法差异由 `channelref` 判）→ 名字里的词 → **留空**；同一条通道不派给两个轴 |
| 不抄的 | `eulerOrder` 六选一、自定义 3D 模型——方向不对用**每通道倍数**掰正（那正是 Marple 自己留的 `multipliers`） |

```powershell
python -m unittest discover -s tests -p test_i3pro.py -k TestAttitude -v
```

**通过判据（本机实测，2026-10-17）**：

| 断言 | 实测 |
| --- | --- |
| 姿态引擎单测 | **22 项全过**（单位与倍数 · 归一化到半开区间 · 非有限值 · 90° 特例 · 正交右手 · **3-2-1 顺序且六种排法两两可分辨** · 投影四种相机 · 相机缺键报错 · PFD 三根刻度 · 挑通道六条） |
| 金标准上挑通道 | **两份都跑**：高避5圈 与 耐久正赛 → roll `Aceinna Roll`、pitch `Aceinna Pitch`、yaw `AngleLocalYaw` |
| 全量单测 | **567 项**（545 + 22）全绿，`Ran 567 tests in 209.195s` |
| 解析裁判 | `python tools\verify_ld_vs_csv.py` → `PASS - 0 channel(s) outside tolerance` |
| 两份金标准快照 | 高避 `PASS`（466 通道行 / 7 圈）、耐久 `PASS`（372 / 26） |
| 真鼠标 | `python tools\verify_clicks.py` → **256 项通过、0 失败** |

**已知边界（写清楚）**：角度**不做多圈累计**（连续横滚两圈会在显示上跳），真实数据里没出现过；
`pfd_scales` 的闭环刻度只画一次（航向不取 360、滚转不取 180，否则两条刻度线重叠）；
兜底那一级可能挑到"名字里有这个词但不是姿态"的通道（例如 `YawAngAcc` 是角加速度），
所以实测名字永远优先，组件还要把挑中的名字显示出来。

**评审后的更正（2026-10-17，`/code-review` 两轴）**：原文写"固定 ZXY，与 Marple 默认一致"，
但实现是 **ZYX / 3-2-1**，且原来那条"钉顺序"的用例在 `pitch=0` 上跑（ZYX 与 ZXY 那时
逐元素相同）——**是空断言**。现在规格改口径（只声明我们自己用 3-2-1，不声称与它一致），
用例改成三轴非零 + 与其余五种排法都可分辨；另外补上"相机缺键直接报错"、
"非有限值（含 `inf`）原样返回"、"耐久金标准也跑一遍"三处。

**五件套的落点**：① `src/i3pro/attitude.py` + `channelpick.py` 的三张姿态表与兜底词表；
② 单测 `TestAttitude`（22 项，两份金标准各一条）；③ 无头断言不加——这一票不动前端
（组件的断言在 #43 补）；④ 本条目；⑤ 两份金标准实跑。

### 组件那一半（ticket #43，2026-10-17 补进本条）

| 改/加 | 内容 |
| --- | --- |
| 注册 `attitude` 组件 | 两个视图（`model` 立体车体 / `pfd` 天地线），拖拽转视角、双击或 `R` 复位；`ALIAS_SLOTS` 加上 `roll/pitch/yaw`（`@别名` 在这三个槽上也管用）；`R` 键归谁管由注册表声明（`resetView`），**不新增类型分派**（仓库那条门限仍是 10 处） |
| 幽灵 | 基准光标（A）处叠一层 **30% 透明**的模型与当前姿态对比——照搬 Marple 的交互 |
| 缺通道三态 | "还没选" / "本场次没有这条通道" / "这一份快照没带它的数据（用 `启动.bat` 打开，或重导快照时带上它）"，标题写"缺 N 条"；**绝不静默少画一轴** |
| NaN 帧 | 不画、不补零（补零会画出一个"车突然躺平"的假姿态） |
| 前后端一致 | 前端 `attitudeMatrix` / `attitudePick` 是后端 `attitude.py` 的镜像（前端没有构建步骤、跑不了 Python，重一份不可避免），由无头断言拿**后端算出来的数**对拍：3-2-1 矩阵最大差 < 1e-12；挑通道规则也对拍（认不出留空） |

```powershell
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"
```

**通过判据（本机实测，2026-10-17）**：

| 断言 | 实测 |
| --- | --- |
| 无头断言（新增 21 处） | 矩阵对拍 · 挑通道对拍（认不出留空）· 加进来就挑到 `Aceinna Roll`/`Aceinna Pitch`/`AngleLocalYaw` · 立体车体画线 · 抬头写全三轴 · **单位换算（10 deg → `10.0°`、10 rad → `-147.0°`）** · 幽灵开关真的少画一整套线 · PFD 填色与刻度 · 缺通道与"快照没带"两种说法 · `R` 复位 · 分享链接往返 |
| 无头断言点总数 | **650** 处（原来 629） |
| 两份金标准快照 | 高避 `PASS`（**467** 通道行 / 7 圈）、耐久 `PASS`（372 / 26） |
| 真鼠标 | `python tools\verify_clicks.py` → **264 项通过、0 失败**（含 #43 新增的 **8 条**：画布点得到 · 从下拉换成有数据的通道 · 真拖拽转视角 · 拖后像素变 · `R` 复位 · 游走换光标 · 光标一换画布重画） |
| 全量单测 | **568 项** 全绿（`Ran 568 tests in 217.791s`；与上一次 140.8 s 的差别只是机器负载） |
| 解析裁判 | `python tools\verify_ld_vs_csv.py` → `PASS - 0 channel(s) outside tolerance` |

**已知边界**：静态快照只嵌导出时选中的 12 条通道，姿态那三条通常不在里面——那时组件会说
"这一份快照没带它的数据"，**用 `启动.bat`（服务模式）是按需取数、照常能画**；要塞进快照就
重导一次（`i3pro render --channels …`）。高避那份快照的通道行数由 466 变 **467**：
`maths/global.json` 里现在有一条 `ST torque`（用户侧车、未提交），**不是这一票改的**。

**评审后的更正（2026-10-17，`/code-review` 两轴）**

1. **真 bug：`unit:"rad"` 时抬头把弧度当度印。** 原来拿"单位系数"当除数，`unit:"rad"` 时
   `10 rad` 被写成 `10.0°`（真值 −147.0°）。已改成一律 ×180/π，并加了一条无头对拍：
   把三条通道写成常量 10，`deg` 读 `10.0°`、`rad` 读 `-147.0°`。
2. **硬性欠账：`tests/test_i3pro.py` 的 `DECLARED` 没加 `attitude`。** 那条清单是"每种显示形式
   都有声明 / 无类型分派 / 有 render"三条守卫的循环对象，不登记新形式等于新形式落在守卫之外。
   已加；并补了一条"前端对拍用的冻结常量仍与后端一致"，把 `smoke_viewer.js` 里那份冻结矩阵
   在后端再钉一遍（否则后端改了约定、前端常量还停在旧值，smoke 照样绿）。
3. **票里那三条真鼠标验收原来一条都没有**（假 DOM 证明不了"拖得动、点得到"）。已补进
   `tools/verify_clicks.py`（见上表 8 条）。
4. 文档补齐：`docs/spec-marple-parity.md` 的 `E` → `R`（两处）、§3.2 增补 `camera` 与
   "`roll/pitch/yaw` 也在 `ALIAS_SLOTS` 里"、`worksheets/README.md` 的 `type` 清单加 `attitude`。
5. **顺手量到一件真事（写下来免得下一个人踩）**：两台车的 IMU 不一样——
   高避那份里 `Aceinna Roll` / `Aceinna Pitch` / `AngleLocalYaw` / `GPS Heading` **全是 0**
   （空模板通道），有数的是 `Roll (2)` / `Pitch (2)` / `yaw MTI`；耐久那份正好反过来
   （`Aceinna Roll -3.69…3.80` / `Aceinna Pitch -2.10…3.48`，而 `Roll (2)` / `Pitch (2)` 全 0）。
   所以"按名字挑"在某一台上会挑到空模板：组件现在会写一句"这三条这一屏没有变化"，换一下下拉
   即可。**"按数据挑（跳过空模板）"留作后续票**，不在这一票里改（那会改到 #42 已验收的挑通道规则）。

---

## A89 · 光标 A–B 区间统计：抬头那三项换范围（本次新增，ticket #44）

**要解决的问题**：图表抬头那三项 `Min / Max / Avg` 一直只算**当前可见区间**（跟着缩放走）。
i2 Pro / Marple 还有另一种问法：**只看基准光标 A 到主光标 B 这一段**。这不是小数点上的差别，
是"我问的是整段还是这一小段"——而原来没有地方能问出后者（导出那条 A–B 范围只给文件，
看不到屏幕上的三个数）。

**证据的成色**：Marple 的 `POST /api/v1/signals/cursor/stats` 实测响应**只有三个数**
`{min, mean, max}`（见 [marple-parity.md](marple-parity.md) 的运行时证据）。所以这一票
**不是新算法**，是给已有统计**换范围**——σ / 斜率 / 积分**不做**（规格 §1 的非目标）；
[CONTEXT.md](../CONTEXT.md) 里那条术语也一并改成"本仓库只做三个"，免得下一个人照着旧定义去实现。

| 改/加 | 内容 |
| --- | --- |
| 范围开关 | 组件栏「显示」里那三项旁多一排：**可见区间**（默认，现状）/ **光标 A–B**，与调色板、抬头字段同处一栏，跟着工作表存（`config.statsRange`）。菜单里的标签**跟着范围走**——选了 A–B 还写"可见区间最小"就是在骗人 |
| 口径 | **左闭右闭**（正好落在 A 或 B 上的样本算数，与导出那条范围同一个口径）、**反序自动交换**（A 在 B 右边是同一个区间）、只数**有效样本**（NaN / inf 不算）、**空区间印 `--` 而不是 0**（给 0 会被读成"这一段的值真的是 0"）；数的是**原始样本**，不重采样不插值 |
| 没有 A–B 时 | **那三项统计量不印数字**（连上一屏留下的字也擦掉），改在同一个抬头里说那句现成的下一步；"光标值"那一列仍在——它是**光标处的一个数**，不是区间统计。**那句话在前端只有一处实现**（`NO_AB_HINT`）：导出面板的「光标 A–B」预置与抬头共用它，`TestViewerScript` 按源码钉住"只写一遍" |
| 前后端一致 | 算术写成纯函数 `src/i3pro/cursorstats.py`（`span_of` / `interval_stats`）；前端 `viewer.html` 的 `rangeStats` / `abSpan` 是它的镜像（前端没有构建步骤、跑不了 Python），两边拿**同一组冻结常量**对拍：受控序列 `x=0…4 / v=10…50`，A=1、B=3 → `20.00 \| 40.00 \| 30.00`，B 挪到 4 → `20.00 \| 50.00 \| 35.00` |

**`cursorstats.py` 是镜像模块（没有生产调用方）**——与 `attitude.py` 同一做法：屏幕上那三个数
就在浏览器里算，没有服务端 / CLI 入口需要它。它存在的理由是"这份口径要有单一出处、要能被
测、要能当前后端裁判"：① 把"左闭右闭 / 反序交换 / 空区间给 `None`"写成可执行的定义；
② 给单测一个对照基准（含两份金标准）；③ 给真鼠标那关一个"两边是不是同一口径"的裁判
（实测逐位相同）。**别为了"让它有出口"去造一个服务端统计接口**——那是另一票的事。

```powershell
python -m unittest discover -s tests -v      # 581 项
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"
python tools\verify_ld_vs_csv.py
python tools\verify_clicks.py                # 需要 Edge + 金标准数据
```

**通过判据（本机实测，2026-10-17）**：

| 断言 | 实测 |
| --- | --- |
| 单测（新增 `TestCursorStats` 11 项 + `TestViewerScript` 2 项） | 受控序列的两个三元组 · 闭区间两端都算数 · 与 `numpy` 逐项对照（4000 点，撒了 **109** 个 NaN） · 反序自动交换 · 缺一头给 `None` · 空区间给 `None` 不是 0 · 两端落在同一处 · 两列长度不齐 · 两份金标准上各取一段与 `numpy` 一致 · 界面那两档与 `cursorstats.RANGES` 同一份 · **「没有 A–B」那句话在前端只出现一次** |
| 无头断言（新增 12 处） | 没有基准光标时三格**全藏起来且一个字都没有**、那句下一步出现 · 摆好 A/B → `20.00\|40.00\|30.00` · B 挪一格 → `20.00\|50.00\|35.00` · A 放到 B 右边结果不变 · 3.2–3.8 s 空区间 → `--\|--\|--` · 这件组件的工作表配置里真的写着 `statsRange: "ab"` · 还原成可见区间 |
| 无头断言点总数 | **662** 处（原来 650） |
| 真鼠标（`tools\verify_clicks.py`） | **273 项通过、0 失败**（含 #44 新增的 **9 项**：「显示」按钮点得到 · 菜单里「A–B」点得到 · 点完这一件组件真的换了范围 · 没放基准光标时抬头只说下一步 · **真按空格**把 A 放在光标处 · 摆好 A/B 后三格又有数字 · 前端的 A–B 区间与后端 `cursorstats.span_of` 逐位相同 · **B 一动三个数跟着变**） |
| 全量单测 | **581 项**全绿（`Ran 581 tests in 213.006s` + `OK`；同一份代码早一次跑是 226.162s，差别只是机器负载） |
| 解析裁判 | `python tools\verify_ld_vs_csv.py` → `PASS - 0 channel(s) outside tolerance`（213/213 通道在 MoTeC 显示精度内） |
| 两份金标准快照 | 高避 `PASS`（**467** 通道行 / 7 圈）、耐久 `PASS`（372 / 26） |

**已知边界**

1. **A–B 只由两个光标决定，不与缩放窗口求交**（评审点出来的歧义，已写进规格 §4）：Marple 的
   `cursor/stats` 请求里只有两个光标时刻、没有窗口；"可见区间"已经是另一种范围，两者混起来
   会算不出用户想要的那个数。serve 模式下取数本身只加载当前窗口，所以那时 A–B 也只会落在
   已加载的样本里——与既有统计同一条边界。
2. **A 与 B 之间一个原始样本都没有时印 `--`**（真鼠标那关留下的实测：A 放在 98.229 s、
   主光标还停在同一个采样间隙里，三格就是 `--\|--\|--`，B 一动就变成 `-0.12\|87.89\|60.20`）。
   这是"空区间不给 0、也不插值"的直接结果，与"可见区间里全是 NaN 时印 `--`"同一个口径。
3. **范围是每件组件的设置**（与抬头字段、调色板同一档），不是全局开关：两件图想看两种范围，
   一件选可见区间、一件选 A–B。
4. **不缓存**：主光标每次移动都重算一遍（与原有"可见区间"统计同一条路）。通道极多的场次上，
   这一条的成本归后面那张"绘图分辨率 / 分桶包络"的票（#46 / #47）一起收。
5. **别与导出那条名字相近的路混**：导出面板的「光标 A–B」是**导出一段数据**（与圈、
   时间段、距离段并列，出文件）；抬头这里是**在屏幕上算三个数**（出数字）。两者共用同一句
   提示文案，但一件是文件、一件是数字。
6. **「显示」菜单每一件组件都有**（#35 起就是这样）：范围开关与那三项统计**只对
   时间/距离图的抬头有意义**，其它显示形式（仪表、姿态、轨迹…）选了不会变——与既有的
   「曲线连接」同一个情况，不是这一票新开的坑。

**评审后的更正（2026-10-17，`/code-review` 两轴）**

1. **规格 §4 那句话有歧义，已在规格里写清**："统计只在**当前可见的样本**上算"指的是
   **用原始样本、不重采样不插值**，不是"再与缩放窗口求交"。A–B 只由两个光标决定
   （Marple 的请求里就没有窗口）——见上面已知边界 1。这是这一票最容易读错的一句。
2. **名字骗人**：`visibleStats` 现在也服务 A–B，改叫 **`rangeStats`**（并按调用方给的
   区间算，自己不管区间怎么来的）。
3. **两处贴底的一句话题算式重复**（"本场次没有" 与 "没有 A–B" 各写了一遍）：收成
   `headNote(host, cls, panel, lift, text)`，一个锚点、一处算式。
4. **工具里不再抄那句话**：`tools/verify_clicks.py` 原来把提示原文又写了一遍（正是这一票
   要求"复用，别写第二遍"要防的事），现在拿 `cursorstats.MISSING_AB` 当裁判。
5. **第二份拷贝现在会被比对**：界面上的两档名字 / 默认范围与 `cursorstats.LABELS` /
   `DEFAULT_RANGE` 之前只比"有没有"，现在**逐字比**（改一边另一边就会红）。
6. `statsOn` 里已经含了 `state.show.measure`，循环里又判了一次——去掉重复判断。

---

## A90 · 热力图：两个通道分箱 + 第三通道着色（本次新增，ticket #45）

**要解决的问题**：i2 Pro / Marple 都有一类"聚合图"是拿**两个通道**当坐标、把样本丢进网格
再上色的（RPM × 进气压力、车速 × 横向加速度这种）。我们此前只有"一条通道"的直方图与
"逐个样本画点"的散点图：前者看不到两个量的联合分布，后者在几万个样本时糊成一片。

**证据的成色（先说清楚）**：Marple 的 Heatmap 在 Aggregate 组里，本轮**没有运行时对照**
（没点开它），所以这一条是**按我们自己的需求定**的规格，只借它两件事：跟直方图同属
"聚合类"，以及**用第三通道着色**的思路。见 [spec §5](spec-marple-parity.md)。

| 改/加 | 内容 |
| --- | --- |
| 引擎 | 新增 `src/i3pro/heatmap.py`（纯函数）：两个轴**等宽分箱**、格数夹在 8–200、上界算进最后一格、范围外的样本**丢掉并报数**（塞进边缘格会让最边上那格凭空变热）、常量轴照 numpy 的规矩撑开 ±0.5、NaN / inf 一律不入格 |
| 着色 | `count`（每格多少样本）与 `z-mean`（格子里第三通道的均值，**连同 min / max 一起给出来**）。没写过着色方式时**跟着色通道走**（有 z 按均值、没有按样本数）。"要均值却没选色通道"这个组合不成立：界面上那条路**拦下来**并 toast 一句"先选一条色通道"、把档位拨回样本数（不发出那个请求）；工作表 / 分享链接带进来的那种组合，面板显示**真正用的那一档**、抬头写一句"这一份工作表写着「第三通道均值」但没选色通道——现在按样本数着色"；服务端 / 引擎那道是 **400 + 同一句理由**（`heatmap.check_scheme` 一处实现）。**任何一条路都不静默降级**——那会让颜色的含义随窗口变 |
| 组件 | 注册 `heatmap`（第 12 种显示形式）：三个按单位分组的下拉（X / Y / 色）+ 格数两个输入框 + 着色与范围两排档位；范围 **当前窗口 / 选中圈 / 整场**（`config.range`，默认 `view`），跟工作表存、能进分享链接 |
| 画法 | 一格一个矩形（**不做离屏缓存**：悬停要重画，缓存反而每动一下鼠标就作废）；色标与格子**同一段归一化**（颜色不骗人）；鼠标**悬停**圈出那一格并在抬头写"这一箱 N 点 · X / Y · 色通道均值（min…max）" |
| 歇处 | 色带只有一处实现（`heatRamp`，直方图的柱子与本组件共用）；"没选圈"那句指路也只有一处（`NO_LAP_HINT`），导出面板那句是它加一句尾巴 |

```powershell
python -m unittest discover -s tests -v      # 595 项
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"
python tools\verify_ld_vs_csv.py
python tools\verify_clicks.py                # 需要 Edge + 金标准数据
```

**通过判据（本机实测，2026-10-17）**：

| 断言 | 实测 |
| --- | --- |
| 单测（新增 `TestHeatmap` 13 项 + `TestHeatmapOverHttp` 1 项 + `TestViewerScript` 1 项） | 逐箱与 `numpy.histogram2d` 相等（4 万点随机 · 显式范围带范围外样本 · 正好落边界 · 常量轴撑开）· `z-mean` 与 `histogram2d(weights=z)/count` 一致且极值框住均值 · NaN 与全 NaN · 格数夹取 · 着色只认两种 · 空窗口也不降级 · 两份金标准 · HTTP 四条参数 / 报错 |
| 无头断言（新增 26 处） | 组件加得进来、控件条建全、X≠Y 默认 · 格数夹到 8–200 · 快照模式说清"用 serve 模式"（**不发请求**）· 没选圈时说下一步 · serve 请求带齐 x/y/z/bx/by/scheme/from/to · 缩放换键 · **着色默认跟着色通道走 / 明确选过就是明确选过 / 要均值却没选色通道时抬头说明白** · 注入载荷后：抬头写清画的是什么、悬停写"这一箱 7 点"、鼠标离开后那句话消失 · 分享链接往返保住两个轴 / 色 / 格数 / 范围 / 着色 |
| 无头断言点总数 | **688** 处（原来 662） |
| 真鼠标（`tools\verify_clicks.py`） | **283 项通过、0 失败**（含 #45 新增的 **10 项**：真点「添加」加出一件 · 默认两条不同通道 · 从三个下拉换成有数据的通道 · serve 模式真的算回来 · 抬头写明哪一段与多少点 · 切「整场」后**点数真的变**（46 348 → 46 400）· 画布点得到 · **真悬停**落到有样本的格子（实测"这一箱 4 点 · X 32.87 km/h · Y 0.12 G"）· **色标真的画在画布上**（画布文字里有 `点数` 与色标的 `17871 / 0.0`）· 量一次响应） |
| 单次响应耗时（金标准，**先量再写**） | 高避场次、整场 60×40、带第三通道着色，走本地服务的一次 `/heatmap`：**16–18 ms**（三次实测：16 / 17 / 18 ms；同一份引擎在进程内算是 12–13 ms，差值就是 HTTP 那一层） |
| 全量单测 | **596 项**全绿（`Ran 596 tests in 222.504s` + `OK`） |
| 解析裁判 | `python tools\verify_ld_vs_csv.py` → `PASS - 0 channel(s) outside tolerance` |
| 两份金标准快照 | 高避 `PASS`（**467** 通道行 / 7 圈）、耐久 `PASS`（372 / 26） |

**已知边界**

1. **快照里没有热力图**——这是有意为之，不是没做完：直方图是"一条通道一份"，可以事先算好
   内嵌；热力图是**两个通道的组合**，十来条通道就是上百对，没法对每一对都先算好。快照打开
   时组件会写明"用 `启动.bat`（serve 模式）打开"，并建议改用直方图。要把热力图也塞进快照，
   得先决定"内嵌哪几对"——那是一票单独的事（现在的答案是"不内嵌"）。
2. **没有门槛（gate）**：直方图有"按门槛排除样本"，热力图这一票不做（规格里没要求）。
   要按条件筛样本，先把它做成一条数学通道再用它当 X / Y。
3. **格数是"显示档位"不是数据**：改格数只影响这一屏的分箱，不动 `.ld`，也不改任何分析结果。
4. **悬停只写出那一格**（多少点 / 色通道的均值与极值），不写"这里是第几号弯"这类语义——
   那要靠区段，是另一条路。

**评审后的更正（2026-10-17，`/code-review` 两轴）**

1. **规格 §5 的"`z` 非空时默认 `z-mean`"原来没落实**：默认恒为 `count`，于是"选了色通道"
   还得再手动拨一次档位才按均值着色。现在 `scheme: null` 表示**没写过**——没写过就跟着色
   通道走（有 z 按均值、没有按样本数），明确选过 `count` 就一直是 `count`；分享链接里
   scheme 那一格空着也按这个口径解（老链接不会因为少一个字段就变含义）。
2. **"要均值却没选色通道"原来只在控件那条路上被拦**：工作表 / 分享链接里带进来的这种组合
   会**静默**按样本数画，而面板上还写着"第三通道均值"——正是这一票想防的"面板含义与画出来
   的颜色对不上"。现在：面板显示**真正用的那一档**（不会再出现假一致）、抬头写一句"这一份
   工作表写着「第三通道均值」但没选色通道——现在按样本数着色，选一条色通道就生效"；服务端 /
   引擎那道仍然 400，且那两句判据与话术**只有一处实现**（`heatmap.check_scheme`，界面、服务、
   引擎三处都调它）。交互那条路（用户在「色」里清空）照旧 toast 提醒并把档位拨回。
3. 文档里的数字按实测改：`TestHeatmap` **13 项**（含新加的空窗口那条）、无头断言点 **688**、
   全量单测 **596**。

---

## A91a · 绘图分辨率五档（本次新增，ticket #46）

**要解决的问题**：绘图抽稀原来是服务端 / 快照参数与前端画布宽度各算各的，
用户看不见"这条线用了多少点"。这一票先把 Marple 实测的 `500 × 2^档位` 收成
工作表级五档旋钮：**最少（500）/ 少（1000）/ 中（2000，默认）/ 多（4000）/ 最多（8000）**。

| 改/加 | 内容 |
| --- | --- |
| 单源 | 新增 `src/i3pro/resolution.py`：后端单源；`render.DEFAULT_BUCKETS` 从它来。前端是受测副本，单测按源码逐项对拍，漂开会红 |
| 工作表 | `worksheets/*.json` 顶层新增 `resolution`。旧文件不写就是 `medium`；导出时 `medium` 不写回，避免七份老文件全出 diff |
| 前端 | 工作表栏加「绘图分辨率」下拉； `/trace` 请求按当前档位带 `buckets=`，`traceKey` 带上分辨率，切档会重新取数 |
| 快照 / 服务 | `render` / `snapshot` 的 `--buckets` 默认改为跟随工作表分辨率；显式传 `--buckets` 时仍以命令行为准 |

```powershell
python -m unittest tests.test_i3pro.TestPlotResolution -v   # 末尾 OK
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"  # PASS
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"     # PASS
python tools\verify_ld_vs_csv.py                            # PASS - 0 channel(s) outside tolerance
python tools\verify_clicks.py                               # 需要 Edge + 金标准数据；末尾 299 项通过、0 失败
```

**通过判据（本机实测，2026-10-18）**：

| 断言 | 实测 |
| --- | --- |
| 单测（新增 `TestPlotResolution` 4 项） | 五档键 / 点数 / 默认回落 · 工作表 `resolution` round-trip · 前端五档表与 `resolution` 模块逐项一致 · 金标准快照载荷按工作表 `min` 预抽稀（`buckets=500`，`Vx KF` 点数 ≤ 1000），显式 `max` 为 `buckets=8000` 且点数更多 |
| 无头断言（新增 #37 一组） | 五档映射 · 非法档位回落中档 · 文案含"最少（500点）" · 切到 `min` 后 `/trace` 请求带 `buckets=500` |
| 两份金标准快照（#47 后重测） | 高避 `PASS`（3 908 297 line segments / 32 859 point rects / 467 通道行） · 耐久 `PASS`（3 646 760 / 142 043 / 372 通道行） |
| 真鼠标（`tools\verify_clicks.py`） | **299 项通过、0 失败** |
| 全量单测 | **605 项**全绿（`Ran 605 tests in 215.037s` + `OK`） |
| 解析裁判 | `python tools\verify_ld_vs_csv.py` → `PASS - 0 channel(s) outside tolerance` |

---

## A91 · 分桶 min/max 包络：抽稀改成包络 + “已抽稀（包络）”（本次新增，ticket #47）

**要解决的问题**：A91a 的五档只决定了“最多画多少个点”，但服务端仍把抽稀结果
打包成一条点列；桶内极值虽然被保留下来，前端却看不出一“点”代表一整桶。Marple
实测的取数机制是每桶 `low_bound` / `up_bound`，对应“桶内真实上下界”，不是插值。
这一票把抽稀结果改成包络：每桶一条竖线，相邻桶连外轮廓，并在图上诚实标出抽稀。

| 改/加 | 内容 |
| --- | --- |
| 纯函数 | `render.envelope_needed(sample_count, buckets)` 复现 `估计采样率 × 窗口秒数 > 2 × 分辨率` 的等价判据；`_bucket_edges` 用固定边界切桶，首桶从 0 开始、末桶到 `n` 结束 |
| 载荷 | `render.downsample` 在 `sample_count > 2 × buckets` 时返回每桶 `min` / `max` / `value`（桶均值）/ `count`、桶中心 `time`（distance 模式同中心 `distance`）、`span` 与 `envelope=true`；否则照旧返回原始点 |
| 服务 / 快照 | `/trace` 与静态快照都走 `render.trace -> render.downsample`，两条路共享同一包络规则；不再各写一份“等距点列”含义 |
| 前端 | `drawTraces` 认 `envelope`：每桶画 `max -> min` 竖线，再把相邻桶的 `max` 连成上轮廓、`min` 连成下轮廓；点样式在包络生效时自动回退成线 |
| 诚实标识 | `graphTitle` 在任一通道本次用了包络时追加 **“已抽稀（包络）”**；标题随取数结果刷新，切档或换窗口后不会留旧角标 |
| 统计 / 缩放 | 抬头 min/max 直接用桶上下界，avg 用 `Σ(桶均值 × 桶样本数) / Σ样本数`；纵轴范围把 `min` / `max` 一并纳入，避免包络竖线被裁掉 |

```powershell
python -m unittest tests.test_i3pro.TestTraceEnvelope -v   # 末尾 OK
python -m unittest discover -s tests -v                    # 末尾 OK
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"  # PASS
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"     # PASS
python tools\verify_ld_vs_csv.py                            # PASS - 0 channel(s) outside tolerance
python tools\verify_clicks.py                               # 需要 Edge + 金标准数据；末尾 299 项通过、0 失败
```

**通过判据（本机实测，2026-10-18）**：

| 断言 | 实测 |
| --- | --- |
| 单测（新增 `TestTraceEnvelope` 5 项） | 桶数 ≤ 档位 · 首末桶覆盖区间两端 · 每桶 `min` / `max` / `mean` / `count` 与 `numpy` 对照 · 不超过 `2 × 档位` 时不启用包络 · `估计采样率 × 窗口秒数 > 2 × 分辨率` 判据可复现 · 金标准快照载荷按同一规则生成包络 |
| 无头断言（新增 #38 一组） | 快照里至少有一条曲线启用包络；标题出现“已抽稀（包络）”；把本次包络标记关掉后标题角标消失；包络生效时点样式不画点；“阶梯保持”真的改变包络桶间连线形态 |
| 真鼠标（`tools\verify_clicks.py`） | **299 项通过、0 失败**（含 #47 新增 **7 项**：找到包络曲线 · 切到最多档 `/trace?buckets=8000` 且点数 ≤ 8000 · 切到最少档 `buckets=500` 且点数 ≤ 500 · 标题写“已抽稀（包络）” · 最多档点数 > 最少档 · 两档画布像素不同 · 三档请求均在 2000 ms 内返回） |
| 性能（高避金标准，真浏览器端到端 `/trace`） | 500 桶 **12 ms** · 2000 桶 **31 ms** · 8000 桶 **104 ms** |
| 两份金标准快照 | 高避 `PASS`（3 908 297 line segments / 32 859 point rects / 10 917 labels / 7 lap rows / 467 通道行） · 耐久 `PASS`（3 646 760 / 142 043 / 12 486 / 26 / 372） |
| 全量单测 | **605 项**全绿（`Ran 605 tests in 215.037s` + `OK`） |
| 解析裁判 | `python tools\verify_ld_vs_csv.py` → `PASS - 0 channel(s) outside tolerance` |

**已知边界**：包络的 `value` 是桶均值，供光标读数与平均统计使用；min/max 是桶内真实极值。
因此在包络生效的整场视图里，平均统计不再假装“用的是原始逐点样本”，而按下采样可见信息计算。
统计区间命中桶中心时会把该桶的 `min/max/count` 一并计入；A–B 或可见区间的边界如果只截到桶的一半，
边界桶会按整桶参与统计。要看逐点读数就放大窗口，让样本数跌回 `≤ 2 × 分辨率`，包络会自动退出。

**后续（ticket #53，2026-10-18）**：包络的分桶从"逐桶调 numpy"换成
`np.minimum.reduceat / np.maximum.reduceat / np.add.reduceat`（一个桶一次向量化调用，
原来是每桶三次）。**逐位相同**由单测保证：`TestTraceEnvelope._reference_envelope` 就是
被替换掉的那段逐桶循环，两份金标准上 `min / max / value / count / time / distance`
连四舍五入后的末位都相等。

```powershell
python -m unittest tests.test_i3pro.TestTraceEnvelope -v   # 8 项，末尾 OK
python tools\bench_viewer.py --http --repeat 3             # 数字写进下表
```

同一台机器、同一份金标准、同样 12 通道（`tools\bench_viewer.py --http --repeat 3`，中位数）：

| 桶 | 高避 计算 ms（#47 → #53） | 高避 HTTP ms（#47 → #53） | 耐久 计算 ms（#47 → #53） | 耐久 HTTP ms（#47 → #53） |
| --- | --- | --- | --- | --- |
| 500 | 99.5 → **43.7** | 100.4 → **43.8** | 139.0 → **82.0** | 137.9 → **81.6** |
| 1000 | 172.3 → **58.1** | 169.5 → **60.0** | 212.0 → **97.7** | 220.0 → **97.3** |
| 2000 | 315.2 → **90.9** | 310.6 → **93.2** | 387.8 → **129.3** | 374.4 → **131.6** |
| 4000 | 602.6 → **156.1** | 601.0 → **155.6** | 674.5 → **196.9** | 671.2 → **204.0** |
| 8000 | 1177.3 → **277.0** | 1148.6 → **279.8** | 1335.5 → **329.2** | 1270.1 → **334.0** |

8000 桶那一行：12 通道的分桶本身从 **~870 ms 掉到 18 ms**（`cProfile` 里 `downsample`
12 次累计 0.018 s）；剩下的 277 ms 里 **149 ms 是 `library.json_safe` 的逐值递归**、
**82 ms 是 `json.dumps`**——那两条另开了票（见性能 map），不在这张票里改。

---

## A92 · 组件栏：分组 / 原因 / 空状态 / 换成与复制（本次新增，ticket #48）

**要解决的问题**：组件入口原来是一串平铺的类型名，选到本场次没有通道的类型也不说话；组件加错类型时只能删掉重加，想试一张图的两个位置也只能从头再配。

**借用边界**：只借 Marple 的四条交互：按用途分组、不可用原因、空状态下一步、同图换类型 / 复制。
不引 Material Symbols，不做 license / 包过滤，不做 workbook / dashboard / report template / sandbox
四套并列容器，也不引在线资源。

| 改/加 | 内容 |
| --- | --- |
| 分组 | `<select id="addType">` 改成 `<optgroup>`：曲线 / 聚合 / 姿态 / 状态与报告。曲线含时间/距离图、散点图、赛道轨迹；聚合含直方图、频谱、热力图、时间差 Δ；姿态含仪表、姿态；状态与报告含状态与故障、时间报告、通道报告 |
| 原因 | 下拉下面新增 `#addTypeNote`。选到本场次没有可用通道的类型时，写一行"这一场没有 …，先导入数据或用通道别名指一条"；姿态、状态与故障分别按姿态通道与状态通道判定 |
| 空状态 | 图组件没有通道时，画布直接写"把左边的通道拖进来"；文案只有一个来源 `EMPTY_COMPONENT_HINT` |
| 换成… | 每件组件工具栏新增"换成…"，菜单按同一套分组列类型；换类型时保留位置、尺寸，并把旧组件声明出来的通道按新组件槽位搬过去（图 / 散点 / 直方图 / 频谱 / 热力图 / 轨迹 / 仪表 / 姿态 / 通道报告各自声明 `adoptChannels`） |
| 复制 | 每件组件工具栏新增"复制"；走既有 `copyComponents` 深拷贝，新 id，位置往右下错开一格，复制件和原件配置独立 |

```powershell
python -m unittest discover -s tests -v      # 末尾 OK
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"   # PASS
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"      # PASS
python tools\verify_ld_vs_csv.py             # PASS - 0 channel(s) outside tolerance
python tools\verify_clicks.py                # 需要 Edge + 金标准数据；末尾 299 项通过、0 失败
```

**通过判据（本机实测，2026-10-18）**：

| 断言 | 实测 |
| --- | --- |
| 无头断言（新增 #48 一组） | 分组标签四项齐全 · 没有通道时添加入口写原因和下一步 · 状态通道缺失也有下一步 · 空组件文案是"把左边的通道拖进来" · "换成…"后 id 不变且通道与位置还在 · "复制"后新 id 独立，改复制件不影响原件 |
| 两份金标准快照（#47 后重测） | 高避 `PASS`（3 908 297 line segments / 32 859 point rects / 467 通道行） · 耐久 `PASS`（3 646 760 / 142 043 / 372 通道行） |
| 真鼠标（`tools\verify_clicks.py`） | **299 项通过、0 失败**（含 #48 新增 **9 项**：分组标签 · 原因文案 · 空状态文案 · 找到可复制可换类型的图 · 真点「复制」 · 两块配置独立 · 真点「换成…」 · 换成菜单里有直方图 · 换后通道和位置都还在） |
| 全量单测 | **605 项**全绿（`Ran 605 tests in 215.037s` + `OK`） |
| 解析裁判 | `python tools\verify_ld_vs_csv.py` → `PASS - 0 channel(s) outside tolerance` |

**已知边界**：换类型只保证"**能搬过去的通道**与位置尺寸保留"，不会把旧组件所有控件设置原样复制成另一套组件；
新组件仍按自己的声明生成默认设置，只继承调色板 / 手选色 / 抬头字段 / 统计范围这些通用显示项。

---

## A93 · 性能基准工具 `tools\bench_viewer.py`（本次新增，ticket #51）

**要解决的问题**：性能票一直只有零散的手工数字——A91 那条"500 / 2000 / 8000 桶 =
12 / 31 / 104 ms"是**单通道、真浏览器**的口径，换一个人、换一张票就得重新量一遍，
而且量到的不是同一件事。这一票先把尺子立起来：两层数字（服务端计算 / HTTP 往返）、
五档分辨率、两份金标准，一条命令跑完，数字原样抄进验收。

| 改/加 | 内容 |
| --- | --- |
| 工具 | `tools/bench_viewer.py`：`--session` / `--data` / `--channels`（默认 12）/ `--repeat`（默认 3，取中位数）/ `--http` / `--json <路径>`；**只读**，不写数据目录 |
| 计算层 | 走真实 `/trace` 路径（`SessionLibrary` + `Api.handle`，不起 socket），报中位数耗时、JSON 字节、gzip(6) 字节 |
| HTTP 层 | `server.bind` 起临时 `ThreadingHTTPServer`（端口自动分配）+ `urllib` 真发一次请求，报往返中位数 |
| 通道 | 挑哪些通道**不在工具里另写一份规则**：直接用 `render.pick_channels(log, 12)`（界面同一条） |
| 预热 | 每档先空跑一次：解析 / 数学通道 / 距离积分是"打开这场次"的一次性代价，不算进稳态 |
| 跳过 | 金标准缺了就打印一句跳过（缺数据的机器上照样能量别的场次） |
| 单测 | `TestBenchViewer` 4 项：不依赖金标准，也**一句耗时都不断言**（换台机器就变的数钉死只会变成随机红） |

```powershell
python tools\bench_viewer.py --http --repeat 3
python -m unittest tests.test_i3pro.TestBenchViewer -v   # 末尾 OK
python tools\bench_viewer.py --session "i2pro_data\20260908-cjh 高避5圈.ld" --channels 2 --repeat 1
```

**通过判据（本机实测，2026-10-18）**：`--http --repeat 3`，每场 12 通道，两份金标准。
计算层是服务端全路径的中位数（预热一次），HTTP 层是临时服务上的真往返：

| 桶 | 高避 计算 ms | 高避 JSON KB | 高避 gzip KB | 高避 HTTP ms | 耐久 计算 ms | 耐久 JSON KB | 耐久 gzip KB | 耐久 HTTP ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 500 | 99.5 | 235.3 | 27.5 | 100.4 | 139.0 | 266.1 | 44.6 | 137.9 |
| 1000 | 172.3 | 471.6 | 114.4 | 169.5 | 212.0 | 531.5 | 166.8 | 220.0 |
| 2000 | 315.2 | 926.3 | 208.2 | 310.6 | 387.8 | 1021.1 | 313.8 | 374.4 |
| 4000 | 602.6 | 1859.4 | 395.7 | 601.0 | 674.5 | 2048.2 | 583.8 | 671.2 |
| 8000 | 1177.3 | 3607.6 | 694.3 | 1148.6 | 1335.5 | 4086.3 | 1077.9 | 1270.1 |

| 断言 | 实测 |
| --- | --- |
| 单测（新增 `TestBenchViewer` 4 项） | 计算层与直接 `Api.handle` 的响应**字节数相同**且 gzip 字节可复算 · HTTP 层与计算层取回同一份载荷 · 查询参数形状与 `api.csv_arg` / `api.int_arg` 一致 · 挑通道与 `render.pick_channels` 逐项相同，且不拿无单位的状态通道（`System Ready`）凑数 |
| 主时间基 | 高避 **46 400** 样本 · 耐久 **194 300** 样本（`timebase.length`） |
| 载荷 | 高避 8000 桶 **3 607.6 KB** / gzip **694.3 KB**；耐久 8000 桶 **4086.3 KB** / gzip **1077.9 KB** |

**已知边界**：这一层是**服务端 + HTTP**，不含浏览器绘制——用户看到的秒数还差"画一帧"，
那一段归 `tools\verify_clicks.py`。所以本表不能和 A91 的"8000 桶 104 ms"对读：那条是
**单通道、真浏览器**，本表是 **12 通道、服务端全路径**（含距离积分与每条通道的上采样），
而且走的就是界面默认挑出来的那 12 条通道。

---

## A94 · 取数载荷跳过逐值 `json_safe`（本次新增，ticket #63）

**要解决的问题**：`/trace` 的耗时在 #53 之后基本只剩序列化。高避 8000 桶 × 12 通道
实测 277 ms，其中 `library.json_safe` 的逐值递归 **149 ms**、`json.dumps` **82 ms**——
而 render 的出口本来就是内置标量，也已经用 `_finite()` 扫过 NaN/Inf，那趟走查是重复劳动。

| 改/加 | 内容 |
| --- | --- |
| 快路径 | `api._json_plain`：`json.dumps(value, ensure_ascii=False, allow_nan=False)`，只有 `/trace` 用它 |
| 保留 | `library.json_safe` 一行没删；数学通道 / 导入 / 报表这些真的可能带 NaN 的入口照旧走 `_json` |
| 报错 | 真漏进 NaN 时当场 400，文案说清"这是 i3pro 的 bug、请报给维护者"（规则 6），**不写出非法 JSON** |
| 单测 | `TestJsonFastPath` 4 项：快路径与旧路径**逐字节相同** · 别的端点仍走 `json_safe` · NaN 当场报错 · 金标准载荷逐值都是内置标量且有限 |

```powershell
python -m unittest tests.test_i3pro.TestJsonFastPath -v   # 4 项，末尾 OK
python tools\bench_viewer.py --http --repeat 3
```

**通过判据（本机实测，2026-10-18，同机同数据，12 通道，中位数）**：

| 桶 | 高避 计算 ms（#53 → #63） | 高避 HTTP ms | 耐久 计算 ms（#53 → #63） | 耐久 HTTP ms |
| --- | --- | --- | --- | --- |
| 500 | 43.7 → **33.9** | 35.3 | 82.0 → **70.7** | 71.4 |
| 2000 | 90.9 → **52.0** | 53.6 | 129.3 → **93.3** | 94.4 |
| 8000 | 277.0 → **125.1** | 127.2 | 329.2 → **177.0** | 181.7 |

| 断言 | 实测 |
| --- | --- |
| 载荷不变 | 高避 8000 桶 **3607.6 KB**、耐久 **4086.3 KB**，与改动前**逐字节相同**（单测拿 `library.dumps` 的输出对拍） |
| 票面判据 | 票面写的是"≤ 120 ms"，实测 **125.1 ms，差 4%**。那个数是拆解时的估算（`json.dumps` 单独量 81 ms + 取数 ~22 ms），没算端点自身的开销。再往下压就得动**载荷字节**了（紧凑分隔符能省 18% 体积），那是另一个决定，不在这张票里 |

---

## A95 · JSON 响应 gzip + 条件请求（本次新增，ticket #52）

**要解决的问题**：服务端已经从 1177 ms 压到 125 ms，轮到传输——一份 12 通道 8000 桶的
载荷是 3.6 MB，局域网里光等它传完就够呛。

| 改/加 | 内容 |
| --- | --- |
| gzip | 只有 **200 的 JSON、体 ≥ 64 KB、客户端带 `Accept-Encoding: gzip`** 时才压；回 `Content-Encoding: gzip` 与 `Vary: Accept-Encoding` |
| 压缩级别 | **1，不是 6**：3.6 MB 载荷实测级别 1 用 29 ms 压到 830 KB（4.3×），级别 6 要 140 ms 才压到 694 KB（5.2×）——多花 110 ms 换 20% 体积，不值 |
| 条件请求 | 响应带 `ETag`（blake2b-16，3.6 MB 实测 6 ms）；`If-None-Match` 命中回 304 空体 |
| 缓存语义 | JSON 这条从 `no-store` 改成 **`no-cache`**（存下来可以，但每次都得先问一句）。两张 HTML 页面与导出的流式文件**一个字没动** |
| 单测 | `TestHttpCompression` 6 项：压了能解回来 · 小响应不压 · 304 无体 · 载荷变了 ETag 就变 · 页面那条路仍是 `no-store` · **响应自己带的头不会被吞掉**（下表） |

```powershell
python -m unittest tests.test_i3pro.TestHttpCompression -v   # 6 项，末尾 OK
python tools\bench_viewer.py --http --repeat 3
```

**通过判据（本机实测，2026-10-18，12 通道，中位数）**。`tools\bench_viewer.py` 这次多了
两列（`gzip ms` 与 `上线 KB`）；上一张票表里的 `HTTP ms` 就是这次的 `HTTP ms`，口径没变：

| 桶 | 高避 JSON KB | 高避 上线 KB | 高避 HTTP → gzip ms | 耐久 JSON KB | 耐久 上线 KB | 耐久 HTTP → gzip ms |
| --- | --- | --- | --- | --- | --- | --- |
| 500 | 235.3 | **45.2** | 37.5 → 37.9 | 266.1 | **65.1** | 70.2 → 74.8 |
| 2000 | 926.3 | **236.6** | 57.2 → 65.0 | 1021.1 | **356.2** | 97.5 → 114.8 |
| 8000 | 3607.6 | **829.8** | 137.5 → 163.7 | 4086.3 | **1266.0** | 212.5 → 237.6 |

票面判据是"传输字节降到 1/3 以下"：高避 3607.6 → **829.8 KB（23%）**、耐久 4086.3 →
**1266.0 KB（31%）**，两条都过。

| 断言 | 实测 |
| --- | --- |
| 解压回来一样 | 单测里 `gzip.decompress(压缩体) == 未压缩体` 逐字节相等；真浏览器那条（`verify_clicks.py`）299 项全过 |
| 小响应不压 | 小于 64 KB 的响应不带 `Content-Encoding` |
| 静态那条路没动 | 首页仍旧 `Cache-Control: no-store`、没有 ETag、不压 |
| 响应自带的头 | 工作表「导出」的 `Content-Disposition` 必须还在——**这条是真浏览器验收先抓到的**：`send()` 里那条新路一开始把 `response.headers` 整个换掉了，于是"导出"变成"跳到那个 URL 看一段 JSON"（`verify_clicks` 的 #33 当场红）。修好后补了一条单测钉住 |
| 顺带降下来的 | `verify_clicks.py` 里那条 #47 的真浏览器 `/trace` 计时：500 / 2000 / 8000 桶从 **12 / 30 / 100 ms** 变成 **7 / 10 / 21 ms**（#53 + #63 + 这一票一起的结果） |

**已知取舍**：压缩是要花 CPU 的。本机（回环）那条路上是**净亏 ~26 ms**（137.5 → 163.7 ms）——
局域网里正相反：少传 2.8 MB，按 802.11ac 的 6–12 MB/s 算是省 230–460 ms。这个工具的主要用法
是"一台笔记本给全队看"，所以选压缩，本机多等的那 26 ms 认了。**不做**"只给非回环的客户端压"：
那会让本机跑出来的验收数字量不到自己刚改的东西。

---

## A96 · `/trace` 响应缓存（本次新增，ticket #54）

**要解决的问题**：同一个窗口会被反复问——缩放来回、两张图看同一段、几个人看同一场次。
每次都要重算一遍（8000 桶 12 通道 ≈ 125 ms 计算 + 26 ms 压缩），而字节一模一样。

| 改/加 | 内容 |
| --- | --- |
| 缓存 | 新模块 `src/i3pro/tracecache.py`：按 key 存最近用过的取数响应，**上限 8 项 / 64 MB**，超了淘汰最近最少用的；只在内存，进程退出就没了 |
| key | `GET /api/session/<名>/trace` 的参数（排序后）＋ **这场数据会不会变的全部指纹**：场次文件（CAN 分卷算每一份）、本地/全局数学定义、圈与 GPS 侧车。**纯 key 驱动，没有主动失效钩子**——那会写成第二处真相 |
| 两种编码都存 | 明文一份、gzip 一份，外加 ETag。命中时连哈希与压缩都不再算——只存明文的话，那 26 ms 的压缩会回来 |
| 顺带修的 | `_entries()` 每条请求都要"打开每份 CSV 读表头"来分帧表/通道表，98 场的目录实测 **18 ms**；改成按文件指纹记住答案（同一趟降到 **9 ms**，冷路径也受益）。这条记忆化有单测钉住"换了内容要重算" |
| 单测 | `TestTraceCache` 8 项 |

```powershell
python -m unittest tests.test_i3pro.TestTraceCache -v   # 8 项，末尾 OK
python tools\bench_viewer.py --http --repeat 3
```

**通过判据（本机实测，2026-10-18，12 通道，中位数）**。bench 的 HTTP 那两列从"一次往返"
改成"**冷 / 热**"（每个档位在干净的服务上打第一炮是冷的，后面同一条请求是热的）：

| 桶 | 高避 HTTP 冷/热 ms | 高避 gzip 冷/热 ms | 耐久 HTTP 冷/热 ms | 耐久 gzip 冷/热 ms |
| --- | --- | --- | --- | --- |
| 500 | 65.6 / **11.5** | 37.9 / **11.5** | 72.6 / **11.3** | 73.7 / **11.0** |
| 2000 | 59.6 / **11.7** | 64.4 / **11.3** | 98.1 / **12.2** | 106.7 / **11.6** |
| 8000 | 136.1 / **12.6** | 161.4 / **11.2** | 189.9 / **12.9** | 225.8 / **12.2** |

票面判据"同 key 第二次的往返降到冷请求的 10% 以下"：高避 12.6/136.1 = **9.3%**、
gzip 11.2/161.4 = **6.9%**；耐久 12.9/189.9 = **6.8%**、gzip 5.4%。**两条都过**。

| 断言 | 实测 |
| --- | --- |
| 第二次真的没重算 | 单测把 `render.trace` 包一层数调用：同一 URL 请求两次只算 **1** 次；换 `buckets` / `upsample` 各算一次；写一份 `.maths.json` 之后再请求要重算 |
| 命中不再压缩 | 单测盯着 `gzip.compress`：第一次 1 次、第二次 **0** 次；两次回复逐字节相同，解压回来等于明文那一份 |
| 上限 | 项数与字节两条上限都按最近最少用淘汰；一条自己就超上限的直接不存（免得把别的全挤掉，自己下次还是超） |

**已知边界**：命中省的是"算"，省不掉"传"——本机回环上 830 KB 也要 ~11 ms 才送到客户端，
所以热请求的地板在这儿，不是 0。另外 key 用的是文件指纹（字节数 + mtime），
**同样大小、mtime 又被工具原样保留**的那种改动认不出来——这与仓库里其它缓存是同一条约定。

---

## A97 · 前端像素列包络（本次新增，ticket #55）

**要解决的问题**：8000 个桶画到大约 800 像素宽的地方，一根竖线上挤着十来条——后画的盖住
先画的，画的次数全白花，观感还更脏。这一票只动**画法**：同一根竖线上的桶先合并
（min 取最小、max 取最大），统计与量程一行都不碰。

| 改/加 | 内容 |
| --- | --- |
| 合并 | `viewer.html` 新增 `pixelColumnsFor(xs, spec, sx, x0, x1, columns)`：桶数 > 像素列数时才合并，否则原样返回（恒等）。列数 = `plotW × devicePixelRatio`（按**物理**像素列算） |
| 只服务画线 | 抬头统计（可见区间 / A–B）、纵轴量程、光标读数仍然用后端给的桶；合并返回**新数组**，绝不就地改 `spec.x / min / max` |
| 边界 | 窗口外的桶在合并时就跳过（不许把窗口外的极值并进边界那一列）；非有限值跳过；标题仍是「已抽稀（包络）」，不加第二层标识 |
| 无头断言 | `tools\smoke_viewer.js` 第 39 组：2000 桶挤进 100 列时段数 ≤ 像素列数 × 3 加余量 · 50 桶 < 100 列时**不合并**（段数恰好 `3×50−2`）· 画完桶数组逐字节没被改 |
| 真浏览器 | `tools\verify_clicks.py` 新增一项：8000 桶整屏重画，三次取平均 |

```powershell
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"   # PASS
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"      # PASS
python tools\verify_clicks.py                                # 需要 Edge；末尾 300 项通过、0 失败
```

**通过判据（本机实测，2026-10-18）**：

| 断言 | 实测 |
| --- | --- |
| 画出的线段数（无头整跑） | 高避 **3 908 297 → 2 312 246**（−41%）· 耐久 **3 646 760 → 2 271 035**（−38%） |
| 真浏览器整屏重画（8000 桶，Edge + CDP，三次平均） | **59 ms/帧**；同一趟的 `/trace` 三档 500 / 2000 / 8000 = 9 / 12 / 22 ms |
| 统计不受影响 | 合并只发生在 `drawTraces` 里；断言直接比"画之前 / 画之后"的桶数组**逐字节相同**（就地合并会让抬头统计跟着错） |
| 真鼠标 | **300 项检查：300 通过、0 失败**（含新增的这一项） |

**与门槛的关系（照实说）**：性能 map 上那条"拖拽缩放每帧 ≤ 16 ms"**还没达到**——
59 ms 是整屏 `renderAll()`（工作表上所有组件一起重画，不只是一张图）。这一票把"画"的次数
按像素列封顶了；剩下的 59 ms 里有多少是别的组件、多少是 DOM 抬头，没拆开量过，要另开一张票。

**画法后来改过（A100）**：包络现在是「一条闭合的带子 + 中间那条均值线」，不再是"每列一根
竖线 + 两条外轮廓"；这一段里"段数 ≤ 像素列 ×3"与"不合并时恰好 `3×50−2`"两条判据没变，
照旧在跑（实测 301 段 / 148 段）。

---

## A98 · Rust 门槛：四条可判的门槛 + 真浏览器拖拽帧量具（本次新增，ticket #56）

**要解决的问题**：性能 map（#49）的最后一问"要不要用 Rust 重写"一直只有感觉。这一票把
**四条门槛**写成可判的（判据命令 + 阈值 + 实测），并补上第 3、4 条缺的量具——
`bench_viewer.py` 量的是服务端与 HTTP，量不了浏览器里的帧。

| 改/加 | 内容 |
| --- | --- |
| ADR | `docs/adr/0007-when-rust-is-allowed.md`：四条门槛 · 触发条件 · 窄接口候选（只列）· 代价表 · 翻案条件 |
| PLAN | `docs/PLAN.md` 新增 M12 指回这份 ADR（票面验收要求"被 PLAN 引用"） |
| 量具 | `tools\verify_clicks.py` 新增 `#56` 一段：真鼠标按住图拖 12 步 + 真滚轮缩 6 格，量每次事件的墙钟（另有一条"空转派发"基线）、浏览器 `Long Task API` 的条数，末尾打三条 NOTE |
| 断言 | 新增 3 条：拖拽真的平移了窗口 · 滚轮真的缩窄了窗口 · 拖拽每步中位 ≤ 200 ms（**回归线**；门槛 16 ms 写在 ADR 里，不写进回归） |

```powershell
python tools\bench_viewer.py --http --repeat 3   # 服务端与 HTTP 那条路
python tools\verify_clicks.py                    # 需要 Edge；真浏览器里的帧
```

**通过判据（本机实测，2026-10-18）**：

| 门槛 | 判据（命令的哪一列 / 哪条 NOTE） | 实测 | 过？ |
| --- | --- | --- | --- |
| 服务端 `/trace` 计算 ≤ 50 ms（12 通道 × 8000 桶） | `bench_viewer.py` 的「计算 ms」列 | 高避 **118.3** / 耐久 **170.2** ms | 不过 |
| HTTP/浏览器端到端 ≤ 150 ms | 同命令的「gzip 冷」列 | 高避 **170.9** / 耐久 **229.9** ms | 不过 |
| 拖拽缩放每帧 ≤ 16 ms | `verify_clicks.py` 的 `#56` NOTE（墙钟 − 空转基线） | 拖拽中位 **29.2 / 32.0** ms − 同趟基线 **11.3 / 5.8** ms = **17.9 / 26.2** ms（两次跑）；滚轮中位 29.8 / 16.2 ms | 不过（差 1.1–1.6×） |
| 长帧（> 50 ms）为零 | 同上 NOTE + `Long Task API` | 两次跑都是：拖 **0**/12 步 · 缩 **0**/6 格 · Long Task **0** 个 | 过 |

**与 A97 的关系（照实说）**：A97 记的 **59 ms**（两次跑 59 / 68 ms）是**缓存全失效的整屏
`renderAll()`**——最坏情况，不是拖拽稳态。真拖拽每步的墙钟是 29–32 ms，扣掉同趟量出来的
CDP 空转派发开销（5.8–11.3 ms），**17.9–26.2 ms** 才是那一帧的工作。门槛仍然不过，但差的
是 1.1–1.6×，不是 3.7×。**"减基线"是两次测量各自的中位数相减**，所以它是个 σ 不小的口径，
别当 0.1 ms 级判据用。这 18–26 ms 里谁占大头（DOM 抬头 / 其它组件 / 画布合成）**还是没
拆开量过**，那件事仍然没做。

**冷路径的拆分（探针，同一份数据、同一档）**：组装（`render.trace` × 12 + 时间轴 + 距离积分）
**25.9 ms** + `json.dumps` **83.9 ms** ≈ 110 ms，对上 bench 的 118.3 ms。组装那 25.9 ms 里
再拆一层：分桶包络 `render.downsample` × 12 = **19.5 ms**（= 窄接口候选）、取数前的重采样
`derive.upsample_to_master` × 12 = **6.6 ms**。Rust 门槛 ADR 的结论就架在这些数上：
候选接口只占冷路径的 **16%**，序列化占 **71%**。

**这一趟的回归**：`verify_clicks.py` **303 项检查：303 通过、0 失败**（原 300 项 + 新增 3 项；
A97 那条里写的"300"是加这三条之前的总数，这里不改旧账）。

---

## A99 · 布局第一票：工作表 tabs 到顶部、左栏瘦身、分辨率上移（本次新增，ticket #58）

**要解决的问题**：左栏什么都塞——工作表 tabs、管理按钮、圈速、区段、注释、GPS、别名、
数学通道、通道处理、通道列表，十块挤在 312 px 里；而 tabs 是**工作区级**导航，却埋在
最下面一层。这一票只搬外壳：面板内容一行不改，组件网格不动。

| 改/加 | 内容 |
| --- | --- |
| 顶部第二排 `#sheetBar` | tabs（`#presetRow`）+ 管理按钮（`#sheetManage`，7 个）+ `当前：<名字>` + **绘图分辨率** + `＋ 添加组件` + 命名行/提示行。分辨率跟 tabs 同一排，因为它是**每套具名工作表一份**（ADR-0008 口径一） |
| 左栏 | 只剩三块：`#sessionGroup`（场次信息；`#fileInfo` 从抬头**搬**过来，还是 `initHeader()` 那一处，不另抄一份）、`#channelGroup`（一行没改）、`#advancedGroup` |
| 高级工具 | `#advancedBody` 默认收起（`hidden` 与 `display:none` 两处都写：真浏览器认前者，无头假 DOM 认后者）；七个面板内容一行没改；点标题展开；**打开过就记住**（`localStorage: i3pro.advanced.v1`）——通道处理 / GPS 的「应用」会整页重载，不记的话每次都要重新点一遍才够得着 |
| 光标值 | 从左栏搬到浮层（`#valuesGroup.float`）：不进左栏也不进右栏（票面要求），仍是 V 键开关 |
| `JUMP_TARGETS` | 按新分组重排：场次 · 通道 · 高级工具 · 圈速 · 区段 · 注释 · GPS · 别名 · 数学通道 · 通道处理。跳折叠区里的那一栏会**先展开再滚**（老教训：隐藏元素 rect 全是 0，跳了等于没跳） |
| 外壳 | `main` 的高度从 `calc(100vh - 45px)` 改成 flex 分配——第二排一加，写死的高度就错；窄窗口（<1180 px）左栏收到 264 px |
| 无头断言 | `tools\smoke_viewer.js` 新增一组：源码顺序证明"新位置"、开机默认收起、点标题开合、七个原面板都还在、V 浮层开合；A78 那组的期望跟着新分组改 |
| 真浏览器 | `tools\verify_clicks.py` 新增 `layout_sheet_bar()`：真 rect 量那一排真在左栏上面、分辨率/tab/按钮真落在那一排里、七个面板默认 0 高、真鼠标点标题展开、V 浮层不压左栏、**1000 px 窄窗口**下顶栏与画布不叠且下拉不越界；另留三张截图 |

```powershell
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"   # PASS
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"      # PASS
python tools\verify_clicks.py                                # 需要 Edge；324 项通过、0 失败
```

**通过判据（本机实测，2026-10-18）**：

| 断言 | 实测 |
| --- | --- |
| 顶部那一排 | `#sheetBar` top 44 / bottom 84，左栏 top 84——一上一下，不叠 |
| 分辨率真的落在那一排里 | `#plotResolution` left 1201 / right 1324，那一排 0–1574 |
| 左栏三块 | 场次信息 83 px · 通道 429 px · 高级工具 16 px（收起时） |
| 七个面板默认 | lap / sections / notes / gps / alias / maths / setup 高度**全是 0**；真鼠标点标题后 382 / 401 / 139 / 243 / 216 / 356 / 120 px |
| 光标值浮层 | left 1270 > 左栏 right 312 ⇒ 不压左栏；按 V 出来 89 px，再按 V 归 0 |
| 窄窗口 1000 px | 顶栏 bottom 119 = 画布 top 119；左栏 264 px · 画布 736 px；下拉 right 598 < 1000 |
| 截图 | `out\shots\verify-layout-default.png`（默认：左栏只剩场次信息与通道）· `verify-layout.png`（展开高级工具）· `verify-layout-narrow.png`（窄窗口） |

**与旧断言的关系（照实说）**：A78 那组里"跳到工作表 / 跳到光标值"两条随分组一起改了
——工作表整块搬去顶栏、光标值成了浮层，两者都不再是左栏的一栏；改的是**落点期望**，
"真滚过去 · 标题贴按钮排 · Ctrl+K 光标落在按钮上 · 方向键不冒泡"四条行为断言一条没删。

---

## A100 · 包络画法：一条带子 + 中间那条曲线（本次改法，用户提的观感）

**要解决的问题**：包络一直是 ticket #55 那套画法——每个像素列一根竖线，外加下上两条
外轮廓线。8000 桶挤到约 800 像素列时，同一根竖线上要叠好几遍，画出来是深浅不一的竖纹；
更要命的是**曲线本身（每个桶的均值）根本没画**——远看是一团糊的色块，看不出信号在哪。

| 改/加 | 内容 |
| --- | --- |
| 画法 | `drawTraces` 的包络分支：上界从左到右 → 下界从右往左 → `closePath` + `fill`（`globalAlpha 0.22`）铺**一条带子**；带子上面再描**桶均值那条线**（`spec.values`，取值方式跟着「曲线连接」那一档走） |
| 数据 | `pixelColumnsFor` 多返回一条 `mean`：按像素列合并时同时算列内均值（非有限值跳过）；不合并的恒等分支直接就是 `spec.values` |
| 一行没动 | 抬头统计 / 纵轴量程 / A–B 统计仍然用后端给的桶；`pixelColumnsFor` 仍然不就地改输入数组（断言照旧比"画之前 / 画之后"逐字节相同） |
| 无头断言 | `smoke_viewer.js`：包络必须 `fill ×1` + `closePath ×1` + `stroke ×1`（带子 + 均值线）、点样式下不许出现 `fillRect`（一个点代表一整桶，仍然按线画）；段数上限与"不合并时恒等"两条老判据照旧 |

```powershell
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"   # PASS
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"      # PASS
python tools\verify_clicks.py                                # 截图在 out\shots\verify-theme-*.png
```

**通过判据（本机实测，2026-10-18）**：

| 断言 | 实测 |
| --- | --- |
| 2000 桶挤进 100 像素列 | 画 **301 段**（判据的上限是 330）；`fill 1 / closePath 1 / stroke 1`，没有 `fillRect` |
| 50 桶 < 100 列（不该合并） | **148 段**，与 A97 记的 `3×50−2` 一模一样 |
| 桶数组没被就地改 | 画完之后 `x / min / max / values` 逐字节相同 |
| 看得到 | `out\shots\verify-theme-dark.png` 与 `verify-theme-marple.png`：带子里那条线清晰可辨 |

---

## A101 · 主题切换：深色（默认）/ 浅色（Marple 风）（本次新增，用户提的）

**要解决的问题**：只有一套深色。要"像 Marple 那样"就得能换一套配色，而且换的时候
**画布也得跟着换**——网格、坐标字、区段带子、图例底都是画在 canvas 上的，只改 CSS 变量等于改一半。

| 改/加 | 内容 |
| --- | --- |
| 两套主题 | `:root` 是深色（默认）；`:root[data-theme="marple"]` 覆盖一组变量（浅底、白面板、深一点的蓝作强调色）。面上那几层（悬停 / 选中 / 对比 / 浮起 / 进度条 / 图例底）从散在规则里的深色字面量收成了变量 |
| 画布 | `UI` 那份颜色从 CSS 变量里取（`refreshUI()`）：网格、坐标字、面板色、强调色都走它；取不到时（无头假 DOM 没有 `getComputedStyle`）用深色那份兜底 |
| 缓存 | `componentKey` 里加上 `themeKey`——离屏画布上画着网格与区段带子，不加这一项切主题会留着旧配色（真浏览器里量出来的） |
| 控件 | 头部 `#themeSel`，选项由 `THEME_LABELS` 一处生成；选过的记在 `localStorage: i3pro.theme.v1` |
| 迹线颜色 | **不跟主题走**：它属于每件组件的调色板（`palette.py` 那几套里，Matplotlib tab10 就是对照表里记的 Marple 那一套），换主题不动数据 |
| 真浏览器 | `verify_clicks.py` 新增 `theme_switch()`：变量真的换了、画布像素指纹跟着变、下拉写着中文名、**真重载之后还是浅色**、最后切回深色 |

```powershell
python tools\verify_clicks.py    # 需要 Edge；332 项通过、0 失败（含 A101 这 8 条）
```

**通过判据（本机实测，2026-10-18）**：

| 断言 | 实测 |
| --- | --- |
| 变量真的换 | `--bg` `#0f1115 → #f2f5f9`；`--panel` `#171a21 → #ffffff` |
| 画布跟着换 | 一帧画布的像素指纹 **变了**（逐像素比；同一天两次跑是 `424287107 → 137888085` 与 `961609568 → 739223012`——指纹本身跟着当前工作表变，判据是"两次不相等"）。第一版按 37 像素步长采样，只碰到透明像素，两套算出来一模一样——白过一条，改成了逐像素 |
| 记得住 | 选过之后 `localStorage.i3pro.theme.v1 = marple`，真重载之后仍是浅色 |
| 截图 | `out\shots\verify-theme-dark.png` · `verify-theme-marple.png` |

**照实说一句**：浅色那套的 **chrome 颜色是我们挑的**（浅底白面板 + 深蓝强调），不是照 Marple 像素抄的
——仓库里关于 Marple 配色的证据只有对照表第 18 条：它的**三套迹线调色板**（含 Matplotlib tab10），
那三套 `palette.py` 里已经有了。要连 chrome 也照抄，得先按 `docs/marple-runtime-capture.md` 的规程
采一次运行时颜色。

---

## A102 · 右栏组件 inspector：低频设置搬出组件标题栏（本次新增，ticket #59）

**要解决的问题**：每块组件的标题栏上挂着七个按钮（↑ ↓ 显示 换成… 复制 CSV ✕），组件一多，
标题栏比图还吵；配色、抬头字段、统计范围这些"**这一张图想怎么看**"的设置，全塞在一个叫
「显示」的小弹窗里。Marple 那类分析工具的做法是：左边是"能看什么"、中间是"正在看什么"、
**右边是选中那一件的设置**——这就是这一票搬的东西。

| 改/加 | 内容 |
| --- | --- |
| 外壳 | `main` 从两栏变三栏 `312px / 1fr / 272px`（<1460 px 时 `288 / 240`，<1180 px 时 `264 / 220`）；右栏 `#inspector` 可收起成 34 px 的窄边，收起状态记在 `localStorage: i3pro.inspector.v1`（外壳的折叠，不进 `state`——ADR-0008 那 39 个键里没有它） |
| 右栏里的组件级设置 | 组件动作（上移 / 下移 / 复制 / 换成… / 删除，紧跟标题，省得往下滚）、通道槽位（色块可点 = 换手选色；清单型的类型带 ✕ 移除）、配色（调色板片 + 那一套的色块排）、抬头与统计（统计范围两档 + 6 个字段开关）、轴范围（有手动量程时给「恢复自动量程」） |
| 右栏里的全局那一格 | 「这一屏怎么看（全局）」：曲线连接（`state.upsample`）与点样式（`state.style`）。它们**不是**组件级——取数只有一条路（#21），所以单开一格、标题上写明"全局"，改的还是原来那个键；快照里曲线连接仍然灰着并说清为什么 |
| 组件标题栏 | 只剩高频动作：报表类的 `CSV` 与 `✕`（画布类只剩 `✕`）。↑ ↓ 复制 换成… 显示 五个按钮与「显示」那个弹窗一起撤掉（`openDisplayMenu` / `popCheck` 随之删除，不留死代码） |
| 点标题栏 = 选中 | `buildWorksheet` 里标题栏的 `mousedown` 先设 `focusId` 再 `beginMove`——那一头会 `stopPropagation`，所以以前点标题栏根本选不中（右栏要靠这一下） |
| 空状态 | 没选中时写"点一下工作表里的任意一块（标题栏或图里都行）…"；工作表本来就是空的时候改说"用上面那排的 ＋ 添加组件"，都带下一步 |
| 顺手修 | `.mdef`（数学通道的定义行）底色以前是写死的深色字面量，浅色主题下是一条黑带子——收成 `--rowbg`（与 A101 里图例底色同一个毛病） |
| 无头断言 | `tools\smoke_viewer.js` 新增第 40 组：没选中 = 空状态、选中后是这一件的设置（标题 / 6 个字段开关 / 5 个动作 / 通道行数）、关 Avg 与选 A–B 落进**这一件**的 config、换调色板落进 config、快照里曲线连接灰着并写明原因、手动量程可清、✕ 真的把通道从这一件里去掉、换组件跟着换、删掉回空状态、右栏收起 / 展开与记忆、组件标题栏按钮 ≤ 4 且不含低频按钮 |
| 真浏览器 | `tools\verify_clicks.py` 新增 `inspector_panel()`（41 条）：真鼠标点标题栏选中、右栏真在画布右边、换调色板后**画布像素指纹真的变**、统计范围 / 抬头字段 / 点样式改得回来、复制 6→7 块再删回空状态、标题栏可见按钮 ≤ 4、收起成 34 px 再展开 |
| 落点更新 | #35（抬头字段 / 调色板）、#44（统计范围）、#48（复制 / 换成…）、A79（曲线连接）四处从组件标题栏改到右栏；右栏是滚动的一栏，点之前先 `scrollIntoView`（不滚过去，真鼠标点到的是窗口外面的空气） |

```powershell
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"   # PASS
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"      # PASS
python tools\verify_clicks.py                                # 需要 Edge；375 项通过、0 失败
```

**通过判据（本机实测，2026-10-18）**：

| 断言 | 实测 |
| --- | --- |
| 三栏 | 右栏 272 px（left 1302 = 画布 right 1302）· 画布 990 px；收起后 34 px、设置那半截高 0，展开回来 272 px |
| 组件标题栏 | 六块组件全是 `['✕']`（报表类多一个 `CSV`）——判据是 ≤ 4 |
| 换调色板画布真的变 | 画布像素指纹 `201012848 → 537496721`，`config.palette = colorblind` |
| 抬头字段 | 真点 Avg：抬头那一格收起来（读到 False）、`headFields().avg === false`；再点回来 True |
| 统计范围 | 真点「光标 A–B」→ `ab`；点回「可见区间」→ `view` |
| 复制 / 删除 | 真点复制：6 → 7 块，右栏跟到新那一块；真点删除：回到 6 块 + 空状态 |
| 通道槽位 | 12 条通道 12 行；清单型的类型（图 / 通道报告）带 ✕，点了真的从这一件组件里去掉 |
| 检查项总数 | `verify_clicks.py` **375 项：375 通过、0 失败**（原 332；新增 43 = 右栏 41 + #35 与 #43 各 1） |
| 截图 | `out\shots\verify-inspector.png`（右栏）· `verify-inspector-settings.png`（#35 的抬头字段走右栏）· `verify-curve-join-panel.png`（A79 的曲线连接走右栏） |

**照实说三句**：

1. **「点样式」没有拿来当"画布真的变"的判据**：这一段曲线是包络，包络按线画（A100），
   点样式对它本来就不该有变化。第一版拿它当判据，真浏览器里量出"指纹一模一样"——那不是
   产品坏了，是判据选错了；现在这条判据用**换调色板**，点样式只验它写进了 `state.style`。
2. **#43 那一条跟着改了拖拽落点**：控件条在窄一点的栏目里会多折一行，画布被推到下面、
   下沿还被 `.comp` 裁掉（老毛病，不是这一票引入的）。测试以前量的是"换通道**之前**"的画布
   坐标，中间那栏一收窄就落到 resize 条上了；现在拖之前重量一次，并把起点放在**看得见的
   那一段**里（往上拖）。是测试的坐标过期，不是产品回归。
3. **A79 那条"界面取的也是线性那一份"顺手修了时序**：它以前等的是"这条通道有值"——而旧窗口
   的值一直都在，所以第一条断言读到的可能是上一段窗口的曲线（一直靠"恰好对上"过）。现在
   取数前先把这条通道的缓存删掉，等来的一定是新窗口那一份（实测：界面与接口都是
   `[57.9, 57.91, 57.92, 57.93]`）。

> **票⑪ 之后这两行的形状变了**（见 A103）：标题栏上只剩「✕」与「…」，报表的「导出 CSV」
> 进了 `…` 菜单，而且平时是**收着**的（`opacity 0`）——"每一块都是 `['✕']`"现在是
> `['✕','…']`。A102 这一票当时的实测数字照旧不改。

---

## A103 · 组件标题栏：平时安静、悬浮 / 选中才出操作 + `…` 菜单（本次新增，ticket #61）

**要解决的问题**：票⑨ 之后标题栏上只剩 `✕`（报表还有 `CSV`），但它们一直是亮着的
（`opacity .3`、hover 变 1）——一屏十几块组件，几十个小按钮还是在抢注意力。而"上移 /
下移 / 复制 / 换成…"这些偶尔才用的动作，既不该常驻标题栏，也不该只藏在右栏里
（手正在画布上时不该为了挪一块跑去右栏）。

| 改/加 | 内容 |
| --- | --- |
| 平时安静 | `.compbar .ctools` 默认 `opacity:0` + `pointer-events:none`；鼠标悬浮这一块（`.comp:hover`）、这一块被选中（`.comp.focus`）、或键盘 Tab 进来（`.compbar .ctools:focus-within`）才 `opacity:1; pointer-events:auto`。**不用 `display:none` / `visibility:hidden`**：看不见的时候按钮仍在 Tab 顺序里——"操作要悬浮才出"不能拿键盘可达性换 |
| 栏上只剩两个 | 「✕」（关掉这一块）与「…」（更多）；报表的「导出 CSV」也跟着进了 `…` |
| `…` 菜单 | 上移 / 下移 / 复制 / 换成… / 导出 CSV（只有报表类有）。**菜单里不放 ✕**——它就在栏上，同一件事不摆两遍 |
| 一处定义、两处入口 | `componentActions(comp)` 一张表说清"有哪些动作、哪个在栏上"（`bar` 那一项）：右栏 inspector 的「组件」那一格渲染**全部**，标题栏的 `…` 菜单渲染**除 `bar` 以外**的，栏上那个 ✕ 的文案与提示也从这张表来。两处各写一份清单才是要避免的那件事——加一个动作时必漏一个入口 |
| 键盘 | `openPopMenu` 打开时把焦点放到第一个能点的项；`popMenuEscape` 关菜单并把焦点还给打开它的按钮（注册在 document 捕获阶段，压过全局那个 Esc）；`aria-haspopup` / `aria-expanded` 跟着开合 |
| 无头断言 | `smoke_viewer.js` 第 41 组：栏上只有 `✕,…`、✕ 的文案确实来自那张表、右栏那一格 = 表里全部、`…` 菜单 = 表里除 `bar` 以外的、点「复制」真多一块、非 Esc 不关 / Esc 关且 `aria-expanded` 撤回、表格类的表与菜单都多一条「导出 CSV」、菜单里的「换成…」把选类型的菜单开出来 |
| 真浏览器 | `verify_clicks.py` 新增 `component_menu()`（18 条）：默认 computed opacity 是 0 而按钮仍有面积（Tab 得到）、真鼠标悬浮才出（`pointer-events` 也真开了）、挪开又收回、选中之后一直出、真按 Tab 从 ✕ 走到 `…`、回车开菜单、Esc 关掉并把焦点还给按钮、菜单里点「复制」6 → 7 块、悬浮新那一块真点 ✕ 收走 |

```powershell
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"   # PASS
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"      # PASS
python tools\verify_clicks.py                                # 需要 Edge；393 项通过、0 失败
```

**通过判据（本机实测，2026-10-18）**：

| 断言 | 实测 |
| --- | --- |
| 默认收着 | `.ctools` computed `opacity 0`、`pointer-events none`；两个按钮 `✕` 21×18、`…` 23×18——**仍有面积**，还在 Tab 顺序里 |
| 悬浮才出 | 鼠标移到这一块上：`opacity 1` / `pointer-events auto`；挪到左栏（且没选中）又回 `0` |
| 选中一直出 | 真点标题栏把这一块选中之后，鼠标挪开仍是 `1` |
| 键盘一条路走通 | 焦点落在 `✕` 上时操作就露出来（`:focus-within`）→ 真按 Tab 到 `…` → 回车开出菜单（上移 / 下移 / 复制 / 换成…）→ Esc 关掉且焦点回到 `…` |
| 菜单里真点得动 | 点「复制」：6 → 7 块；悬浮新那一块、点它悬浮出来的 `✕`：回到 6 块 |
| 检查项总数 | `verify_clicks.py` **393 项：393 通过、0 失败**（原 375；新增 18） |
| 截图 | `out\shots\verify-component-bar-idle.png`（默认：标题栏上只剩标题与行内尺寸）· `verify-component-menu.png`（`…` 菜单开着） |

**照实说三句**：

1. **「… 菜单」与右栏那一格是有意的两个入口，不是两份定义**：上移 / 下移 / 复制 / 换成…
   两处都点得到，但清单只在 `componentActions` 里写一遍；「✕」只摆在栏上，所以菜单里没有
   "删除"。票面那句"避免两处重复"按"别维护两份清单"落实。
2. **没做"折叠这一块"**：Marple / i2 Pro 的标题栏上还有折叠，但那要动组件网格的重排
   （我们的组件是绝对定位，折起来会在工作表里留一个洞），不属于这一票——要做单开一票。
3. **既有那些"点 ✕ 删一块"的验收一条没改**：真鼠标点击本身先发一次 `mouseMoved`，
   悬浮状态就亮了，所以 #33 那条照样点得到——收起来只是视觉上的安静。

---

## A104 · 双圈对比真的画两条圈（本次修法，ticket #65）

**要解决的问题**：用户报"圈速对比做不到两个圈的数据来对比，只能切分圈"。查下来是
**画法丢了**：双圈对比模式下横轴换成了"一圈的距离"（`lane()` = `state.overlay.distance`），
但图上画的还是 `state.traces`——那是**整场 / 当前时间窗**的曲线。于是"双圈对比"画的是
这一场开头那 800 米，选中的两条圈只剩标题里的名字；Δ 面板倒是对的（它一直读 overlay）。

| 改/加 | 内容 |
| --- | --- |
| 数据源 | 新增 `overlaySeries(name)`：双圈对比下给出 `state.overlay` 里那两条圈（已经落在**同一条距离网格**上，一个插值都不做）。`graphTraceSpecs(name, comp)` 在对比模式给**两条** spec（蓝 = 基准圈、橙 = 对比圈），别的时候照旧一条 |
| 纵轴 | `groupScaleFor` 在对比模式按**两条圈**的值量程——只按 `state.traces` 量的话，两条线会被裁掉一半 |
| 抬头 | 对比模式下那一行印"蓝 x · 橙 y"，Δ 变成**两条圈的值差**（Δ值），min/max/avg 也是"蓝 / 橙"一对；色块画成左蓝右橙两半 |
| 标题 | 组件标题追加"蓝=圈 X · 橙=圈 Y"（图上那两条线才有名字）；对比模式下不再写"已抽稀（包络）"——画的是 overlay，不是那一屏的抽稀迹线 |
| 缓存 | `componentKey` 里带上这一对圈的名字 + `ensureOverlay` 拿到新载荷后 `dataVersion += 1`：换一条对比圈必须重画（否则留着上一对的线） |
| 语义色 | `--ref` / `--cmp` 两个主题变量（深色 `#2f6bff` / `#ff6a00`，浅色 `#1f4fd8` / `#c2410c`），画布线也走它们（与 A101 同一条纪律）。这两个色**不在任何一套调色板里**——"画布上有没有这两个色"因此能当判据 |
| 无头断言 | `smoke_viewer.js` 第 42 组：数据源是那两条圈、两条线共用同一条距离网格、颜色就是那对语义色、抬头同时印两条圈的值与 Δ值、标题写清是哪两条圈、切回时间轴只剩一条 |
| 真鼠标 | `verify_clicks.py` 新增 `lap_overlay()`：真点「基」「比」选两条圈，量**图表画布**上两种语义色的像素数，再换一条对比圈看画布是否重画 |

```powershell
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"   # PASS
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"      # PASS
python tools\verify_clicks.py                                # 需要 Edge；402 项通过、0 失败
```

**通过判据（本机实测，2026-10-18）**：

| 断言 | 实测 |
| --- | --- |
| 选中的就是这两条圈 | 真点「圈 1 的基」「圈 2 的比」之后：`ref=1 cmp=2`，`state.overlay.laps = ['1','2']`，模式 `overlay` |
| 画布上真的画出两条圈 | 图表画布上语义色像素：蓝 **5143** / 橙 **12021**（修之前是 **0 / 0**，见下；同一场不同缩放，像素数会变） |
| 换一条对比圈会重画 | 改选圈 3：蓝 **5569** / 橙 **13505**（像素数变了 ⇒ 真的重画了） |
| 标题 | `时间/距离图 · 12 通道 · 分栏 · 蓝=圈 1 · 橙=圈 2` |
| 抬头那一行 | `蓝 87.9 · 橙 84.3`，Δ值 `-3.6`，min/max/avg 各印"蓝 / 橙"一对 |
| 检查项总数 | `verify_clicks.py` **402 项：402 通过、0 失败**（原 393；新增 9） |
| 截图 | `out\shots\verify-lap-overlay.png` |

**怎么验的（这条判据自己先红过）**：把 `graphTraceSpecs` 里的对比分支临时短路（等于回到
修复前那种"只画 `state.traces`"），同一条真鼠标用例**当场变红**：

```
FAIL  A104 画布上真的画出了两条圈（蓝 + 橙两种颜色的线都在）  — refPx: 0, cmpPx: 0
FAIL  A104 换一条对比圈，画布跟着重画  — 0/0 -> 0/0
```

第一版判据用的是默认调色板里那条蓝（`#4cc2ff`）当"基准圈色"，短路之后**照样绿**
（1856 / 1540 像素——那是别的组件用同一个蓝画的）：判据得挑**只有这两条圈会用**的色，
并且只看**图表那一块画布**。这一版才真的能红。

**历史**：v1（`c713c8c`）里有 `renderOverlayCharts()`——每条通道两条线（蓝 / 橙）。
2026-09-13 的工作台重写（`7f9c96e`）把它删了，只留下 Δ 面板，于是"双圈对比"看着像
只有一条圈；既有的自动化一条都没红（后端载荷那条 `test_overlay_is_distance_aligned`
是绿的，smoke 的 overlay 那条只查"模式切过去没有"）。

## A105 · 侧边栏列表缓存：按路径当键 + DBC 指纹按行记 + 写盘合并（本次修法，ticket #66）

**要解决的问题**：用户问"工作一步为什么要 20 分钟"。`pytest --durations` 量出来单测
630 项 **207.6 s** 里有 **137 s**（两成七）花在两条用例上——`TestServer::test_http_api_end_to_end`
70.2 s、`TestApiLayerWithoutASocket::test_每个请求都拿得到一个回复` 66.9 s。两条都在冷算
同一件事：真数据目录的场次列表。

冷算本身没有不合理（真数据 47 场冷 **68.5 s**、命中 **0.04 s**，一千七百倍）。不合理的是
这份磁盘缓存**永远命中不了**：它按**场次名**当键、DBC 指纹是整份文件级的一个字段、而
`_write_index_cache` 是**整份覆盖**。于是任何一个用临时 roots 的库写一次盘（`http_session`
一共 26 处，roots 带进程号），真数据那几十行就被顶掉——温度每次都回到 0。

**这条也是真机上的问题**：换个数据目录、或者两个进程各用一套 roots，用户就要再付一次
68 s 首屏。

| 改/加 | 内容 |
| --- | --- |
| 键 | 磁盘上那份列表改成按**场次文件路径**当键（原来是场次名）：同名的两份（真数据 + 临时目录副本）各占一行，谁也不顶掉谁 |
| DBC 钥匙 | 从"整份清单共用一个文件级字段"改成**压成 sha256 记在每一行里**。共用一个字段时它一变就是"全作废"，而"全作废"恰好把不同 roots 组合的库拴在一起——只有真受影响的行该失效 |
| 写盘 | `_write_index_cache` 改成**合并**（`cache.write_json` 的原子写不变），顺手丢掉**文件已经不在了**的行：临时 roots 用完就删，不丢就是只增不减 |
| 新助手 | `cache.digest(value)`：把一份指纹压成 sha256。整份 DBC 树指纹（本机 `i2pro_data/dbc` 实测 34 份文件）抄进每一行会让那份 JSON 白胖一圈；用**完整** sha256 不截短，因为这里要的恰恰是"没变就绝不误判成没变" |
| 形状版本 | `LISTING_CACHE_SHAPE` 2 → 3，旧文件自动当没有 |
| 单元测试 | 新增"别的 roots 写过盘之后这一份不用重算"（就是本票的病）、"文件已经不在的行不再留在盘上"、`digest` 稳定性；`TestCacheStamps` 那条改成钉新形状（DBC 钥匙按行，且改 DBC 不动行里别的字段） |

```powershell
# 换一个空的缓存目录跑两遍：第一遍是冷启动，第二遍是热启动
$cold = "out\_coldtmp"; $env:TEMP = $cold; $env:TMP = $cold
python -m pytest tests\test_i3pro.py -q --durations=12    # 冷
python -m pytest tests\test_i3pro.py -q --durations=12    # 热
python -m unittest tests.test_i3pro.TestListingCache tests.test_i3pro.TestCacheStamps -v
```

**通过判据（本机实测，2026-10-18）**：

| 断言 | 实测 |
| --- | --- |
| 单测总时长（冷缓存） | **140.9 s**（改前 **207.6 s**） |
| 单测总时长（热缓存） | **72.4 s**（改前第二遍照样 207.6 s：整份单测跑完之后 `%TEMP%\i3pro-listing-cache.json` 里 `sessions` 是 **0 行**，实测） |
| 第二次冷算消失 | 热跑慢榜里已经没有 `TestApiLayerWithoutASocket::test_每个请求都拿得到一个回复`（改前 66.9 s）；`TestServer::test_http_api_end_to_end` 从 70.2 s 降到 **2.94 s**——剩下的 2.9 s 是两份 staged 金标准每次都得重算（它们的路径带进程号，这是设计） |
| 真数据列表冷/热 | 47 场：冷 **68.55 s** / 热 **0.04 s** |
| 别的 roots 写过之后 | 真数据那一批照样 **0.04 s**；盘上 48 行共存（47 行真数据 + 1 行临时 roots），键是文件路径 |
| 新用例单跑 | `TestListingCache` + `TestCacheStamps` 共 **10 项 / 0.43 s 全过** |
| 两份金标准实跑 | 真数据 47 场里那两条都在：`20260908-cjh 高避5圈` **圈数 7**、`20260524-耐久正赛` **圈数 26**（`SessionLibrary([i2pro_data]).listing()`，就是这条路） |
| 整份单测 | `python -m unittest discover -s tests`：**633 项 / 70.8 s**，3 失败 25 跳过——3 条红的是缺 `can_data\*.csv`，改前改后一样 |

**怎么验的（这条判据自己先红过）**：`TestListingCache::test_别的_roots_写过盘之后这一份不用重算`
在改之前是红的——它用 `mock.patch.object(SessionLibrary, "summary", side_effect=AssertionError)`
把重算这条路封死，"另一拨 roots 写过之后再读还能给出行"就必须真的命中磁盘。改之前那一步
会抛 `不该重算`。

**已知的两条差**：单测里那 3 条红的（`TestDbc` ×2、`TestSplitSessionsAreWhole` ×1）是
`can_data\*.csv` 原始帧表不在导致的，改前改后一样红，与本票无关。`verify_clicks.py` 那
227 s 里另有约 130 s 是写死的 `sleep`（169 处字面量共 104 s，加上 74 次点击 ×0.25 s、
35 次按键 ×0.2 s）——那一张是下面的 A106。

## A106 · 真鼠标盘把写死的 `sleep` 换成「等页面安静」（本次修法，ticket #67）

**要解决的问题**：`tools\verify_clicks.py` 实测 **276.6 s**，其中**约 130 s 是真在等**——
172 处字面量 `time.sleep` 共 **104.4 s**（0.4 s ×31、0.5 s ×23、0.6 s ×19、1.0 s ×18……），
外加 `click()` 尾巴 0.25 s ×74、`key_named()` 尾巴 0.2 s ×35。形状是"发个真事件 → 睡死
一段时间 → 断言"：睡多久是猜的，猜短了会闪，猜长了就是纯白等。

| 改/加 | 内容 |
| --- | --- |
| 新原语 | `Browser.settle()`：**待触发的短定时器一个不剩** + **距上次异步活动 ≥120 ms** + **本次至少等了 60 ms**，三条齐了才返回；超时返回 False，**不在这里报错**——报错交给紧随其后的那条断言，它才知道这条在验什么 |
| 探针 | `SETTLE_HOOK` 经 `Page.addScriptToEvaluateOnNewDocument` 装（跑在页面自己的脚本之前）。包 `fetch` 记"最近活动"，包 `setTimeout` 记活动 + 待触发个数，**只算 ≤2 s 的**（长的是"四秒后收起提示"这类收尾，不该算忙） |
| 动作助手 | `click` / `key` / `key_named` / `insert_text` 的固定尾巴换成 `settle()`；`bring_to_front()` 先到前台再派发事件 |
| 删冗余 | "动作之后"那类写死 `sleep` 删掉 **64 处 / 41.9 s**（那个动作本身已经等过了）。留了 4 处 >1.5 s 的（2 s ×2、2.5 s ×2，等导出/删文件这种慢活） |
| 自量 | 整场跑完打一行 NOTE：`settle` 次数、总耗时、中位、最慢；撞到耐心上限的那几次连**当时页面在干什么**一起打出来 |

**三条走错的路（都是量出来的，不是看出来的）**——留着是因为下一个改这个盘的人一定会再
踩一遍：

1. **拿 `requestAnimationFrame` 当"画过了"的凭据**：隐藏的标签页 rAF **完全停**（实测
   0 fps），而这个盘同时开着十几个标签页、只有最后开的是 `visible`。于是"等两帧"在某些
   页上永远等不到，只能撞满超时——实测约 13 次撞满 5 s、60 s 以上，正好是"等安静"总耗时里
   的大头。改成一条时间下限（`MIN_MS`）之后这个坑没有了，也更简单。
2. **拿 `performance.now()` 当时间基准**：它**每个文档各从 0 起算**，而工作台保存侧车之后
   会 `location.reload()`。跨过那次重载，"现在 − 开始时"直接变成负数，那条下限永远满足
   不了——实测正好卡在 `/session/...` 上又撞满 5 s 超时两次。改用 `Date.now()`（跨文档
   连续），"这次等了多久"则用 Python 侧的单调钟量。
3. **以为慢在 `settle` 里**：其实往**隐藏**页派发鼠标事件本身就慢——`Input.dispatchMouseEvent`
   要等被冻住的渲染器回话。实测同一个 `click` 在隐藏页 **5.09 s**、先 `Page.bringToFront`
   之后 **0.142 s**。所以是"动手之前先到前台"，不是"等的时候先到前台"。

```powershell
python tools\verify_clicks.py      # 需要 Edge；402 项通过、0 失败
```

**通过判据（本机实测，2026-10-18）**：

| 断言 | 实测 |
| --- | --- |
| 检查项 | **402 项：402 通过、0 失败**（与改前同一份判据） |
| 总时长 | **170.4 s**（改前 **276.6 s**，省 **106 s / 38%**） |
| 等安静本身 | 167 次共 **67.5 s**，中位 **78 ms**，最慢 **2.10 s**，**0 次**撞满耐心上限 |
| 单次动作 | 真点一次 **0.177 s**（三个标签页各量一次都是 0.18-0.20 s；改前 `click` 尾巴是固定 0.30 s） |
| 该等的等得到 | 页面里挂一个 300 ms 防抖 → `settle` 等 **0.431 s**（改前固定睡 0.5 s，不够的时候就是闪） |
| 不该等的不等 | 页面里挂一个 5000 ms 长定时器 → `settle` **0.066 s** 就返回 |
| 连跑两遍 | 同一版**连续两次**全过（"等安静"最怕偶发闪） |

**怎么验的（判据自己先红过）**：第一版只有"有 `fetch` 在飞 + 待触发定时器 + 两帧"三条，
实测 **400/402**——`#32 点「取消」之后卡片关掉、场次列表里没有它` 与
`#32 取消也要把暂存文件删掉` 当场变红，总时长反而涨到 **322 s**（撞满超时）。收紧判据之后
才 402/402。

**这一票之后还剩什么**：`verify_clicks.py` 里仍有 102 处字面量 `sleep`（约 62 s），多数是
"等一次 2 s+ 的导出/计算完成"，那本来就该由它自己的 `wait_for` 管；`settle` 那 67.5 s 里的
大头是**页面真的在忙**（缩放后要为十几路组件重新取数），不是白等。

## A107 · 缩放窗口可复现：救活 `/trace` 响应缓存（本次修法，ticket #68）

**要解决的问题**：`TraceCache`（`tracecache.py` 开头那句"缩放来回"）在它自己声称要服务的
用法上几乎从不命中。缩放的每一步是浮点乘法，放大再**原路**缩回得到的是"差 5e-14 秒的
**另一个**窗口"（实测 `from` 一模一样、`to` 是 `...5432099998` 对 `...54321`），而服务端
响应缓存的 key 用的是窗口的完整 `repr`（`server.py` 的 `_cache_key`）——所以那是两个 key。

| 改/加 | 内容 |
| --- | --- |
| 新原语 | `windowRange()` 把**发出去的** `from`/`to` 吸到"全场跨度的百亿分之一"的网格上（`requestGrid()` / `snapToGrid()`）。同一个窗口来回一趟就是同一个 key |
| 唯一写入口 | `setView(from, to)`：`state.view` 的钳位、"全场"（`null`）语义只有一处。13 处旁路写入全部改走它，另一处 `state.view = defaultRange()` 也改了 |
| 结构守卫 | `tests/test_i3pro.py` 新增：正则扫 `viewer.html`，直写 `state.view =` 只允许出现在 `setView` 里（**改前是红的**：旁路 25 处） |
| 无头断言 | `smoke_viewer.js` 新一组 #68：真滚轮"放大 6 格 → 原路缩回 6 格"，`windowRange()` 前后**逐字相同**、且两端都在网格上 |
| 量具 | 新增 `tools/measure_trace_cache.py`：真 Edge + CDP 走真滚轮，服务跑在**本进程**里给 `TraceCache.get` 打桩，数每一趟的命中率 |

**关键的一条：吸的是请求，不是 `state.view`。** 工单里先提的方案是吸 `state.view`——实测
**不行**：每吸一次，光标处的 pivot 就跟着挪一点（`zoomFactor` 的 pivot 由当前 lane 反算），
误差沿轨迹累积。放大 6 格再缩回 6 格，两端差 **4.6e-8 s**（比不吸附时的 5e-14 还**大**三个
数量级），返回那一趟 6 次只命中 **3** 次。改成"只吸请求"之后，轨迹自己的漂移还是 5e-14，
一个 2e-8 的格子必然把它吸收掉。

网格量级是**两头卡出来的**：下界是浮点漂移（5e-14），上界是验收里已经在用的容差
（`smoke_viewer.js` 那几条"跳到某区段"用 `< 1e-6`，`verify_clicks.py` 比"取回来的数据 vs
`state.view`"也用 1e-6）。工单原本写的 1e-6 × 全场 ≈ 0.46 ms 时误差最多半格 0.23 ms，**比
容差大两个数量级**，实测当场红了 4 条；1e-10 × 全场 ≈ 20 ns，半格 10 ns，比容差小 100 倍、
比一个 1 kHz 采样点小 5 个数量级。

```powershell
python tools\measure_trace_cache.py                       # 需要 Edge；判据见下
python tools\measure_trace_cache.py --session "20260524-耐久正赛" --port 9339
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"       # PASS
python -m unittest tests.test_i3pro.TestViewerScript -v          # 含那条结构守卫
```

**通过判据（本机实测，2026-10-19）**：

| 断言 | 实测 |
| --- | --- |
| 返回那一趟命中率（高避 5 圈） | **6 次取数 6 命中**（改前 **1 命中**） |
| 返回那一趟命中率（耐久正赛） | **6 次取数 6 命中** |
| 第一次经过 | 6 次 0 命中——**这是对的**：没看过的窗口天然是新的、必然冷。判据只钉"返回" |
| 去程与回程的窗口 | 逐字相同（`to=208.203529389438` 两边都是这个） |
| 结构守卫 | 当前绿；拿 `git show HEAD:src/i3pro/web/viewer.html` 跑同一段逻辑，抓到 **25 处**旁路 |

**怎么验的（判据自己先红过）**：把 `windowRange()` 的吸附临时撤掉（返回原值），两条 smoke
断言当场变红，而且**正是工单里的签名**：

```
FAIL  the requested window is not on the grid: {"from":120.123456789,"to":200.987654321}
FAIL  zooming back did not reproduce the same requested window:
      {"from":120.123456789,"to":200.98765432099998} vs {"from":120.123456789,"to":200.987654321}
```

**判据看的是 `windowRange()`，不是 `state.view`**：key 由前者构成，而 `state.view` 是精确
浮点、本来就差 5e-14——拿它当判据等于钉错了地方（这一条也是实测出来的：第一版量具直写
`state.view` 当起点，返回趟最后一步永远"没命中"，看着像产品没修好，其实是量具自己造了
一个应用里不存在的起点）。

**量具踩过的坑（都写进 `measure_trace_cache.py` 了）**：

1. **滚轮点要落在画布上**。工作台那些下拉框是浮在画布上面的，取"第一张 canvas 的中心"
   实测 `elementFromPoint` 给的是 `SELECT`——事件照样冒泡到 `#charts`，但
   `event.target.closest("canvas")` 是 null，滚轮处理函数当场返回。第一版就是这么量出一批
   假数的。现在只在 `elementFromPoint(x, y) === 那张画布` 的点里挑。
2. **预热要等到第一笔取数落地**，否则滚轮那一阶段一次取数都不触发，量具空转。起点还要走
   `zoomTo`（应用里那条路），不能直写 `state.view`。

**这一票不动产品行为**：吸的是请求参数，而 20 ns 比一个 1 kHz 采样点小 5 个数量级，取到的
样本集不变，画出来的曲线逐点相同。

## A108 · 单位行末尾多一个分隔符不再顶掉表头（本次修法，ticket #64）

**要解决的问题**：两列的分号表

```
Time;Vx KF
s;km/h;
0.00;0
0.01;1
```

按老逻辑读不出时间列。`_header_index` 挑"最宽的非数字行"，而 `"s;km/h;"` 按 `;` 切开是
**3 个字段**（末尾一个空串），比两列表头的 2 个字段更宽，于是单位行被当成表头，列名变成
`["s", "km/h", ""]`。

| 改/加 | 内容 |
| --- | --- |
| 修法 | `csvlog._content_width()`：比较宽度前去掉行尾空字段。`_header_index()` 改用它，`"s;km/h;"` 与 `"s;km/h"` 就是同一行 |
| 单测 | `TestTextImport.test_a_trailing_separator_on_the_unit_row_does_not_become_the_header`：两列 `layout_of` = `(0, 2)`、三列老行为 = `(0, 2)`、预览与导入的列名都是 `["Vx KF"]`、时间列是第 0 列 |
| 同路守卫 | `layout_of` 同时服务分隔文本与 Excel；`TestTextImport`（20 项）与 `TestXlsxImport`（13 项）都实跑通过 |
| 顺手修 | 三条 CAN 用例的缺数据守卫以前只看 `can_data/` 目录在不在；目录只剩 `.can.json` 侧车时会红。现在它们和 `_can_id_counts()` 共用 `_can_frame_files()`：没有原始帧表就按规则 skip |

```powershell
python -m unittest tests.test_i3pro.TestTextImport -v        # 20 项
python -m unittest tests.test_i3pro.TestXlsxImport -v       # 13 项

$p = Join-Path $env:TEMP "i3pro-64-two.csv"
Set-Content -LiteralPath $p -Encoding utf8 "Time;Vx KF`ns;km/h;`n0.00;0`n0.01;1`n"
$env:PYTHONPATH = "E:\桌面\i3pro\src"
python -m i3pro info $p                                     # 1 通道，不再是 ValueError
```

**通过判据（本机实测，2026-10-19）**：

| 断言 | 实测 |
| --- | --- |
| 两列表的 `layout_of` | **`(0, 2)`**（改前 `(1, 2)`，单位行被当表头） |
| 三列表的 `layout_of` | `(0, 2)`（老行为没变） |
| CLI 读那份两列表 | **1 通道 / 100 Hz**，不再报"找不到时间列" |
| 四条回归 | 单测 **635 项 / 71.2 s，0 失败、28 跳过**（跳过的是缺数据/缺可选裁判的用例）；`verify_ld_vs_csv.py` PASS；两份金标准 smoke 各 **880 项 PASS**；`verify_clicks.py` **402/402** |

**关于跳过**：本机 `can_data\` 里的原始帧表 `.csv` 已经清掉（只留 `.can.json` 侧车），
所以那三条 CAN 用例现在按 `AGENTS.md` §7 的约定 skip。它们**不算通过**——要在有帧表的机器上
再跑一遍；判定标准仍是 `TestDbc` 的 46 条信号与 cantools 逐帧对拍、`TestSplitSessionsAreWhole`
的 12 个 `+N` 场零不一致。

## A109 · 底部概览/状态 dock 固定（本次新增，ticket #62）

**要解决的问题**：概览波形与状态行以前跟工作表住在同一个滚动容器里，内容一多它们就被
滚走；"现在看的是哪一段"和"当前光标 / 圈 / 缩放"正好是滚动时最想看见的两行字。这一票把
中间那栏拆成滚动区（`#chartScroll`：工作表 + 滚动条 + 快捷键说明）与底部固定 dock
（`#bottomDock`：概览 + 状态行），dock 可折叠、折叠状态记在本机。

| 改/加 | 内容 |
| --- | --- |
| 结构 | `section#charts` 改成 flex 列：`#chartScroll` 滚动、`#bottomDock` 固定在底边；dock **不在**滚动容器里 |
| 尺寸 | `#overviewHost` 给 `min-height:88px`，`#statusLine` 给 `min-height:18px`：没概览数据时也不塌成一条缝 |
| 折叠 | `#dockFold` 收起 / 展开；dock body 显示时走 CSS，收起状态写 `localStorage['i3pro.dock.v1']`（与右栏 inspector 同一个外壳状态模式，不进 `state`） |
| 无头断言 | `smoke_viewer.js` 新增一组：dock 存在、点「收起概览」body 真的收掉、滚动区变高、状态记在本机、展开后概览仍可拖动定位 |
| 真鼠标 | `verify_clicks.py` 新增 `bottom_dock()`：量真 rect 证明 dock 不在滚动容器里、折叠后滚动区真的变高、收起后**真重载一次**仍是收起的、概览拖动仍改窗口、1280×720 与 1920×1080 两种窗口下各 rect 不重叠/不越界/不溢出并各留一张截图 |

```powershell
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"       # 888 项 PASS
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"          # 888 项 PASS
python tools\verify_clicks.py                                    # 需要 Edge；414 项通过、0 失败
```

**通过判据（本机实测，2026-10-19）**：

| 断言 | 实测 |
| --- | --- |
| dock 不在滚动容器里 | `#chartScroll.contains(#bottomDock)` = **false**（内容滚它不跟着走） |
| 展开时 dock 高度 | **152.6 px**（概览 98.9 + 状态 18.7；`#charts` 823 px） |
| 折叠后画布高度 | 滚动区 **670.4 → 790.0 px**（+119.6），dock body 的 rect 变成 0 |
| 折叠状态记忆 | 收起写 `0`、展开写 `1`，与 `i3pro.dockOpen()` 对得上 |
| 重载后仍然收起 | 收起状态写进 localStorage 后真 `location.reload()`；重载完 `i3pro.dockOpen()` = **false**、`#dockBody` 高度 0 |
| 概览拖动 | 收起再展开之后仍能改窗口：lane `[185.644, 278.341]` → `[151.636, 244.333]`（先缩到 40%–60%，否则整场窗口被 clamp，量不出"真的动了"） |
| 窗口 1280×720 | dock `bottom=720`，状态行 `bottom=714`，滚动区 `bottom=567.4`：不重叠、不裁切；截图 `out\shots\verify-dock-1280x720.png` |
| 窗口 1920×1080 | dock `bottom=1080`，状态行 `bottom=1074`，滚动区 `bottom=927.4`：不重叠、不裁切；截图 `out\shots\verify-dock-1920x1080.png` |
| 四条回归 | 单测 **635 项 / 70.1 s，0 失败、28 跳过**；`verify_ld_vs_csv.py` **PASS**；两份 smoke 各 **888 项 PASS**；`verify_clicks.py` **414/414** |

## A110 · 左栏拖宽/折叠 + 通道四档（本次新增，ticket #60）

**要解决的问题**：左栏固定 312 px——400+ 通道的场次里不够看，大屏上又浪费；通道列表
只有"全量"一种看法，常用通道每次都得重新搜。这一票把左栏做成可拖宽 / 可折叠 / 宽度记住，
并把通道列表分成 **固定 / 最近 / 当前 / 全量** 四档。

| 改/加 | 内容 |
| --- | --- |
| 拖宽 | 左栏右缘 `#asideGrip`（绝对定位，不跟左栏内容滚）：拖动改 `--aside-w`，双击回默认；宽度夹在 **220–560 px** |
| 折叠 | `#asideFold` 收成 34 px 窄边，展开按钮留在窄边上；折叠状态与宽度一起记住 |
| 通道四档 | `CHANNEL_VIEWS` 一处声明四档：固定 / 最近 / 当前 / 全量；切档后列表行数跟着换（`chCount` 仍是"已选 / 总数"，不随档位变） |
| 固定 / 最近 | 通道行右边的 ★ 固定常用通道；勾选 / 点过的通道进"最近"（最多 12 条） |
| 记忆位置 | 全部写 `localStorage['i3pro.aside.v1']`（宽度 / 是否拖过 / 折叠 / 当前档 / 固定 / 最近）。**不写工作表文件**：工作表是发给队友的，不该被我的左栏宽度影响（ADR-0008：外壳状态归外壳） |
| 窄窗口 | 没拖过宽度时，窄窗口仍走原来的两档（≤1460 用 288、≤1180 用 264）；拖过之后显示宽度再夹到 `innerWidth - 520`，但不改用户记住的值 |

```powershell
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"       # 901 项 PASS
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"          # 901 项 PASS
python tools\verify_clicks.py                                    # 需要 Edge；428 项通过、0 失败
```

**通过判据（本机实测，2026-10-19）**：

| 断言 | 实测 |
| --- | --- |
| 真鼠标拖把手 | 左栏 **rect 312 → 382 px**（JS 值同样 312 → 382，拖 +70 px） |
| 拖宽上下限 | 拖到天边夹到 **220 / 560 px** |
| 折叠 | 左栏剩 **34 px** 窄边，"展开"按钮仍在窄边上 |
| 折叠记忆 | 收起后真 `location.reload()`；重载完 `asideOpen()` = **false**、宽度仍 34 px |
| 展开回到记住的宽度 | JS 与 rect 都是 **360 px**（重载前记住的值） |
| 通道四档行数 | 全量 **438 行**、当前组件已选 **12 行**、固定 **1 行** |
| 固定 / 最近 | 点 ★ 后 `isPinned('G Force Lat')` = **true**；点过通道行后 `recentChannels()` = **["G Force Lat"]** |
| 四档都点得到 | 真鼠标点 `全量 / 当前 / 固定 / 最近` 四个按钮都生效；「最近」那一档 rows=1 |
| 无头断言 | 两份金标准 smoke 各 **901 项 PASS**（四档都在、切档行数变化、折叠再展开宽度不变） |
| 四条回归 | 单测 **635 项 / 70.1 s，0 失败、28 跳过**；`verify_ld_vs_csv.py` **PASS**；两份 smoke 各 **901 项 PASS**；`verify_clicks.py` **428/428** |

**真 Edge 抓到过两个假绿（都记在这儿）**：

1. `restoreAside()` 无条件加 `.asideSized`，把窄窗口那两档媒体查询关掉了——**票⑧ 的
   "窄窗口下左栏变窄"当场变红**（左栏 312、画布 416）。改成"拖过才加 `.asideSized`"。
2. `setAside()` 只改了 class，忘了把 `folded` 写进偏好对象——**"折叠状态记住"是假的**，
   重载又展开。`smoke` 的假 DOM 里没读这条持久化路径，只有真重载才露出来。

## A111 · 连续交互的渲染调度：同一帧只画一次（本次新增，架构评审 C1）

**要解决的问题**：连续交互以前每来一个事件就同步跑一遍 `renderAll()`——一次 `mousemove`
会把所有组件、概览、滚动条、状态行和 inspector 全广播一次。同一帧里的多个鼠标事件各自
重画一次，主线程被切成一串小任务；`renderAll()` 又同时是画轴的依据和工具里的同步入口，
不能直接改成异步。这一票把"要画什么"收成渲染层的粗粒度脏位，连续事件登记、由 `rAF`
合并成一帧；离散动作仍走同步 `renderAll()`。

| 改/加 | 内容 |
| --- | --- |
| 调度器 | `viewer.html` 新增 `RENDER_CURSOR / VIEW / DATA / LAYOUT / CHROME / COMPONENTS` 与 `scheduleRender()`；`flushRender()` 按脏位只跑依赖它的那几块，`renderAll()` 仍是同步全量入口 |
| 连续路径 | 图内光标、滚轮、图内拖拽、概览拖动、滚动条拖动、姿态拖拽、热力图 hover、纵轴缩放、窗口 resize 都改走 `scheduleRender(...)`；离散点击 / 表单变更仍走 `renderAll()` |
| 状态归属 | 脏位与调度统计只在渲染层（`renderScheduler`），不进 `state`、不进工作表文件（ADR-0008）；公共 `renderAll()` 的同步语义保留，工具与旧验收不需要改写 |
| 无头断言 | `smoke_viewer.js` 装一个可控 rAF：连续登记 `cursor + view` 只留一个回调，flush 后脏位不丢；两份金标准各 **905 项 PASS** |
| 真鼠标 | `verify_clicks.py` 在同一帧里发 8 次 `mousemove`，断言"登记 8 次 / flush 1 次"，并打印事件到下一帧的派发耗时与帧内渲染耗时；总数 **429/429** |

```powershell
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"       # 905 项 PASS
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"          # 905 项 PASS
python tools\verify_clicks.py                                    # 需要 Edge；429 项通过、0 失败
```

**通过判据（本机实测，2026-10-19）**：

| 断言 | 实测 |
| --- | --- |
| 同一帧 8 次连续 `mousemove` | 登记 **8** 次、flush **1** 次；事件派发 **0.3 ms**、那一帧的渲染 **2.7 ms** |
| 旧路径的对照 | 旧代码同一批 8 次事件会同步跑 8 次全量广播。A98 同机记过的全量稳态是 **17.9–26.2 ms/次**，照旧路径约 **143–210 ms**；这是按旧记录推算，**不是本轮同趟对照** |
| 真鼠标拖 12 步 | 中位 **10.7 ms** / 最大 **67.9 ms**（>16 ms 的 2 步、>50 ms 的 1 步）；空转派发基线中位 **16.8 ms**；Long Task **0 个**。这一列现在量的是事件派发墙钟，渲染已推迟到下一帧，**不能再拿它减基线当渲染耗时** |
| 真滚轮缩 6 格 | 中位 **17.4 ms** / 最大 **33.7 ms**（>50 ms 的 0 格）；Long Task **0 个** |
| 整屏 8000 桶直接 `renderAll()` | **32 ms/帧**（`#55`；这是直接全量入口的当前机器数，不是调度器的收益） |
| 四条回归 | 单测 **635 项 / 71.3 s，0 失败、28 跳过**；`verify_ld_vs_csv.py` **PASS**（`0 channel(s) outside tolerance`）；两份 smoke 各 **905 项 PASS**；`verify_clicks.py` **429/429** |

**边界**：这次修的是"同一帧里别重复广播"，不是"每个组件只画自己依赖的那一块"。
按组件失效（C4）留到下一张票；本票的 `RENDER_ALL` 仍与旧 `renderAll()` 同一套全量顺序，
所以直接调用 `renderAll()` 的性能与改前一致。

## A112 · `/trace` 二进制载荷：serve 走 TypedArray，JSON 留给快照（本次新增，架构评审 C2）

**要解决的问题**：ADR-0007 已经量出冷路径 118.3 ms 里约 **83.9 ms 是 `json.dumps`**——
服务端已经把数据算成 Python 列表，却又把它逐值变成 JSON 文本，浏览器再逐值解回来。
这一票给 `/trace` 加 `format=bin`：帧头只放通道元数据，大数组直接是 `float64` / `float32`
/ `int32` 字节，浏览器建 TypedArray 视图；快照与不传 `format` 的请求仍走 JSON。

| 改/加 | 内容 |
| --- | --- |
| 编解码层 | 新增 `src/i3pro/tracewire.py`：magic `I3TR` + 版本 + UTF-8 头 + 8 字节对齐数组；`encode()` 生产、`decode()` 当单测裁判 |
| 路由 | `act_trace` 支持 `format=json`（默认）与 `format=bin`；不认识的格式回 400 并列出可选项。`format` 进缓存 key，两种编码各自缓存 |
| 传输 | 二进制响应类型 `application/x-i3pro-trace`，与 JSON 共用 ETag / 条件请求 / gzip；命中缓存时不再重压 |
| 前端 | `apiGet` 支持可选 reader；`decodeTraceBinary()` 用 `DataView` + TypedArray 解码，失败回退 JSON；`traceUsesEnvelope` 接受 TypedArray |
| 精度 | 时间 `float64`；值 / min / max / 距离 `float32`；验收对 JSON 对拍按显示精度容差（A79 的 3 条改为 `1e-4 × max(1, |v|)`） |
| 协议决策 | [ADR-0009](adr/0009-binary-trace-payload.md)；ADR-0007 的翻案条件 1 已补记 |

```powershell
python tools\bench_viewer.py --http --repeat 3                    # JSON / BIN 的冷热耗时与字节
node tools\smoke_viewer.js "out\20260908-cjh 高避5圈.html"       # 908 项 PASS
node tools\smoke_viewer.js "out\20260524-耐久正赛.html"          # 908 项 PASS
python tools\verify_clicks.py                                    # 需要 Edge；429 项通过、0 失败
```

**通过判据（本机实测，2026-10-19）**：

| 断言 | 实测 |
| --- | --- |
| 高避 8000 桶计算 | JSON **118.8 ms** → BIN **49.3 ms**（明文 **3607.6 → 2630.1 KB**） |
| 耐久 8000 桶计算 | JSON **171.3 ms** → BIN **88.9 ms**（明文 **4086.3 → 2630.1 KB**） |
| 高避 HTTP 冷/热 | JSON **126.8 / 7.0 ms** → BIN **84.3 / 5.6 ms**（BIN gzip 上线 **720.1 KB**） |
| 耐久 HTTP 冷/热 | JSON **178.1 / 7.1 ms** → BIN **135.5 / 5.6 ms**（BIN gzip 上线 **1049.0 KB**） |
| 二进制与 JSON 对拍 | 合成 raw / envelope 两种形状 round-trip；两份金标准默认通道逐点一致（float32 容差内） |
| 缓存与压缩 | 二进制响应也有 `ETag` / `Cache-Control: no-cache` / gzip；第二次请求命中缓存，不重算不重压 |
| 前端解码 | 无头 smoke 手工造帧，断言 `Float64Array` / `Float32Array` 与元数据；真 Edge serve 模式实际走 `format=bin` |
| 四条回归 | 单测 **642 项 / 71.2 s，0 失败、28 跳过**；`verify_ld_vs_csv.py` **PASS**；两份 smoke 各 **908 项 PASS**；`verify_clicks.py` **429/429** |

**边界**：BIN 的 gzip 上线字节比 JSON 略大（高避 **720.1 vs 694.3 KB**，+3.7%）——
二进制把明文缩了约 27%，但十进制 JSON 的文本冗余更高、压得更狠。这次换来的主要是
服务端 CPU 与冷 HTTP 时间；热请求两者都已被响应缓存吃掉，所以只差约 1.4 ms。
