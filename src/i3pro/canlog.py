"""把**原始 CAN 帧表**读成 i3pro 的场次（ticket #38 / #39）。

日志长这样（GBK、CRLF，第 2 列被 Excel 写成了 ``="17:54:13.761949`` 这种半截引号，
**相对时间在第 3 列**，数据列是 ``x| 5a 64 46 23 55 00 7f 00``）::

    序号,系统时间,时间标识,CAN通道,ID号,帧类型,帧格式,CAN类型,长度,数据
    1000000,="17:54:13.761949,385.624895,ch1,0x27,数据帧,标准帧,CAN,8,x| 01 01 ...

产出与 ``.ld`` **同形状**的场次：进侧边栏、能画图、能加数学通道、能导出。距离轴 /
切圈 / 区段用不了——这批日志里**没有车速也没有 GPS**（实测 43 个 ID 全查过），
所以报告里会写明，而不是等用户画不出距离轴再来猜。

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

from . import dbc as dbcmod, ld as ldmod, sidecar
from .csvlog import CsvSession

__all__ = [
    "CanSession", "DEFAULT_ROLES", "GROUP_TOLERANCE_S", "GROUP_WINDOW_S",
    "can_roles", "choose_database", "dbc_directory", "group_recordings", "looks_like_frames",
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

#: OBD/UDS 诊断请求与响应的 ID 区间。实测那 6 个"各 37,252 帧"的 ID 全在这里面
#: （当时车上插着诊断电脑），分析时不该被当成车辆数据。
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


def write_can_map(session_path: str | Path, **values) -> Path:
    """把这次导入的选择写进侧车：列角色、用了哪份 DBC、主时间基、并场与否。

    这是"重开可复现"的那一半：同样的输入文件 + 同样的侧车，必须得到同样的场次。
    """
    stored = sidecar.read("canmap", session_path) or {}
    stored.update({k: v for k, v in values.items() if v is not None})
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
    positions = {role: find(role) for role in
                 ("time", "id", "data", "wall", "format", "length")}
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


def group_recordings(
    paths: list[str | Path],
    window_s: float = GROUP_WINDOW_S,
    tolerance_s: float = GROUP_TOLERANCE_S,
) -> list[dict]:
    """把"记录器在 100 万帧处切开"的文件并回一次记录（ticket #39）。

    判据是**两个时钟同时接上**：墙钟的间隔在 ``window_s`` 之内，而且
    ``相对时钟的增量``与``墙钟的间隔``一致（``tolerance_s`` 以内）。只看墙钟会把
    两次相隔几分钟的记录并到一起；只看相对时钟会把"归零重开"的并到一起。
    """
    summaries = sorted(
        (summarise(path) for path in paths),
        key=lambda item: (item["first_wall"] if not math.isnan(item["first_wall"]) else item["first_t"]),
    )
    groups: list[dict] = []
    for item in summaries:
        joined = False
        if groups:
            previous = groups[-1]
            last = previous["items"][-1]
            wall_gap = item["first_wall"] - last["last_wall"]
            clock_gap = (item["first_t"] - last["last_t"]) - wall_gap
            if (
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


def choose_database(databases: list[tuple[str, dbcmod.Database]], counts: dict[int, int],
                    extended_ids: set[int]) -> tuple[str, dbcmod.Database, list[dict]]:
    """一份日志可能配着好几份 DBC（仪表一份、传感器一份），选覆盖帧数最多的那份。

    返回 ``(文件, 库, 候选表)``：候选表要进导入报告——"为什么是它"必须能复核，
    尤其是那份覆盖 0 帧的（实测 dashboard DBC 全是扩展 ID，日志里没有扩展帧）。
    """
    scored: list[dict] = []
    best: tuple[str, dbcmod.Database] | None = None
    best_covered = -1
    for name, database in databases:
        covered = sum(
            frames for frame_id, frames in counts.items()
            if database.covers(frame_id, frame_id in extended_ids)
        )
        scored.append({"file": name, "covered_frames": covered, "messages": len(database.messages),
                       "signals": database.signal_count})
        if covered > best_covered:
            best_covered, best = covered, (name, database)
    if best is None:
        raise ValueError(
            "没有可用的 DBC。把本车的 DBC 放进数据目录的 dbc/ 里"
            "（例如 i2pro_data/dbc/Sensors.dbc），或者在侧车里用 dbc 指定文件名。"
        )
    return best[0], best[1], scored


def load_databases(
    directories: list[Path] | Path, only: str | None = None
) -> tuple[Path, list[tuple[str, dbcmod.Database]]]:
    """在候选目录里找 DBC：``only`` 指定时找那一份，否则读找到的第一个目录下的全部。

    返回 ``(用了哪个目录, [(文件名, 库)])``。候选目录是有顺序的：数据目录自己的
    ``dbc/`` 优先，其次是别处配置的（场次库会把每个数据根的 ``dbc/`` 都递进来——
    原始帧日志在 ``can_data/``，而 DBC 放在 ``i2pro_data/dbc/``）。
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
        if only:
            path = directory / only
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                return directory, [(path.name, dbcmod.parse(text, source=path.name))]
            continue
        found = [
            (path.name, dbcmod.parse(path.read_text(encoding="utf-8", errors="replace"),
                                     source=path.name))
            for path in sorted(directory.glob("*.dbc"))
        ]
        if found:
            return directory, found
    if only:
        raise ValueError(
            f"侧车里写的 DBC {only} 不在任何候选目录里"
            f"（找过：{'、'.join(str(d) for d in tried)}）。"
            "把这份 DBC 放进去，或者把侧车里的 dbc 字段改成实际的文件名。"
        )
    raise ValueError(
        "找不到任何 DBC。把本车的 DBC 放进数据目录的 dbc/ 里"
        f"（找过：{'、'.join(str(d) for d in tried)}）——"
        "没有 DBC 就只能看到一堆读不懂的字节。"
    )


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
    per_id: dict[int, list] = {}
    samples: dict[int, str] = {}
    extended_ids: set[int] = set()
    for path in paths:
        header = read_header(path)
        positions = _positions(header, roles)
        time_at, id_at, data_at = positions["time"], positions["id"], positions["data"]
        format_at, width = positions["format"], positions["width"]
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
                if format_at is not None and "扩展" in cells[format_at]:
                    extended_ids.add(frame_id)
                body = cells[data_at]
                if frame_id not in wanted:
                    if frame_id not in samples:
                        samples[frame_id] = _sample_bytes(body)
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
            "extended_ids": extended_ids}


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

    chosen_name = dbc_file or stored.get("dbc") or None
    candidates_dirs = default_dbc_directories(first)
    if dbc_dir:
        # 调用方给的目录优先（场次库知道自己的根目录在哪），再退回到默认那几处。
        given_dirs = list(dbc_dir) if isinstance(dbc_dir, (list, tuple)) else [dbc_dir]
        candidates_dirs = given_dirs + [d for d in candidates_dirs if d not in given_dirs]
    directory, databases = load_databases(candidates_dirs, only=chosen_name)
    wanted = {message.frame_id for _name, db in databases for message in db.messages_only}
    scan = _scan(given, roles, wanted, started)
    counts, per_id = scan["counts"], scan["per_id"]
    if not counts:
        raise ValueError(
            f"{first.name}: 一行帧都没读出来。确认列角色对不对"
            f"（侧车 {_kind().suffix} 的 roles，或 --role 列名=角色）。"
        )
    database_name, database, candidates = choose_database(databases, counts, scan["extended_ids"])
    digest = hashlib.sha256((directory / database_name).read_bytes()).hexdigest()

    # 选中的 DBC 覆盖了哪些 ID：其余留在报告里（实测 75.4% 的帧属于这一类）。
    covered_ids = [
        frame_id for frame_id in counts
        if database.covers(frame_id, frame_id in scan["extended_ids"])
    ]
    undecoded_counts = {frame_id: count for frame_id, count in counts.items()
                        if frame_id not in set(covered_ids)}
    columns: dict[str, np.ndarray] = {}
    channel_rows: list[dict] = []
    report: list[dict] = []
    too_short = 0
    seen_names: dict[str, list[str]] = {}
    for frame_id in covered_ids:
        message = database.find(frame_id, frame_id in scan["extended_ids"])
        for signal in message.signals:
            seen_names.setdefault(signal.name, []).append(message.name)

    for frame_id in covered_ids:
        message = database.find(frame_id, frame_id in scan["extended_ids"])
        if message.multiplexed:
            raise dbcmod.DbcError(
                f"报文 {message.name}（0x{message.frame_id:X}）用了多路复用，本轮不支持"
                "解码成通道：同一个报文里不同信号的字节位置取决于选择子的取值。"
                "下一步：把它拆成不带复用的 DBC 再导。"
            )
        moments, payloads = per_id.get(frame_id, ([], []))
        if not moments:
            continue
        order = np.argsort(np.asarray(moments, dtype=np.float64), kind="stable")
        times = np.asarray(moments, dtype=np.float64)[order]
        payloads = [payloads[index] for index in order]
        matrix = _payload_matrix(payloads)
        for signal in message.signals:
            values = _signal_values(signal, matrix)
            if values is None:
                too_short += 1
                continue
            index = np.searchsorted(times, axis, side="right") - 1
            leading = int(index[0] + 1) if index.size and index[0] >= 0 else 0
            index = np.clip(index, 0, len(values) - 1)
            name = signal.name
            if len(seen_names.get(signal.name, [])) > 1:
                # 实测 Channel_0 同时出现在两条报文里：撞名时用报文名当命名空间。
                name = f"{message.name}.{signal.name}"
            columns[name] = values[index]
            update_rate = _rate_of(times)
            # 显示精度跟着 factor 走：factor 0.0025 的胎温要看到 4 位小数，
            # factor 1 的计数一位都不需要。
            decimals = max(0, min(6, int(math.ceil(-math.log10(abs(signal.factor)))))) \
                if signal.factor else 0
            channel_rows.append({
                "name": name, "message": message.name, "signal": signal.name,
                "unit": signal.unit, "update_rate": update_rate,
                "samples": int(len(times)),
                "leading_gap_s": float(axis[leading - 1] - times[0]) if leading > 1 and len(times) else 0.0,
                "decimals": decimals,
            })
            report.append({
                "column": f"0x{message.frame_id:X}", "status": "通道", "name": name,
                "matched_by": f"DBC:{database_name}", "unit": signal.unit,
                "rate": master_rate, "rate_from": "主时间基",
                "samples": int(axis.size), "update_rate": update_rate,
                "message": message.name, "decimals": decimals,
            })

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

    def _rate(count: int) -> float:
        return round(count / duration, 2) if duration > 0 else 0.0

    undecoded = sorted(
        (
            {
                "id": f"0x{frame_id:X}", "frames": count,
                "rate": _rate(count),
                "sample": scan["samples"].get(frame_id, ""),
                "diagnostic": frame_id in DIAGNOSTIC_IDS,
            }
            for frame_id, count in undecoded_counts.items()
        ),
        key=lambda row: -row["frames"],
    )
    recording_evidence: list[str] = []
    if merge:
        for group in group_recordings(given) if len(given) > 1 else []:
            recording_evidence.extend(group["evidence"])
    stamp = _stamp_from_name(first.name)
    notes: list[str] = []
    if not any(name in columns for name in ("Vx KF", "GPS Speed", "Ground Speed")):
        notes.append(
            "这批日志里没有车速 / GPS 通道，所以没有距离轴：切圈、区段、圈差都用不了，"
            "画图、散点、直方图、频谱、数学通道、导出照常可用。"
        )
    if too_short:
        notes.append(f"有 {too_short} 条信号因为某一帧的字节数不够而整条跳过（DBC 与日志可能不是同一版）。")

    can = {
        "frames": total_frames,
        "ids": len(counts),
        "covered_frames": covered_frames,
        #: 被这份 DBC 覆盖的 ID（报告里"哪些读得懂"要能核对，测试也用它求并集）
        "covered_ids": [f"0x{frame_id:X}" for frame_id in sorted(covered_ids)],
        "coverage": round(covered_frames / total_frames, 4) if total_frames else 0.0,
        "covered_messages": len(covered_ids),
        "dbc": {"file": database_name, "sha256": digest,
                "messages": len(database.messages), "signals": database.signal_count,
                "skipped": list(database.skipped)},
        "dbc_candidates": candidates,
        "sources": [path.name for path in given],
        "merged": bool(merge and len(given) > 1),
        "merge_evidence": recording_evidence,
        "master_rate": master_rate,
        "bytes": sum(path.stat().st_size for path in given),
        "channels": channel_rows,
        "undecoded": undecoded,
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
        write_can_map(first, roles=roles, dbc=database_name, rate=master_rate)
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
