"""把**原始 CAN 帧表**读成 i3pro 的场次（ticket #38 / #39）。

日志长这样（GBK、CRLF，第 2 列被 Excel 写成了 ``="17:54:13.761949`` 这种半截引号，
**相对时间在第 3 列**，数据列是 ``x| 5a 64 46 23 55 00 7f 00``）::

    序号,系统时间,时间标识,CAN通道,ID号,帧类型,帧格式,CAN类型,长度,数据
    1000000,="17:54:13.761949,385.624895,ch1,0x27,数据帧,标准帧,CAN,8,x| 01 01 ...

产出与 ``.ld`` **同形状**的场次：进侧边栏、能画图、能加数学通道、能导出。距离轴取决于
这批日志里到底有没有车速：13 份 DBC **取并集**之后 `0xC1 Throttle_INFO.Vx_KF`
（`TH.dbc`）就是车速，跑起来的日志因此**有距离轴**（实测 0.1–5546.8 m），切圈靠手工
信标（没有 GPS）；只有几十秒、车没动的日志里那条是死通道，那种场次就没有距离轴。
报告把这两种情况分开说，而不是等用户画不出距离轴再来猜。详见 ticket #40 与 A53。

多份 DBC 要**取并集**而不是挑一份：这台车把 CAN 布局拆成了 13 份小 DBC，任何单独一份
只覆盖 0–22% 的帧，只挑一份会让用户后来补上的 DBC 一条都不参与解码。

三条硬约束（都有实测支撑，写在票里）：

1. **零阶保持到主时间基**。CAN 的更新率是 11.04 / 16.8 / 29.85 / 42.5 / 43.0 / 47.83 /
   66.6 / 69.0 / 75.0 / 76.25 Hz，**没有一个能整除 100 Hz**。若把"源 43.5 Hz、目标
   100 Hz"交给 ``channels.hold_factor``，它算出来是 ``round(100/43.5)=2``（当成 50 Hz）：
   曲线会整体错位且不报错。所以这里**导入时**就把每帧的值保持到主时间基的网格上，
   下游拿到的列本来就在主时间基上。
2. **真实更新率进元数据**。保持之后的列是 100 Hz 的网格，但"这条胎温 43.5 Hz 才更新
   一次"是物理事实，界面上要看得见（``Channel.update_rate``）。
3. **DBC 的 DLC 不算数**。任何地方都用日志里那一帧**实际的字节数**（实测 ``0x66D``：
   DBC 写 4、日志发 8）。

帧 -> 通道的换算在 :mod:`i3pro.dbc` 里，本模块只管"文件、时间轴、报告"。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import cache, dbc as dbcmod, derive as derivemod, ld as ldmod, sidecar
from .csvlog import CsvSession

__all__ = [
    "CanSession", "DEFAULT_ROLES", "GROUP_TOLERANCE_S", "GROUP_WINDOW_S",
    "can_roles", "dbc_directory", "group_recordings", "looks_like_frames",
    "read_can_session", "summarise", "write_can_map",
]

#: 表头 -> 角色。**列名可以改**（换一个 CAN 工具导出就换名字），所以这张表是默认值，
#: 真正的取值来自 ``<场次>.can.json`` 侧车（见 :func:`can_roles`）。
DEFAULT_ROLES = {
    "index": "序号",
    "wall": "系统时间",
    "time": "时间标识",
    "bus": "CAN通道",
    "id": "ID号",
    "frame": "帧类型",
    "format": "帧格式",
    "protocol": "CAN类型",
    "length": "长度",
    "data": "数据",
}

#: 两份连续记录之间"还能算同一场"的墙钟间隔上限（秒）。实测两次切分分别是
#: 26 µs 与 249 µs——记录器在恰好 1,000,000 帧处切文件，中间没有停。
GROUP_WINDOW_S = 1.0

#: 墙钟差与相对时钟差必须一致到什么程度（秒）。相对时钟被归零的那几份文件会在这里
#: 露馅：墙钟只差几分钟，而相对时钟从 412 s 掉回 0 s。
GROUP_TOLERANCE_S = 0.05

#: 一份日志最多认几条总线（``ch1``…``ch8`` 这种写法）。只用来给"帧 ID × 座位号"
#: 编个互不相同的整数键：超过这个数就当成没有总线（报告会退化成不区分总线）。
MAX_BUSES = 64

#: OBD/UDS 诊断请求与响应的 ID 区间（0x7DF 起）。**它不等于"这段里都不是车辆数据"**：
#: 实测 S-Motion Correvit 传感器就发在 0x7E0–0x7E8（ticket #42 补上那份 DBC 之后，
#: 这 6 个 ID 解出了 21 条通道）。所以这里只给"**没有 DBC** 的 ID"一个提示，措辞也
#: 只能是"可能"——真正的判据永远是"有没有 DBC 解得开"。
DIAGNOSTIC_IDS = range(0x7DF, 0x7E8)


def _kind(name: str = "canmap"):
    return sidecar.kind_of(name)


def dbc_directory(session_path: str | Path) -> Path:
    """DBC 放哪：跟数据放在一起的 ``dbc/``（实测 ``i2pro_data/dbc/``）。"""
    return Path(session_path).parent / "dbc"


def default_dbc_directories(session_path: str | Path) -> list[Path]:
    """没指定目录时按顺序找这三处：场次旁边 -> 仓库里的 ``i2pro_data/dbc`` -> 当前目录。

    原始帧日志实测放在 ``can_data/``，而 DBC 放在 ``i2pro_data/dbc/``——它们不在
    一起，所以"只看场次旁边"会在命令行里直接失败。
    """
    path = Path(session_path)
    candidates = [
        dbc_directory(path),
        Path(__file__).resolve().parents[2] / "i2pro_data" / "dbc",
        Path.cwd() / "i2pro_data" / "dbc",
    ]
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _decode_text_line(raw: bytes) -> str:
    return raw.decode("gbk", errors="replace")


def read_header(path: str | Path) -> list[str]:
    """第一行表头（GBK）。"""
    with Path(path).open("rb") as handle:
        return [cell.strip() for cell in _decode_text_line(handle.readline()).rstrip("\r\n").split(",")]


def looks_like_frames(path: str | Path) -> bool:
    """这是原始 CAN 帧表吗？

    判据是**表头特征**（不是文件名）：至少要认出时间、ID、数据三列，而且第二列带
    Excel 写坏的那个 ``="`` 前缀。认不出就当普通通道表走 CSV 那条路——宁可报"没有
    时间列"，也不能把一张通道表按帧解。
    """
    path = Path(path)
    if path.suffix.lower() != ".csv":
        return False
    try:
        header = read_header(path)
    except OSError:
        return False
    if len(header) < 8:
        return False
    wanted = {DEFAULT_ROLES["time"], DEFAULT_ROLES["id"], DEFAULT_ROLES["data"]}
    if not wanted.issubset(set(header)):
        return False
    try:
        with path.open("rb") as handle:
            handle.readline()
            first = _decode_text_line(handle.readline())
    except OSError:
        return False
    fields = first.split(",")
    return len(fields) >= 10 and fields[1].startswith('="')


def can_roles(session_path: str | Path, overrides: dict | None = None) -> dict[str, str]:
    """这一批帧表的列角色：默认值 <- 侧车 <- 调用方给的覆盖。"""
    stored = sidecar.read("canmap", session_path) or {}
    roles = dict(DEFAULT_ROLES)
    roles.update({k: v for k, v in (stored.get("roles") or {}).items() if v})
    roles.update({k: v for k, v in (overrides or {}).items() if v})
    return roles


def write_can_map(session_path: str | Path, drop: tuple[str, ...] = (), **values) -> Path:
    """把这次导入的选择写进侧车：列角色、用了哪份 DBC、主时间基、并场与否。

    这是"重开可复现"的那一半：同样的输入文件 + 同样的侧车，必须得到同样的场次。
    ``drop`` 里的键会被删掉——``values`` 里给 ``None`` 是"别动"（保留侧车里已有的
    值），而"这一条现在不成立了"要能真的拿掉（例如改成并集之后那条旧的 ``dbc``）。
    """
    stored = sidecar.read("canmap", session_path) or {}
    stored.update({k: v for k, v in values.items() if v is not None})
    for key in drop:
        stored.pop(key, None)
    return sidecar.write("canmap", session_path, stored)


def _wall_seconds(text: str) -> float:
    """``="17:54:13.761949`` -> 当天秒数（跨零点时由调用方补一天）。"""
    cleaned = text.strip().lstrip("=").strip('"')
    parts = cleaned.split(":")
    if len(parts) != 3:
        return math.nan
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return math.nan


def _positions(header: list[str], roles: dict[str, str]) -> dict[str, int | None]:
    """表头里的列下标。**每次导入算一遍**，不是每行算一遍（差一秒级的时间）。"""
    def find(role: str):
        name = roles.get(role)
        return header.index(name) if name in header else None

    if roles.get("time") not in header or roles.get("id") not in header \
            or roles.get("data") not in header:
        raise ValueError(
            f"表头里找不到 {roles.get('time')} / {roles.get('id')} / {roles.get('data')} 这几列。"
            "换一个 CAN 工具导出时列名会变，把对应关系写进 "
            f"<场次>{_kind().suffix} 的 roles 里，或用 --role 列名=角色 指定。"
        )
    #: ``bus``（``CAN通道``）是可选的：日志里有几条总线（实测 ch1/ch2/ch3），
    #: 报告要能说清每条 ID 是从哪条总线收到的——同一条 ID 出现在两条总线上时，
    #: 现在按同一个 ID 解，那件事必须吵出来（见 read_can_session 的 notes）。
    positions = {role: find(role) for role in
                 ("time", "id", "data", "wall", "format", "length", "bus")}
    positions["width"] = max(
        index for index in (positions["time"], positions["id"], positions["data"]) if index is not None
    )
    return positions


def _sample_bytes(body: str) -> str:
    """数据列 ``x| 5a 64 46 23 …`` -> ``"5a 64 46 23 …"``（报告里给前 8 字节）。"""
    if "|" not in body:
        return ""
    return " ".join(body.split("|", 1)[1].split()[:8])


def _row(cells: list[str], roles: dict[str, str], positions: dict) -> tuple | None:
    """一行 -> ``(相对时间, 帧 ID, 扩展帧?, 载荷, 墙钟秒)``；读不出来返回 ``None``。"""
    if len(cells) <= positions["width"]:
        return None
    try:
        moment = float(cells[positions["time"]])
        frame_id = int(cells[positions["id"]], 16)
    except ValueError:
        return None
    fmt = positions["format"]
    extended = bool(fmt is not None and len(cells) > fmt and "扩展" in cells[fmt])
    body = cells[positions["data"]]
    payload = bytes.fromhex(body.split("|", 1)[1]) if "|" in body else b""
    wall = positions["wall"]
    wall_seconds = _wall_seconds(cells[wall]) if wall is not None and len(cells) > wall else math.nan
    return (moment, frame_id, extended, payload, wall_seconds)


def summarise(path: str | Path, roles: dict[str, str] | None = None) -> dict:
    """一份帧表的首尾（只看第一行和最后一行，不整份扫）。

    并场判断只需要这四个时间：第一次/最后一次的墙钟与相对时钟。
    """
    path = Path(path)
    header = read_header(path)
    roles = dict(DEFAULT_ROLES, **(roles or {}))
    positions = _positions(header, roles)
    with path.open("rb") as handle:
        handle.readline()
        first = _decode_text_line(handle.readline())
        handle.seek(0, 2)
        size = handle.tell()
        # 从尾部回退找一个完整的行：文件末尾可能有换行。
        step = 4096
        tail = b""
        position = size
        while position > 0 and tail.count(b"\n") < 2:
            position = max(0, position - step)
            handle.seek(position)
            tail = handle.read(size - position) + tail
            step *= 2
    lines = [line for line in tail.split(b"\r\n") if line.strip()]
    last = _decode_text_line(lines[-1]) if lines else ""
    head = _row(first.rstrip("\r\n").split(","), roles, positions)
    end = _row(last.split(","), roles, positions)
    if head is None or end is None:
        raise ValueError(
            f"{path.name}: 首行或末行不是一帧数据。"
            "确认这份文件是原始 CAN 帧表（而不是解码后的通道表），"
            "列名不一样时用侧车的 roles 指定。"
        )
    return {
        "path": path,
        "first_t": head[0], "last_t": end[0],
        "first_wall": head[4], "last_wall": end[4],
        "bytes": size,
    }


def _wall_day(path: str | Path) -> str:
    """文件名里的日期（``2026_10_03_173345_ID0001.csv`` → ``2026-10-03``）；认不出给空串。

    墙钟那一列（``系统时间``）只有**时刻**（``17:33:45.395073``），没有日期。所以只按它
    排序时，跨天的日志会互相穿插——实测：data 目录里多了别的日期的日志之后，
    ``10-05 17:52`` 那份正好插进 ``10-03 17:47`` 与 ``10-03 17:54`` 之间，把本该并成
    一场的两卷切开了（侧边栏里于是多出一场）。日期从文件名拿：那是记录仪自己的命名。
    """
    return _stamp_from_name(Path(path).name)[0]


def group_recordings(
    paths: list[str | Path],
    window_s: float = GROUP_WINDOW_S,
    tolerance_s: float = GROUP_TOLERANCE_S,
) -> list[dict]:
    """把"记录器在 100 万帧处切开"的文件并回一次记录（ticket #39）。

    判据是**两个时钟同时接上**：墙钟的间隔在 ``window_s`` 之内，而且
    ``相对时钟的增量``与``墙钟的间隔``一致（``tolerance_s`` 以内）。只看墙钟会把
    两次相隔几分钟的记录并到一起；只看相对时钟会把"归零重开"的并到一起。
    排序与判据都要带上**文件名里的日期**（见 :func:`_wall_day`）：墙钟列只有时刻。
    """
    summaries = sorted(
        (summarise(path) for path in paths),
        key=lambda item: (
            _wall_day(item["path"]),
            item["first_wall"] if not math.isnan(item["first_wall"]) else item["first_t"],
        ),
    )
    groups: list[dict] = []
    for item in summaries:
        joined = False
        if groups:
            previous = groups[-1]
            last = previous["items"][-1]
            wall_gap = item["first_wall"] - last["last_wall"]
            clock_gap = (item["first_t"] - last["last_t"]) - wall_gap
            # 名字里没日期时退回原来的判据（只看时钟）——不因为"认不出日期"就不并。
            day_a, day_b = _wall_day(last["path"]), _wall_day(item["path"])
            if (
                (not day_a or not day_b or day_a == day_b)
                and
                not math.isnan(wall_gap) and not math.isnan(clock_gap)
                and 0 <= wall_gap <= window_s and abs(clock_gap) <= tolerance_s
            ):
                previous["items"].append(item)
                previous["evidence"].append(
                    f"{item['path'].name} 接在 {last['path'].name} 后面："
                    f"墙钟差 {wall_gap * 1000:.3f} ms，相对时钟差与它一致"
                    f"（相差 {clock_gap * 1000:.3f} ms）"
                )
                joined = True
        if not joined:
            groups.append({"items": [item], "evidence": []})
    return groups


def coverage_of(databases: list[tuple[str, dbcmod.Database]], counts: dict[int, int],
                extended_ids: set[int]) -> dict[str, int]:
    """每份 DBC **各自**覆盖了多少帧。

    这个数字有两个用处：进导入报告（"为什么用了它 / 它一条都对不上"要能复核），
    以及同一条 ID 被两份 DBC 定义成不同样子时当裁判（ticket #40）。
    """
    return {
        name: sum(frames for frame_id, frames in counts.items()
                  if database.covers(frame_id, frame_id in extended_ids))
        for name, database in databases
    }


def _dbc_files(directory: Path) -> list[tuple[str, Path]]:
    """目录（**含子目录**）下的 DBC：``(报告里用的名字, 路径)``。

    名字带相对路径（``261004/Xsens_MTi_600_series.dbc``）——同一份 DBC 可能出现在
    不止一层里，报告上要分得清用的是哪一份；顶层文件仍然只写文件名，老报告里的
    标签不会因为这次改动而变。排序稳定，同样的目录永远给同样的顺序。
    """
    out: list[tuple[str, Path]] = []
    # 顶层先出、子目录后出：同一份 DBC 在两处都有（实测 ``TH.dbc`` 与
    # ``261004/ECU_To_MoTeC/TH.dbc`` 同哈希）时，报告上用的还是那个短名字。
    paths = sorted(directory.rglob("*.dbc"),
                   key=lambda path: (len(path.relative_to(directory).parts), str(path)))
    for path in paths:
        relative = path.relative_to(directory)
        label = path.name if relative.parent == Path(".") else relative.as_posix()
        out.append((label, path))
    return out


def load_databases(
    directories: list[Path] | Path, only: str | None = None
) -> tuple[Path, list[tuple[str, dbcmod.Database]]]:
    """在候选目录里找 DBC：``only`` 指定时找那一份，否则把找到的**并起来**。

    三条规矩（第一条是 ticket #42 修的）：

    * **子目录也算**：车队按批次分文件夹（实测 ``i2pro_data/dbc/261004/``），旧版
      只 ``glob("*.dbc")`` 顶层，新加的 DBC 一条都不参与解码——实测同一份日志
      **61 条通道 → 124 条**（S-Motion 的地面速度、Xsens MTi 的姿态/经纬度、
      ``sw260425`` 的方向盘转角都在里面）；
    * **同一个目录里逐字节相同的只留一份**（实测 ``Sensors.dbc`` 与
      ``261004/Sensors10.4.dbc`` 同哈希、``261004/ECU_To_MoTeC/*`` 与顶层那几份
      同哈希），报告里不会出现两份一模一样的贡献；
    * **候选目录之间仍然是"第一个有 DBC 的目录说了算"**：显式给一个 ``dbc_dir``
      就应该能隔离出一套 DBC 来（固定用某一份、测试里造冲突都靠它）。要找子目录里的
      文件，递归已经覆盖了，不需要再并目录。

    返回 ``(用了哪个目录, [(报告里的名字, 库)])``；名字是**相对那个目录**的路径，
    所以报告算 sha256 时 ``目录 / 名字`` 永远拼得对。
    """
    directory, sources = dbc_sources(directories, only)
    return directory, [
        (label, dbcmod.parse(path.read_text(encoding="utf-8", errors="replace"),
                             source=label))
        for label, path in sources
    ]


def dbc_sources(
    directories: list[Path] | Path, only: str | None = None
) -> tuple[Path, list[tuple[str, Path]]]:
    """:func:`load_databases` 的发现那一半：``(用了哪个目录, [(名字, 路径)])``。

    只找文件、不解析——"侧车里的摘要还算不算数"要拿这份列表算指纹（见
    :func:`cached_summary`），为此把 18 份 DBC 再解析一遍是浪费。
    """
    if isinstance(directories, (str, Path)):
        directories = [Path(directories)]
    tried: list[Path] = []
    for directory in directories:
        directory = Path(directory)
        if directory in tried:
            continue
        tried.append(directory)
        if not directory.is_dir():
            continue
        found: list[tuple[str, Path]] = []
        seen: set[str] = set()
        for label, path in _dbc_files(directory):
            if only and path.name != only:
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            found.append((label, path))
        if found:
            return directory, found
    if only:
        raise ValueError(
            f"侧车里写的 DBC {only} 不在任何候选目录里"
            f"（找过：{'、'.join(str(d) for d in tried)}，含子目录）。"
            "把这份 DBC 放进去，或者把侧车里的 dbc 字段改成实际的文件名。"
        )
    raise ValueError(
        "找不到任何 DBC。把本车的 DBC 放进数据目录的 dbc/ 里（含子目录）"
        f"（找过：{'、'.join(str(d) for d in tried)}）——"
        "没有 DBC 就只能看到一堆读不懂的字节。"
    )


#: 指纹怎么算只有一处（:mod:`i3pro.cache`，ticket #49）。
_stamp = cache.file_stamp


def _dbc_stamp(directories, only: str | None) -> list | None:
    """当前这套 DBC 的 ``[[名字, sha256], …]``；一份都没有时给 ``None``。"""
    try:
        _directory, sources = dbc_sources(directories, only)
    except ValueError:
        return None
    # 内容指纹：改一行 DBC 就得让摘要重算，而 mtime 可能被工具保留。
    return [[label, cache.content_stamp(path)[1]] for label, path in sources]


def cache_summary(
    session_path: str | Path, facts: dict, *, names: list[str],
    directories, only: str | None = None,
) -> None:
    """把侧边栏要的摘要写进 ``.can.json``（写不进去也不吵：缓存不是数据）。

    侧边栏要为每一场算摘要：``.ld`` 读个头就行，**CAN 场次却要整场解码一次**。
    实测 41 场数据下 ``SessionLibrary.listing()`` 要 **32.3 s**（第二次走内存缓存
    0.01 s）——队友打开页面就得等半分钟。所以把摘要连同"它当时是基于什么算的"
    一起写下来：源文件 / DBC 文件 / 圈侧车三样都没变就直接用，变了一样就重算。
    """
    here = Path(session_path).parent
    laps = sidecar.path_of("laps", session_path)
    stamp = {
        "sources": [_stamp(here / name) for name in names],
        "dbc": _dbc_stamp(directories, only),
        "laps": _stamp(laps) if laps.exists() else None,
    }
    try:
        write_can_map(session_path, summary={"facts": facts, "stamp": stamp})
    except OSError:
        pass          # 只读目录 / 权限不够：那就每次重算，别让列表挂掉


def cached_summary(session_path: str | Path, directories) -> dict | None:
    """侧车里那份摘要还算不算数；不算（或没有）就返回 ``None``，让调用方重算。"""
    stored = sidecar.read("canmap", session_path) or {}
    block = stored.get("summary") or {}
    facts, stamp = block.get("facts"), block.get("stamp")
    if not isinstance(facts, dict) or not isinstance(stamp, dict):
        return None
    sources = stamp.get("sources")
    if not isinstance(sources, list) or not sources:
        return None
    here = Path(session_path).parent
    if [_stamp(here / str(row[0])) for row in sources] != sources:
        return None                      # 帧表（或它的分卷）动过了
    pinned = stored.get("dbc") if stored.get("dbc_mode") == "file" else None
    if _dbc_stamp(directories, pinned if isinstance(pinned, str) else None) \
            != stamp.get("dbc"):
        return None                      # DBC 目录动过了
    laps = sidecar.path_of("laps", session_path)
    if (_stamp(laps) if laps.exists() else None) != stamp.get("laps"):
        return None                      # 圈 / 信标改过了
    return facts


@dataclass
class CanSession(CsvSession):
    """CAN 场次：与 CSV 场次同形状，多一份按帧算出来的导入报告。

    它继承 :class:`~i3pro.csvlog.CsvSession` 是**故意的**：下游（画图 / 散点 / 报表 /
    导出 / 数学通道）只认那套访问器，多一种会话形状就要在每一处加分支——ticket #18
    刚把这条缝收干净。
    """

    #: 帧数、ID 数、覆盖率、未定义 ID、用了哪份 DBC……（界面与 CLI 都读它）
    can: dict = field(default_factory=dict)

    def metadata(self) -> dict:
        meta = super().metadata()
        meta["format"] = "can"
        meta["can"] = self.can
        meta["file_size"] = self.can.get("bytes", meta.get("file_size", 0))
        return meta


def _scan(paths: list[Path], roles: dict[str, str], wanted: set[int], offset: float) -> dict:
    """扫一遍所有帧。

    只做三件事，因为它们各自都贵，能省就省：

    * **计数**——每个 ID 多少帧（所有帧都要）；
    * **留下载荷**——只有 ``wanted``（候选 DBC 里出现过的 ID）才解析时间与字节。
      实测 75.4% 的帧不属于任何 DBC，为它们做 ``float`` + ``fromhex`` 是白花钱；
    * **样例字节**——没被覆盖的 ID 各留一次前 8 字节，进导入报告。

    时间轴按第一帧归零（记录器的相对时钟可能从 354.26 开始，也可能归零）。
    """
    counts: dict[int, int] = {}
    #: 总线用**整数座位号**记：``frame_id * MAX_BUSES + 座位`` → 帧数。
    #: 每帧一次整数键的加法与字典操作，比"嵌套字典 + 字符串键"快（实测 100 万帧
    #: 省 0.2 s）；座位号到名字的映射每份文件只建一次。
    bus_counts: dict[int, int] = {}
    bus_index: dict[str, int] = {}
    bus_names: list[str] = []
    per_id: dict[int, list] = {}
    samples: dict[int, str] = {}
    extended_ids: set[int] = set()
    for path in paths:
        header = read_header(path)
        positions = _positions(header, roles)
        time_at, id_at, data_at = positions["time"], positions["id"], positions["data"]
        format_at, width = positions["format"], positions["width"]
        bus_at = positions["bus"]
        with path.open("r", encoding="gbk", errors="replace", newline="") as handle:
            handle.readline()
            for line in handle:
                cells = line.split(",")
                if len(cells) <= width:
                    continue
                try:
                    frame_id = int(cells[id_at], 16)
                except ValueError:
                    continue
                counts[frame_id] = counts.get(frame_id, 0) + 1
                if bus_at is not None and len(cells) > bus_at:
                    raw_bus = cells[bus_at]
                    seat = bus_index.get(raw_bus)
                    if seat is None:
                        seat = len(bus_names)
                        bus_index[raw_bus] = seat
                        bus_names.append(raw_bus.strip() or "?")
                    key = frame_id * MAX_BUSES + seat
                    bus_counts[key] = bus_counts.get(key, 0) + 1
                if format_at is not None and "扩展" in cells[format_at]:
                    extended_ids.add(frame_id)
                body = cells[data_at]
                # 每条 ID 都留一个样例字节：报告里"未解码"的行也要能看见长什么样，
                # 否则读者分不清"这条没人定义"和"这条定义了但这次没解"（真发生过：
                # 只给 `wanted` 之外的 ID 存样例，于是别的 DBC 里定义、这次没解的
                # 那几条在报告里是空白的）。
                if frame_id not in samples:
                    samples[frame_id] = _sample_bytes(body)
                if frame_id not in wanted:
                    continue
                try:
                    moment = float(cells[time_at])
                except ValueError:
                    continue
                bucket = per_id.get(frame_id)
                if bucket is None:
                    bucket = per_id[frame_id] = [[], []]
                bucket[0].append(moment - offset)
                bucket[1].append(bytes.fromhex(body.split("|", 1)[1]))
    return {"counts": counts, "per_id": per_id, "samples": samples,
            "extended_ids": extended_ids,
            "bus_counts": bus_counts, "bus_names": bus_names}


def _buses_of(frame_id: int, bus_counts: dict[int, int],
              bus_names: list[str]) -> list[str]:
    """这条 ID 是从哪几条总线收到的（按名字排序）；日志里没有总线列就是空的。"""
    base = frame_id * MAX_BUSES
    return sorted(bus_names[seat] for seat in range(len(bus_names))
                  if bus_counts.get(base + seat))


def _bus_totals(bus_counts: dict[int, int], bus_names: list[str]) -> dict[str, int]:
    """每条总线各收了多少帧（记录仪挂几条总线时用得上，实测 ch1/ch2/ch3）。"""
    totals: dict[str, int] = {}
    for key, count in bus_counts.items():
        name = bus_names[key % MAX_BUSES]
        totals[name] = totals.get(name, 0) + count
    return totals


@dataclass
class _Decoded:
    """一批帧解出来的东西：列 + 两套报告行 + 三种"没解成通道"的计数。

    "哪条信号没解出来、为什么"是 CAN 导入最容易出错的地方，所以它跟列一起返回，
    而不是让调用方自己去猜（ticket #47 从 424 行的 read_can_session 里拆出来）。
    """

    columns: dict[str, np.ndarray]
    channels: list[dict]
    report: list[dict]
    #: 某一帧的字节数不够，整条信号跳过
    too_short: int = 0
    #: 多路复用里"这次一帧都没有"的分支（不是解码失败，是这一路没发）
    empty_branches: list[dict] = field(default_factory=list)
    #: 多路复用里"选择子取值不在 DBC 里"的帧（跳过了，但要能看见）
    stray_selectors: list[dict] = field(default_factory=list)


def _decode_channels(
    database,
    covered_ids: list[int],
    per_id: dict[int, list],
    axis: np.ndarray,
    master_rate: float,
    origin: dict,
    extended_ids: set[int],
    bus_counts: dict[int, int],
    bus_names: list[str],
) -> _Decoded:
    """把每条被覆盖的报文解成"落在主时间基上的列" + 两套报告行。

    这里回答的是一个领域问题：**这 ID 上这些字节，本场次是哪些通道、值是多少**。
    多路复用（`M` / `m<n>`）按选择子分路：同一个 ID 上不同分支的帧各归各的列，
    各自零阶保持到主时间基（ticket #43）。

    撞名（实测 `Channel_0` 同时出现在两条报文里）用报文名当命名空间——不然两条通道
    会互相盖住，图上只剩一条。
    """
    seen_names: dict[str, list[str]] = {}
    for frame_id in covered_ids:
        message = database.find(frame_id, frame_id in extended_ids)
        for signal in message.signals:
            seen_names.setdefault(signal.name, []).append(message.name)

    decoded = _Decoded(columns={}, channels=[], report=[])
    for frame_id in covered_ids:
        message = database.find(frame_id, frame_id in extended_ids)
        moments, payloads = per_id.get(frame_id, ([], []))
        if not moments:
            continue
        order = np.argsort(np.asarray(moments, dtype=np.float64), kind="stable")
        times = np.asarray(moments, dtype=np.float64)[order]
        payloads = [payloads[index] for index in order]
        matrix = _payload_matrix(payloads)
        # 多路复用：先算一次选择子，每条信号只认**自己那一路的帧**。按普通信号解会把
        # 别路的字节当成自己的值；而"给不匹配的格子填 NaN"会让 100 Hz 网格上一半是
        # 空的（两条分支交替发），那不是数据的样子。
        selector = None
        selector_signal = dbcmod.multiplexer_of(message)
        if selector_signal is not None:
            raw = _signal_values(selector_signal, matrix)
            if raw is not None:
                scale = selector_signal.factor or 1.0
                selector = np.rint(
                    (raw - selector_signal.offset) / scale
                ).astype(np.int64)
                # 选择子取到 DBC 里没有的分支：这几帧不属于任何一路。**不报错**
                # （那会把整条报文的所有分支都拖下水），但要计数并报出来。
                known = {value for value in
                         (dbcmod.branch_of(one) for one in message.signals)
                         if value is not None}
                stray = ~np.isin(selector, sorted(known) or [-1])
                if stray.any():
                    decoded.stray_selectors.append({
                        "message": message.name, "frames": int(stray.sum()),
                        "values": np.unique(selector[stray]).tolist()[:6],
                    })
        for signal in message.signals:
            values = _signal_values(signal, matrix)
            if values is None:
                decoded.too_short += 1
                continue
            own_times = times
            branch = dbcmod.branch_of(signal)
            if branch is not None:
                if selector is None:
                    decoded.too_short += 1
                    continue
                keep = selector == branch
                if not keep.any():
                    decoded.empty_branches.append({
                        "message": message.name, "signal": signal.name,
                        "branch": branch,
                    })
                    continue
                values, own_times = values[keep], times[keep]
            index = np.searchsorted(own_times, axis, side="right") - 1
            leading = int(index[0] + 1) if index.size and index[0] >= 0 else 0
            index = np.clip(index, 0, len(values) - 1)
            name = signal.name
            if len(seen_names.get(signal.name, [])) > 1:
                name = f"{message.name}.{signal.name}"
            decoded.columns[name] = values[index]
            update_rate = _rate_of(own_times)
            # 显示精度跟着 factor 走：factor 0.0025 的胎温要看到 4 位小数，
            # factor 1 的计数一位都不需要。
            decimals = max(0, min(6, int(math.ceil(-math.log10(abs(signal.factor)))))) \
                if signal.factor else 0
            # 这条通道来自哪份 DBC（并集之后必须能回答，否则"少了一条"没法查）。
            source = origin.get((message.extended, message.frame_id), "")
            bus_text = "/".join(_buses_of(message.frame_id, bus_counts, bus_names))
            decoded.channels.append({
                "name": name, "message": message.name, "signal": signal.name,
                "unit": signal.unit, "update_rate": update_rate,
                "samples": int(own_times.size),
                "leading_gap_s": (float(axis[leading - 1] - own_times[0])
                                  if leading > 1 and own_times.size else 0.0),
                "decimals": decimals, "dbc": source, "branch": branch,
                "bus": bus_text,
            })
            decoded.report.append({
                "column": f"0x{message.frame_id:X}", "status": "通道", "name": name,
                "matched_by": f"DBC:{source}", "unit": signal.unit,
                "rate": master_rate, "rate_from": "主时间基",
                "samples": int(axis.size), "update_rate": update_rate,
                "message": message.name, "decimals": decimals, "dbc": source,
                "bus": bus_text,
            })
    return decoded


def _undecoded_rows(undecoded_counts: dict[int, int], samples: dict[int, str],
                    duration: float, bus_counts: dict[int, int],
                    bus_names: list[str]) -> list[dict]:
    """读不懂的 ID 一张表：帧数从多到少，带总线、一帧样例字节与"可能是诊断"的提示。"""
    def rate(count: int) -> float:
        return round(count / duration, 2) if duration > 0 else 0.0

    return sorted(
        (
            {
                "id": f"0x{frame_id:X}", "frames": count, "rate": rate(count),
                "sample": samples.get(frame_id, ""),
                "diagnostic": frame_id in DIAGNOSTIC_IDS,
                "bus": "/".join(_buses_of(frame_id, bus_counts, bus_names)),
            }
            for frame_id, count in undecoded_counts.items()
        ),
        key=lambda row: -row["frames"],
    )


def _dbc_contribution(databases, origin: dict, counts: dict[int, int],
                      extended_ids: set[int], covered_by: dict[str, int],
                      channel_rows: list[dict], directory: Path) -> list[dict]:
    """每份 DBC 贡献了哪些 ID / 多少帧 / sha256：可复现，也能一眼看出"哪份没用上"。"""
    by_file: list[dict] = []
    for name, entry in databases:
        owned = sorted(
            f"0x{key[1]:X}" for key, source in origin.items() if source == name
        )
        hits = [frame_id for frame_id in counts
                if entry.covers(frame_id, frame_id in extended_ids)]
        by_file.append({
            "file": name,
            "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest(),
            "messages": len(entry.messages),
            "signals": entry.signal_count,
            "covered_frames": covered_by.get(name, 0),
            "covered_ids": sorted(f"0x{frame_id:X}" for frame_id in hits),
            # 并集里真正归它名下的 ID：与 hit 不同——撞 ID 时只有胜出的那份算数
            "used_ids": owned,
            "channels": sum(1 for row in channel_rows if row["dbc"] == name),
        })
    return by_file


def _report_notes(
    *,
    legacy_pin: str | None,
    conflicts: list[dict],
    databases: list,
    too_short: int,
    empty_branches: list[dict],
    stray_selectors: list[dict],
    bus_totals: dict[str, int],
    by_file: list[dict],
    counts: dict[int, int],
    extended_ids: set[int],
    bus_counts: dict[int, int],
    bus_names: list[str],
) -> list[str]:
    """导入报告里那些**必须吵出来**的话。

    静默的两种下场这里都堵上了：① 少了东西不说（并集里哪份 DBC 一条都没用上、
    哪条分支这次没发）；② 混了东西不说（同一条 ID 出现在两条总线上——DBC 只按 ID
    认报文，现在会把两边混着解）。
    """
    notes: list[str] = []
    if legacy_pin:
        notes.append(
            f"侧车里记着旧版本的「用了 {legacy_pin}」，本次按并集解"
            "（旧版本每次导入都会自动写这一条，和「点名固定一份」长得一样）。"
            "要固定成一份：把侧车的 dbc 写成文件名，并把 dbc_mode 设成 \"file\"。"
        )
    if not conflicts and len(databases) > 1:
        notes.append(f"{len(databases)} 份 DBC 按并集解码，没有一条 ID 被重复定义。")
    for row in conflicts:
        notes.append(f"ID {row['id']} 有不止一份定义：{row['reason']}")
    if too_short:
        notes.append(f"有 {too_short} 条信号因为某一帧的字节数不够而整条跳过"
                     "（DBC 与日志可能不是同一版）。")
    if empty_branches:
        shown = "、".join(f"{row['message']}.{row['signal']}(第 {row['branch']} 路)"
                          for row in empty_branches[:4])
        notes.append(
            f"多路复用里有 {len(empty_branches)} 条分支信号这次一帧都没有"
            f"（{shown}{'…' if len(empty_branches) > 4 else ''}）——"
            "是这条报文这次没发那一路，不是解码失败。"
        )
    if stray_selectors:
        shown = "、".join(
            f"{row['message']} {row['frames']} 帧"
            f"（取值 {'/'.join(str(v) for v in row['values'])}）"
            for row in stray_selectors[:3]
        )
        notes.append(
            f"多路复用里有 {len(stray_selectors)} 条报文出现了 DBC 没定义的选择子分支"
            f"：{shown}——那几帧不属于任何一路，已经跳过（不是整条报文解不了）。"
        )
    # 总线（`CAN通道`）：记录仪可能同时挂着几条（实测 ch1/ch2/ch3）。**DBC 只按 ID
    # 认报文**，所以同一条 ID 出现在两条总线上时必须吵出来，不许静默合并。
    if len(bus_totals) > 1:
        shown = "、".join(
            f"{seat} {count:,} 帧"
            for seat, count in sorted(bus_totals.items(), key=lambda kv: -kv[1])
        )
        notes.append(f"这批日志有 {len(bus_totals)} 条总线：{shown}；"
                     "每条通道来自哪条总线写在通道表的「总线」一列。")
    shared = {frame_id: seats for frame_id in counts
              if len(seats := _buses_of(frame_id, bus_counts, bus_names)) > 1}
    if shared:
        shown = "、".join(
            f"0x{frame_id:X}（{'/'.join(seats)}）"
            for frame_id, seats in sorted(shared.items())[:5]
        )
        notes.append(
            f"有 {len(shared)} 条 ID 出现在**不止一条总线**上：{shown}。"
            "DBC 只按 ID 认报文，所以现在这几条是按同一个 ID 解、两边混在一起——"
            "确认它们是不是同一个东西；不是的话，把两条总线分开导。"
        )
    if len(databases) > 1:
        dead = [row["file"] for row in by_file if row["covered_frames"] == 0]
        if dead:
            notes.append(
                f"{len(dead)} 份 DBC 的 ID 在这批日志里一条都没出现"
                "（车上的布局和它不一致）：" + "、".join(dead) + "。"
            )
            # "布局对不上"与"记录仪根本没接那条总线"是两种原因，报告要说清是哪一种
            # （ticket #38 的验收点名了那份 dashboard DBC）。判据两条：这份 DBC 的
            # 报文**全是扩展帧**，而这批日志里**一帧扩展帧都没有**。
            if not extended_ids:
                by_entry = dict(databases)
                buses = [
                    name for name in dead
                    if by_entry.get(name) is not None
                    and by_entry[name].messages
                    and all(extended for extended, _frame in by_entry[name].messages)
                ]
                if buses:
                    # 按 DBC 文件里的写法印（扩展帧在 `BO_` 里带 0x80000000 标记），
                    # 这样用户能在自己的 DBC 里搜到这个号。
                    ids = sorted({
                        frame | (0x80000000 if extended else 0)
                        for name in buses
                        for extended, frame in by_entry[name].messages
                    })
                    span = f"0x{ids[0]:X}–0x{ids[-1]:X}"
                    notes.append(
                        f"其中 {'、'.join(buses)} 的报文**全是扩展帧**（{span}，"
                        f"共 {len(ids)} 个 ID），而这批日志里一帧扩展帧都没有——"
                        "不是布局对不上，是记录仪没接那条总线。"
                    )
    return notes


def read_can_session(
    paths: list[str | Path] | str | Path,
    *,
    dbc_dir: str | Path | None = None,
    rate: float | None = None,
    merge: bool = True,
    roles: dict[str, str] | None = None,
    dbc_file: str | None = None,
    write_sidecar: bool = True,
    use_sidecar: bool = True,
) -> CanSession:
    """一批原始帧表 -> 一个场次。

    ``merge`` 打开时先把连续记录并成一次（ticket #39，实测 9 份 -> 7 场）；
    列角色、DBC、主时间基都从侧车读，缺省值写在 :data:`DEFAULT_ROLES` 里。
    """
    given = [Path(paths)] if isinstance(paths, (str, Path)) else [Path(p) for p in paths]
    if not given:
        raise ValueError("没有给任何帧表文件。")
    first = given[0]
    # ``use_sidecar=False`` 就是"重新自动识别一遍"：不看这个场次上次存的选择。
    stored = (sidecar.read("canmap", first) or {}) if use_sidecar else {}
    roles = can_roles(first, roles) if use_sidecar else dict(DEFAULT_ROLES, **(roles or {}))
    if rate is None:
        try:
            rate = float(stored.get("rate") or 0) or None
        except (TypeError, ValueError):
            rate = None
    master_rate = float(rate or 100.0)
    if merge and len(given) == 1:
        # 单份文件也要看一眼同目录的邻居：切分后的第 1 份不是"一场"。
        siblings = sorted(p for p in first.parent.glob("*.csv") if looks_like_frames(p))
        if len(siblings) > 1 and first in siblings:
            groups = group_recordings(siblings)
            for group in groups:
                names = [item["path"] for item in group["items"]]
                if first in names:
                    given = names
                    break

    summaries = [summarise(path, roles) for path in given]
    if len(summaries) > 1:
        # 调用方给的文件顺序不一定对（拖进来一批就是随机的）：按墙钟排一遍，
        # 再要求它们**首尾相接**——中间断了还硬拼，会把两段之间的一段时间铺成
        # 一条平线，而且一声不吭。
        order = sorted(
            range(len(summaries)),
            key=lambda index: (
                summaries[index]["first_wall"]
                if not math.isnan(summaries[index]["first_wall"])
                else summaries[index]["first_t"]
            ),
        )
        summaries = [summaries[index] for index in order]
        given = [given[index] for index in order]
        first = given[0]
        for previous, following in zip(summaries, summaries[1:]):
            wall_gap = following["first_wall"] - previous["last_wall"]
            clock_gap = (following["first_t"] - previous["last_t"]) - wall_gap
            if not (0 <= wall_gap <= GROUP_WINDOW_S and abs(clock_gap) <= GROUP_TOLERANCE_S):
                raise ValueError(
                    f"{previous['path'].name} 与 {following['path'].name} 不是同一次记录"
                    f"（墙钟差 {wall_gap:.3f} s，相对时钟差与它相差 {clock_gap:.3f} s）。"
                    "一次只导入一次记录的文件，或者用 canlog.group_recordings 先分组——"
                    "中间断开的两段硬拼成一场，会让它们之间的那段变成一条平线。"
                )
    started = summaries[0]["first_t"]
    duration = float(summaries[-1]["last_t"] - started)
    if duration <= 0:
        raise ValueError(
            f"{first.name}: 时间跨度是 0，读不出可用的帧。确认列角色对不对"
            f"（侧车 {_kind().suffix} 的 roles，或 --role 列名=角色）。"
        )
    axis = np.arange(int(round(duration * master_rate)) + 1, dtype=np.float64) / master_rate

    # 侧车里的 ``dbc`` 只在**明确指名**时才算数。旧版本每次导入都会把"当时挑中的
    # 那一条"写进侧车，那种自动写的值和用户点名固定一份长得一模一样——所以加了
    # ``dbc_mode`` 把两者分开（ticket #40）。没有 ``dbc_mode`` 的旧侧车按并集解，
    # 并在报告里说明这件事，不静默改变行为。
    pinned = stored.get("dbc") if stored.get("dbc_mode") == "file" else None
    chosen_name = dbc_file or pinned
    legacy_pin = None if chosen_name else stored.get("dbc")
    candidates_dirs = default_dbc_directories(first)
    if dbc_dir:
        # 调用方给的目录优先（场次库知道自己的根目录在哪），再退回到默认那几处。
        given_dirs = list(dbc_dir) if isinstance(dbc_dir, (list, tuple)) else [dbc_dir]
        candidates_dirs = given_dirs + [d for d in candidates_dirs if d not in given_dirs]
    directory, databases = load_databases(candidates_dirs, only=chosen_name)
    wanted = {message.frame_id for _name, db in databases for message in db.messages_only}
    scan = _scan(given, roles, wanted, started)
    counts, per_id = scan["counts"], scan["per_id"]
    bus_counts = scan["bus_counts"]
    bus_names = scan["bus_names"]

    if not counts:
        raise ValueError(
            f"{first.name}: 一行帧都没读出来。确认列角色对不对"
            f"（侧车 {_kind().suffix} 的 roles，或 --role 列名=角色）。"
        )
    # 多份 DBC 取并集：这台车把 CAN 布局拆成 13 份小 DBC，单独任何一份都只覆盖
    # 0–22% 的帧。只挑一份的话，用户后来补的 DBC 一条都不参与解码（ticket #40）。
    covered_by = coverage_of(databases, counts, scan["extended_ids"])
    database, origin, conflicts = dbcmod.merge(databases, covered_by)

    # 并集覆盖了哪些 ID：其余留在报告里（实测仍有一半以上的帧读不懂）。
    covered_ids = [
        frame_id for frame_id in counts
        if database.covers(frame_id, frame_id in scan["extended_ids"])
    ]
    undecoded_counts = {frame_id: count for frame_id, count in counts.items()
                        if frame_id not in set(covered_ids)}
    # 解码（含多路复用分路）与两张报告表都在 _decode_channels 里——加一种新的解码规则
    # 只碰那一个函数，不用在 400 行的编排里找位置。
    decoded = _decode_channels(database, covered_ids, per_id, axis, master_rate,
                               origin, scan["extended_ids"], bus_counts, bus_names)
    columns, channel_rows, report = decoded.columns, decoded.channels, decoded.report
    too_short = decoded.too_short
    empty_branches = decoded.empty_branches
    stray_selectors = decoded.stray_selectors

    channels_list = [
        ldmod.Channel(
            name=row["name"], short_name=row["signal"][:8], unit=row["unit"],
            sample_rate=master_rate, sample_count=int(axis.size),
            data_offset=0, data_type=0, bytes_per_sample=0,
            multiplier=1, divider=1, decimals=row["decimals"], shift=0,
            channel_id=index, index=index,
            update_rate=row["update_rate"],
        )
        for index, row in enumerate(channel_rows)
    ]

    covered = set(covered_ids)
    covered_frames = sum(count for frame_id, count in counts.items() if frame_id in covered)
    total_frames = sum(counts.values())

    undecoded = _undecoded_rows(undecoded_counts, scan["samples"], duration,
                                bus_counts, bus_names)
    recording_evidence: list[str] = []
    if merge:
        for group in group_recordings(given) if len(given) > 1 else []:
            recording_evidence.extend(group["evidence"])
    stamp = _stamp_from_name(first.name)

    by_file = _dbc_contribution(databases, origin, counts, scan["extended_ids"],
                                covered_by, channel_rows, directory)

    bus_totals = _bus_totals(bus_counts, bus_names)
    # 报告里那些"要吵出来"的话集中在 _report_notes：加一条新的警告
    # 只碰那一个函数。
    notes = _report_notes(
        legacy_pin=legacy_pin, conflicts=conflicts, databases=databases,
        too_short=too_short, empty_branches=empty_branches,
        stray_selectors=stray_selectors, bus_totals=bus_totals, by_file=by_file,
        counts=counts, extended_ids=scan["extended_ids"],
        bus_counts=bus_counts, bus_names=bus_names,
    )

    can = {
        "frames": total_frames,
        "ids": len(counts),
        "covered_frames": covered_frames,
        #: 被并集覆盖的 ID（报告里"哪些读得懂"要能核对，测试也用它求并集）
        "covered_ids": [f"0x{frame_id:X}" for frame_id in sorted(covered_ids)],
        "coverage": round(covered_frames / total_frames, 4) if total_frames else 0.0,
        "covered_messages": len(covered_ids),
        "dbc": {
            "method": "file" if chosen_name else "union",
            "files": by_file,
            "messages": len(database.messages),
            "signals": database.signal_count,
            "skipped": list(database.skipped),
            "conflicts": conflicts,
            "pinned": chosen_name,
            "legacy_pin_ignored": legacy_pin,
        },
        "sources": [path.name for path in given],
        "merged": bool(merge and len(given) > 1),
        "merge_evidence": recording_evidence,
        "master_rate": master_rate,
        "bytes": sum(path.stat().st_size for path in given),
        "channels": channel_rows,
        "undecoded": undecoded,
        #: 多路复用里"这次一帧都没有"的分支（不是解码失败，是这一路没发）
        "empty_branches": empty_branches,
        #: 多路复用里"选择子取值不在 DBC 里"的帧（跳过了，但要能看见）
        "stray_selectors": stray_selectors,
        #: 每条总线各收了多少帧（记录仪挂几条总线时用得上，实测 ch1/ch2/ch3）
        "buses": bus_totals,
        "notes": notes,
    }
    session = CanSession(
        path=first, channels=channels_list, columns=columns, time=axis,
        sample_rate=master_rate, duration=duration,
        header={"rate_from": "主时间基（导入时零阶保持）", "metadata": {}},
        device="CAN", log_date=stamp[0], log_time=stamp[1],
        event_name=first.stem, report=report, can=can,
    )
    if write_sidecar:
        # 只记"这次是怎么解的"，不碰数据文件本身（ADR-0001）。
        # ``dbc`` 只在明确指名时写；写 None 会保留侧车里已有的值（write_can_map 跳过
        # None），所以并集时那条旧的 dbc 要显式 drop 掉——不然"旧侧车按并集解"的
        # 提示会一直跟着这一场（实测过）。
        write_can_map(
            first, roles=roles, dbc=chosen_name,
            dbc_mode="file" if chosen_name else "union", rate=master_rate,
            drop=() if chosen_name else ("dbc",),
        )
    # 距离轴是**算出来**的，不是写死的：拿到真正的场次之后再问一次"有没有速度源"。
    if derivemod.speed_channel(session) is None:
        notes.append(
            "这批日志里没有车速 / GPS 通道，所以没有距离轴：切圈、区段、圈差都用不了，"
            "画图、散点、直方图、频谱、数学通道、导出照常可用。"
        )
    else:
        notes.append(
            f"距离轴来源：{derivemod.speed_channel(session)}（速度积分）；"
            "这批日志没有 GPS，所以切圈要靠手工信标。"
        )
    return session


def _rate_of(times: np.ndarray) -> float:
    """这条报文的**实测**更新率（帧数 / 首尾时间跨度）。"""
    if times.size < 2:
        return 0.0
    span = float(times[-1] - times[0])
    return round((times.size - 1) / span, 2) if span > 0 else 0.0


def _payload_matrix(payloads: list[bytes]) -> np.ndarray:
    """这一串载荷排成 ``(帧数, 字节数)`` 的矩阵，给向量化解码用。"""
    widths = {len(payload) for payload in payloads}
    if len(widths) == 1:
        width = widths.pop()
        if width == 0:
            return np.zeros((len(payloads), 0), dtype=np.uint8)
        return np.frombuffer(b"".join(payloads), dtype=np.uint8).reshape(len(payloads), width)
    width = max(widths)
    matrix = np.zeros((len(payloads), width), dtype=np.uint8)
    for index, payload in enumerate(payloads):
        matrix[index, :len(payload)] = np.frombuffer(payload, dtype=np.uint8)
    return matrix


def _signal_values(signal, data: np.ndarray) -> np.ndarray | None:
    """一条信号在一批帧上的值，**按列算**。

    位序与 :func:`i3pro.dbc.raw_value` 逐位相同（那里是逐帧的参考实现，测试里两者
    互相对拍）。逐位循环写成 Python 的话，900 万次信号解码要好几秒；这里每个位
    位置只做一次数组运算。
    """
    if data.shape[1] == 0 or signal.length <= 0:
        return None
    count = data.shape[0]
    if signal.byte_order == "little":
        raw = np.zeros(count, dtype=np.uint64)
        for step in range(signal.length):
            index = signal.start_bit + step
            if index // 8 >= data.shape[1]:
                return None
            bit = (data[:, index // 8] >> np.uint8(index % 8)) & np.uint8(1)
            raw |= bit.astype(np.uint64) << np.uint64(step)
    else:
        # 锯齿编号 -> 从最高位开始数的线性位置（与 cantools 的换算一致）。
        start = 8 * (signal.start_bit // 8) + (7 - signal.start_bit % 8)
        raw = np.zeros(count, dtype=np.uint64)
        for step in range(signal.length):
            index = start + step
            if index // 8 >= data.shape[1]:
                return None
            bit = (data[:, index // 8] >> np.uint8(7 - index % 8)) & np.uint8(1)
            raw = (raw << np.uint64(1)) | bit.astype(np.uint64)
    if signal.signed:
        signed = raw.astype(np.int64)
        limit = 1 << (signal.length - 1)
        signed[signed >= limit] -= 1 << signal.length
        return signed.astype(np.float64) * signal.factor + signal.offset
    return raw.astype(np.float64) * signal.factor + signal.offset


def _stamp_from_name(name: str) -> tuple[str, str]:
    """``2026_10_03_173345_ID0001.csv`` -> ``("2026-10-03", "17:33:45")``。"""
    parts = Path(name).stem.split("_")
    if len(parts) >= 4 and parts[0].isdigit() and len(parts[0]) == 4:
        clock = parts[3]
        when = f"{parts[0]}-{parts[1]}-{parts[2]}"
        if len(clock) >= 6:
            return when, f"{clock[0:2]}:{clock[2:4]}:{clock[4:6]}"
        return when, ""
    return "", ""
