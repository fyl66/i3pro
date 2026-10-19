# viewer 状态谁拥有：`i3pro.state` 的四档归属表

布局 map（GitHub #50）的第一张票。三栏改版要动 DOM、要搬控件，而"这个状态跟着谁走"
今天只散在代码注释里。这张表**追认现状**（不翻案、不改一行代码）：先把归属钉死，
后面的搬家才不会把同一份混乱复制到三个位置。

核对日期 **2026-10-18**，当时 `i3pro.state` 有 **39 个键**（数法见最后一节）。
表里的"档"就是票面那四档，另加一节"今天没有归属"。

> **2026-10-19 更新（C4）**：`dataVersion` 已从 `state` 移到模块级 `revisions`
> （通道 / 轨迹 / 圈 / 数学 / 区段 / 对比圈六个依赖版本号，见
> [ADR-0010](0010-dependency-revision-stamps.md)）。下面那张表是 **2026-10-18 的现状记录**，
> 保留作历史；当前实现不再把 `dataVersion` 当 `state` 键。

## 四档是什么

| 档 | 跟着谁走 | 今天存在哪 |
| --- | --- | --- |
| **全局** | 不跟工作表、也不跟场次 | 只在页面内存里（刷新回默认） |
| **工作表级** | 一份 `worksheets/*.json` | 文件本身；"编辑态"那几个只在本页内存 |
| **组件级** | 某一件组件 | `components[i].config`（写进工作表文件），或只在本页内存 |
| **场次级** | 这一场 | 服务端载荷 / 侧车文件 / `localStorage` 的 `i3pro.worksheet.v2:<场次>` |

## 全局（17 个）

| 键 | 存在哪 / 谁给 | 一句话 |
| --- | --- | --- |
| `channelFocus` | 只在内存 | 最后点过的那一行通道（面板与快捷键的落点） |
| `cursor` | 只在内存 | 主光标（B）在哪一刻 |
| `dataVersion` | 只在内存 | 数据版本计数；各种缓存拿它当失效键 |
| `datum` | 只在内存 | 基准光标 A / B 的两刻 |
| `datumOn` | 只在内存 | 基准光标开着没有（D 键） |
| `focusId` | 只在内存（切工作表会清空） | 现在选中哪一件组件 |
| `mathsEdit` | 只在内存 | 数学编辑器正在改第几条定义 |
| `mode` | 只在内存（URL `?mode=overlay` 能指一下） | 时间轴 / 距离轴 / 叠加 |
| `pending` | 只在内存 | 在途请求数 |
| `placeBeacon` | 只在内存 | "正在放信标"模式 |
| `sectionsForce` | 只在内存 | 上一次重切被手工改动挡住过？再点一次带 force |
| `setupChannel` | 只在内存 | 通道处理面板正在改哪条通道 |
| `show` | 只在内存（`show.measure / legend / values` 三个子键） | M / L / V 三个**总闸** |
| `showNotes` | 只在内存 | 图上画不画注释 |
| `showSections` | 只在内存 | 图上画不画区段带子（时间轴模式） |
| `style` | 只在内存 | 线还是点（S 键） |
| `view` | 只在内存（切工作表会清空） | 当前窗口 `[起, 止]` |

## 工作表级（4 个）

一份工作表文件认得的顶层键（`worksheets.py` 的 `_TOP_KEYS`）是
`schema / name / order / hints / components / aliases / resolution`——其中 `name` 与 `order`
是文件身份，前端不放进 `state`；`hints` 是「规则挑通道」的默认词（`sheetHints()` →
`applyPicks()`），也不进 `state`。落到 `state` 里的是这四键：

| 键 | 存在哪 / 谁给 | 一句话 |
| --- | --- | --- |
| `aliases` | 文件里的 `aliases`（**有内容才写**，老文件一个字节都不多）；由工作表目录条目带过来 | 别名表的**工作副本**，点保存才写回文件 |
| `aliasesDirty` | 只在内存 | 别名有没保存的改动（"你没保存"那句话靠它） |
| `components` | 文件里的 `components`；场次记忆里也有一份副本 | 这一屏有哪几件组件，每件的 `config` 与位置都在这儿 |
| `resolution` | 文件里的 `resolution`（**非默认才写**）+ 场次记忆里一份副本 | 绘图分辨率档位（口径一，见下） |

## 组件级（1 个 + 一整棵树）

真正的"每件组件自己的设置"不在 `state.*` 上，而在 `state.components[i].config` 里
（画哪些通道、颜色、抬头字段 `config.show.*`、统计范围 `config.statsRange`、分栏
`config.mode`、调色板……）。它们**跟着工作表文件走**，换工作表就换一套。

`state` 上唯一带组件身份的是：

| 键 | 存在哪 / 谁给 | 一句话 |
| --- | --- | --- |
| `panelZoom` | 只在内存（键是 `组件 id + "|" + 分组`） | 每栏纵轴的手动量程；切工作表会清空 |

## 场次级（16 个）

| 键 | 存在哪 / 谁给 | 一句话 |
| --- | --- | --- |
| `aliasLanding` | 服务端按场次算（`aliases.annotate`） | 别名在这一场落到了哪条真通道（前端只查表） |
| `canUndo` | 服务端响应 | 服务内存里还有没有一版可撤销的配置 |
| `cmp` | 服务端载荷 / URL | 对比圈 |
| `gps` | 侧车 `<场次>.gps.json` | GPS 校正配置与"坏在哪"的计数 |
| `lapsConfig` | 侧车 `<场次>.laps.json` | 圈配置草稿（切分方式、信标、trusted） |
| `maths` | 服务端 `/maths` | 数学通道的定义 / 覆盖 / 报错 / 可用函数 |
| `notes` | 侧车 `<场次>.notes.json` | 图上的注释点 |
| `overlay` | 服务端载荷 | 叠加要用的另一份数据 |
| `overview` | 服务端载荷 | 概览条的时间序列 |
| `preset` | 浏览器记忆 `i3pro.worksheet.v2:<场次>` 的 `preset` | 现在用的**是哪一套工作表**（一个名字，指向文件） |
| `ref` | 服务端载荷 / URL | 参考圈 |
| `sections` | 服务端载荷（快照模式来自注入的 `DATA.sections`） | 赛道区段 |
| `traces` | 服务端 `/trace` | 已经取回来的曲线（窗口一变就重取） |
| `track` | 服务端载荷 | 轨迹图画什么 |
| `upsample` | 浏览器记忆的 `upsample` | 曲线"点与点之间"怎么取值（A79）；**不跟工作表走** |
| `windowKey` | 只在内存（跟着 `traces`） | 已经加载的窗口是哪一个（缓存失效用） |

## 今天没有归属（1 个）

| 键 | 现状 |
| --- | --- |
| `groupMode` | 字面量里声明了 `"tiled"`，**全文件没有一处写它**，只有画布缓存键读它——读到的永远是那个常量。真正生效的是每件组件的 `config.mode`（G 键改的就是它）。 |

它是这一票唯一的"名不副实"：搬家时别把它当成"全局分栏开关"，那会让两处定义打架。
本票**不动它**（票面要求只追认）；删它或接上它，留给动组件栏的那张票。

## 口径一：绘图分辨率是「每套具名工作表一份」

不是每张图一份，也不是全场共用一格：

- 写：`saveCurrentSheet()` / `saveSheetAs()` / `newSheet()` 把
  `resolution: resolutionKey(state.resolution)` 一起 PUT/POST；
  `worksheets.py` 只在**非默认**时才把它写进文件（`export_payload()`），
  所以仓库里那几份老工作表一个字节都没多。
- 读：`applyPreset(name)` 用 `currentSheet().resolution` 覆盖 `state.resolution`
  ——切工作表 = 切分辨率档。
- 兜底：`saveLayout()` 在浏览器记忆里另存一份，免得"刚切完档还没保存就刷新"丢掉。

这正是 Marple「工作区级」语义在 i3pro 的对应物——我们那层容器叫**工作表**，
不是每张图，也不是每场。

## 口径二：M / L 是总闸，组件栏里的是细调

- `state.show.measure / legend / values` 是**全局总闸**：键盘 M / L / V 一键开关，
  所有组件一起听。
- 每件组件的**细调**在 `comp.config.show.*`（抬头里 `cursor / delta / min / max / avg / unit`
  六个字段各一个开关，见 `headFields()`）与 `comp.config.statsRange`（可见区间 / A–B），
  由组件栏的「显示」菜单写，**存进工作表**。
- 两层是"与"的关系：总闸关了，组件自己开着也不显示（`statsOn = state.show.measure && …`）。

搬家时（右栏 inspector）只能碰第二层；把总闸塞进 inspector，就成了"这一件组件的设置"
——那是另一回事。

## 搬家时的三条规矩

1. **工作表 tabs 只能当 `preset` 的显示**：点一下 = `applyPreset()`，它会把
   `view / panelZoom / windowKey / focusId` 一起清空。tabs 自己不许再存一套"每张表上次看到哪"。
2. **右栏 inspector 只写 `components[i].config.*`**（组件级）；`state.show.*` 那三个总闸
   留在工具栏/快捷键上。
3. **左栏瘦身只搬全局临时态**（`channelFocus / setupChannel / placeBeacon`）——
   通道选择本身属于组件（`config.channels`），不属于左栏。

> **2026-10-18 票⑨ 落地时的补充（右栏 inspector）**：右栏里除了组件级那几格，还有
> 一格叫「这一屏怎么看（全局）」——`state.upsample`（曲线连接）与 `state.style`
> （点样式）两项。它们**不是**组件级：改的仍然是原来那两个键（取数只有一条路 #21，
> 两件图各要一种连接方式就得取两遍数），标题上也写明了"全局"，一格一类、不混。
> 规矩 2 那句"只写组件级"按它的本意读：**不许把组件级的设置与全局总闸混成一栏**。
> `state.show.measure / legend / values` 那三个总闸仍然只在工具栏与快捷键上，一个都没
> 搬进右栏；票⑨ 的票面把曲线连接与点样式列进了右栏，所以这两项按"全局那一格"落位。
>
> 同一票还把"点标题栏 = 选中这一件"接上了（`buildWorksheet` 里那条 `beginMove` 会
> `stopPropagation`，冒不到组件自己的那个 mousedown 上，于是点标题栏以前选不中）。

## 这张表是怎么核对的（票面判据）

核对日期 **2026-10-18**；当时的键数 **39**（37 + 2）。数法：

1. `const state = { … };` 那个字面量里有 **37** 个键（`mode` … `dataVersion`）。
2. `state.X =` 形式的赋值里出现过 **32** 个键，其中 `maths` / `mathsEdit` 两个字面量里没有
   ——它们是运行时补上去的（`state.maths = data` 那几处）。
3. `state["…"]` 与 `Object.assign(state, …)` 两种"偷偷加键"的写法**都是空集**，
   所以 37 ∪ 32 = **39** 就是全集。

```powershell
rg -n "const state = \{" src\i3pro\web\viewer.html          # 字面量：37 个键
rg -o "state\.([A-Za-z_$][\w$]*)\s*=" src\i3pro\web\viewer.html `
  | Sort-Object -Unique                                     # 赋值：32 个键
rg -n "state\[|Object\.assign\(state" src\i3pro\web\viewer.html   # 应当是空输出
```

## Considered Options

- **先把三栏 DOM 搬完再补契约**：否决。状态归属没定就搬家，等于把同一份混乱复制到
  左栏、右栏、顶部三处，之后再拆比一开始就拆贵得多。
- **把状态尽量塞进工作表文件**：否决。`cursor / view / pending / sectionsForce` 这类是**会话态**，
  写进文件只会让每拖一下鼠标都产生 diff，还会把"谁改了这份文件"搅浑。
- **本 ADR：一张表，逐键写清跟着谁走**：一张纸能回答"这个键搬家时该跟谁"，也是后面
  五张布局票共同的前提。
