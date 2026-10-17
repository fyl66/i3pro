"""姿态：三个角度 → 可画的姿态（对齐 Marple 的 Attitude / PFD，ticket #42）。

Marple 的姿态图有两个视图：一个立体模型（``ATTITUDE``）和一块"开飞机"的仪表
（``PFD``，"Primary Flight Display"）。它俩吃的是同一组东西——**roll / pitch / yaw
三条通道**，各带一个倍数，外加角度单位。这个模块只做那条公共的算术，画布在 #43。

约定**写死**，不做成配置：

* 世界系 **x 前 / y 左 / z 上**（车身系同向，原点在车中心）；
* **正 roll 抬左、正 pitch 抬头、正 yaw 左转**——注意世界系 y 朝左，所以"抬头"是绕
  ``+y`` 的**负向**旋转，矩阵里写成 ``-sin`` 是故意的，不是符号写错；
* 组合顺序 ``R = Rz(yaw) · Ry(pitch) · Rx(roll)``（**ZYX / 3-2-1**，航空与车辆的通用约定）；
* 屏幕系 **y 向下**（canvas 约定），所以"上"投影出负 y。

Marple 把顺序做成了六选一的 ``eulerOrder``（``FigureAttitude-*.js`` 里有一张名字映射表）。
**我们不抄那个选择**，固定 3-2-1：真实数据方向不对时用每通道倍数掰正（那正是它自己留的
``multipliers``），比多一个永远没人改的设置好。**它的默认顺序我们没能实证**（那一段不在
仓库里，只有 minify 过的映射表），所以这里不声称"与 Marple 一致"——只说我们自己用哪个。

判据出处：``docs/marple-parity.md`` 第 6/7 行、``docs/spec-marple-parity.md`` §3。
"""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np

from . import channelpick, channelref

__all__ = [
    "normalize_angle",
    "pick_channels",
    "pfd_scales",
    "project",
    "rotation_matrix",
]

_TABLES = {
    "roll": channelpick.ATTITUDE_ROLL,
    "pitch": channelpick.ATTITUDE_PITCH,
    "yaw": channelpick.ATTITUDE_YAW,
}

#: PFD 刻度的格宽（度）。俯仰 −90…90、滚转 −180…170、航向 0…350。
_STEP = 10
_SPAN = 360 // _STEP


def _about_x(angle: float) -> np.ndarray:
    """绕 x 转 ``angle``。三个轴的矩阵**只在这一处**写，免得同一套正负号散成两处。"""
    cos, sin = math.cos(angle), math.sin(angle)
    return np.array([[1.0, 0.0, 0.0], [0.0, cos, -sin], [0.0, sin, cos]])


def _about_y(angle: float) -> np.ndarray:
    """绕 y 转 ``angle``。"""
    cos, sin = math.cos(angle), math.sin(angle)
    return np.array([[cos, 0.0, -sin], [0.0, 1.0, 0.0], [sin, 0.0, cos]])


def _about_z(angle: float) -> np.ndarray:
    """绕 z 转 ``angle``。"""
    cos, sin = math.cos(angle), math.sin(angle)
    return np.array([[cos, -sin, 0.0], [sin, cos, 0.0], [0.0, 0.0, 1.0]])


def normalize_angle(value: float, unit: str = "deg", gain: float = 1.0) -> float:
    """一个角度读数 → 弧度，落在 ``[-π, π)``。

    ``unit`` 只认 ``"deg"`` 与 ``"rad"``（配置文件里只有这两个选项）；``gain`` 是
    每通道倍数——方向反了的传感器就靠它取负，而不是改这里的约定。

    **非有限值（NaN 与 ±inf）原样返回**：画法要能认出"这一帧没有数据"并跳过，
    不能在这里悄悄给个 0，也不能让 ``inf`` 落到 ``math.fmod`` 上炸一个英文报错。
    """
    if unit not in ("deg", "rad"):
        raise ValueError(
            f"角度单位只认 'deg' 与 'rad'，收到 {unit!r}；把配置里的 unit 改成 'deg' 或 'rad'")
    number = float(value) * float(gain)
    if not math.isfinite(number):
        return number
    radians = math.radians(number) if unit == "deg" else number
    wrapped = math.fmod(radians, 2 * math.pi)
    if wrapped >= math.pi:
        wrapped -= 2 * math.pi
    elif wrapped < -math.pi:
        wrapped += 2 * math.pi
    return wrapped


def rotation_matrix(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """车身 → 世界的三阶旋转矩阵；三个角都是**弧度**（先过 :func:`normalize_angle`）。

    见模块开头那份约定：``Rz(yaw) · Ry(pitch) · Rx(roll)``，正 pitch 抬头。
    """
    return _about_z(yaw) @ _about_y(pitch) @ _about_x(roll)


def project(vertices, matrix, camera) -> np.ndarray:
    """顶点 → 屏幕坐标（正交投影，无第三方依赖）。

    ``vertices`` 是车身系里的点（``(N, 3)`` 或等长的序列），``matrix`` 是
    :func:`rotation_matrix` 给的姿态，``camera`` 是 ``{"yaw", "pitch", "scale"}``：
    前两个是**相机**的转角（**单位是度**，因为它们是鼠标拖出来的界面量），
    ``scale`` 是像素/单位。相机先偏航再俯仰，等同于把场景转回去。

    三个键**必须有**（缺了直接 ``KeyError``）：这里给默认值的话，一个拼错的名字会安静地
    按"相机 0 度"画出来，而画出来的姿态看不出错——那比当场炸掉贵得多。

    返回 ``(N, 2)`` 的屏幕坐标，**y 向下**（canvas 约定）。
    """
    points = np.asarray(vertices, dtype=float).reshape(-1, 3)
    model = np.asarray(matrix, dtype=float).reshape(3, 3)
    yaw = math.radians(float(camera["yaw"]))
    pitch = math.radians(float(camera["pitch"]))
    scale = float(camera["scale"])
    #: 相机左转 yaw ⇔ 场景绕 z 反向转（Rz(−yaw)）；相机低头 pitch ⇔ 场景绕 x 正转
    #: （Rx(+pitch)，俯仰 90° 正好是俯视）。两者符号不同是约定，由用例钉住。
    turn = _about_z(-yaw)
    tilt = _about_x(pitch)
    rotated = (tilt @ turn @ model @ points.T).T
    return np.column_stack([rotated[:, 0] * scale, -rotated[:, 2] * scale])


def pfd_scales() -> dict[str, tuple[int, ...]]:
    """PFD 的三根刻度（度）：俯仰 −90…90、滚转 −180…170、航向 0…350。

    闭环刻度只画一次：航向不取 360（那就是 0）、滚转不取 180（与 −180 是同一条线），
    否则两条刻度线会重叠在同一处。格宽是模块常量（10°）**不做参数**——规格没要它，
    而且 360 除不尽的格宽会让三根刻度长度不一致。
    """
    return {
        "pitch": tuple(range(-90, 90 + _STEP, _STEP)),
        "roll": tuple(-180 + _STEP * index for index in range(_SPAN)),
        "heading": tuple(_STEP * index for index in range(_SPAN)),
    }


def pick_channels(known: Iterable[str]) -> dict[str, str | None]:
    """这一场拿哪三条通道当 roll / pitch / yaw。

    三级：**实测名字**（:mod:`i3pro.channelpick` 的三张表，写法差异由
    :mod:`i3pro.channelref` 判）→ **名字里的词**（roll / pitch / yaw / heading）→ 留空。

    两条规矩：

    * **认不出就留空，不猜**——挑错一条姿态通道，画出来的车是歪的，而没人看得出来；
    * **同一条通道不派给两个轴**：兜底那一级是按词找的，一个名字里同时带 roll 与 pitch
      的话（``Body Roll Pitch``）会两头命中，所以按 roll → pitch → yaw 的顺序挑，
      已经派出去的就不再考虑。

    兜底那一级可能挑到"名字里有这个词"但根本不是姿态的通道（例如 ``YawAngAcc`` 是
    角加速度），所以实测名字永远优先，组件还要把挑中的名字显示出来。
    """
    names = {str(name) for name in (known or ()) if name}
    table = channelref.name_table(names)
    picked: dict[str, str | None] = {}
    for axis in ("roll", "pitch", "yaw"):
        hit = None
        for candidate in _TABLES[axis]:
            hit = channelref.lookup(names, candidate, table=table)
            if hit:
                break
        if hit is None:
            hit = _contains(names - set(picked.values()), channelpick.ATTITUDE_NAME_HINTS[axis])
        picked[axis] = hit
    return picked


def _contains(names: Iterable[str], hints: Iterable[str]) -> str | None:
    """名字（忽略空格 / 下划线 / 大小写）里出现某个词的第一条。

    按词序优先、词内按名字排序取第一条，所以同样的输入永远给同样的答案。
    """
    ordered = sorted(names)
    for hint in hints:
        wanted = channelref.normalise(hint)
        for name in ordered:
            if wanted and wanted in channelref.normalise(name):
                return name
    return None
