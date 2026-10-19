# viewer 缓存失效：一个 `dataVersion` 换成按依赖记的 revision

C4 之前，`state.dataVersion` 是一个粗粒度计数器：任何一处数据改动都让所有组件的
`componentKey`、inspector 和报表刷新键一起变化。结果是换一对对比圈、改一条数学通道，
离屏的散点图和报表也会被判定为失效；缓存命中与不命中都说不清原因。

## 决策

1. **`dataVersion` 移出 `state`**，改成模块级 `revisions`，只记六项依赖：
   `channels` / `traces` / `laps` / `maths` / `sections` / `overlay`。
2. **版本号单调递增**，通过 `revisions.bump(name)` 写入；`revisionKey(...names)` 把
   需要的那几项拼进缓存键。未认识的 name 直接抛错，避免打错字后静默失去失效。
3. **画布缓存按组件声明读依赖**。曲线图的 `canvasRevision` 读
   `channels / traces / maths / sections / laps / overlay` 与 `showSections`；散点图不把
   这些全量塞进画布键，它只在自己的 `dataKey` 变化时清画布缓存。
4. **取数键各自带依赖**：
   - 散点 / 直方图 / 频谱：`channels + maths`
   - 热力图：`channels + maths + laps`
   - 时间 / 通道报表：`laps + maths + sections`
   - 轨迹：`channels`
5. **窗口、样式、主题仍留在各自的键里**，不升级成全局 revision。窗口是 `state.view`
   的直接输入，主题是 `themeKey`，它们本来就能精确表达“这一屏变了没有”。

## 实测

`tools/smoke_viewer.js` 新增两条对照：

- `revisions.bump("traces")` 后，曲线图的 `componentKey` 必须变化，散点图的
  `componentKey` 必须不变。
- 同一趟里，报表的 `reportFetchKey` 不因 `traces` 变化；`revisions.bump("sections")`
  后必须变化。

真 Edge 的 `#55` 全量重画基准也改成 `revisions.bump("traces")`，保证它量的仍然是
曲线图真的重画，而不是缓存命中。

## 代价与边界

- 这是一张**依赖声明表**，不是自动依赖分析。新增一个组件数据源时，作者仍要决定它
  读哪几个 revision；漏写会表现为“该重算的没重算”，所以每条新依赖都要有无头断言。
- revision 只在内存里，刷新页面回到 0；它不写工作表、不写侧车，也不参与分享链接
  （与 ADR-0008 的外壳状态归属一致）。
- 窗口、分辨率、曲线连接方式仍走原有键；它们不是“数据 revision”，不塞进 `revisions`。

## Considered Options

- **保留 `dataVersion`，只在组件键里加更多条件**：否决。粗粒度计数器仍在，离屏组件
  继续陪跑，而且“为什么失效”仍然不可解释。
- **给每个组件一个独立的 revision**：否决。组件数量可变，且同一个数据源会重复记账；
  依赖源只有六类，按依赖记更小、更稳定。
- **自动从组件配置推导依赖**：否决。当前前端没有构建步骤，配置是普通对象；自动推导
  会引入一条难验证的隐式规则，不如显式声明加无头断言。

## 退出方式

删掉 `revisions`、把各缓存键恢复成一个粗粒度计数器，并删除 `componentKey` 的
`canvasRevision` 分支即可。`revisions` 没有持久化状态，不需要迁移。
