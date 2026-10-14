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
