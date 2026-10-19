# session-handle interface 暂缓：先别为一个适配器造包装

C5 的提议是给 `SessionLibrary` 加一个小的 session handle，让 server / api / cli 只拿
`load · stamp · summary`。这张 ADR 记录这次没有落地的理由，以及将来什么信号出现时
应该重新打开它。

## 核对（2026-10-19）

`SessionLibrary` 当前有 **15 个公开方法**：

`members_of / data_stamp / names / path_of / get / apply_maths / maths_state /
maths_names / summary / listing / remember_laps / laps_undo_slot /
forget_laps_undo / close / upload_dir`

server / api / cli 实际用到其中 **11 个**。其中 API 的会话内路径不是三件事，而是：

- `get`（加载）
- `maths_state` / `maths_names` / `apply_maths`
- `laps_undo_slot` / `remember_laps` / `forget_laps_undo`

`summary` 没有直接调用方（它主要被 `listing` 内部用），而 `names` / `listing` /
`upload_dir` / `close` 是库级动作，不属于单个会话。

## 决策

**暂缓 C5。** 现在不加 `SessionHandle`，不把 `load / stamp / summary` 做成新层。

原因是两边的形状都不对：

1. **只做三个方法的 handle 是浅包装**：server 仍要 `names()` 做未知场次页，API 仍要
   绕过它调用 maths / laps undo / upload；handle 没有减少调用方需要知道的规则。
2. **把所有会话内动作搬进 handle 会得到第二套宽接口**：它至少要覆盖 8 个方法，还要
   与 `SessionLibrary` 的现有实现和测试保持同步；在只有一个存储实现、没有测试替身
   或第二后端的今天，这是为假设的变化付维护成本。

`SessionLibrary` 继续作为唯一的场次模块。它内部已经有 discovery、listing cache、
maths attach、laps undo 这些深实现；C5 的 leverage 还不存在。

## 重新打开的触发条件

出现下面任一条时，先写新的 ADR，再一起迁移 server / api / cli：

- 出现第二种场次存储或会话来源（例如远端库、内存测试库），需要同一个 interface 的两个
  适配器；
- 需要在不启动 socket、也不复制 `SessionLibrary` 缓存规则的前提下替换会话加载；
- API / server 里再次出现“每个调用方各自拼 `path_of` + `get` + `data_stamp`”的新路径，
  说明当前接缝已经漏了。

真要打开时，handle 的最小接口应覆盖 `load / stamp / maths_state / maths_names /
apply_maths / undo_slot / remember_laps / forget_laps`，而不是只包三个方法。

## Considered Options

- **现在就做 `load / stamp / summary` handle**：否决。浅包装，不能减少 server / api / cli
  对库级方法的直接依赖。
- **现在就做全会话 handle 并迁移所有调用方**：否决。没有第二适配器，改动面覆盖
  `library.py`、`server.py`、`api.py`、`cli.py`，收益不可测，违背“一个适配器只是假设的缝”。
- **把 `SessionLibrary` 直接拆成 discovery / cache / maths / laps 多个对象**：否决。
  当前模块内部已经有清晰私有方法；拆成多个公开对象会把缓存失效规则暴露给调用方。

## 退出方式

这份 ADR 不改变运行代码。将来若触发条件成立，直接删除本 ADR 的“暂缓”结论并新开一份
接口 ADR；`SessionLibrary` 的现有公开方法可以先保留为兼容面，迁移完成后再收窄。
