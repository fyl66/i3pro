"""波形配色：三套调色板 + 一个"缺失"语义灰（ticket #35）。

为什么这件事值得一个模块、而不是前端里的一个数组：**"八条线同屏不糊"是可以量的**。
深色底上叠八到十几条曲线时，靠"看着还行"选出来的颜色经常在某个屏幕/某个色觉
类型上糊成一团，而且改坏了没人会发现。所以配色在这里定，判据也在这里算：

* :func:`delta_e` —— CIE76 色差（sRGB → 线性 → XYZ → Lab），两色"看着差多少"；
* :func:`contrast_ratio` —— WCAG 相对亮度对比度，管的是"在深色底上看得见吗"；
* :func:`min_delta_e` —— 一套调色板里**最像的两条**差多少，这就是可辨性的下界。

:data:`MISSING` 是**语义色**，不属于任何一套调色板：它表示"这里本该有一条线、
但本场次没有"，和"这是第 7 条通道"是两件事。所以它必须与所有调色板颜色都拉得开
（:func:`missing_is_distinct`），否则用户会把灰线当成一条真通道。

前端拿到的就是这里的数据（页面载荷 ``palettes``），不另存一份——同一套颜色
在两个地方各写一遍，改一处忘一处就是"导出的截图和屏幕上的不一样"。
"""

from __future__ import annotations

__all__ = [
    "BACKGROUND",
    "MISSING",
    "PALETTES",
    "colors",
    "contrast_ratio",
    "delta_e",
    "labels",
    "min_delta_e",
    "missing_is_distinct",
]

#: 图里的底色（``--panel``）。对比度都按它算——深色底才是这个项目的主场。
BACKGROUND = "#171a21"

#: 「本场次没有」那条通道的灰（ticket #34 的语义色，不是某套调色板的一员）。
MISSING = "#4a5160"

#: 三套调色板。``default`` 是原来的那一套（老链接、老工作表看起来不变），
#: ``colorblind`` 走 Okabe–Ito 的八色（红绿色觉异常下也能分开），``contrast``
#: 是给投影/强光环境准备的高饱和高亮版本。
PALETTES: dict[str, dict] = {
    "default": {
        "label": "默认",
        "doc": "原来的十六色；同一套表换成别的调色板时这条是退路。",
        "colors": [
            "#4cc2ff", "#ffb454", "#35d07f", "#ff5d6c", "#b48cff", "#4ce0d2",
            "#ff8ad1", "#c9d24b", "#7aa7ff", "#ff9e6b", "#6bdcff", "#a0e57c",
            "#ff6bd6", "#8bd45a", "#5f7dff", "#ffc857",
        ],
    },
    "colorblind": {
        "label": "色盲友好",
        "doc": "Okabe–Ito 八色（原色偏暗，这里按深色底提亮，色相关系不动）。",
        "colors": [
            "#56b4e9", "#e69f00", "#009e73", "#f0e442",
            "#0072b2", "#d55e00", "#cc79a7", "#ffffff",
        ],
    },
    "contrast": {
        "label": "高对比",
        "doc": "投影 / 强光下用；亮度和色相都拉开，代价是看着比较吵。",
        "colors": [
            "#00e5ff", "#ffd400", "#00ff85", "#ff3b6b",
            "#c08cff", "#00ffd5", "#ff8ee0", "#e8ff4d",
            "#7fb6ff", "#ff8800", "#39f2ff", "#b6ff00",
        ],
    },
}


def labels() -> dict[str, str]:
    """``{身份: 按钮上的字}``——界面只认这一份，加一套颜色不用改前端。"""
    return {key: value["label"] for key, value in PALETTES.items()}


def colors(name: str | None) -> list[str]:
    """一套调色板的颜色；名字不认识就当 ``default``（老工作表没有这个字段）。"""
    entry = PALETTES.get(str(name or "")) or PALETTES["default"]
    return list(entry["colors"])


def _srgb_to_linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _rgb(text: str) -> tuple[float, float, float]:
    """``#rrggbb`` → 0–1 的三个分量；短写 ``#abc`` 也认。"""
    body = str(text).strip().lstrip("#")
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    if len(body) != 6:
        raise ValueError(f"{text!r} 不是 #rrggbb 形式的颜色。")
    try:
        return tuple(int(body[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        raise ValueError(f"{text!r} 不是 #rrggbb 形式的颜色。") from None


def _lab(text: str) -> tuple[float, float, float]:
    r, g, b = (_srgb_to_linear(c) for c in _rgb(text))
    # sRGB → XYZ（D65），再 XYZ → Lab。常数用标准的，别自己约。
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = (0.2126729 * r + 0.7151522 * g + 0.0721750 * b) / 1.00000
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (841 / 108) * t + 4 / 29

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(one: str, other: str) -> float:
    """CIE76 色差：0 = 一模一样，一般 > 2.3 才"人眼看得出来"，> 20 一眼就分得开。"""
    a, b = _lab(one), _lab(other)
    return float(sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5)


def _luminance(text: str) -> float:
    r, g, b = (_srgb_to_linear(c) for c in _rgb(text))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(one: str, other: str) -> float:
    """WCAG 对比度（1–21）。深色底上画线，3:1 是"看得见"的下限。"""
    a, b = _luminance(one), _luminance(other)
    lo, hi = min(a, b), max(a, b)
    return float((hi + 0.05) / (lo + 0.05))


def min_delta_e(palette: str | None) -> float:
    """一套调色板里**最像的两条**差多少（可辨性的下界）。"""
    swatches = colors(palette)
    worst = float("inf")
    for i in range(len(swatches)):
        for j in range(i + 1, len(swatches)):
            worst = min(worst, delta_e(swatches[i], swatches[j]))
    return worst


def missing_is_distinct(threshold: float = 12.0) -> bool:
    """"缺失"灰与**所有**调色板颜色都拉得开吗？"""
    for entry in PALETTES.values():
        for swatch in entry["colors"]:
            if delta_e(MISSING, swatch) < threshold:
                return False
    return True
