# 项目规则（改这个仓库之前先读）

这几条是硬性约定，不是建议。它们的存在理由只有一个：**这个项目最大的风险是"写它的人毕业了"**。
所以规则的重点不是代码风格，而是让下一个人能相信仓库里写的每一句话。

---

## 1. 新增功能 = 五件套，缺一不合并

任何新增的组件、命令、算法，必须**同时**交付：

| # | 交付物 | 放哪 |
| --- | --- | --- |
| ① | 实现（算法写成不依赖框架的纯函数） | `src/i3pro/` |
| ② | 单元测试 | `tests/test_i3pro.py` |
| ③ | 无头交互断言（前端/交互类） | `tools/smoke_viewer.js` |
| ④ | 验收条目（可复制的命令 + 通过判据） | `docs/ACCEPTANCE.md` |
| ⑤ | 两份金标准数据实跑通过 | `20260908-cjh 高避5圈.ld` + `20260524-耐久正赛.ld` |

没有 ④ 的功能**不算做完**——因为没人知道怎么判断它坏了。

## 2. 文档里的每个数字必须实测

不许把"设计目标"写成"已实现"。写进 README / PLAN / ACCEPTANCE 的每一个性能数字、
精度数字、圈数、通道数，都必须是**这条命令跑出来的结果**，能在本机复现。

反面教材：`request/i2pro功能.txt` 是一份对 i2 Pro 的整理，里面混着大量我们**还没做**的能力。
它只能当需求清单读，**不能当"已实现"的证据**。

## 3. 解析正确性只认一个裁判：MoTeC 自己导出的 CSV

`tools/verify_ld_vs_csv.py` 必须始终 `PASS`。判据是每个可比通道都落在**该通道自己的显示精度**内
（`0.5 × 10^-decimals`）。任何人改动 `ld.py` / `importer.py` / `motec_csv.py` 之后，
这条不过就不算改完。

## 4. 不新增运行期第三方依赖

运行期只允许 `numpy` / `pandas` / `pyarrow`（Python）和浏览器原生能力（前端）。
要引入新的运行期依赖，必须先写一份 ADR 说明：为什么现有工具做不到、代价是什么、怎么退出。
**测试工具**不受此限（例如 PyInstaller、pypdf 只用于开发）。

## 5. 前端不许有构建步骤

`src/i3pro/web/viewer.html` 必须始终是"一个文件、双击即开、无 CDN、无 npm"。
任何需要打包器、需要 `node_modules`、需要联网拉资源的方案都不接受。

## 6. 面向队友的文案用中文

界面、CLI 输出、README、报错信息用中文；代码标识符、提交信息类型前缀用英文。
报错要说"下一步做什么"，不只是"哪里错了"。

## 7. 改动后必须跑完整回归

```powershell
python -m unittest discover -s tests -v      # 全部通过
python tools\verify_ld_vs_csv.py             # PASS，0 channel(s) outside tolerance
node tools\smoke_viewer.js out\<场次>.html   # 交互断言全过
```

三条全绿才算改完。缺数据的机器上相关用例会自动 skip，这不算通过——要在有数据的机器上跑。

## 8. 不能碰的东西

* `i2pro_data/`、`i2pro-help/` 不进仓库（前者是车队数据，后者是 MoTeC 版权材料）。
* 同名数据文件**永不覆盖**：导入一律走 `importer.unique_target()` 的 `-1/-2` 后缀。
* `.ld` 一律**只读**：本项目的定位是读取与分析，不写回 MoTeC 私有格式。

## 9. 明确不做（第一轮已确认）

视频组件、Alarms 告警（第二轮已改为待办，见 PLAN）、外部数学插件（VB.NET）、
Setup Sheets（依赖 Excel）、Matlab 导出、Mixture Map、Drag 直线加速项目模式、
多 Workbook 工程体系。要翻案请先改 `docs/PLAN.md`。

---

## 附：目录约定

```
src/i3pro/          Python 包（ld 解析 / derive 派生量 / laps 切圈 / store 存储 / render 载荷 / server 服务 / importer 导入）
src/i3pro/web/      前端模板（viewer.html；它是模板，数据由 render 注入）
tools/              开发工具（解析对照、无头前端驱动、exe 打包）
tests/              单测；依赖 i2pro_data/ 的用例在缺数据时自动 skip
docs/               PLAN.md 规划 · ACCEPTANCE.md 验收清单 · ld-format.md 格式逆向记录
out/                生成物（快照 HTML / Parquet），已 gitignore
```

## Agent skills

这一段是给工程技能（`to-tickets` / `triage` / `to-spec` / `wayfinder` / `domain-modeling` 等）读的仓库级约定。

### Issue tracker

Issue 与规格走**本仓库的 GitHub Issues**（`fyl66/i3pro`，用 `gh` CLI）；PR 不作为 triage 入口。
见 `docs/agents/issue-tracker.md`。

### Triage labels

沿用五个默认 triage 标签：`needs-triage` / `needs-info` / `ready-for-agent` /
`ready-for-human` / `wontfix`（标签字符串与角色同名）。
见 `docs/agents/triage-labels.md`。

### Domain docs

**单上下文（single-context）**：根目录 `CONTEXT.md` + `docs/adr/`。
两个都还不存在，这是正常的——`/domain-modeling` 会在真正需要钉死术语或决策时才创建；
探索代码时找不到就静默跳过，不提示、不预先创建。
见 `docs/agents/domain.md`。
