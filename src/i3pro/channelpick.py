"""挑通道：哪几类通道名是"打开就先看"的、叠加对比默认带哪几条、轨迹按谁上色。

这三张表原先住在 :mod:`i3pro.render` 里——那是**载荷层**（L3）。而"这份表的列名该不该
当通道看"是**读取层**（L1）的问题：``csvlog.canonical_names()`` 要拿这三张表去认列名，
于是读取层反向 import 了载荷层，成了整个包里唯一一处 L1 → L3 的依赖（2026-10-15
架构评审量出来的）。表本身与"怎么搭页面"无关，所以搬到这里：两边都只 import 它。

**这里只放"名字表"**，不放规则：

* "这条通道名指本场次哪条通道"——:mod:`i3pro.channelref`（写法差异那条规则）；
* "这份 `speed / brake / steering` 算不算可用速度"——:data:`i3pro.derive.SPEED_CANDIDATES`
  （**那是另一个问题**：它按"能不能积分出距离轴"排序，顺序有用，别把这两张表合并）；
* 工作表的"挑通道"规则（patterns / limit / index / special）在
  :mod:`i3pro.worksheets` 与前端 :data:`COMPONENT_TYPES` 里。
"""

from __future__ import annotations

__all__ = [
    "ATTITUDE_NAME_HINTS",
    "ATTITUDE_PITCH",
    "ATTITUDE_ROLL",
    "ATTITUDE_YAW",
    "DEFAULT_CHANNEL_PRIORITY",
    "OVERLAY_PRIORITY",
    "SPEED_FOR_COLORING",
]

#: Channels a driver/vehicle-dynamics engineer opens first, best first.
DEFAULT_CHANNEL_PRIORITY = (
    "Vx KF",
    "Ground Speed",
    "GPS Speed",
    "SpeedFL",
    "SpeedFR",
    "SpeedRL",
    "SpeedRR",
    "Brake Signal",
    "TH",
    "Throttle",
    "Brake Pressure",
    "G Force Lat",
    "G Force Long",
    "G Force Vert",
    "Steering Angle",
    "SW Angle",
    "SteerAngle",
    "AMKFR ActualTorqueValue",
    "Battery Power",
    "MCU1 FR TempMotor",
    "GPS Heading",
    "AngleSlip",
)

#: Channels the distance-axis overlay compares by default.
OVERLAY_PRIORITY = (
    "Vx KF",
    "Ground Speed",
    "SpeedFR",
    "Brake Signal",
    "TH",
    "G Force Long",
    "AMKFR ActualTorqueValue",
)

#: 轨迹图按哪条速度上色（第一条本场次有的就用它）。
SPEED_FOR_COLORING = ("Vx KF", "Ground Speed", "GPS Speed", "SpeedFR", "SpeedFL")

#: 姿态图的三轴默认候选（实测名字，写法差异由 :mod:`i3pro.channelref` 判）。
#: 金标准里实测存在 ``Aceinna Roll`` / ``Aceinna Pitch`` / ``AngleLocalYaw``；
#: ``Roll (2)`` / ``Pitch (2)`` 是 MTi 那一路解出来的同名量（没有单位）。
#: 兜底（"名字里带 roll/pitch/yaw"）是**规则**不是名字表，所以住在 :mod:`i3pro.attitude`。
ATTITUDE_ROLL = ("Aceinna Roll", "Roll (2)")
ATTITUDE_PITCH = ("Aceinna Pitch", "Pitch (2)")
ATTITUDE_YAW = ("AngleLocalYaw",)

#: 姿态图的**兜底**词表：实测名字一条都对不上时，拿这些词去名字里找（忽略空格 / 下划线 /
#: 大小写）。放在这里而不是 :mod:`i3pro.attitude`，是因为"哪几类名字算姿态通道"与上面三张表
#: 是同一个问题；**怎么匹配**（顺序、排重）那条规则仍在 :func:`i3pro.attitude.pick_channels`。
ATTITUDE_NAME_HINTS = {
    "roll": ("roll",),
    "pitch": ("pitch",),
    "yaw": ("yaw", "heading"),
}
