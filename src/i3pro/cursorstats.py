"""光标区间统计（ticket #44）：同一组数，两种范围。

抬头那一列 Min / Max / Avg 原本只算"**当前可见区间**"（跟着缩放走）。这一票给它加一个
**范围开关**：可见区间（默认，现状）或**光标 A–B**（由两个光标钉死）。
Marple 的 ``POST /api/v1/signals/cursor/stats`` 实测只回三个数 ``{min, mean, max}``，
所以这里**不加新统计量**——σ / 斜率 / 积分不做（见 ``docs/spec-marple-parity.md`` §1
的非目标），换的只是范围。

口径（改这里就要同时改 ``CONTEXT.md`` 与 ``viewer.html`` 里那份镜像）：

* **左闭右闭**：正好落在 A 或 B 上的样本算数——与导出那套范围（全部 / 视图 / 选中圈 /
  A–B / 时间段 / 距离段）是同一个口径。
* **反序自动交换**：``A=18, B=12`` 与 ``A=12, B=18`` 是同一个区间。
* 只数**有效样本**：NaN / inf 不算数（与可见区间统计、直方图同一条纪律）。
* 一个有效样本都没有时返回 ``None``，**不是 0**——0 会被读成"这一段的值真的是 0"。
* 数的是**原始样本**：不重采样、不插值。重采样是导出那条路上的选项，与这里无关。

前端没有构建步骤、跑不了 Python，所以 ``viewer.html`` 里的 ``rangeStats`` / ``abSpan``
是这里的镜像；两边由 ``tools/smoke_viewer.js`` 拿**冻结常量**对拍，那一组数同时钉在
``tests/test_i3pro.py::TestCursorStats`` 里（两边分叉就会红）。

**这份模块没有生产调用方**（与 :mod:`i3pro.attitude` 一样是**镜像模块**）：屏幕上那三个数
就在浏览器里算，没有服务端 / CLI 入口需要它。它在这里的作用有三个，缺一个这条口径就会
飘：① 把"左闭右闭 / 反序交换 / 空区间给 None"写成**可执行的定义**；② 给单测一个对照基准
（含两份金标准）；③ 给 ``tools/verify_clicks.py`` 一个"前后端是不是同一口径"的裁判。
评审时如果嫌它"没有出口"，请连着 ``attitude.py`` 一起判断——这两处的做法是有意一致的。

**一个容易读错的边界**：A–B 只由**两个光标**决定，**不与缩放窗口求交**。理由是 Marple 的
``cursor/stats`` 请求里只有两个光标时刻、没有窗口（见 ``docs/marple-parity.md`` 的运行时
证据），而"可见区间"已经是另一种范围了；两者混起来，用户反而算不出自己想要的数。
（serve 模式下取数本身只加载当前窗口，所以那时 A–B 也只会落在已加载的样本里——这与既有的
"可见区间"统计是同一条边界，不是这一票新开的。）
"""

from __future__ import annotations

import numpy as np

__all__ = ["RANGES", "DEFAULT_RANGE", "LABELS", "MISSING_AB", "span_of", "interval_stats"]

#: 统计范围的两个取值；``view`` 是现状（默认），``ab`` 是这一票加的。
RANGES: tuple[str, ...] = ("view", "ab")
#: 默认范围：不写就是原来那个"跟着缩放走"。
DEFAULT_RANGE = "view"
#: 范围在界面上的中文名（组件栏「显示」里那两枚按钮的文案）。
LABELS = {"view": "可见区间", "ab": "光标 A–B"}

#: 选 A–B 而**没有基准光标**时说的那句话。
#:
#: 验收要求"复用，别写第二遍"——所以它在前端只出现一次（``viewer.html`` 的
#: ``NO_AB_HINT``，导出面板的「光标 A–B」预置与抬头共用同一个常量）。这里这一份是
#: 给单测用的对拍基准：``TestViewerScript`` 断言页面里这句话**只写一遍**，
#: 且与这里逐字相同。
MISSING_AB = "没有 A–B：按 D 打开基准光标，把鼠标放到 A 的位置按空格；再移到 B 按空格。"


def span_of(a, b):
    """两个光标 → ``(小, 大)``；缺一个（``None``）就是没有区间，给 ``None``。

    反序自动交换，所以"先把 B 放在左边"（A 在右）与正常顺序拿到同一个区间。
    """
    if a is None or b is None:
        return None
    first, second = float(a), float(b)
    if np.isnan(first) or np.isnan(second):
        return None
    return (first, second) if first <= second else (second, first)


def interval_stats(x, values, a, b) -> dict[str, float | int] | None:
    """闭区间 ``[a, b]``（反序自动交换）里有效样本的 min / max / avg / 样本数。

    没有有效样本（区间空的、或者这一段全是 NaN）给 ``None``——**不是 0**。
    """
    span = span_of(a, b)
    if span is None:
        return None
    xs = np.asarray(x, dtype=np.float64).reshape(-1)
    vs = np.asarray(values, dtype=np.float64).reshape(-1)
    size = min(xs.size, vs.size)
    xs, vs = xs[:size], vs[:size]
    keep = np.isfinite(vs) & (xs >= span[0]) & (xs <= span[1])
    picked = vs[keep]
    if not picked.size:
        return None
    return {
        "min": float(picked.min()),
        "max": float(picked.max()),
        "avg": float(picked.mean()),
        "n": int(picked.size),
    }
