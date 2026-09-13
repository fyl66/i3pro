"""频谱：一条通道在一个区间里的频率成分（ticket #10）。

几件必须同时说清的事，否则算出来的"频谱"没法信：

* **按通道自己的采样率算，不按主时间基。** 慢通道（10 Hz 的悬架、1 Hz 的温度）在主
  时间基上是被"保持"拉长的，它的频谱里会出现一堆**保持台阶造出来的假高频**。所以这里
  只吃通道原生采样序列，奈奎斯特频率 = 该通道采样率 / 2，和 i2 Pro 的说明一致。
* **用的是 Welch 平均，不是一次 FFT。** 数据比 FFT 点数长时切成多段（可重叠）各算一次
  再平均——这正是 i2 Pro 里 ``FFT Points`` 的语义，也是 MATLAB ``pwelch`` 的做法：
  频率分辨率 = 采样率 / FFT 点数，段数越多谱越平滑（方差越小）。
* **窗函数是必须选的，不是装饰。** 不做窗等于矩形窗，非整格频率上的正弦会漏成一圈
  旁瓣（泄漏）。默认 Hann。
* **短数据补零要说出来。** 数据比 FFT 点数短时补零能让曲线画出来，但那条曲线的
  频率分辨率是**名义上的**——不说明白，用户会以为真能分辨到那么细。

本模块是纯函数，不碰 :class:`~i3pro.ld.LogFile`——取数在
:func:`i3pro.render.spectrum` 里。
"""

from __future__ import annotations

import numpy as np

#: 默认 FFT 点数。1024 点在 100 Hz 上给出 0.098 Hz 一格，座舱/悬架都够看。
DEFAULT_POINTS = 1024
#: 夹取范围：16 点以下分辨率没意义，32768 点以上单通道就要跑好几秒。
MIN_POINTS = 16
MAX_POINTS = 32768
#: 窗函数。名字与 MATLAB / i2 Pro 一致，Hann 是默认（旁瓣低、主瓣不太宽）。
WINDOWS = ("hann", "hamming", "blackman", "rectangular", "flattop")
DEFAULT_WINDOW = "hann"
#: 段间重叠比例。0.5 是 Welch 的经典取值（段数翻倍、方差减半、代价是多算一次）。
DEFAULT_OVERLAP = 0.5
#: 纵轴两种：功率谱密度（单位²/Hz）与有效值（RMS，单位与通道相同）。
SCALES = ("psd", "amplitude")
DEFAULT_SCALE = "psd"


def clamp_points(points) -> tuple[int, str | None]:
    """FFT 点数取整、**就近取 2 的幂**、夹进 16–32768。

    radix-2 的 FFT 最快，i2 Pro 的默认点数也是 2 的幂；用户填 1000 时给 1024 并
    说明一句，比"偷偷按 1000 算"好——不然频率分辨率对不上他心里的数。
    """
    try:
        value = int(round(float(points)))
    except (TypeError, ValueError):
        return DEFAULT_POINTS, f"FFT 点数看不懂（{points!r}），按默认的 {DEFAULT_POINTS} 算。"
    if value < MIN_POINTS:
        return MIN_POINTS, f"FFT 点数最少 {MIN_POINTS}，已按 {MIN_POINTS} 算。"
    if value > MAX_POINTS:
        return MAX_POINTS, f"FFT 点数最多 {MAX_POINTS}，已按 {MAX_POINTS} 算。"
    snapped = 1 << max(0, value - 1).bit_length()
    if snapped != value:
        return snapped, f"FFT 点数已就近取 2 的幂：{value} → {snapped}。"
    return value, None


def window_values(kind: str, size: int) -> np.ndarray:
    """窗函数取值。未知名字**报错而不是悄悄用矩形窗**——那会把泄漏留在谱里。"""
    name = str(kind or DEFAULT_WINDOW).strip().lower()
    if name not in WINDOWS:
        raise ValueError(
            f"窗函数只认 {' / '.join(WINDOWS)}，收到的是 {kind!r}。"
            "不确定就用 hann（默认，旁瓣低）。"
        )
    size = int(size)
    if size <= 0:
        raise ValueError("窗函数的长度要是正数——先确认这一段里有数据。")
    if name == "rectangular":
        return np.ones(size, dtype=np.float64)
    if name == "flattop":
        # 与 MATLAB flattopwin 同一个系数（幅值精度优先，主瓣宽）。
        n = np.arange(size, dtype=np.float64)
        return (0.21557895
                - 0.41663158 * np.cos(2 * np.pi * n / (size - 1))
                + 0.277263158 * np.cos(4 * np.pi * n / (size - 1))
                - 0.083578947 * np.cos(6 * np.pi * n / (size - 1))
                + 0.006947368 * np.cos(8 * np.pi * n / (size - 1)))
    if name == "blackman":
        n = np.arange(size, dtype=np.float64)
        return (0.42 - 0.5 * np.cos(2 * np.pi * n / (size - 1))
                + 0.08 * np.cos(4 * np.pi * n / (size - 1)))
    # numpy 的 hann/hamming 是**对称**窗（与 MATLAB 一致，不是 "periodic" 那一版）。
    return np.hanning(size) if name == "hann" else np.hamming(size)


def fill_gaps(values) -> tuple[np.ndarray, int]:
    """NaN / 无穷大**按时间线性插值补上**，并报出补了几个。"""
    data = np.asarray(values, dtype=np.float64)
    good = np.isfinite(data)
    missing = int((~good).sum())
    if not missing:
        return data, 0
    if data.size and not good.any():
        raise ValueError(
            "这一段里全是无效样本（NaN），算不出频谱。先换个窗口，"
            "或者在左侧通道表里确认这条通道这一段真在记录。"
        )
    index = np.arange(data.size, dtype=np.float64)
    return np.interp(index, index[good], data[good]), missing


def _segments(size: int, points: int, overlap: float) -> list[int]:
    """Welch 分段起点。重叠比例夹在 0–0.95，步长至少 1 个采样。"""
    overlap = max(0.0, min(0.95, float(overlap)))
    step = max(1, int(round(points * (1.0 - overlap))))
    if size <= points:
        return [0]
    starts = list(range(0, size - points + 1, step))
    if starts[-1] + points < size:
        # 尾巴上不足一段：再补一段（和 MATLAB 一样，宁可多算一段也不丢尾部）。
        starts.append(size - points)
    return starts


def _smooth(values: np.ndarray, width: int) -> np.ndarray:
    """频域滑动平均：**保持总功率不变**，只把谱抹平。"""
    width = int(width)
    if width <= 1 or values.size < 3:
        return values
    width = min(width, values.size)
    if width % 2 == 0:
        width += 1 if width < values.size else -1
    half = width // 2
    kernel = np.ones(width, dtype=np.float64) / width
    padded = np.concatenate([np.full(half, values[0]), values,
                             np.full(half, values[-1])])
    out = np.convolve(padded, kernel, mode="same")[half:half + values.size]
    total = float(values.sum())
    scaled = float(out.sum())
    # 抹平会改掉边缘的和；整段能量应该守恒，不然"平滑"会看着像"变强/变弱"。
    return out * (total / scaled) if scaled else out


def welch(
    values,
    fs: float,
    points=DEFAULT_POINTS,
    window: str = DEFAULT_WINDOW,
    overlap: float = DEFAULT_OVERLAP,
    smooth: int = 1,
    scale: str = DEFAULT_SCALE,
    detrend: bool = True,
) -> dict:
    """Welch 平均周期图。

    缩放与 MATLAB ``pwelch(x, points, 0, points, fs)`` 同一口径：单边功率谱密度
    满足 ``Σ P·Δf ≈ 总功率``（去均值后就是方差）。``scale="amplitude"`` 时给
    ``√(P·Δf)``——每格的有效值（RMS），单位与通道相同，且**每格的平方和就是
    整段的有效值平方**（``√(Σ A²) = RMS``）。别写成 ``√(2P·Δf)``：单边谱的
    倍乘已经在功率上做过了，再乘一次等于把幅值放大 √2 倍。
    """
    if scale not in SCALES:
        raise ValueError(
            f"纵轴只认 {' / '.join(SCALES)}，收到的是 {scale!r}。"
            "要能量的量纲（单位²/Hz）用 psd，要和通道同单位（有效值）用 amplitude。"
        )
    fs = float(fs)
    if not np.isfinite(fs) or fs <= 0:
        raise ValueError(
            f"采样率得是正数，收到的是 {fs!r}——这条通道的采样率可能坏了，"
            "先在左边通道表里看它的单位与采样率。"
        )
    nfft, notice = clamp_points(points)
    kind = str(window or DEFAULT_WINDOW).strip().lower()
    weights = window_values(kind, nfft)
    data, filled = fill_gaps(values)
    size = int(data.size)
    padded = size < nfft
    if padded:
        data = np.concatenate([data, np.zeros(nfft - size, dtype=np.float64)])
        extra = (f"这一段只有 {size} 个样本，不足 {nfft} 点，已补零到 {nfft}——"
                 f"频率分辨率名义上是 {fs / nfft:.4g} Hz，实际分辨能力受数据长度限制。")
        notice = f"{notice} {extra}" if notice else extra
    starts = _segments(data.size, nfft, overlap)
    window_power = float(np.sum(weights**2))
    total = np.zeros(nfft // 2 + 1, dtype=np.float64)
    for start in starts:
        chunk = np.array(data[start:start + nfft], dtype=np.float64)
        if detrend:
            chunk = chunk - chunk.mean()
        spectrum = np.fft.rfft(chunk * weights)
        power = (np.abs(spectrum) ** 2) / (fs * window_power)
        if power.size > 2:
            power[1:-1] *= 2.0        # 单边谱：除 DC 与奈奎斯特外都要乘 2
        total += power
    total /= max(1, len(starts))
    frequencies = np.fft.rfftfreq(nfft, d=1.0 / fs)
    df = fs / nfft
    if scale == "amplitude":
        total = np.sqrt(total * df)
    if int(smooth) > 1:
        total = _smooth(total, int(smooth))
    peak = int(np.argmax(total)) if total.size else 0
    return {
        "frequencies": frequencies,
        "power": total,
        "points": nfft,
        "window": kind,
        "overlap": max(0.0, min(0.95, float(overlap))),
        "smooth": int(smooth),
        "scale": scale,
        "sample_rate": fs,
        "nyquist": fs / 2.0,
        "resolution": df,
        "segments": len(starts),
        "samples": size,
        "filled": filled,
        "padded": padded,
        "peak_index": peak,
        "peak_frequency": float(frequencies[peak]) if total.size else None,
        "peak_value": float(total[peak]) if total.size else None,
        "notice": notice,
    }
