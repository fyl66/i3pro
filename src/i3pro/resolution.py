"""绘图分辨率五档。

Marple 的 ``PLOT_RESOLUTION`` 实测是 ``500 × 2^档位``；这里把档位键、点数与中文名
作为后端单源。前端没有构建步骤，所以 viewer.html 里有一份同样的 JS 副本，由单测
和 smoke 断言两份不会漂开。
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_RESOLUTION",
    "PLOT_RESOLUTIONS",
    "normalise",
    "points",
]

#: 顺序就是界面里从上到下的顺序。
PLOT_RESOLUTIONS = (
    ("min", 500, "最少"),
    ("low", 1000, "少"),
    ("medium", 2000, "中"),
    ("high", 4000, "多"),
    ("max", 8000, "最多"),
)

DEFAULT_RESOLUTION = "medium"


def normalise(key: object) -> str:
    """非法 / 缺省档位回落到 ``medium``。"""
    text = str(key or "").strip().lower()
    return text if any(text == row[0] for row in PLOT_RESOLUTIONS) else DEFAULT_RESOLUTION


def points(key: object) -> int:
    """档位 -> 点数；非法 / 缺省同样回落到默认点数。"""
    want = normalise(key)
    for row_key, count, _label in PLOT_RESOLUTIONS:
        if row_key == want:
            return count
    raise AssertionError("normalise returned an unknown resolution")
