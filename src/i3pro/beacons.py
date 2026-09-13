"""信标与切分配置：用户对"这一场怎么切圈"做的每一个决定（ticket #24）。

``laps.py`` 里原来同时住着四件事：切圈算法、**信标配置与编辑规则**、距离轴与圈差、
圈表。这个模块是其中第二件——什么是一个信标、配置长什么样、改名 / 插穿越 / 删信标
的规则、一版与一版怎么比、"上一步"怎么交回去、侧车文件放哪。

词汇表（``CONTEXT.md``）里两条最容易搞混的：

* **信标 / beacon** = 起终点线的**一次穿越**（位置与时刻都是它的属性），不是那条线。
  一个信标 = 一条独立的圈序列，所以八字绕环的左环、右环各算一个。
* **圈 / lap** = 同一个信标两次穿越之间的那段。

这个模块不切圈、不算距离：它只回答"用户改了什么、这一改怎么落盘"。
切圈在 :mod:`i3pro.laps`，距离轴与圈差在 :mod:`i3pro.axes`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable

from . import sidecar

__all__ = [
    "Beacon",
    "LapConfig",
    "check_new_crossings",
    "clean_name",
    "config_path",
    "insertion_notice",
    "load_config",
    "merge_crossings",
    "reconcile_edits",
    "same_config",
    "save_config",
    "undo_config",
    "unique_name",
]


#: Sidecar written next to the ``.ld`` file. The ``.ld`` itself stays read-only
#: (see AGENTS.md); everything the user edits about laps lives here.
#: 侧车后缀由 ``sidecar.KINDS["laps"]`` 说了算；这个名字留着给老代码与文档引用。
CONFIG_SUFFIX = sidecar.kind_of("laps").suffix

#: A beacon name becomes the prefix of every lap label in its series
#: (``左环 3``), so it stays short enough for the side panel and a share link.
MAX_BEACON_NAME = 24
DEFAULT_BEACON_NAME = "信标"
DEFAULT_CROSSING_NAME = "手工穿越"
#: A hand-entered crossing this close to a boundary that already exists is that
#: boundary - see ``merge_crossings``.
CROSSING_SNAP = 0.05


@dataclass


# --------------------------------------------------------------- sidecar
@dataclass
class Beacon:
    """**One crossing of the start/finish line** - not the line itself.

    Its position (where the line is drawn) and its time (when the car went
    through) are both attributes of the same thing. Detection gives the time
    from the position; a missed crossing can be entered as a time alone.
    One beacon yields one independent lap series.
    """

    name: str
    lat: float | None = None
    lon: float | None = None
    time: float | None = None

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None

    def as_dict(self) -> dict:
        out: dict = {"name": self.name}
        if self.lat is not None:
            out["lat"] = round(float(self.lat), 7)
        if self.lon is not None:
            out["lon"] = round(float(self.lon), 7)
        if self.time is not None:
            out["time"] = round(float(self.time), 4)
        return out


@dataclass
class LapConfig:
    """Everything the user has decided about this session's laps.

    Stored as ``<session>.laps.json`` next to the log. Keeping it out of the
    ``.ld``/``.ldx`` preserves the "logs are read-only" rule and lets the file
    travel with the data folder, be diffed in git, and ride along a share link.
    """

    mode: str = "auto"                       # auto | run | figure8
    #: Beacons in the sense defined above. One beacon = one lap series, so a
    #: figure-of-eight gets two (left loop + right loop) and each is timed
    #: independently - the only reliable way on a self-crossing course.
    beacons: list[Beacon] = field(default_factory=list)
    trusted: dict[str, bool] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "beacons": [b.as_dict() for b in self.beacons],
            "trusted": dict(self.trusted),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LapConfig":
        """Read the current shape **and** every shape written before the merge.

        Older sidecars stored the line as ``gate`` / ``gates`` and the times as
        a bare list of ``beacons`` floats. Both must keep loading: a user who
        placed beacons before the merge must not lose them.
        """
        beacons: list[Beacon] = []
        for item in data.get("beacons") or []:
            if isinstance(item, dict):
                beacons.append(
                    Beacon(
                        name=str(item.get("name") or f"信标{len(beacons) + 1}"),
                        lat=_opt_float(item.get("lat")),
                        lon=_opt_float(item.get("lon")),
                        time=_opt_float(item.get("time")),
                    )
                )
            else:                                   # old: a bare crossing time
                beacons.append(Beacon(name=f"信标{len(beacons) + 1}", time=float(item)))
        for index, item in enumerate(data.get("gates") or []):   # old: named lines
            if isinstance(item, dict) and item.get("lat") is not None:
                beacons.append(
                    Beacon(name=str(item.get("name") or f"信标{index + 1}"),
                           lat=float(item["lat"]), lon=float(item.get("lon")))
                )
            elif isinstance(item, (list, tuple)) and len(item) == 3:  # (name, lat, lon)
                beacons.append(Beacon(name=str(item[0]), lat=float(item[1]), lon=float(item[2])))
        gate = data.get("gate")                     # old: a single unnamed line
        if gate and len(gate) == 2:
            beacons.append(Beacon(name=f"信标{len(beacons) + 1}",
                                  lat=float(gate[0]), lon=float(gate[1])))
        return cls(
            mode=str(data.get("mode") or "auto"),
            beacons=beacons,
            trusted={str(k): bool(v) for k, v in (data.get("trusted") or {}).items()},
        )


def _opt_float(value) -> float | None:
    return None if value is None else float(value)


def reconcile_edits(old: LapConfig, new: LapConfig) -> LapConfig:
    """Apply the beacon-name rules to a config the user just edited.

    The UI sends the whole config, so the rules live here, once, instead of being
    re-implemented by every caller (and re-implemented slightly differently each
    time): a name is trimmed, an empty name falls back to what the beacon was
    called before, duplicates get a numeric suffix, and over-long names are
    truncated.

    Renaming also carries the ``trusted`` marks over to the new label. Labels are
    ``<beacon name> <lap number>``, so without this a rename would silently lose
    every "this lap is untrusted" decision the user made in the lap table.

    Names that did not change are left exactly as they are - an existing sidecar
    must not be rewritten behind the user's back.
    """
    beacons = list(new.beacons)
    trusted = dict(new.trusted)
    pairs = _pair_beacons(old.beacons, beacons)
    for index, beacon in enumerate(beacons):
        before = pairs[index]
        if before is not None and before.name == beacon.name:
            continue
        fallback = before.name if before is not None else DEFAULT_BEACON_NAME
        name = unique_name(
            clean_name(beacon.name, fallback),
            [b.name for other, b in enumerate(beacons) if other != index],
        )
        beacons[index] = replace(beacon, name=name)
        if before is not None:
            trusted = _migrate_trusted(trusted, before.name, name)
    return replace(new, beacons=beacons, trusted=trusted)


def _pair_beacons(old: list[Beacon], new: list[Beacon]) -> list[Beacon | None]:
    """Say which beacon each edited beacon came from.

    The client sends the whole list, so the pairing rule decides whether an edit
    reads as a rename (marks follow) or as a delete + an insert (marks do not).
    Positions alone get a delete wrong: removing the middle of
    ``[左环, 右环, 手工穿越]`` slides ``手工穿越`` into slot 1 and hands it
    ``右环``'s marks. Names alone get a rename-onto-an-existing-name wrong: two
    beacons called ``右环`` would move the marks sideways to a different line.

    So: an edit that keeps the length is a rename in place (pair by position, the
    physical beacon is what the marks belong to); an edit that changes the length
    inserted or deleted something (pair by name, which is what survives both).
    Leftovers are paired by position only when that is unambiguous. A beacon that
    is simply gone comes back as ``None`` and carries no marks anywhere.
    """
    if len(old) == len(new):
        return [before for before in old]
    pairs: list[Beacon | None] = [None] * len(new)
    claimed: set[int] = set()
    for index, beacon in enumerate(new):
        for other, before in enumerate(old):
            if other not in claimed and before.name == beacon.name:
                pairs[index] = before
                claimed.add(other)
                break
    left_old = [index for index in range(len(old)) if index not in claimed]
    left_new = [index for index in range(len(new)) if pairs[index] is None]
    if len(left_old) == len(left_new):                  # one rename + one insert
        for before_index, index in zip(left_old, left_new):
            pairs[index] = old[before_index]
    return pairs


def check_new_crossings(old: LapConfig, new: LapConfig, duration: float) -> str | None:
    """Reject a hand-entered crossing that is not inside this session.

    Returns a message for the user (in Chinese, saying what to do next) or
    ``None`` when the edit is fine. Only crossings that are *new* in this edit
    are checked: an out-of-range time that already sits in a sidecar must never
    lock the user out of editing that session. "New" is judged by the moment
    itself and not by the name, so renaming such a crossing still works.
    """
    known = _crossing_times(old)
    for beacon in new.beacons:
        if beacon.has_position or beacon.time is None:
            continue
        if round(float(beacon.time), 4) in known:
            continue
        if not math.isfinite(beacon.time):
            return f"信标「{beacon.name}」缺少穿越时刻"
        if not 0.0 <= beacon.time <= duration:
            return (
                f"「{beacon.name}」的穿越时刻 {beacon.time:.3f} s 超出本场时长 "
                f"0–{duration:.1f} s，请把光标放到图上再插入"
            )
    return None


def insertion_notice(
    old: LapConfig, new: LapConfig, laps_before: int, laps_after: int
) -> str | None:
    """Warn when a hand-entered crossing did not split anything.

    A crossing *is* a boundary, so a crossing that lands inside a series always
    changes the lap count. When it does not - the session has no boundary to
    insert into yet, or the moment coincides with one that is already there - the
    user has to be told, because a button that quietly does nothing reads as
    "the program is broken".
    """
    added = _crossing_times(new) - _crossing_times(old)
    if not added:
        return None
    if laps_after > laps_before:
        return None
    return (
        "这次穿越没有切出新圈：它和已有边界重合，或者本场还没有可插入的边界。"
        "请放大到那一圈、把光标放准再插一次。"
    )


def same_config(current: LapConfig, other: LapConfig | None) -> bool:
    """是不是同一版配置。

    「撤销」按钮的判据就在这里：上一版和当前这一版一模一样时，撤销没有东西可撤，
    控件应当是灰的，而不是点下去什么都不发生。比较用 :meth:`LapConfig.as_dict`
    ——那正是落盘与过线的形状，所以"同一版"指的是**边车里同一版**：经纬度比到
    小数第 7 位、时刻比到小数第 4 位（``as_dict`` 的舍入），再细的差别本来也存不下去。
    """
    if other is None:
        return False
    return current.as_dict() == other.as_dict()


def undo_config(current: LapConfig, previous: LapConfig | None) -> LapConfig | None:
    """撤销上一步：把**上一版配置原样交回来**；没有可撤销的一步时给 ``None``。

    撤销不是"反向编辑"，而是"把上一版再提交一次"。**原样**是关键：改名撤销之后
    「可信 / 不可信」标记回来的原因是它们本来就挂在旧名字上（上一版就是这么存的），
    不是有人又迁移了一遍；插入撤销之后那条边界不在上一版里；删掉的信标还在列表里。
    所以调用方拿到它之后只需要落盘，不必再跑 ``reconcile_edits``——上一版正是那些
    规则自己的输出。

    调用方负责保管 ``previous``——按 ticket 的约定只留一版（一个槽，落在服务内存里），
    所以这里是**一级撤销**，撤销完就把它清掉，不做重做。
    """
    if previous is None or same_config(current, previous):
        return None
    return previous


def _crossing_times(config: LapConfig) -> set[float]:
    """The moments of every hand-entered crossing, at sidecar precision."""
    return {
        round(float(b.time), 4)
        for b in config.beacons
        if not b.has_position and b.time is not None
    }


def merge_crossings(edges: Iterable[float], times: Iterable[float]) -> list[float]:
    """Add hand-entered crossings to the boundaries that are already there.

    A crossing that lands within :data:`CROSSING_SNAP` of an existing boundary
    *is* that boundary: pointing at i2 Pro's own detected crossing and clicking
    would otherwise leave a hair-thin phantom lap (the times sidecars carry are
    rounded, so the two never match bit for bit).
    """
    merged = sorted(float(edge) for edge in edges)
    for when in times:
        if all(abs(float(when) - edge) > CROSSING_SNAP for edge in merged):
            merged.append(float(when))
    return sorted(merged)


def clean_name(name: object, fallback: str) -> str:
    """Trim the name; empty means "keep whatever it was called before"."""
    return str(name or "").strip() or fallback


def unique_name(stem: str, taken: Iterable[str], limit: int = MAX_BEACON_NAME) -> str:
    """Truncate to ``limit`` characters and add " 2", " 3"… until it is free."""
    used = set(taken)
    name = stem[:limit]
    if name not in used:
        return name
    for number in range(2, 1000):
        suffix = f" {number}"
        candidate = stem[: max(1, limit - len(suffix))] + suffix
        if candidate not in used:
            return candidate
    return name


def _migrate_trusted(trusted: dict[str, bool], old: str, new: str) -> dict[str, bool]:
    """Move ``<old> <n>`` trusted marks onto ``<new> <n>``.

    The remainder after the prefix must be a plain lap number: a beacon that
    legitimately ends in a digit (``左环`` renamed to ``左环 2``) leaves labels
    like ``左环 2 1``, which a rename of ``左环`` must not re-point at itself.
    """
    if old == new:
        return trusted
    prefix = old + " "
    migrated: dict[str, bool] = {}
    for key, value in trusted.items():
        rest = key[len(prefix) :] if key.startswith(prefix) else None
        migrated[f"{new} {rest}" if rest is not None and rest.isdigit() else key] = value
    return migrated


def config_path(ld_path: str | Path) -> Path:
    """``<session>.ld`` -> ``<session>.laps.json``."""
    return sidecar.path_of("laps", ld_path)


def load_config(ld_path: str | Path) -> LapConfig:
    """读信标侧车：没存过 = 空配置；**读坏了要报错**（见 ``sidecar``，文件不会被删）。

    以前这里把坏文件悄悄当空——那正是"用户以为没编辑过、一保存就把旧的覆盖掉"的
    由来。现在坏文件会带一句"修好它或删掉它"抛出来。
    """
    return LapConfig.from_dict(sidecar.read("laps", ld_path))


def save_config(ld_path: str | Path, config: LapConfig) -> Path:
    return sidecar.write("laps", ld_path, config.as_dict())
