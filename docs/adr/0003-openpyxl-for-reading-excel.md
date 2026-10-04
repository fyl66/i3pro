# Excel 只读那一侧用 openpyxl（写出仍走仓库自带的最小 OOXML 写入器）

规则 4 原先只允许 `numpy` / `pandas` / `pyarrow`。**读别人的 `.xlsx`** 是第一个
真正撑破这条清单的需求（ticket #31），所以在这里把边界钉死：**只有读用 openpyxl**。

## 为什么现有工具做不到

- **`pandas.read_excel` 不是替代品**：`engine="openpyxl"` 就是 openpyxl 的壳，
  换成它只是把依赖藏进 pandas 的调用里，装上还是一样的包。
- **自己解 OOXML 的读侧**：写侧我们已经自己写了（`src/i3pro/xlsx.py`，只产出数字、
  文本、空单元格——格式由我们说了算）。读侧面对的是别人造的工作簿，要处理共享字符串、
  内联字符串、1900 日期序列号与日期格式、公式缓存值、合并单元格、多张 sheet、
  空行与稀疏维度。每一样都有"看着成功、数字是错的"这一种失败方式，而这一侧的
  正确性没有第二个裁判（写侧还有 Excel 自己当裁判）。
- **代价**：多一个需要跟着 Python 版本走的第三方包；装不上的机器读不了 Excel
  （报错会写清下一步：装一次 `pip install openpyxl`，或把表另存为 CSV）。

## 怎么退出

读侧全部收在 `src/i3pro/xlslog.py` 一个模块里，只用到了 openpyxl 的四处：
`load_workbook(read_only=True, data_only=True)`、`sheetnames`、`iter_rows(values_only=True)`、
`close()`。要换实现（自写 OOXML 读取器、或换别的库），只需要在这一个文件里替换这四处，
其余代码看到的一直是"表头那几行 + 一张数据表"，装配成场次那一步在
`csvlog.session_from_frame`（与 CSV 共用）。

写侧不受影响：`i3pro export --format xlsx` 仍然用标准库拼 OOXML，openpyxl 在
`tests/` 里当独立裁判（规则 4 对测试工具本来就豁免）。

## Considered Options

- **读也用最小自写解析器**：能保住零依赖，但要覆盖上面那一串坑，而且没有第二个裁判
  能证明它读对了；风险落在"用户拿到一份看着正常、数字缺了一半的场次"。
- **写也换成 openpyxl**：纯churn——写出这一侧已经通过真 Excel 与 pandas 的双重校验
  （含 104 万行分 sheet），换掉只是把已验证的东西重写一遍。
- **只支持 CSV、让用户先另存为 CSV**：把成本推给用户，而且 Excel 正是队友最常发的格式。

## 规则 4 的允许清单（本次更新）

运行期依赖：`numpy` / `pandas` / `pyarrow` / **`openpyxl`（仅读 `.xlsx`，见本 ADR）**。
新增别的依赖仍然要先写 ADR。
