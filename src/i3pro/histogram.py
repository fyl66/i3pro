"""直方图：一条通道在一个区间里取值的分布（ticket #9）。

几件必须同时说清的事，否则算出来的东西看着像分布、其实不是：

* **数的是原始样本**，不是画图时那份 Min/Max 抽稀的结果——抽稀每一格都塞进一个
  极小值和一个极大值，分布会变成一根根假的长尾。所以界面只在 serve 模式下按
  当前窗口现问（``/api/session/<名>/histogram``），快照里内嵌的是导出时算好的几份。
* **哪些样本没进去**：被门槛排除的和非有限数（NaN）分开报条数。把被排除的混进
  样本数里，用户会以为通道真的有那么多样本。
* **颜色代表什么**：按第三通道着色时，每根柱子用的是那一箱里该通道的**均值**，
  连同极值一起给出来——只看均值会把"这里一会儿 0 一会儿 100"画成一片中间色。

门槛（gating）复用数学通道那一套：``gate`` 可以直接是一条通道名（非零为真），
也可以是一条数学通道表达式；解析在 :func:`gate_values` 里做，只有一处实现。

本模块是纯函数，不碰 :class:`~i3pro.ld.LogFile`——取数在
:func:`i3pro.render.histogram` 里。
"""

from __future__ import annotations

import numpy as np

from . import derive, maths

#: 默认格数。40 根柱子在 4 列宽（约 420 px）的组件里正好看得清。
DEFAULT_BINS = 40
#: 夹取范围：再少就不是分布，再多就是一片一像素的毛刺。
MIN_BINS = 4
MAX_BINS = 500
#: 三种画法，和 i2 Pro 的直方图一致（柱状 / 折线 / 文字）。
STYLES = ("bars", "line", "text")
#: 门槛的三种问法：非零为真、落在区间内为真、落在区间外为真。
GATE_MODES = ("nonzero", "range", "outside")


def clamp_bins(count) -> tuple[int, str | None]:
    """格数取整并夹进 4–500，夹过了带一句话出来。

    不夹的话 ``0`` 会让 ``np.histogram`` 直接抛异常，``100000`` 会画出一万根一像素
    的柱子——两种都不是"用户想要的分布"，而是一句"我按多少算的"。
    """
    try:
        value = int(round(float(count)))
    except (TypeError, ValueError):
        return DEFAULT_BINS, f"格数看不懂（{count!r}），按默认的 {DEFAULT_BINS} 算。"
    if value < MIN_BINS:
        return MIN_BINS, f"格数最少 {MIN_BINS}，已按 {MIN_BINS} 算。"
    if value > MAX_BINS:
        return MAX_BINS, f"格数最多 {MAX_BINS}，已按 {MAX_BINS} 算。"
    return value, None


def summarize(values) -> dict:
    """一张图上看得到的统计量；没有有效样本时每个字段都是 ``None``。"""
    data = np.asarray(values, dtype=np.float64)
    finite = data[np.isfinite(data)]
    if not finite.size:
        return {key: None for key in ("min", "max", "mean", "median", "std", "p5", "p95")}
    return {
        "min": float(finite.min()),
        "max": float(finite.max()),
        "mean": float(finite.mean()),
        "median": float(np.median(finite)),
        "std": float(finite.std()),
        "p5": float(np.percentile(finite, 5)),
        "p95": float(np.percentile(finite, 95)),
    }


def gate_keeps(values, mode: str = "nonzero", lo=None, hi=None) -> np.ndarray:
    """门槛通道的取值 →「这一条样本算不算数」。非有限数一律不算数。"""
    data = np.asarray(values, dtype=np.float64)
    if mode not in GATE_MODES:
        raise ValueError(
            f"门槛模式只认 {' / '.join(GATE_MODES)}，收到的是 {mode!r}。"
            "想做「持续 N 秒才算」这类条件，把条件写成数学通道表达式再选「非零」就行。"
        )
    if mode == "range":
        if lo is None or hi is None:
            raise ValueError("门槛模式 range 需要同时给 gate_min 和 gate_max（上下限）。")
        return np.isfinite(data) & (data >= float(lo)) & (data <= float(hi))
    if mode == "outside":
        if lo is None or hi is None:
            raise ValueError("门槛模式 outside 需要同时给 gate_min 和 gate_max（区间）。")
        return np.isfinite(data) & ((data < float(lo)) | (data > float(hi)))
    # 与数学通道内部一致：0 为假，非零为真。
    return np.isfinite(data) & (data != 0)


def gate_values(log, gate: str, size: int) -> np.ndarray:
    """把门槛条件取成主时间基上的一列。

    先当通道名认（i2 Pro 里 gating 大多数时候就是"拿另一条通道当条件"），不是
    通道名就交给数学通道去编译求值——**不另造第二套语法**，报错也是那套报错。
    """
    text = str(gate or "").strip()
    if not text:
        raise ValueError("门槛条件不能是空的。要么留空（不筛），要么给一条通道名或表达式。")
    if log.has(text):
        values = np.asarray(derive.hold_to_master(log, text), dtype=np.float64)
    else:
        try:
            values = np.asarray(maths.evaluate(text, log), dtype=np.float64)
        except maths.MathError as exc:
            raise ValueError(
                f"门槛条件算不出来：{exc}"
                "（门槛用的是「数学通道」那套语法，函数表在那个面板里，"
                "也可以直接填一条通道名。）"
            ) from exc
    if values.size >= size:
        return values[:size]
    pad = values[-1] if values.size else np.nan
    return np.concatenate([values, np.full(size - values.size, pad)])


def bin_edges(values, count: int = DEFAULT_BINS, lo=None, hi=None) -> np.ndarray:
    """等宽分箱的边界（``count + 1`` 个，严格递增）。

    通道整段是同一个值（一直没踩的刹车）时，范围要显式撑开：撑多少随量级走，
    这样柱子落在真实值上，而不是被 numpy 默认成 ``值 ± 0.5``——后者在几千那种
    量级上会把柱子画到 0 附近。
    """
    data = np.asarray(values, dtype=np.float64)
    finite = data[np.isfinite(data)]
    if lo is None:
        lo = float(finite.min()) if finite.size else 0.0
    if hi is None:
        hi = float(finite.max()) if finite.size else 1.0
    lo, hi = float(lo), float(hi)
    if not np.isfinite(lo) or not np.isfinite(hi):
        lo, hi = 0.0, 1.0
    if hi <= lo:
        pad = max(0.5, abs(lo) * 1e-3)
        lo, hi = lo - pad, hi + pad
    return np.linspace(lo, hi, int(count) + 1)


def colour_stats(values, colour, edges) -> list[dict]:
    """每根柱子里那条"上色通道"的均值 / 最小 / 最大 / 条数（空箱给 ``None``）。

    ``values`` 与 ``colour`` 必须已经对齐到同一批样本上；调用者负责这件事，
    因为只有它知道哪些样本被窗口和门槛留下了。
    """
    data = np.asarray(values, dtype=np.float64)
    paint = np.asarray(colour, dtype=np.float64)
    size = min(data.size, paint.size)
    data, paint = data[:size], paint[:size]
    edges = np.asarray(edges, dtype=np.float64)
    boxes = max(1, edges.size - 1)
    keep = np.isfinite(data) & np.isfinite(paint)
    index = np.clip(np.digitize(data[keep], edges) - 1, 0, boxes - 1)
    painted = paint[keep]
    out: list[dict] = []
    for box in range(boxes):
        chunk = painted[index == box]
        out.append(
            {
                "colour_count": int(chunk.size),
                "colour_mean": float(chunk.mean()) if chunk.size else None,
                "colour_min": float(chunk.min()) if chunk.size else None,
                "colour_max": float(chunk.max()) if chunk.size else None,
            }
        )
    return out


def histogram(
    values,
    count=DEFAULT_BINS,
    gate=None,
    colour=None,
    gate_mode: str = "nonzero",
    gate_lo=None,
    gate_hi=None,
) -> dict:
    """把一串样本数进箱子里。

    ``gate`` / ``colour`` 都是**已经对齐到同一批样本上**的序列（见
    :func:`gate_values`）：本函数不认识场次，也就不会自己去取数。
    """
    bins, notice = clamp_bins(count)
    data = np.asarray(values, dtype=np.float64)
    excluded = 0
    if gate is None:
        keep = np.ones(data.size, dtype=bool)
    else:
        keep = gate_keeps(gate, gate_mode, gate_lo, gate_hi)
        if keep.size != data.size:
            raise ValueError(
                f"门槛通道与要统计的通道长度对不上（{keep.size} vs {data.size}），"
                "先确认门槛给的是本场次的通道。"
            )
        excluded = int((~keep).sum())
    chosen = data[keep]
    finite = chosen[np.isfinite(chosen)]
    skipped = int(chosen.size - finite.size)
    edges = bin_edges(finite, bins)
    counts, _ = np.histogram(finite, bins=edges)
    out_bins = [
        {"lo": float(edges[i]), "hi": float(edges[i + 1]), "count": int(counts[i])}
        for i in range(bins)
    ]
    if colour is not None:
        for box, extra in zip(out_bins, colour_stats(chosen, colour, edges)):
            box.update(extra)
    else:
        for box in out_bins:
            box.update({"colour_count": 0, "colour_mean": None,
                        "colour_min": None, "colour_max": None})
    payload = {
        "bins": out_bins,
        "count": int(finite.size),
        "excluded": excluded,
        "skipped": skipped,
        "range": [float(edges[0]), float(edges[-1])],
        "stats": summarize(finite),
        "notice": notice,
    }
    # 门槛把整段都吃掉了：图上会是一条空线，不说明白就像"这条通道没有数据"。
    if excluded and not finite.size:
        payload["notice"] = "门槛把这一段的所有样本都排除了——放宽条件或者换个窗口再看。"
    elif excluded:
        extra = f"已按门槛排除 {excluded} 个样本，剩下 {int(finite.size)} 个参与统计。"
        payload["notice"] = f"{notice} {extra}" if notice else extra
    if not finite.size and not payload["notice"]:
        payload["notice"] = "这一段里没有有效样本——先放大到有数据的一段。"
    elif skipped and not payload["notice"]:
        payload["notice"] = f"另有 {skipped} 个样本不是有限数（NaN），没有参与统计。"
    return payload
