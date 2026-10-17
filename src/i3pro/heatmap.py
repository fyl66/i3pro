"""热力图：两个通道分箱，用第三通道（或样本数）着色（ticket #45）。

**证据的成色**：Marple 的 Heatmap 在 Aggregate 组，本轮**没有运行时对照**（没点开它），
所以这一票按我们自己的需求定，只借它两件事——它跟直方图同属"聚合类"，以及它用**第三
通道着色**的思路。见 ``docs/spec-marple-parity.md`` §5。

几条必须说清的规矩（和直方图同一条纪律）：

* **数的是原始样本**：不是画图那份 Min/Max 抽稀的曲线——抽稀扔掉的点正好是分布的两端，
  拿它画热力图会把"这一片真的有数据"画没。所以这条路只在 serve 模式按需调用。
* **两个轴等宽分箱**，格数夹在 8–200（再少看不出形状，再多是一片一像素的毛刺）。
  上界算进最后一格——与 :func:`numpy.histogram2d` 同一个口径（单测逐箱对拍）。
* **着色两种问法**：``count``（这一格有多少点）与 ``z-mean``（这一格里第三通道的均值，
  连同 min / max 一起给——只看均值会把"这里一会儿 0 一会儿 100"画成一片中间色）。
* x / y / z 里有非有限数（NaN / inf）的样本**不进任何格子**，并单独报出条数：
  把丢掉的和有效的混在一起数，用户会以为通道真有多少样本。
* 一个轴整段是常量时，范围照 numpy 的规矩撑开 ±0.5（否则轴宽是 0，什么也画不出来），
  并在 ``notice`` 里说一句。

纯函数，不碰 :class:`~i3pro.ld.LogFile`——取数在 :func:`i3pro.render.heatmap` 里。
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "DEFAULT_BINS",
    "MIN_BINS",
    "MAX_BINS",
    "SCHEMES",
    "check_scheme",
    "clamp_bins",
    "bin_edges",
    "bin_index",
    "payload",
]

#: 默认格数：60 × 40。横向宽一点符合组件的形状，总格数 2400 在浏览器里画得动。
DEFAULT_BINS: tuple[int, int] = (60, 40)
#: 夹取范围：再少就不是形状，再多就是一片一像素的毛刺。
MIN_BINS = 8
MAX_BINS = 200
#: 两种着色：样本数 / 第三通道的均值。
SCHEMES = ("count", "z-mean")


def check_scheme(scheme: str, has_z: bool) -> None:
    """着色方式与"有没有色通道"配不配——**这一句话只有一处实现**。

    界面、服务层、引擎三处都要判这件事：界面是为了**别发一个算不出来的请求**（拦下来
    并提示），服务层与引擎是为了**报 400 而不是静默降级**（静默降级会让颜色的含义随
    窗口变）。判据与话术放在这里，三处都调它。
    """
    if scheme not in SCHEMES:
        raise ValueError(
            f"着色只认 {' / '.join(SCHEMES)}，收到的是 {scheme!r}。"
            "要按别的量着色，先把它做成一条数学通道再来选。"
        )
    if scheme == "z-mean" and not has_z:
        raise ValueError(
            "按第三通道着色（z-mean）得有第三条通道；只分箱着色请选「样本数」。"
        )


def clamp_bins(count, fallback: int = DEFAULT_BINS[0]) -> tuple[int, str | None]:
    """格数取整并夹进 8–200，夹过了带一句话出来。"""
    try:
        value = int(round(float(count)))
    except (TypeError, ValueError):
        return fallback, f"格数看不懂（{count!r}），按默认的 {fallback} 算。"
    if value < MIN_BINS:
        return MIN_BINS, f"格数最少 {MIN_BINS}，已按 {MIN_BINS} 算。"
    if value > MAX_BINS:
        return MAX_BINS, f"格数最多 {MAX_BINS}，已按 {MAX_BINS} 算。"
    return value, None


def bin_edges(values, count: int, lo: float | None = None,
              hi: float | None = None) -> np.ndarray:
    """等宽分箱的边界（``count + 1`` 个数）——与 numpy 的算法同一份。

    非有限数（NaN / inf）不参与定范围；整段没有有效值、或者上界不比起界大时，
    照 :func:`numpy.histogram2d` 的规矩撑开 ±0.5（否则轴宽是 0，一格里什么也放不下）。
    """
    data = np.asarray(values, dtype=np.float64).reshape(-1)
    finite = data[np.isfinite(data)]
    first = float(finite.min()) if finite.size else 0.0
    last = float(finite.max()) if finite.size else 1.0
    if lo is not None:
        first = float(lo)
    if hi is not None:
        last = float(hi)
    if not np.isfinite(first) or not np.isfinite(last) or last <= first:
        first, last = first - 0.5, last + 0.5
        if last <= first:                     # 两个都还不是有限数（全 NaN 又没给范围）
            first, last = 0.0, 1.0
    # **必须**用 `np.linspace`：它算的是 `step * i + start` 并把最后一格钉成 `last`，
    # 而"自己写 (last-first)*i/count"在浮点上会差一个末位——差一个末位就让边界上的
    # 样本落到隔壁格，"逐箱与 numpy.histogram2d 相等"这条验收立刻就红（实测踩过）。
    return np.linspace(first, last, int(count) + 1, dtype=np.float64)


def bin_index(values, edges: np.ndarray) -> np.ndarray:
    """每个值落在第几格（``0 … len(edges) - 2``）。

    **上界算进最后一格，范围外的（比上界还大的）给 ``-1``**——与 :func:`numpy.histogram2d`
    同一个约定（它也是"范围外的丢掉"，只是它不说；我们靠 ``-1`` 把它们数出来）。
    把范围外的塞进边缘格会让最边上那一格凭空变热——那是假数据，比少几个点糟得多。
    """
    count = len(edges) - 1
    data = np.asarray(values, dtype=np.float64)
    idx = np.searchsorted(edges, data, side="right") - 1
    idx[data > edges[-1]] = -1                # 比上界大的丢掉
    idx[idx == count] = count - 1             # 正好等于上界的进最后一格
    idx[~np.isfinite(data)] = -1              # NaN / inf 一律不算
    return idx


def payload(
    x_values,
    y_values,
    z_values=None,
    bins: tuple[int, int] | None = None,
    scheme: str = "count",
    x_range: tuple[float, float] | None = None,
    y_range: tuple[float, float] | None = None,
) -> dict:
    """两通道分箱 + （可选）第三通道着色，返回一张可以直接画的表。

    ``counts[ix][iy]`` 是格数矩阵；``z_mean`` / ``z_min`` / ``z_max`` 同形状，
    格里没有样本时是 ``None``（JSON 里给 ``null``，前端画成底色）。
    """
    check_scheme(scheme, z_values is not None)

    nbx, notice_x = clamp_bins((bins or DEFAULT_BINS)[0])
    nby, notice_y = clamp_bins((bins or DEFAULT_BINS)[1], DEFAULT_BINS[1])
    xs = np.asarray(x_values, dtype=np.float64).reshape(-1)
    ys = np.asarray(y_values, dtype=np.float64).reshape(-1)
    size = min(xs.size, ys.size)
    xs, ys = xs[:size], ys[:size]
    zs = None
    if z_values is not None:
        zs = np.asarray(z_values, dtype=np.float64).reshape(-1)[:size]

    keep = np.isfinite(xs) & np.isfinite(ys)
    if zs is not None:
        keep &= np.isfinite(zs)          # z 不是数的那一对不算（否则均值会被污染）
    excluded = int(size - keep.sum())

    x_edges = bin_edges(xs[keep], nbx, *(x_range or (None, None)))
    y_edges = bin_edges(ys[keep], nby, *(y_range or (None, None)))
    flat: np.ndarray = np.zeros(1, dtype=np.int64)
    counts = np.zeros((nbx, nby), dtype=np.int64)
    picked = np.zeros(0, dtype=np.float64)
    if keep.any():
        ix = bin_index(xs[keep], x_edges)
        iy = bin_index(ys[keep], y_edges)
        inside = (ix >= 0) & (iy >= 0)
        # 落在给定范围外的点照 numpy 的规矩丢掉（不塞进边缘格），但要**数出来**：
        # 静默少样本与静默多一格假数据一样会被读错。
        excluded += int((~inside).sum())
        ix, iy = ix[inside], iy[inside]
        flat = ix * nby + iy
        counts = np.bincount(flat, minlength=nbx * nby).reshape(nbx, nby)
        if zs is not None:
            picked = zs[keep][inside]

    z_mean = z_min = z_max = None
    if zs is not None and picked.size:
        sums = np.zeros(nbx * nby)
        np.add.at(sums, flat, picked)          # 求和 / 求极值都用"无样本给哨兵值"的写法，
        lo_flat = np.full(nbx * nby, np.inf)   # 最后再按"这一格有没有样本"一次性换成 NaN
        hi_flat = np.full(nbx * nby, -np.inf)
        np.minimum.at(lo_flat, flat, picked)
        np.maximum.at(hi_flat, flat, picked)
        present = counts.reshape(-1) > 0
        z_mean = np.where(present, sums / np.maximum(counts.reshape(-1), 1),
                          np.nan).reshape(nbx, nby)
        z_min = np.where(present, lo_flat, np.nan).reshape(nbx, nby)
        z_max = np.where(present, hi_flat, np.nan).reshape(nbx, nby)

    def triple(matrix):
        if matrix is None:
            return None
        return [[None if not np.isfinite(v) else float(v) for v in row] for row in matrix]

    total = int(counts.sum())
    notices = [n for n in (notice_x, notice_y) if n]
    if not total:
        notices.append("这个区间里没有有效样本——X / Y / 色 三条通道是不是选错了，"
                       "或者时间轴的起止反了？")
    elif excluded:
        notices.append(f"有 {excluded} 对样本因为 X / Y" + ("/ 色" if zs is not None else "")
                       + " 不是有限数（NaN）没进格子")
    # 常量列：轴的范围被撑开了（见 bin_edges），这不是数据里有异常，但用户得知道
    # "这一屏这一轴没有变化"——不然他会盯着一条竖线找原因。
    if total and xs[keep].size and float(xs[keep].min()) == float(xs[keep].max()):
        notices.append("X 通道这一段一直是一个值（区间已撑开，只为能画出来）")
    if total and ys[keep].size and float(ys[keep].min()) == float(ys[keep].max()):
        notices.append("Y 通道这一段一直是一个值（区间已撑开，只为能画出来）")

    z_flat = None
    if z_mean is not None:
        finite_z = z_mean[np.isfinite(z_mean)]
        z_flat = None if not finite_z.size else [float(finite_z.min()), float(finite_z.max())]
    return {
        "scheme": scheme,
        "bins": [nbx, nby],
        "x_range": [float(x_edges[0]), float(x_edges[-1])],
        "y_range": [float(y_edges[0]), float(y_edges[-1])],
        "count": total,
        "excluded": excluded,
        "counts": counts.astype(int).tolist(),
        "z_mean": triple(z_mean),
        "z_min": triple(z_min),
        "z_max": triple(z_max),
        # 色标用哪一段：按样本数着色就看最大格里的点数；按 z 着色就看 z 的均值范围。
        "count_range": [0, int(counts.max()) if total else 0],
        "z_range": z_flat,
        "notice": "；".join(notices) if notices else None,
    }
