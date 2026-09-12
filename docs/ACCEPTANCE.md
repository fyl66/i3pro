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

## 全量回归

```powershell
python -m unittest discover -s tests -v
```

**通过判据**：`Ran 21 tests` + `OK`（无数据文件时相关用例自动 skip，不算失败）。

测试覆盖：

| 分组 | 内容 |
| --- | --- |
| `TestHeader` | 元数据、通道链表自洽性、数据块连续性 |
| `TestScaling` | 缩放公式、负小数位（×10）、与 CSV 导出对照 |
| `TestDerived` | 距离单调性、GPS 轨迹尺度合理 |
| `TestLaps` | 两种赛道的切圈、距离轴重叠、Δ 收敛 |
| `TestStore` | Parquet 往返、列式裁剪、SQL 查询 |
| `TestCsvReader` | i2 Pro CSV 导出结构解析 |
| `TestRender` | 静态/服务两种 payload、自包含性 |
| `TestServer` | HTTP 端到端：场次列表、工作台页、通道、时间窗、对比圈、赛道、404 |
| `TestIndependentParsers` | 第二套实现交叉验证、213 通道 CSV 全量对照 |
| `TestViewerScript` | 无头跑前端脚本，时间轴与双圈两条渲染路径 |
