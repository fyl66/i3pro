"""把一份 CAN 记录仪帧表对到它对应的 ``.ld`` 场次，并把两边的通道合成一场。

为什么需要这个模块：C125 记的东西和记录仪记的东西**从来不在同一份文件里**。
实测 ``10.5湾区``：``.ld`` 有 459-474 条通道（电机 / AMK / MTI / GPS），
就是没有 IVT 的 ``I_Data`` / ``U1_Data``；帧表解出来只有 114 条（``Vx_KF``、
``latitude_MTI``、``I_Data``...），没有电机通道。想知道"电机电流推母线电流"，
两边必须在同一个场次里。

这个模块回答两个问题，各自只回答一次：

* **对到哪一场**（``ld_window`` / ``recording_window`` / ``candidates``）——
  ``.ld`` 头里的 ``log_time`` 是**记录结束时刻**（不是开始），
  ``begin = log_time - duration``。帧表那边用文件名里的日期 + 首/末墙钟。
  墙钟只做到"挑出候选"，真正的偏移交给 :func:`align`。
* **偏移是多少**（``align``）——两边各有各的零点，而且**记录仪自己的钟不准**：
  实测 ``10.7湾区`` 同一个下午，它的 ``系统时间`` 与 C125 差了 19 s 到 311 s
  （``jhy绕桩2`` 的峰落在墙钟估计外 311 s 处）。所以偏移要在**相对时钟**上用
  两边都有的通道（``Vx KF`` / ``Vx_KF``）互相关求出来；墙钟只用来挑候选、
  给搜索一个中心，配不配对由速度与三条加速度轴的相关系数说话。

合并的时间轴**以 ``.ld`` 为准**（用户确认）：时长、时间零点、采样率都是
``.ld`` 的，CAN 通道按零阶保持重采样上去，覆盖不到的地方留空。

纯函数：这里不读盘、不写盘、不 import 服务层。``merge_columns`` 只把列算出来，
落盘由 :func:`write_merged` 在文件末尾管。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from . import channelref, timebase

__all__ = [
    "Alignment",
    "Candidate",
    "Pairing",
    "align",
    "candidates",
    "consistent_pairings",
    "expected_offset",
    "frame_groups",
    "hold_into",
    "hold_sample",
    "ld_window",
    "merge_columns",
    "merge_sources",
    "name_key",
    "overlap_seconds",
    "recording_window",
    "write_merged",
]

#: 配对用的共同通道：两边都有速度（``.ld`` 叫 ``Vx KF``，帧表解码叫 ``Vx_KF``，
#: 归一化之后是同一个键）。
SPEED_CANDIDATES = ("Vx KF", "Vx_KF", "VxKF")
#: 第二判据用的三条加速度轴（``.ld`` 的写法 -> 帧表解码的写法）。
#:
#: 为什么是三条轴、取中位数：速度在急加速/急减速之外几乎是平的，短窗口上很容易
#: 撞出假的高相关（实测：窗口里只有几段直线时，错偏移上也能给出 r=1.0000），
#: 所以拿加速度复核同一条偏移。但**只看一根轴会误杀真配对**——实测 10.7 的
#: ``jhy耐久``：速度 r=1.0000、``accZ`` 0.970、``accX`` 0.832，``accY`` 只有
#: 0.359（那一段记录仪的 ``accY_MTI`` 自己就是死的）。三条轴的中位数取的是
#: 0.832，一眼就是同一段驾驶。反过来，假配对的三条轴中位数仍在 0 附近
#: （实测 ``cjh直线`` 对上 094537 那一次：-0.058 / +0.121 / +0.106，中位数 0.106）。
ACCEL_AXES = (
    ("accX MTI", "accX_MTI"),
    ("accY MTI", "accY_MTI"),
    ("accZ MTI", "accZ_MTI"),
)
#: 复核用的相关系数门槛。加速度两边是同一个传感器（``.ld`` 走 CAN、帧表走
#: 原始帧），对齐之后实测真配对 >= 0.9；给 0.6 是给两边的量程/符号留余量。
MIN_ACCEL_R = 0.6
#: 同一场 ``.ld`` 上认下来的几次记录，隐含的 C125 钟差最多允许差这么多秒。
#:
#: 每次匹配都能反推出"这份 ``.ld`` 的绝对时间跟头里写的差了多少"（``expect - lag``）：
#: ``.ld`` 头里的绝对时间不可靠，而记录仪那边是准的，所以这个差值就是这份 ``.ld``
#: 自己的钟差。同一场里的几次记录推出来的值应当接近（实测真配对之间差 24–115 s）。
#:
#: 差出好几百秒的多半是**重复动作**撞出来的：``jhy耐久``（耐久，一圈一圈跑）除了
#: 自己那一次记录，还认下了 19 分钟前的另一次——两者推出来的钟差差 308 s，而那次
#: 记录在 ``.ld`` 真正的时间窗里**根本还没开始**。超过这个值就只留最贴墙钟的那一组，
#: 见 :func:`consistent_pairings`。
E_TOLERANCE_S = 150.0
#: 墙钟上"挨得着"的定义：``.ld`` 的窗口按 :data:`MAX_LAG_S` 放宽之后，
#: 与一次记录至少还要重叠这么多秒，才算候选。少于这个数就是明摆着两回事。
MIN_OVERLAP_S = 20.0
#: 允许的钟差上限：墙钟给出的偏移与互相关搜出来的偏移相差超过它就不认。
#: 这个差值来自两处——两边的"零点"取法不同（记录仪取首帧、``.ld`` 取第一个采样
#: 点），``.ld`` 头的时刻只精到秒，以及**记录仪自己的钟**。实测：``10.5湾区``
#: 最大 41 s（``102857``），``10.4湾区测试`` 最大 105 s（``133436``），
#: ``10.7湾区`` 最大 311 s（``jhy绕桩2``：墙钟说 +273.9 s，峰在 -37.0 s）。
#: 现在跟 :data:`SEARCH_S` 一样宽——搜索范围之内都算允许，真正的判据始终是
#: **两条通道的相关系数**；把 ``--max-lag`` 调小可以重新收紧这道门。
MAX_LAG_S = 900.0
#: 互相关在"墙钟估出来的偏移"附近搜多少秒。
#:
#: 墙钟那个估计只当**搜索中心**（以及平手时往哪边靠），不当结论。它的误差来自
#: ``.ld`` 头里的时刻只到秒、两边"零点"的取法不同，以及记录仪自己的钟不准——
#: 最后这一条是主要的。实测 ``10.7湾区`` 同一个下午，记录仪的 ``系统时间`` 与
#: C125 的差在 19 s 到 311 s 之间跳（``jhy绕桩2`` 的峰在墙钟估计外 311 s 处、
#: ``jhy高避`` 在 146 s 处；两场都被数据本身认了出来，`r=1.0000`）。所以这里
#: 不按"钟差只有几秒"来设，而是给足 900 s 的余量——代价只是多扫几千次互相关
#: （每次几百个点，很便宜），配不配对仍然由两条通道的相关系数说话。
SEARCH_S = 900.0
#: 大范围搜索的步长 [s]。900 s x 2 / 0.2 = 9000 次比较，每次几百个点，很便宜；
#: 峰顶再按 :data:`LAG_STEP_S` 精修一次。
SEARCH_STEP_S = 0.2
#: 偏移搜索的步长。100 Hz 的轴，0.01 s 已经比任何一次记录仪启动抖动都细。
LAG_STEP_S = 0.01
#: 相关系数低于这个值就不认。**0.99**（2026-10-13 从 0.9 提上来的）。
#:
#: 实测：真配对速度相关都在 0.9927 以上（最松的是 ``庞仙耐久两圈``），而放宽搜索、
#: 加跟随窗之后冒出来的假配对在 0.906–0.908（``cjh直线`` 对上 ``100816``、
#: ``cjh100限速直线`` 对上 ``100027``，两边的墙钟窗口压根没重叠）。0.99 正好卡在中间。
MIN_CORRELATION = 0.99
#: 用加速度把偏移钉准的范围 [s]。
#:
#: 速度那条曲线太平：`r` 在半个秒里几乎不变，Vx 自己定不到 0.1 s 以内（实测
#: ``庞仙耐久两圈`` 对 ``112831``：Vx 峰与加速度峰差 0.47 s，加速度复核因此从
#: 0.96 掉到 0.22）。加速度是尖的，所以在 Vx 认下来的那座峰附近 ±REFINE_S 里
#: 用它重新定一次偏移——**Vx 的判据在那条偏移上仍然要过**。
REFINE_S = 1.0

#: 加速度复核失败后的全局粗搜步长 [s]。Vx 会先挑"点更多的那座峰"（10.29
#: ``练车3``：错峰 r=1.000、真峰 r=0.9997），如果加速度只在错峰 ±1 s 里微调，
#: 就永远回不到真峰。所以复核失败时还要在墙钟允许的偏移范围里用 1 s 一格再
#: 搜一遍；精修仍由 :data:`REFINE_S` / ``step_s`` 负责。
ACCEL_SEARCH_STEP_S = 1.0
#: 相关差不到这个值的峰，看成"一样高"：这时候**谁的数据多谁算数**。
#:
#: 20 个点的窗口和 300 个点的窗口给出同一个 `r`，说明问题的只有后者。实测
#: ``cjh直线`` 对 ``100027`` 那一次：真偏移上只能凑出 197 个点（记录仪那段只到
#: `.ld` 的末尾），而一个错偏移能凑满 228 个点——先按 `r` 排、再按点数挑，
#: 才挑得回真的那个。
TIE_CORRELATION = 0.02
#: 互相关至少要落在这么多个点上才作数（比较窗是 1 Hz，所以约等于秒数）。
#:
#: 10 个点太少。搜索放宽到 ±900 s 之后，墙钟窗口夹不住的那一头会剩下几个到
#: 二十几个点，一段平滑的速度曲线在这上面随手就是 `r=1.000000`——实测这一条
#: 抢走过 4 个真峰（`80定速滑行`/`cjh直线`/`cjh高避油门线松了`/`YJW修改过流后跑车`）：
#:
#: | 场次 | 假峰 | 真峰 |
#: | --- | --- | --- |
#: | `80定速滑行` | +131.67 s `r=1.000000` n=23 | −649.93 s `r=0.999929` n=276 |
#: | `cjh直线` | +15.07 s `r=1.000000` n=6 | −223.93 s `r=0.999614` n=197 |
#: | `cjh高避油门线松了` | +495.87 s `r=1.000000` n=11 | +175.07 s `r=0.999703` n=300 |
#: | `YJW修改过流后跑车` | −605.93 s `r=1.000000` n=6 | −932.93 s `r=0.999072` n=276 |
#:
#: 真配对的实测最少 132 个点（`cjh高避TV1`），假峰最多 53 个（`cjh阶跃+脉冲`
#: 对 `175226` 那一次）。60 落在两者中间。
MIN_COMPARE_POINTS = 60
#: 互相关的比较窗口上限：最多比这么多秒的同一条时间线。窗口越长越稳，但要把
#: "整段反复跑同一个八字"那种周期性算进去——300 s 在实测数据上给的是同一段驾驶。
COMPARE_WINDOW_S = 300.0
#: 比较窗口里的采样率。速度信号在 1 Hz 上已经足够认人。
COMPARE_RATE = 1.0


def _normal(text: str) -> str:
    """去掉空格 / 下划线 / 大小写——"同一个名字的两种写法"只认这一条。"""
    return channelref.normalise(text)


def _same_name(left: str, right: str) -> bool:
    """两个通道名是不是"同一个名字的两种写法"（``accX MTI`` / ``accX_MTI``）。

    这里**不能**用 :func:`_normal` 那种"只留字母数字"的归并：``AngVelX_body`` 与
    ``AngVelX_hor`` 归并之后都是 ``angvelx``，两条真的不同的通道会被当成一条丢掉
    （实测 CAN 那侧就是这么少了十几条通道）。所以只在"两边都只差空格 / 下划线 /
    大小写"时才认为同名。
    """
    return name_key(left) == name_key(right)


def name_key(name: str) -> str:
    """"同一个名字的两种写法"的键：**只**抹掉空格 / 下划线、统一大小写。

    比 :func:`channelref.normalise`（抹掉所有非字母数字）**严**：那一条会把
    ``AngVelX_body`` 与 ``AngVelX_hor`` 并成一条，把两条真的不同的通道吃掉。
    合并（:func:`merge_sources`）与接续（``stitch.stack``）判"同一条通道"都用它，
    这样"同名"在这两条路上只有一个答案。
    """
    return name.replace("_", "").replace(" ", "").lower()


#: 判断"这一列是空的"的容差。C125 里没接上的模板通道不是**精确**的 0：实测
#: ``传杰八字`` 的 ``I Data`` 是 ``-0.0015 … 0``（记录仪自己的量化底噪），
#: 严格 `== 0` 会判成"有数据"，于是帧表里真的有数的 ``I_Data`` 被丢掉。
#: 反过来，IVT 的量程到 1500 A、分辨率 1 mA，小于 10 mA 的读数本来就等于没有。
ZERO_TOLERANCE = 0.01


def _empty(values: np.ndarray, *, inclusive: bool = False) -> bool:
    """这一列是不是"占位空列"：全是 NaN，或者全都是 0（含量化底噪）。

    C125 的工程里挂着 IVT 的模板通道（``I Data`` / ``U1 Data`` …：单位 A / V，
    名字对得上，但传感器没接上，整段都是 0）。合并时如果让这种空列把帧表里
    **真的有数**的 ``I_Data`` 顶掉，用户拿到的是一个全 0 的母线电流——正是他想
    要的那条通道反而没了。所以空列可以被对面替换掉。

    ``inclusive`` 只给"同名碰撞、对面真的有数"这一步用：实测
    ``FR/FL Aero Ride Height`` 的模板列恰好是 ``-0.01 … 0``，不含等号的判据会把
    ``-0.01`` 留成"有数据"，于是 ``0x66D`` 解出来的真实高度被当成重名丢掉。全局
    判断仍用不含等号的版本，避免把本来有值、恰好落在边界的整段通道提前清空。
    """
    if values.size == 0:
        return True
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return True
    if inclusive:
        return bool(np.all(np.abs(finite) <= ZERO_TOLERANCE))
    return bool(np.all(np.abs(finite) < ZERO_TOLERANCE))


# --------------------------------------------------------------------- 时间窗
def ld_window(log) -> tuple[datetime, datetime]:
    """``.ld`` 的绝对墙钟窗口 ``(开始, 结束)``。

    头里的 ``log_time`` 是记录**结束**时刻：实测 9 份 10.5 数据，
    ``log_time - duration`` 才是开始时刻（``cjh阶跃+脉冲.ld`` = 17:51:15 -> 18:13:50，
    与帧表的首/末墙钟对上）。日期是 ``dd/mm/yyyy``。
    """
    day, month, year = str(log.log_date).split("/")
    end = datetime.strptime(f"{year}-{month}-{day} {log.log_time}", "%Y-%m-%d %H:%M:%S")
    start = end - timedelta(seconds=float(log.duration))
    return start, end


def recording_window(items: list[dict]) -> tuple[datetime, datetime]:
    """一次记录（可能由几卷拼成）的绝对墙钟窗口。

    ``items`` 是 ``canlog.group_recordings`` 里那种 ``{"path", "first_wall",
    "last_wall", ...}``；墙钟列只有时刻，日期从文件名拿。
    """
    if not items:
        raise ValueError("空的一次记录，算不出时间窗。")
    ordered = sorted(items, key=lambda item: item["first_wall"])
    return (
        _absolute(ordered[0]["path"], ordered[0]["first_wall"]),
        _absolute(ordered[-1]["path"], ordered[-1]["last_wall"]),
    )


def _absolute(path, wall: float) -> datetime:
    """``.csv`` 文件名里的日期 + 墙钟时刻 -> 绝对时间。"""
    name = Path(path).name
    parts = name.split("_")
    day = "-".join(parts[:3]) if len(parts) >= 3 else "1970-01-01"
    base = datetime.strptime(day + " 00:00:00", "%Y-%m-%d %H:%M:%S")
    return base + timedelta(seconds=float(wall))


@dataclass
class Candidate:
    """一个"时间上挨得着"的帧表记录。是不是真的同一段驾驶还没验。"""

    sources: list[Path]
    window: tuple[datetime, datetime]
    overlap_s: float

    @property
    def main(self) -> Path:
        return self.sources[0]

    @property
    def label(self) -> str:
        name = self.main.name
        return name + (f" +{len(self.sources) - 1}" if len(self.sources) > 1 else "")


def candidates(
    ld_start,
    ld_end,
    groups,
    *,
    min_overlap_s: float = MIN_OVERLAP_S,
    tolerance_s: float | None = None,
) -> list[Candidate]:
    """墙钟上挨得着的帧表记录：``.ld`` 的窗口放宽 ``tolerance_s`` 之后还重叠
    ``min_overlap_s`` 秒的那些，按"挨得近"从前往后排。

    ``tolerance_s`` 默认 :data:`MAX_LAG_S`。放宽是必须的：``.ld`` 头里的绝对时间
    **不可靠**——实测 10.7 同一个下午差 19 s 到 505 s（还会跳），日期甚至能差一天
    （``20261008-传杰最小半径绕圆.ld`` 头写 08/10，其实是 07/10 录的）。
    拿它当硬筛子会漏掉真配对：``庞仙耐久两圈`` 对上 ``112831`` 那一次，两个墙钟
    窗口之间还差 24 s 的缝，可内容 `r=1.0000`、加速度复核 0.96（见
    `docs/ACCEPTANCE.md` A76）。

    ``Candidate.overlap_s`` 记的是**真实**重叠，可能是负的（=两个墙钟窗口之间有
    缝）；报告里照实写。一份 ``.ld`` 可能对上**不止一次**记录（记录仪分成几段、
    中间还断过），所以这里不挑"最近的一条"，全都交给 :func:`align` 去验。
    """
    reach = MAX_LAG_S if tolerance_s is None else float(tolerance_s)
    slack = timedelta(seconds=reach)
    out: list[Candidate] = []
    for group in groups:
        items = group["items"] if isinstance(group, dict) else group
        if not items:
            continue
        start, end = recording_window(items)
        overlap = (min(ld_end, end) - max(ld_start, start)).total_seconds()
        reachable = (min(ld_end + slack, end)
                     - max(ld_start - slack, start)).total_seconds()
        if reachable < min_overlap_s:
            continue
        ordered = sorted(items, key=lambda item: item["first_wall"])
        out.append(Candidate(
            sources=[Path(item["path"]) for item in ordered],
            window=(start, end),
            overlap_s=overlap,
        ))
    out.sort(key=lambda item: -item.overlap_s)
    return out


def frame_groups(frames) -> list[dict]:
    """把帧表按"一次记录"分组（``canlog.group_recordings`` 的那套判据）。"""
    from . import canlog

    paths = [Path(path) for path in frames]
    return canlog.group_recordings(paths) if paths else []


def expected_offset(ld_start: datetime, candidate: Candidate) -> float:
    """墙钟给出的偏移估计 [s]：``.ld`` 的 ``t`` 秒对应记录仪的 ``t + offset`` 秒。

    ``.ld`` 的 0 秒 = 它的窗口起点 ``B_ld``；记录仪的 0 秒 = 它首帧的墙钟
    ``B_can``（``read_can_session`` 把时间轴按第一帧归零）。同一个绝对时刻在
    两边分别是 ``B_ld + t`` 与 ``B_can + t_can``，所以

        ``t_can = t + (B_ld - B_can)``，即 ``offset = B_ld - B_can``。

    记录仪先开机时 ``B_can < B_ld``，偏移是**正的**。墙钟只精到秒级（``.ld``
    头里没有毫秒），所以这个值只当搜索中心，精确值由互相关给。
    """
    return (ld_start - candidate.window[0]).total_seconds()


def overlap_seconds(ld_start: datetime, ld_end: datetime, candidate: Candidate) -> float:
    """两边墙钟上都真的有内容的那一段，有多少秒。"""
    begin = max(ld_start, candidate.window[0])
    end = min(ld_end, candidate.window[1])
    return max(0.0, (end - begin).total_seconds())


# ---------------------------------------------------------------------- 对齐
@dataclass
class Alignment:
    """两条速度曲线互相关的结果。"""

    offset_s: float
    r: float
    points: int
    ok: bool
    reason: str = ""
    #: 复核用的加速度相关系数（``None`` = 有一边没有这条通道，没复核）。
    accel_r: float | None = None
    #: 这个结论是用哪种比较窗得来的（``wall_window`` = 锚在墙钟那段重叠上，
    #: ``follow_window`` = 跟着偏移走）。写进侧车，报告里也好看出是哪条路认的。
    window: str = ""

    def as_dict(self) -> dict:
        return {
            "offset_s": round(self.offset_s, 4),
            "vx_r": round(self.r, 4),
            "vx_points": self.points,
            "accel_r": None if self.accel_r is None else round(self.accel_r, 4),
            "ok": self.ok,
            "reason": self.reason,
            "window": self.window,
        }


def hold_sample(times: np.ndarray, values: np.ndarray, at: np.ndarray) -> np.ndarray:
    """零阶保持重采样：``at`` 落在两个采样点之间时取前一个（与 canlog 一致）。

    实现在 :func:`i3pro.timebase.hold_sample`（``derive`` 把卫星数搬到经纬度那条
    轴上时用的是同一个函数）。这里是留个本地名字，调用方不必改。
    """
    return timebase.hold_sample(times, values, at)


def hold_into(times: np.ndarray, values: np.ndarray, at: np.ndarray, into: np.ndarray) -> None:
    """同 :func:`hold_sample`，但写进 ``into``，而且**只填还没有值的位置**。

    一个 ``.ld`` 对上不止一次记录时（实测 ``10.4湾区测试``：同一场高避的两段分别
    被两次记录录到），两次记录各自覆盖一段时间轴。先来的先填，后来的只补空档——
    同一时刻不互相盖，合出来的场次哪一段来自哪一次记录是可解释的。
    """
    at = np.asarray(at, dtype=np.float64)
    if times.size == 0 or values.size == 0:
        return
    inside = (at >= times[0]) & (at < times[-1])
    if not inside.any():
        return
    index = np.clip(np.searchsorted(times, at, side="right") - 1, 0, values.size - 1)
    fill = inside & ~np.isfinite(into)
    into[fill] = values[index[fill]]
def _speed_of(session, names) -> tuple[np.ndarray, np.ndarray]:
    """会话里第一条存在的候选通道 ``(time, values)``；都没有就抛 ``KeyError``。"""
    known = {channel.name for channel in session.channels}
    for name in names:
        hit = channelref.lookup(known, name)
        if hit is None:
            continue
        channel = session.channel(hit)
        times = np.asarray(channel.time(), dtype=np.float64)
        values = np.asarray(session.values(hit), dtype=np.float64)
        if times.size and values.size:
            size = min(times.size, values.size)
            return times[:size], values[:size]
    raise KeyError(" / ".join(names))


def _pearson(left: np.ndarray, right: np.ndarray) -> float:
    """两个数组各自的均值去掉之后的皮尔森相关；常数向量给 -2（认不出来）。"""
    left = left - left.mean()
    right = right - right.mean()
    denom = float(np.sqrt((left * left).sum() * (right * right).sum()))
    if denom <= 0.0:
        return -2.0
    return float((left * right).sum() / denom)


def _accel_correlation(log, session, at: np.ndarray, lag: float) -> float | None:
    """同一条偏移上复核加速度：三根轴各算一次相关，取**中位数**。

    一条轴有一个测点坏了不该拖垮整场（实测 ``jhy耐久``：``accY`` 0.359、
    ``accZ`` 0.970）；三条轴都没有（有一边缺这条通道）就给 ``None`` = 没复核。
    """
    found: list[float] = []
    for names in ACCEL_AXES:
        try:
            ref_t, ref_v = _speed_of(log, names)
            can_t, can_v = _speed_of(session, names)
        except KeyError:
            continue
        ref = hold_sample(ref_t, ref_v, at)
        other = hold_sample(can_t, can_v, at + float(lag))
        mask = np.isfinite(ref) & np.isfinite(other)
        if int(mask.sum()) < MIN_COMPARE_POINTS:
            continue
        found.append(_pearson(ref[mask], other[mask]))
    if not found:
        return None
    return float(np.median(found))


def align(
    log,
    session,
    *,
    ld_start: datetime | None = None,
    can_window: tuple[datetime, datetime] | None = None,
    expect_s: float = 0.0,
    max_lag_s: float = MAX_LAG_S,
    step_s: float = LAG_STEP_S,
    window_s: float = COMPARE_WINDOW_S,
    rate: float = COMPARE_RATE,
    search_s: float = SEARCH_S,
) -> Alignment:
    """在相对时钟上互相关求偏移：``log.values(t)`` 对应 ``session.values(t + offset)``。

    两边各有各的零点，所以"偏移"是记录仪相对 ``.ld`` 的零点差。整件事分三步：

    1. 墙钟给一个估计 ``expect_s``（调用方用 :func:`expected_offset` 算），只当搜索中心；
    2. 在 ``±search_s`` 里用两边都有的速度通道互相关找最像的偏移
       （怎么挑峰见 :data:`TIE_CORRELATION`）；
    3. 同一条偏移上再拿**三条加速度轴的中位数**复核，`r < MIN_ACCEL_R` 就不认。

    **比较窗试两种，先过判据的算**（2026-10-13 补）：

    * **墙钟窗**：锚在"两边墙钟上真的重叠的那一段"上，与偏移无关。钟差只有几秒到
      几十秒时它最稳——历史数据全是这么配出来的，所以**先试它**。
    * **跟随窗**：跟着偏移走，取这个偏移下两边都真的覆盖到的那一段。``.ld`` 头里的
      绝对时间不可靠（10.7 实测差 19–505 s、还会跳，日期也能差一天），钟差大到几百秒
      时墙钟窗会落到数据外面去、只剩一小截可比。实测 ``cjh100限速直线`` 对上
      ``100816``：墙钟窗只盖住 86 个点、那一段记录仪的 MTI 恰好是死的，速度
      `r=0.9999` 却判成不配对；跟随窗盖住 283 个点，同一偏移上加速度复核 0.941，配对。

    挑峰的顺序两种窗不一样，因为窗长的性质不同：墙钟窗的窗长与偏移无关（点数恒定），
    照旧取 `r` 最高的那个；跟随窗的窗长随偏移变（短窗上平滑曲线随手就是
    `r=1.0000`），所以"`r` 相差不到 :data:`TIE_CORRELATION` 时点数多的优先"。
    """
    try:
        ref_t, ref_v = _speed_of(log, SPEED_CANDIDATES)
        can_t, can_v = _speed_of(session, SPEED_CANDIDATES)
    except KeyError as exc:
        return Alignment(0.0, 0.0, 0, False, f"两边都找不到速度通道（{exc}）")

    ref_v = np.asarray(ref_v, dtype=np.float64)
    if not np.isfinite(ref_v).any():
        return Alignment(0.0, 0.0, 0, False, "这一场没有可用的速度样本")

    step = 1.0 / float(rate)
    ld_lo, ld_hi = 0.0, float(log.duration)
    can_lo, can_hi = float(can_t[0]), float(can_t[-1])
    center = float(expect_s)
    reach = float(search_s) if search_s else float(max_lag_s)
    floor = float(MIN_COMPARE_POINTS) * step
    cap = float(window_s)

    def wall_window(_lag: float):
        """锚在"两边墙钟上真的重叠的那一段"上（与偏移无关；老办法，先试它）。

        完全照搬以前的算式，包括那两处用 ``expect_s`` 的夹取——**就是为了让这一路的
        结果与以前逐字节一致**：已经合出来的场次不该因为这次改动而变。墙钟不全
        （没给 ``ld_start`` / ``can_window``）或那段重叠太短就没有这个窗。
        """
        if ld_start is None or can_window is None:
            return None
        begin = max(ld_start, can_window[0])
        end = min(ld_start + timedelta(seconds=ld_hi), can_window[1])
        ld_begin = (begin - ld_start).total_seconds()
        ld_end = (end - ld_start).total_seconds()
        can_begin = (begin - can_window[0]).total_seconds()
        can_end = (end - can_window[0]).total_seconds()
        lo = max(ld_begin, can_begin - center, can_lo - center)
        hi = min(ld_end, can_end - center, can_hi - center)
        span = min(cap, hi - lo)
        if span < floor:
            return None
        return lo + np.arange(0.0, span, step)

    def follow_window(lag: float):
        """跟着偏移走：这个偏移下两边都真的覆盖到的那一段。"""
        lo = max(ld_lo, can_lo - float(lag))
        hi = min(ld_hi, can_hi - float(lag))
        span = min(cap, hi - lo)
        if span < floor:
            return None
        return lo + np.arange(0.0, span, step)

    def full_window(lag: float):
        """这个偏移下两边共同覆盖的**完整**区间，不截到 300 s。

        只在墙钟窗与跟随窗都失败后用。实测 10.29 ``练车3`` 的前 300 s 几乎是
        静止，真实偏移 +992 s 在那里 Vx 只有 0.355、
        加速度 -0.178；同一偏移改用完整 749 s，Vx 0.9997、加速度 0.9343。
        截断窗口会把一段本来能认出的真实驾驶淹掉。
        """
        lo = max(ld_lo, can_lo - float(lag))
        hi = min(ld_hi, can_hi - float(lag))
        if hi - lo < floor:
            return None
        return lo + np.arange(0.0, hi - lo, step)

    def attempt(window_of, *, by_count: bool, refine: bool) -> Alignment:
        """用一种窗把整个偏移范围搜一遍，给出这一种的结论。"""
        def score(lag: float, window_fn=None) -> tuple[float, int]:
            at = (window_of if window_fn is None else window_fn)(lag)
            if at is None:
                return -2.0, 0
            ref = hold_sample(ref_t, ref_v, at)
            other = hold_sample(can_t, can_v, at + float(lag))
            mask = np.isfinite(ref) & np.isfinite(other)
            count = int(mask.sum())
            # 点太少就不算数：几个点上平滑曲线随手就是 r=1.000000。
            if count < MIN_COMPARE_POINTS:
                return -2.0, count
            return _pearson(ref[mask], other[mask]), count

        def sweep(start: float, radius: float, stride: float):
            found: list[tuple[float, int, float]] = []
            for distance in np.arange(0.0, radius + 1e-9, stride):
                lags = (start, start - distance) if distance == 0.0 else (
                    start + distance, start - distance)
                for lag in lags:
                    value, count = score(float(lag))
                    if value > -2.0:
                        found.append((value, count, float(lag)))
            return found

        coarse = sweep(center, reach, float(SEARCH_STEP_S))
        if not coarse:
            return Alignment(0.0, 0.0, 0, False,
                             f"两边能对上的时段太短（不到 {MIN_COMPARE_POINTS:g} 个点），"
                             f"做不了互相关", None, window_of.__name__)
        if by_count:
            # 窗长随偏移变：`r` 相差不到 TIE_CORRELATION 的看成一样高，这时点数多的
            # 那个才说明问题（几十个点的短窗上平滑曲线随手就是 r=1.0000）。
            top = max(row[0] for row in coarse)
            tied = [row for row in coarse if row[0] >= top - TIE_CORRELATION]
            tied.sort(key=lambda row: (-row[1], abs(row[2] - center)))
            choice = tied[0]
        else:
            # 窗长与偏移无关：照老办法取 `r` 最高的（平手时先遇到的、也就是离墙钟近的）。
            choice = max(coarse, key=lambda row: row[0])
        # 精修：在粗搜那个格子附近按 step_s 找最高的 `r`（点数在 0.2 s 之内几乎不变，
        # 这一步不能再按"点数多优先"，否则永远跳不出 0.2 s 的格子）。
        fine = sweep(choice[2], float(SEARCH_STEP_S), float(step_s))
        best_r, best_count, best_lag = max(
            fine + [choice], key=lambda row: (row[0], -abs(row[2] - choice[2])))

        # 第二判据：同一条偏移上再核一遍加速度。速度在短窗口上会撞出假的高相关
        # （实测有 r=1.0000 落在错的偏移上、只覆盖几十秒直线的例子），加速度一直有
        # 起伏，配上它才是"两条曲线真的重合"。
        peak = best_lag
        at = window_of(peak)
        accel_r = None if at is None else _accel_correlation(log, session, at, peak)
        if refine and accel_r is not None and accel_r < MIN_ACCEL_R:
            # Vx 那座峰加速度不像时，不能只在它旁边微调：还要回到墙钟允许的
            # 整个范围、用完整共同覆盖窗找加速度自己的峰，并要求 Vx 在同一条
            # 偏移上也过判据。
            # 实测 10.29 ``练车3``：Vx 先锁到 +1710.672 s（r=1.000、加速度
            # -0.030），真偏移 +992 s 的加速度是 0.934、Vx 0.9997。
            radius = min(reach, float(max_lag_s))
            fallback: tuple[float, int, float, float] | None = None
            for distance in np.arange(0.0, radius + 1e-9, ACCEL_SEARCH_STEP_S):
                lags = (center,) if distance == 0.0 else (
                    center + distance, center - distance)
                for lag in lags:
                    at2 = full_window(float(lag))
                    if at2 is None:
                        continue
                    value = _accel_correlation(log, session, at2, float(lag))
                    if value is None or value < MIN_ACCEL_R:
                        continue
                    check, count = score(float(lag), full_window)
                    if check < MIN_CORRELATION or count < MIN_COMPARE_POINTS:
                        continue
                    row = (float(value), int(count), float(check), float(lag))
                    if fallback is None or row[:2] > fallback[:2]:
                        fallback = row
            if fallback is not None:
                accel_r, best_count, best_r, best_lag = fallback
                peak = best_lag
                window_of = full_window
                at = window_of(peak)
        if refine and accel_r is not None:
            # 用加速度把偏移钉准（原因见 REFINE_S）：只在这座峰附近 ±REFINE_S 里找，
            # 而且**Vx 的判据在那条偏移上仍要过**——两条通道都得同意才算数。
            for offset in np.arange(-REFINE_S, REFINE_S + 1e-9, float(step_s)):
                lag = peak + float(offset)
                at2 = window_of(lag)
                if at2 is None:
                    continue
                value = _accel_correlation(log, session, at2, lag)
                if value is None or value <= accel_r:
                    continue
                check, count = score(lag)
                if check < MIN_CORRELATION or count < MIN_COMPARE_POINTS:
                    continue
                accel_r = value
                best_r, best_count, best_lag = check, count, lag
        drift = abs(best_lag - center)
        ok = bool(best_r >= MIN_CORRELATION and best_count >= MIN_COMPARE_POINTS
                  and drift <= float(max_lag_s))
        reason = ""
        if not ok:
            if best_r < MIN_CORRELATION:
                reason = f"互相关只有 r={best_r:.3f}（要 >= {MIN_CORRELATION}）"
            elif drift > float(max_lag_s):
                reason = (f"互相关说偏移是 {best_lag:+.2f} s，跟墙钟差的 "
                          f"{drift:.1f} s 对不上")
        elif accel_r is not None and accel_r < MIN_ACCEL_R:
            ok = False
            reason = (f"加速度复核只有 r={accel_r:.3f}（要 >= {MIN_ACCEL_R}）"
                      f"——速度像，别的通道不像")
        return Alignment(best_lag, max(best_r, -1.0), best_count, ok, reason, accel_r,
                         window_of.__name__)

    # 墙钟窗那一路一行都不动（历史数据、已经合出来的场次都靠它；改一点偏移
    # 就会让那些文件对不上）；跟随窗是新加的，才需要用加速度把偏移钉准。
    first = attempt(wall_window, by_count=False, refine=False)
    if first.ok:
        return first
    second = attempt(follow_window, by_count=True, refine=True)
    if second.ok:
        return second
    # 两种都不认：报证据多的那一个（`r` 低的那个连"像"都谈不上）。
    return first if first.r >= second.r else second


# ---------------------------------------------------------------------- 合并
@dataclass
class Pairing:
    """一场 ``.ld`` 与它对应的帧表记录（含偏移与证据）。"""

    ld_path: Path
    candidate: Candidate
    alignment: Alignment
    session: object = None
    #: 墙钟给出的偏移估计（``.ld`` 窗口起点 − 记录窗口起点）。用来算"隐含钟差"。
    expect_s: float = 0.0
    #: 因为重名（归一化之后同键）没有写进合并场的 CAN 通道名。
    skipped: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.ld_path.stem

    @property
    def ok(self) -> bool:
        return self.alignment.ok

    @property
    def clock_error(self) -> float:
        """这次配对隐含着"C125 头里的绝对时间比真的差了多少秒"。"""
        return float(self.expect_s) - float(self.alignment.offset_s)

    def as_dict(self) -> dict:
        return {
            "ld": self.name,
            "sources": [path.name for path in self.candidate.sources],
            "overlap_s": round(self.candidate.overlap_s, 1),
            **self.alignment.as_dict(),
        }


def consistent_pairings(pairings, tolerance_s: float = E_TOLERANCE_S):
    """同一场 ``.ld`` 上认下来的几次记录，只留"隐含钟差"挨在一起的那一组。

    返回 ``(留, 丢)``；``丢`` 里每项是 ``(配对, 比最小那个大了多少秒)``。
    剩一个的时候它就是最小那个（没有参照物，谈不上剔除）。理由见
    :data:`E_TOLERANCE_S`。
    """
    if not pairings:
        return [], []
    errors = [abs(item.clock_error) for item in pairings]
    floor = min(errors)
    kept, dropped = [], []
    for item, error in zip(pairings, errors):
        if error - floor <= float(tolerance_s):
            kept.append(item)
        else:
            dropped.append((item, error - floor))
    return kept, dropped


def master_axis(log) -> np.ndarray:
    """合并场次的时间轴 = ``.ld`` 自己的那条（时长 / 零点 / 采样率都是它的）。"""
    return timebase.axis(log)


def merge_columns(log, session, offset_s: float):
    """把两边的列并起来：``(time, columns, units, skipped)``。

    这是"一次记录"的便捷写法（回归与探查脚本用得多）；一个 ``.ld`` 对上不止一次
    记录时走 :func:`merge_sources`。
    """
    return merge_sources(log, [(session, offset_s)])


def merge_sources(log, sources):
    """把 ``.ld`` 与若干次记录并起来：``(time, columns, units, skipped)``。

    ``sources`` 是 ``[(对应帧表的场次, 偏移秒数), ...]``。``time`` 是 ``.ld`` 的
    主时间基（相对秒）；``.ld`` 的通道原序在前，帧表里**本场次没有的**通道接在
    后面（按名字排序，输出稳定）。

    同一个名字的两种写法（``Vx KF`` / ``Vx_KF``、``accX MTI`` / ``accX_MTI``）算
    同一条——保留 ``.ld`` 的名字，CAN 那份不写第二列，名字进 ``skipped``。注意
    **不能**用"只留字母数字"的归并：``AngVelX_body`` 与 ``AngVelX_hor`` 会撞成
    一条（见 :func:`_same_name`）。

    多次记录之间按传入顺序填：先来的先写，后来的只补空档（:func:`hold_into`）。
    """
    from . import channels as channelsmod

    axis = master_axis(log)
    columns: dict[str, np.ndarray] = {}
    units: dict[str, str] = {}
    skipped: list[str] = []

    # 先按 .ld 的通道把列摆好（顺序 = .ld 里的顺序）。空占位列要**在原值上**判，
    # 不能等重采样完再判——重采样会把"整段 0"变成"全 NaN"（或在轴外留 NaN），
    # 那时候判出来的是另一个问题。
    lds: dict[str, np.ndarray] = {}
    placeholder: set[str] = set()
    for channel in log.channels:
        values = np.asarray(log.values(channel), dtype=np.float64)
        channel_rate = channelsmod.sample_rate(log, channel)
        if channel_rate <= 0:
            channel_rate = float(log.sample_rate) or 1.0
        grid = np.arange(values.size, dtype=np.float64) / channel_rate
        units[channel.name] = str(getattr(channel, "unit", "") or "")
        lds[channel.name] = values
        if _empty(values):
            placeholder.add(channel.name)
            # 占位列整段是 0（C125 里没接上的模板通道）。它的 0 **不是数据**，
            # 所以合并场次里这一列从"空"开始，让帧表那条真的把它填满；一条都
            # 填不到时给的是 NaN（"没有数据"），而不是一排看起来像读数的 0。
            columns[channel.name] = np.full(axis.size, np.nan)
        elif values.size == axis.size:
            # 采样点与主时间基**一一对应**（都是 100 Hz 一整场）时原样搬。
            # 这条不是省事：零阶保持的右端是**开**的（`hold_sample` 只认
            # `at < times[-1]`），照着搬会把最后一个真样本吃掉。实测 10.25 的
            # 两段耐久：`.ld` 里 `Vx KF` 末值 −0.09，合并产物最后一行却是空行；
            # 两段接起来之后那一格落在接缝上，距离轴从那里起全是 NaN。
            columns[channel.name] = values
        else:
            columns[channel.name] = hold_sample(grid, values, axis)

    taken = {name_key(name) for name in columns}
    # 帧表那边本场次没有的通道：按名字排序接在后面（输出稳定、可 diff）。
    seen_can: dict[str, str] = {}
    for session, _offset in sources:
        for channel in session.channels:
            if name_key(channel.name) in taken or channel.name in seen_can:
                continue
            seen_can[channel.name] = str(getattr(channel, "unit", "") or "")
    for name in sorted(seen_can):
        columns[name] = np.full(axis.size, np.nan)
        units[name] = seen_can[name]

    for session, offset_s in sources:
        can_t = np.asarray(session.time, dtype=np.float64)
        for channel in session.channels:
            # 走 ``values()`` 而不是 ``session.columns[...]``：CSV 场次的列分两本
            # （原生在 ``columns``、数学通道在 ``derived``，见 A77），直接按名字查
            # 字典会让"这一场算出来的数学通道"在合并时 KeyError。
            values = np.asarray(session.values(channel.name), dtype=np.float64)
            unit = str(getattr(channel, "unit", "") or "")
            key = name_key(channel.name)
            if key in taken:
                # 同名：对面是空的（C125 里没接上的 IVT 模板通道，整段 0）就换成真的
                # 有数的这一条；真的有数就不动它，把 CAN 的这条记进 skipped。
                existing = next(name for name in lds if _same_name(name, channel.name))
                replaceable = existing in placeholder or _empty(
                    lds[existing], inclusive=True
                )
                # ``Distance`` 不参与替换：记录仪的 ``Distance`` 可能是计数器 /
                # 无效哨兵（实测这份日志从 0 跳到 429496），而 ``.ld`` 那条空列
                # 下面有 ``derive.distance_series`` 的速度积分兜底。让它保持空列，
                # 比把整条距离轴换成 429 km 安全。
                keep_ld = name_key(existing) in ("distance", "distance2")
                if not replaceable or keep_ld:
                    if channel.name not in skipped:
                        skipped.append(channel.name)
                    continue
                if existing not in placeholder:
                    # 边界上的模板列原先按"有 0"摆着；换真值时先把整列清空，
                    # CAN 没覆盖到的两端才不会被一排假的 0 填住。
                    columns[existing] = np.full(axis.size, np.nan)
                hold_into(can_t, values, axis + float(offset_s), columns[existing])
                if unit:
                    units[existing] = unit
                continue
            hold_into(can_t, values, axis + float(offset_s), columns[channel.name])
    return axis, columns, units, skipped


#: 合并宽表里浮点写多少位有效数字（与 ``export.FLOAT_FORMAT`` 同一档）。
FLOAT_FORMAT = "%.10g"
#: 一次写多少行：480 列 x 20000 行 = 960 万个格子，峰值几十 MB。
CHUNK_ROWS = 20_000


def write_merged(
    path: str | Path,
    time: np.ndarray,
    columns: dict[str, np.ndarray],
    *,
    units: dict[str, str] | None = None,
    index_name: str = "time_s",
    chunk_rows: int = CHUNK_ROWS,
    progress=None,
) -> dict:
    """把合并后的宽表写成 CSV（UTF-8 带 BOM，Excel 双击就能开）。

    表头第一列是 ``time_s``，第二行是单位；其余列是通道名。那是 i3pro 自己
    导出宽表的形状（``csvlog`` 读得回来，``looks_like_frames`` 不会把它当帧表）。
    缺失的格子写空字符串。返回 ``{"path", "rows", "columns", "bytes"}``。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    names = list(columns)
    units = units or {}
    total = int(time.size)
    step = max(1, int(chunk_rows))
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow([index_name, *names])
        writer.writerow([""] + [units.get(name, "") for name in names])
        for start in range(0, total, step):
            stop = min(total, start + step)
            rows: list[list[str]] = []
            for row in range(start, stop):
                cells = [FLOAT_FORMAT % float(time[row])]
                for name in names:
                    value = columns[name][row]
                    cells.append("" if not np.isfinite(value) else FLOAT_FORMAT % float(value))
                rows.append(cells)
            writer.writerows(rows)
            if progress is not None:
                progress(stop, total)
    return {
        "path": str(target),
        "rows": total,
        "columns": len(names),
        "bytes": target.stat().st_size,
    }
