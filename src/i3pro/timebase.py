"""主时间基：一个场次的时间轴只有一处答案（ticket #22）。

词汇表（`CONTEXT.md`）点名过「主时间基」——数学通道是主时间基上的浮点列，画图、
散点、切圈、报表、Parquet 与测试里的合成场次都从同一条轴上取。在这一票之前，
"这条轴多长、步长多少"这句话在 `src/i3pro` 里被独立写了 **15 遍**（散在 8 个模块里；
票面按 `sample_rate` 这一种写法数出来是 14 处，`store.build_table` 里那处把变量
叫 `rate`，所以没被那条命令数到）。`laps._master_time` 与 `maths._master_axis`
是两份私有实现，`report.master_time` 更是连自己文件外都没有调用者。

现在只有这里回答这个问题，判据是可复现的：

    rg -n 'int\\(round\\(.*sample_rate.*\\)\\) \\+ 1' src     -> 1 行（就是下面那一行）
    rg -n 'int\\(round\\(.*sample_rate.*\\)\\) \\+ 1' tests   -> 0 行

`--rate`（`i3pro convert --rate`）想让 Parquet 换一个采样率时，换的是**同一个答案**
——`length(log, rate)` / `axis(log, rate)`，而不是另造一条轴。
"""

from __future__ import annotations

import numpy as np


def rate_of(source, rate: float | None = None) -> float:
    """这条轴用哪个采样率：默认是场次自己的，`rate` 给定时按给定的算。

    除零兜底与原来各处一致（`rate or 1.0`）：拿不到采样率时按 1 Hz 算，
    而不是让整条轴变成 NaN。
    """
    chosen = float(rate) if rate else float(getattr(source, "sample_rate", 0.0) or 0.0)
    return chosen or 1.0


def length(source, rate: float | None = None) -> int:
    """主时间基有多少个采样点（含 t=0 那一个，所以是 `+ 1`）。"""
    sample_rate = rate_of(source, rate)
    return int(round(float(source.duration) * sample_rate)) + 1


def axis(source, rate: float | None = None) -> np.ndarray:
    """主时间基本身（秒）：`0, 1/rate, …, duration`。"""
    sample_rate = rate_of(source, rate)
    return np.arange(length(source, rate), dtype=np.float64) / sample_rate
