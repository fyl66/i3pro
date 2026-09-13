# i3pro

**保留 MoTeC C125 硬件，用开源工具链补上 i2 Pro 缺的那一半：**
原生 `.ld` 解析 → Parquet 列式存储 → GPS 自动切圈 / 距离轴双圈对比 → 免安装浏览器工作台。

不依赖任何商业软件、不需要联网、不需要 npm、不需要 Rust。
只用 Python 标准库 + numpy/pandas/pyarrow，`git clone` 下来就能跑。

```
i2pro_data/*.ld ──► 原生解析(mmap) ──► Parquet + 元数据 ──► SQL / 单通道秒级裁剪
                        │
                        └──► GPS 切圈 · 距离轴重叠 · Δ时间 ──► 自包含 HTML 工作台
                                                            └──► 本地服务（局域网 + 可分享链接）
```

---

## 一键启动

**双击 `启动.bat`** 就行——它会起本地服务并自动打开浏览器。（第一次运行 Windows 防火墙可能弹窗，选“允许访问”。）

| 双击这个 | 干什么 | 什么时候用 |
| --- | --- | --- |
| **`启动.bat`** | 起服务 + 开浏览器，可任意缩放，控制台会打印一个局域网地址 | 日常分析、在 P 房几个人一起看 |
| **`导出快照.bat`** | 把每个场次导出成 `out\<场次>.html`，再打开索引页 | 要把某一场发给没装 Python 的队友 |

命令行等价写法：

```powershell
cd E:\桌面\i3pro
.\i3pro.cmd serve --data i2pro_data --open         # 等价于 启动.bat
.\i3pro.cmd snapshot --data i2pro_data --out out   # 等价于 导出快照.bat
.\i3pro.cmd info i2pro_data\*.ld                   # 场次概览
```

`i3pro.cmd` 会自动探测可用的 Python（`python` / `py -3` / `python3`，以及
`%LOCALAPPDATA%\Programs\Python\Python3*`），把 `src/` 加进 `PYTHONPATH` 再调
`python -m i3pro`——**不需要 `pip install`，不需要联网**。

### ⚠️ 不要双击 `src\i3pro\web\viewer.html`

那是**模板**，本身不含数据——数据由 i3pro 在生成页面时注入。直接打开它只会看到一段说明
（以前是空白页 + 一个控制台报错，现在会明确告诉你去点哪个 bat）。

### 两种模式的区别只有一条：缩放时会不会补细节

* **`启动.bat`（serve）**：每次缩放按可见区间重新取全分辨率数据 —— 1 秒窗口 = 100 个原始样本，
  单次请求 3 ms。想看细节用这个。
* **`导出快照.bat`（snapshot）**：波形是提前抽稀好的，放大到很细的时间段会看到折线，
  但 HTML 自包含，发给谁都能双击打开。

---

## 已验证的能力（有数据支撑，不是接口声明）

全部结论来自 `i2pro_data/` 里 **8 个真实 C125 日志**（36.6–118.8 MB）和
MoTeC 自己导出的两个 CSV（182 MB / 425 MB）。

| 能力 | 证据 |
| --- | --- |
| **原生解析 `.ld`**（不需要 i2 Pro，也不需要任何转换工具） | 8 个日志全部解析成功，342–437 通道，int16/int32 与 100/50/25/20/10/5/2/1 Hz 混采 |
| **数值正确性** | 与 MoTeC 导出的 CSV 逐样本比对：两个场次各 **213/213 个可比通道全部落在显示精度内**，最大偏差 0.005 G / 0.4 deg/s（都是半个显示位）。见 `tools/verify_ld_vs_csv.py` |
| **独立实现交叉验证** | 与另一套独立逆向的解析器 `gotzl/ldparser` 对比：通道数、通道名、单位、采样点数完全一致，抽样通道数值 `rtol=1e-9` |
| **`.ld` → Parquet** | `耐久正赛` 118.8 MB → 19.0 MB（压掉 84%），**1.3 s**；`高避5圈` 36.6 MB → 5.3 MB，0.4 s |
| **单通道秒级抽取** | 19.4 万点抽稀成一个通道 → **7 ms**；整套元数据+切圈+对比+轨迹 payload → 0.04 s |
| **GPS 自动切圈** | C125 的 beacon 没接线、`.ldx` 里 `Total Laps = 1`，i2 Pro 切不出圈。i3pro 用起终点门 + 航向判据切：`高避5圈` → **5 个完整圈**（40.4–51.2 s，806–816 m），`耐久正赛` → **23 个完整圈**（最快 54.900 s） |
| **距离轴双圈对比** | 两圈按 1 m 步长插值到同一距离轴，输出 Δ 曲线、最大损失点；Δ 终点与圈速差一致（< 0.6 s） |
| **浏览器工作台** | 单文件 HTML（约 0.7 MB，数据全内嵌），多通道同步光标 + 缩放平移 + 时间轴/距离轴/双圈三模式 + 赛道速度着色 + 全通道搜索 + PNG 导出 + 分享链接。无服务器、无 CDN、双击即开 |
| **i2 Pro 级交互** | 双击拖拽框选缩放（`Alt` 纵向 / `Ctrl` 框选）· 概览条与滚动条 · 基准光标与 Δ 测量 · 可见区间 min/max/avg · 光标处全通道数值 · 点/线样式 · 按同单位分组的共享纵轴 · 状态故障带 · 散点组件（X×Y、第三通道着色、跟随缩放、光标联动） |
| **工作表 / 组件** | 12 列网格，**拖标题栏移动、拖右下角同时改宽高、边缘吸附到相邻组件**；组件类型：时间/距离图 · 散点图 · 赛道轨迹 · **仪表**（数值/列表/条形/表盘/方向盘）· 时间差 Δ · 状态与故障；6 套预设（分析/双圈对比/动力/底盘/车手/仪表台）；每个图有自己的通道列表；**整张工作表连同位置尺寸能编码进分享链接** |
| **图表抬头** | i2 Pro 的 Measurements 列：`通道名 \| 光标值 \| Δ \| Min \| Max \| Avg`，光标值随鼠标实时跳动，不用低头看左栏 |
| **圈速交互** | 单击某圈 = 设成基准圈**并跳到该圈**；`Ctrl+点击` = 对比圈；`Shift+点击` = 只跳转 |
| **信标编辑** | 点信标名**就地改名**（回车保存 / `Esc` 取消，重名自动加后缀，可信标记跟着迁移）；「＋ 穿越」在**光标处插入一次漏掉的穿越**（i2 Pro 的 Missed Beacon），它只加边界、不会把已有圈次清空 |
| **切分方式** | 自动挑门 / 按运行分段（八字、直线加速、skidpad 的正确单位）/ 八字按环；一刀切不出圈的场次也能手工给信标 |
| **缩放不丢细节（serve 模式）** | 快照是预先抽稀的；`serve` 模式每次缩放按可见区间重新取样：1 秒窗口返回 100 个原始样本，单次请求 3 ms |
| **数学通道** | 白名单表达式（53 个函数，**不用 `eval`**）造派生通道，效果与原生通道一样：可画图、可进散点、可切圈、可进报表。两种作用域——**本地**跟着场次（`<场次>.maths.json`），**全局**在仓库里（`maths/global.json`），同名时本地赢，界面上用角标写明谁生效。坏式子不进侧车，一条坏了不拖累其它条 |
| **工程化** | 96 项单测全绿（真实数据回归 + HTTP 端到端 + 无头驱动前端 129 个断言点），零第三方运行期依赖；改这个仓库的硬性规则见 [AGENTS.md](AGENTS.md) |

完整验收清单与复现命令见 **[`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md)**；
规划、里程碑与风险见 **[`docs/PLAN.md`](docs/PLAN.md)**。

---

## 命令

| 命令 | 作用 |
| --- | --- |
| `info <files...>` | 场次概览：设备、日期、时长、采样率、通道数 |
| `channels <file> [--filter <子串>]` | 列出通道（名称/单位/采样率/缩放/小数位） |
| `laps <file> [--json <path>]` | 圈速表（含相对最快圈的 Δ 与异常段标记） |
| `track <file>` | 圈速柱状速览（终端里快速判断切圈对不对） |
| `delta <file> [--ref 2] [--cmp 5]` | 距离轴双圈对比，输出 Δ 曲线 JSON 与最大损失点 |
| `convert <files...> --out out` | `.ld` → Parquet + `meta.json` |
| `series <parquet> --channels a,b --from 400 --to 405` | 按列 + 按时间窗裁剪（Parquet 列式存储的真正用处） |
| `query "<SQL>" --parquet <files...>` | 对已转换数据集跑 SQL |
| `export <file> --out x.csv` | 导出选中通道为 CSV |
| `render <file> [--channels a,b] [--ref 2] [--cmp 5]` | 生成自包含 HTML 工作台 |
| `serve [--data dir] [--host 0.0.0.0] [--port 8731] [--open]` | 本地/局域网 Web 工作台 |
| `snapshot [--data dir] [--out out] [--open]` | 批量导出每个场次的离线 HTML + 索引页 |

---

## 快捷键（对齐 MoTeC i2 Pro 的键位）

| 类别 | 键 | 作用 |
| --- | --- | --- |
| 缩放 | 双击拖拽 | 横向框选放大（`Alt` 纵向 · `Ctrl` 框选） |
| | 双击 | 以点击处为中心放大 2× |
| | 滚轮 | 以指针处为中心缩放 |
| | <kbd>↑</kbd> <kbd>↓</kbd> / <kbd>Alt</kbd>+上下 | 横向 / 纵向缩放 |
| | <kbd>F2</kbd> / <kbd>W</kbd> / <kbd>Z</kbd> | 全出 / 默认一圈 / 缩到两光标之间 |
| | <kbd>Esc</kbd> | 取消正在框选 |
| 平移 | 拖拽图内或坐标轴 · 双击滚动条 | 平移 / 全出 |
| | <kbd>Shift</kbd>+左右 · <kbd>F</kbd> <kbd>B</kbd> · <kbd>H</kbd> | 平移 · 前/后翻页 · 以光标居中 |
| 光标 | 鼠标移动 · <kbd>←</kbd> <kbd>→</kbd> · <kbd>Ctrl</kbd>+左右 | 移动 / 步进 0.01 s / 步进 1 s |
| | <kbd>D</kbd> · <kbd>空格</kbd> · <kbd>X</kbd> | 基准光标开关 · 放置 · 与主光标交换 |
| 显示 | <kbd>S</kbd> | 线样式 ⇄ 点样式 |
| | <kbd>G</kbd> | 分栏 ⇄ 重叠 |
| | <kbd>M</kbd> | 可见区间 min/max/avg |
| | <kbd>L</kbd> / <kbd>V</kbd> / <kbd>E</kbd> | 图例 / Values 窗口 / 增删状态与故障组件 |
| 圈 | 单击某圈 | 设成基准圈并跳到该圈 |
| | <kbd>Ctrl</kbd>+点击 · <kbd>Shift</kbd>+点击 | 设成对比圈（切双圈对比）· 只跳转 |
| | <kbd>N</kbd> <kbd>P</kbd> · <kbd>Ctrl</kbd>+<kbd>F</kbd> · <kbd>Q</kbd> | 上/下一圈 · 最快圈 · 交换主/对比圈 |
| 工作表 | 组件标题栏 | 拖拽移动（吸附）· `↑` `↓` 换序 · `✕` 移除 · 右下角拖拽同时改宽高 |
| | 左上 | 下拉选组件类型 → 「＋ 添加组件」；预设按钮一键换整张表 |

在浏览器控制台里可以用 `i3pro.state` / `i3pro.zoomTo(...)` 直接调试视图。

---

## 目录结构

```
启动.bat            一键起服务 + 开浏览器（给队友用这个）
导出快照.bat        一键导出所有离线 HTML 快照
i3pro.cmd           命令行入口（自动探测 Python），两个 bat 都调它
docs/               PLAN.md 规划 · ACCEPTANCE.md 验收清单 · ld-format.md 格式逆向记录
tools/              verify_ld_vs_csv.py 解析对照 · smoke_viewer.js 无头驱动前端
tests/              96 项单测
maths/              全局数学通道定义（global.json，跨场次复用）
out/                生成物（快照 HTML / Parquet），已在 .gitignore 里
i2pro_data/         试车数据（.ld/.ldx/.csv），不进仓库

src/i3pro/
├─ ld.py            .ld 原生解析（mmap，不复制数据）
├─ motec_csv.py     i2 Pro CSV 导出读取（用于对照与兜底）
├─ derive.py        速度源选择 / 距离轴积分 / GPS 局部投影
├─ laps.py          GPS 切圈 / 距离轴重叠 / Δ时间
├─ store.py         Parquet + 元数据落盘、列式裁剪、SQL
├─ render.py        工作台 payload 构建 / 通道分组 / Min-Max 降采样 / 自包含 HTML
├─ maths.py         数学通道：白名单表达式编译求值 / 作用域 / 派生列缓存
├─ server.py        标准库 HTTP 服务（场次列表 + JSON API + 工作台页）
├─ cli.py           命令行入口
└─ web/viewer.html  前端（手写 Canvas，无框架、无构建）
```

---

## 数据里必须知道的坑

以下都是在这批真实日志上量出来的，不是猜测：

1. **MoTeC 的 `Distance` 通道恒为 0**，C125 的 beacon 输入没有接线，
   `.ldx` 里 `Total Laps` 永远是 1。**距离轴和圈次都得自己算**——
   这正是 i3pro 存在的理由。
2. **默认速度源是 `Vx KF`**（记录仪自己的卡尔曼滤波纵向速度，100 Hz）。
   轮速通道 `SpeedFR/FL/...` 在部分场次不存在，`GPS Speed` 在部分场次恒为 0。
3. **`decimals` 是有符号 16 位**，`0xffff` 表示 −1（即 ×10）。
   按无符号读会让 `Timestamp MTI` 之类通道整整差 100 倍。
4. **CSV 导出会把慢通道重采样到 100 Hz 并去掉 `(LoRes)` 后缀**，
   所以拿 CSV 校验时只能比对与导出采样率相同的通道。
5. 格式细节与未解析部分见 [`docs/ld-format.md`](docs/ld-format.md)。

---

## 开发

```powershell
python -m unittest discover -s tests -v      # 96 项，无数据文件时自动 skip
python tools\verify_ld_vs_csv.py             # 与 i2 Pro CSV 逐通道对照
node tools\smoke_viewer.js out\demo.html     # 无头驱动前端：缩放/光标/分组/信标编辑/数学通道等 129 个断言点
```

算法层（`derive` / `laps` / `render`）是不依赖框架的纯函数，改动请优先补单测——
这支车队最现实的风险是「写代码的人毕业了」。

### 常见问题

**双击 `src\i3pro\web\viewer.html` 一片空白 / 没有数据**
那是模板文件，不含数据。双击 `启动.bat`，或者双击 `out\<场次名>.html`。

**双击 `启动.bat` 提示 "Could not find a working Python 3"**
装一个 Python 3.10+（[python.org](https://www.python.org/downloads/)），安装时勾上
`Add python.exe to PATH`。注意本机如果装过 `py` 启动器但没有注册解释器，脚本会自动跳过它。

**端口 8731 被占用**
启动脚本会自动往后找 10 个端口，控制台会打印实际用的那个。也可以手动指定
`--port 9000`。

**`git push` 连不上 github.com（超时 / connection reset）**
浏览器能开 GitHub、git 却不行，通常是系统走本地代理而 git 没走。查一下
`HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings` 里的 `ProxyServer`
（本机是 `127.0.0.1:7890`），然后让 git 也用它：

```powershell
git config --global http.proxy http://127.0.0.1:7890
git config --global https.proxy http://127.0.0.1:7890
```

**某个场次切不出圈**：看 `laps` 的 `complete` 列。直线/单圈测试本来就只有 1 段，
首尾的进出场段会被标记成「进出场/泊车/异常段」，不计入最快圈统计。

**通道值看起来差 100 倍**：检查是不是把 `decimals` 当无符号读了（见上文第 3 条）。

---

## 许可证

GPL-3.0。格式逆向的原始工作来自 [gotzl/ldparser](https://github.com/gotzl/ldparser)，
本项目保留其源码在 `vendor/ldparser/`（含 GPL-3.0 全文）用于**测试交叉验证**，
运行期不依赖它。若希望以 MIT 发布，需要移除 `vendor/` 与相关引用。
