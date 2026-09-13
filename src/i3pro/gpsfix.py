"""GPS 校正（ticket #14）：把坏定位**标出来**，按需修掉，而不是当成原点用。

C125 掉星时给的定位是 ``(0, 0)``（几内亚湾），有些场次的 ``GPS Sats Used`` 本身
是死通道。还有一类更隐蔽的坏点：经纬度看着完全合法，但定位**整体跳到几百米外并留
在那里**。这批日志上量到的第二种更多，而且都发生在场次尾声：

====================  ======================  ============================
场次                   跳点（>200 km/h）        最大一跳
====================  ======================  ============================
20260524-耐久正赛            1                214.5 m / 0.05 s（15443 km/h）
高避陈君灏                   6                495.5 m / 0.30 s
FSS_jhy_endu                6                621.3 m / 105 s
20260912-TV0              112                （掉星 279 s 之后的重新定位）
====================  ======================  ============================

16 个场次里**一个孤立毛刺都没有**——每一次都是"跳过去就不回来"。所以这里不做
"删掉跳点"：删哪一边都是猜。做的是**断开连线 + 标出来**，于是轨迹图不再画一条
不存在的直线，切圈也不会把一次跳变当成一次过门。

三件事分开，互不牵连：

1. :func:`annotate` / :func:`classify` —— 每个采样点归类（好点 / 跳点 / 空档），
   **永远算**，和开关无关。"这段数据不可信"本身就是结论。
2. :func:`correct` —— 按时移、按需插值到主采样率，**只有配置打开时才动数值**。
   关闭时 ``derive.gps_track()`` 返回的 time/x/y 与旧实现逐点相同（单测
   ``array_equal`` 钉住）。
3. :func:`load_config` / :func:`save_config` —— ``<场次>.gps.json`` 侧车，
   ``.ld`` 永远只读。

作用域（轨迹 / 切圈 / 距离轴）由 :func:`scoped` 处理：同一个配置，按调用方关心
的那一格决定要不要真的修正。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from . import sidecar

__all__ = [
    "DEFAULT_GAP_S",
    "DEFAULT_SPIKE_KMH",
    "FixConfig",
    "annotate",
    "classify",
    "config_path",
    "correct",
    "for_log",
    "load_config",
    "path_distance",
    "resolve",
    "save_config",
    "scoped",
    "summary",
]

#: 侧车后缀由 ``sidecar.KINDS["gps"]`` 说了算；这个名字留着给报错文案引用。
SIDE_SUFFIX = sidecar.kind_of("gps").suffix

#: 缺省跳点阈值：200 km/h。这批日志里最快的车是 78.7 km/h，而错位定位跳出来的
#: 隐含速度最低也有 800 km/h——两头都留了足够余量，不用看数据就能定这一条。
DEFAULT_SPIKE_KMH = 200.0

#: 缺省空档阈值：1 秒。正常采样间隔是 0.02~0.05 s，1 秒已经是"整整一段没定位"。
DEFAULT_GAP_S = 1.0


@dataclass(frozen=True)
class FixConfig:
    """一个场次的 GPS 校正参数。缺省 = 不修正，只标注。"""

    enabled: bool = False
    #: 固定时间偏移（秒）。正数 = 定位比记录仪时间戳晚，要把定位往前挪。
    offset_s: float = 0.0
    #: 按 GPS 更新周期计的偏移（个）。慢速定位的固定延迟常常正好是几个周期，
    #: 用周期数比用秒数好记：换场次采样率变了也不用重算。
    offset_ratio: float = 0.0
    #: 把低频定位插值到主采样率（100 Hz）。按段插值，空档处不补点。
    resample: bool = False
    spike_kmh: float = DEFAULT_SPIKE_KMH
    gap_s: float = DEFAULT_GAP_S
    scope_track: bool = True
    scope_laps: bool = True
    #: 距离轴默认**不跟着变**：现在所有圈速、区段、报表都建立在速度积分的距离轴
    #: 上，换基准得用户自己点头。
    scope_distance: bool = False

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "offset_s": self.offset_s,
            "offset_ratio": self.offset_ratio,
            "resample": self.resample,
            "spike_kmh": self.spike_kmh,
            "gap_s": self.gap_s,
            "scope_track": self.scope_track,
            "scope_laps": self.scope_laps,
            "scope_distance": self.scope_distance,
        }

    @classmethod
    def from_dict(cls, raw) -> "FixConfig":
        """从 JSON 造一份配置；每个字段不合法都说清是哪个、该填什么。"""
        if not isinstance(raw, dict):
            raise ValueError(
                'GPS 校正配置要是一个 JSON 对象，例如 {"enabled": true, "offset_s": 0.2}'
            )
        base = cls()

        def number(key: str, default: float, low: float, high: float, unit: str) -> float:
            if key not in raw or raw[key] is None:
                return default
            try:
                value = float(raw[key])
            except (TypeError, ValueError):
                raise ValueError(
                    f"gps.{key} 要是一个数字（{unit}），收到 {raw[key]!r}"
                ) from None
            if not math.isfinite(value) or not (low <= value <= high):
                raise ValueError(
                    f"gps.{key} 要在 {low:g}~{high:g} {unit} 之间，收到 {value:g}"
                )
            return value

        def flag(key: str, default: bool) -> bool:
            if key not in raw or raw[key] is None:
                return default
            return bool(raw[key])

        return cls(
            enabled=flag("enabled", base.enabled),
            offset_s=number("offset_s", base.offset_s, -60.0, 60.0, "秒"),
            offset_ratio=number("offset_ratio", base.offset_ratio, -50.0, 50.0, "个更新周期"),
            resample=flag("resample", base.resample),
            spike_kmh=number("spike_kmh", base.spike_kmh, 20.0, 5000.0, "km/h"),
            gap_s=number("gap_s", base.gap_s, 0.1, 600.0, "秒"),
            scope_track=flag("scope_track", base.scope_track),
            scope_laps=flag("scope_laps", base.scope_laps),
            scope_distance=flag("scope_distance", base.scope_distance),
        )


# ------------------------------------------------------------------ 标注


def classify(
    time: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    *,
    spike_kmh: float = DEFAULT_SPIKE_KMH,
    gap_s: float = DEFAULT_GAP_S,
) -> dict:
    """给一段（已滤掉无定位与卫星不足的）轨迹打标。

    ``breaks[i] is True`` 表示第 ``i`` 个点与第 ``i-1`` 个点之间**不许连线**：
    要么时间上隔着一段空档，要么两点之间的隐含速度快到不可能是车。
    """
    n = int(np.asarray(time).size)
    breaks = np.zeros(n, dtype=bool)
    jumps = np.zeros(n, dtype=bool)
    holes: list[dict] = []
    jump_rows: list[dict] = []
    if n >= 2:
        dt = np.diff(time)
        step = np.hypot(np.diff(x), np.diff(y))
        # 时间戳没往前走的两点之间没有速度可言，不要凭它判跳点
        safe_dt = np.where(dt > 0, dt, np.inf)
        kmh = step / safe_dt * 3.6
        hole = dt > gap_s
        jump = kmh > spike_kmh
        breaks[1:] = hole | jump
        jumps[1:] = jump
        jumps[:-1] |= jump          # 跳变的两端都算坏点：说不好是哪一边错
        for i in np.flatnonzero(hole):
            holes.append(
                {
                    "from": float(time[i]),
                    "to": float(time[i + 1]),
                    "seconds": float(dt[i]),
                }
            )
        for i in np.flatnonzero(jump):
            jump_rows.append(
                {
                    "from": float(time[i]),
                    "to": float(time[i + 1]),
                    "meters": float(step[i]),
                    "kmh": float(kmh[i]),
                }
            )
    pieces = [int(p.size) for p in np.split(np.arange(n), np.flatnonzero(breaks)) if p.size]
    return {
        "breaks": breaks,
        "jumps": jumps,
        "holes": holes,
        "jump_rows": jump_rows,
        "segments": len(pieces),
        "longest_hole_s": max([h["seconds"] for h in holes], default=0.0),
    }


def annotate(track: dict, config: FixConfig | None = None) -> dict:
    """只加标注字段，**一个数值都不动**。返回新的 dict。"""
    cfg = config or FixConfig()
    time = np.asarray(track["time"], dtype=float)
    marks = classify(
        time,
        np.asarray(track["x"], dtype=float),
        np.asarray(track["y"], dtype=float),
        spike_kmh=cfg.spike_kmh,
        gap_s=cfg.gap_s,
    )
    out = dict(track)
    out["breaks"] = marks["breaks"]
    out["jumps"] = marks["jumps"]
    out["holes"] = marks["holes"]
    out["jump_rows"] = marks["jump_rows"]
    out["segments"] = marks["segments"]
    return out


def summary(track: dict, config: FixConfig | None = None) -> dict:
    """面板上那行"标记了什么"：坏点计数 + 空档 + 跳点规模。"""
    cfg = config or FixConfig()
    annotated = track if "jump_rows" in track else annotate(track, cfg)
    dropped = track.get("dropped") or {}
    return {
        "enabled": bool(cfg.enabled),
        "rate": float(track.get("rate") or 0.0),
        "samples": int(np.asarray(track["time"]).size),
        "no_fix": int(dropped.get("no_fix") or 0),
        "low_sats": int(dropped.get("low_sats") or 0),
        "jumps": len(annotated.get("jump_rows") or []),
        "worst_jump_m": max(
            [float(r["meters"]) for r in (annotated.get("jump_rows") or [])], default=0.0
        ),
        "holes": len(annotated.get("holes") or []),
        "longest_hole_s": max(
            [float(h["seconds"]) for h in (annotated.get("holes") or [])], default=0.0
        ),
        "segments": int(annotated.get("segments") or 1),
        "spike_kmh": float(cfg.spike_kmh),
        "gap_s": float(cfg.gap_s),
    }


# ------------------------------------------------------------------ 修正


def _resample_segments(
    time: np.ndarray,
    breaks: np.ndarray,
    fields: dict[str, np.ndarray],
    master_rate: float,
) -> tuple[np.ndarray, dict[str, np.ndarray], np.ndarray]:
    """把每一段独立插值到主采样率的格子上；段与段之间**不插值**。

    空档里没有数据，插出来的每一点都是编的——所以干脆不生成，段与段之间留一个
    时间跳变，交给 ``breaks`` 去断开连线。
    """
    edges = np.flatnonzero(breaks)
    pieces = np.split(np.arange(time.size), edges)
    out_time: list[np.ndarray] = []
    out_fields: dict[str, list[np.ndarray]] = {k: [] for k in fields}
    joins: list[int] = []
    total = 0
    for piece in pieces:
        if piece.size < 2:
            continue
        t0, t1 = float(time[piece[0]]), float(time[piece[-1]])
        i0 = int(math.ceil(t0 * master_rate - 1e-6))
        i1 = int(math.floor(t1 * master_rate + 1e-6))
        if i1 <= i0:
            continue
        grid = np.arange(i0, i1 + 1) / master_rate
        if out_time:
            joins.append(total)
        out_time.append(grid)
        for key, values in fields.items():
            out_fields[key].append(np.interp(grid, time[piece], values[piece]))
        total += grid.size
    if not out_time:
        empty = np.zeros(0)
        return empty, {k: empty.copy() for k in fields}, np.zeros(0, dtype=bool)
    joined = np.concatenate(out_time)
    joined_breaks = np.zeros(joined.size, dtype=bool)
    for index in joins:
        if 0 < index < joined_breaks.size:
            joined_breaks[index] = True
    return (
        joined,
        {k: np.concatenate(v) for k, v in out_fields.items()},
        joined_breaks,
    )


def correct(track: dict, config: FixConfig, master_rate: float | None = None) -> dict:
    """按配置修正：时间偏移 + （可选）插值到主采样率。不修改入参。"""
    if not config.enabled:
        return annotate(track, config)
    time = np.asarray(track["time"], dtype=float)
    fields = {
        "x": np.asarray(track["x"], dtype=float),
        "y": np.asarray(track["y"], dtype=float),
        "lat": np.asarray(track["lat"], dtype=float),
        "lon": np.asarray(track["lon"], dtype=float),
    }
    rate = float(track.get("rate") or 1.0)
    shift = float(config.offset_s) + float(config.offset_ratio) / max(rate, 1e-9)
    if shift:
        time = time + shift
    marks = classify(time, fields["x"], fields["y"],
                     spike_kmh=config.spike_kmh, gap_s=config.gap_s)
    forced = marks["breaks"]
    if config.resample and master_rate:
        time, fields, forced = _resample_segments(
            time, forced, fields, float(master_rate)
        )
        marks = classify(time, fields["x"], fields["y"],
                         spike_kmh=config.spike_kmh, gap_s=config.gap_s)
        forced = forced | marks["breaks"]
    out = dict(track)
    out["time"] = time
    out.update(fields)
    out["breaks"] = forced
    out["jumps"] = marks["jumps"]
    out["holes"] = marks["holes"]
    out["jump_rows"] = marks["jump_rows"]
    out["segments"] = marks["segments"]
    out["fix"] = {
        "enabled": True,
        "offset_s": shift,
        "resampled": bool(config.resample and master_rate),
        "samples_before": int(np.asarray(track["time"]).size),
        "samples_after": int(time.size),
        "scope_track": config.scope_track,
        "scope_laps": config.scope_laps,
        "scope_distance": config.scope_distance,
    }
    return out


def scoped(config: FixConfig | None, scope: str) -> FixConfig | None:
    """同一份配置，但只保留 ``scope``（track / laps / distance）那一格。

    这一格没开就返回一份 ``enabled=False`` 的配置——调用方只需要看 ``enabled``，
    不用再记住"哪些作用域还没有值"。
    """
    if config is None:
        return config
    if not config.enabled or not getattr(config, f"scope_{scope}", False):
        return replace(config, enabled=False)
    return config


def resolve(log, fix: FixConfig | None = None, scope: str = "track") -> FixConfig:
    """这个消费方（``scope``）现在该用的配置。

    ``fix is None``（缺省）表示"按这个场次的 ``<场次>.gps.json`` 侧车"，没存过侧车
    就是不校正。**所有的隐式读盘都收在这一处**：距离轴会被主图、赛道区段、报表、
    圈速各算一遍，任何一处漏传参数都会让同一个数字出现两个值，而这个仓库最怕的
    就是"数字对不上却没人发现"。
    """
    config = fix if fix is not None else for_log(log)
    return scoped(config, scope) or FixConfig()


def path_distance(
    time: np.ndarray, x: np.ndarray, y: np.ndarray, breaks=None
) -> np.ndarray:
    """沿轨迹累计的里程（米）。跳变与空档处**不累加**：那段不知道车走了多少。"""
    step = np.hypot(np.diff(x), np.diff(y))
    if breaks is not None and np.asarray(breaks).size == np.asarray(time).size:
        step = np.where(np.asarray(breaks)[1:], 0.0, step)
    return np.concatenate([[0.0], np.cumsum(step)])


# ------------------------------------------------------------------ 侧车


def config_path(session_path: str | Path) -> Path:
    """``<场次>.ld`` / ``<场次>.csv`` -> ``<场次>.gps.json``。"""
    return sidecar.path_of("gps", session_path)


def load_config(session_path: str | Path) -> FixConfig | None:
    """读侧车；没存过给 ``None``（"还没设过"，不是错误）。"""
    data = sidecar.read("gps", session_path)
    return None if data is None else FixConfig.from_dict(data)


def save_config(session_path: str | Path, config: FixConfig) -> Path:
    return sidecar.write("gps", session_path, config.as_dict())


def for_log(log, *, strict: bool = False) -> FixConfig:
    """这个场次现在生效的 GPS 校正。

    ``strict=False``（读路径）时侧车坏了就当没设过：一个人的 JSON 打错不该让整个
    工作台打不开。写入路径用 ``strict=True``，让错误直接弹到用户面前。
    """
    try:
        return load_config(log.path) or FixConfig()
    except ValueError:
        if strict:
            raise
        return FixConfig()
