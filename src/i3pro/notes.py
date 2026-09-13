"""注释（Notes）：钉在某个时刻上的一条带文字的标记。

i2 Pro 的 Notes 是"在数据上放一条带文字的标记"，复盘时一眼看到"这里换了刹车点"。
它和信标（beacon）是两码事：**信标是穿过一次线**，会切圈；注释只是给人看的记号，
不参与切圈、比圈与报表——所以它有自己的侧车 ``<场次>.notes.json``，那张文件写坏了
最多是注释没了，圈速表一个数都不会动。

算法是不依赖框架的纯函数（``normalize`` / ``add_note`` / ``update_note`` /
``remove_note`` / ``marks``），只在 ``load_notes`` / ``save_notes`` 里碰文件。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

#: 侧车文件后缀：``<场次>.ld`` -> ``<场次>.notes.json``。
SIDE_SUFFIX = ".notes.json"
#: 一条注释最多这么多字。再长就截断——它不是备注本，是要在图上看得见的一行字。
MAX_TEXT = 200
#: 一场最多这么多条。真到这个数，先删几条旧注释比让侧车无限长更合理。
MAX_NOTES = 500


class NoteError(ValueError):
    """注释存不下来时说清楚**下一步做什么**，不只是"哪里错了"。"""


@dataclass(frozen=True)
class Note:
    """一条注释：一个时刻（秒）加一行字。"""

    time: float
    text: str

    def as_dict(self) -> dict:
        return {"time": round(float(self.time), 3), "text": self.text}


def clean_text(value: object) -> str:
    """整成一行字：折掉换行与多余空格（图上的标记只放得下一行），截到 200 字。"""
    text = " ".join(str(value if value is not None else "").split())
    return text[:MAX_TEXT]


def check_time(value: object, duration: float) -> float:
    """时刻必须是这一场里的一个数。说不清位置就没法画，宁可拒绝。"""
    try:
        when = float(value)
    except (TypeError, ValueError):
        raise NoteError(
            "注释要有一个时刻（秒）才画得出来。把鼠标移到图上，光标停在哪儿，"
            "「＋ 注释」就放在哪儿。"
        ) from None
    if not np.isfinite(when):
        raise NoteError("注释的时刻是 NaN / inf，画不出来。把鼠标移到图上再点「＋ 注释」。")
    if when < 0 or when > float(duration) + 1e-9:
        raise NoteError(
            "注释要落在这一场的 0–%.3f s 之间，现在写的是 %.3f s。"
            "把鼠标移到图上想标注的位置，再点「＋ 注释」。" % (duration, when)
        )
    return when


def normalize(raw: object, duration: float) -> list[Note]:
    """把一份（可能被手改过的）JSON 收拾成能用的注释表：查错、去空、按时刻排序。

    这是**唯一**一处校验：界面、HTTP 接口、单元测试都从这里过，所以"文字不能为空"
    这类规则只有一份实现。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise NoteError(
            '注释表要写成 {"notes": [{"time": 12.5, "text": "……"}]} 这样的数组，'
            "现在拿到的是 %s。" % type(raw).__name__
        )
    if len(raw) > MAX_NOTES:
        raise NoteError(
            "最多 %d 条注释，现在有 %d 条。先删掉几条再保存。" % (MAX_NOTES, len(raw))
        )
    out: list[Note] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise NoteError(
                "第 %d 条注释不是一个对象（要有 time 与 text 两个字段）。"
                "在界面上删掉它重建一条最快。" % index
            )
        text = clean_text(item.get("text"))
        if not text:
            raise NoteError(
                "第 %d 条注释没有文字：写一句「这里做了什么」"
                "（例如「这里换了刹车点」）再保存。" % index
            )
        out.append(Note(time=check_time(item.get("time"), duration), text=text))
    out.sort(key=lambda note: note.time)
    return out


def add_note(notes: list[Note], when: object, text: object, duration: float) -> list[Note]:
    """加一条，返回新的注释表（不改传进来的那份）。"""
    rows = [note.as_dict() for note in notes]
    rows.append({"time": when, "text": text})
    return normalize(rows, duration)


def update_note(
    notes: list[Note], index: int, text: object, duration: float | None = None
) -> list[Note]:
    """改第 ``index`` 条的文字（时刻不动）。索引对不上就说清下一步。"""
    if not 0 <= index < len(notes):
        raise NoteError(
            "要改的那条注释已经不在了（列表可能被别处刷新过）。"
            "刷新页面，看准了再改。"
        )
    rows = [note.as_dict() for note in notes]
    rows[index] = {"time": rows[index]["time"], "text": text}
    span = duration if duration is not None else float("inf")
    return normalize(rows, span)


def remove_note(notes: list[Note], index: int) -> list[Note]:
    """删掉第 ``index`` 条，返回剩下的（不改传进来的那份）。"""
    if not 0 <= index < len(notes):
        raise NoteError(
            "要删的那条注释已经不在了（列表可能被别处刷新过）。刷新页面，再删一次。"
        )
    out = list(notes)
    del out[index]
    return out


def _fill(source_time, source_value, when: float) -> float | None:
    """在一条主采样序列上按时刻取值；落在两端之外就返回 None（不猜）。"""
    if source_time is None or source_value is None:
        return None
    times = np.asarray(source_time, dtype=float)
    values = np.asarray(source_value, dtype=float)
    if times.size < 2 or values.size != times.size:
        return None
    if when < times[0] - 1e-9 or when > times[-1] + 1e-9:
        return None
    return float(np.interp(when, times, values))


def marks(
    notes: list[Note],
    track: dict | None = None,
    master_time=None,
    master_distance=None,
) -> list[dict]:
    """注释 + 它在轨迹图与距离轴上的落点。

    * ``distance``：在**主采样序列**上按时刻插值（100 Hz 的格子，误差远小于像素）。
      没有速度通道、或这条注释落在序列之外时是 ``None``。
    * ``x`` / ``y``：轨迹是抽稀过的（``track_payload`` 默认 1500 点），所以取**时刻
      最近的那个采样**，不插值——抽稀点之间隔着几米，插出来的位置没有意义。

    两者拿不到就老实给 ``None``：时间轴上的竖线照画，轨迹图上不画，不猜一个位置。
    """
    out = [dict(note.as_dict(), distance=None, x=None, y=None) for note in notes]
    for row in out:
        distance = _fill(master_time, master_distance, row["time"])
        if distance is not None:
            row["distance"] = round(distance, 2)
    if not track or not track.get("time"):
        return out
    times = np.asarray(track["time"], dtype=float)
    xs = np.asarray(track["x"], dtype=float)
    ys = np.asarray(track["y"], dtype=float)
    if times.size < 1 or xs.size != times.size or ys.size != times.size:
        return out
    for row in out:
        when = row["time"]
        if when < times[0] - 1e-9 or when > times[-1] + 1e-9:
            continue
        index = int(np.argmin(np.abs(times - when)))
        row["x"] = round(float(xs[index]), 2)
        row["y"] = round(float(ys[index]), 2)
    return out


# ------------------------------------------------------------------ sidecar
def config_path(session_path: str | Path) -> Path:
    """``<场次>.ld`` -> ``<场次>.notes.json``（和圈、区段两个侧车挨着放）。"""
    return Path(session_path).with_suffix(SIDE_SUFFIX)


def load_notes(session_path: str | Path) -> list[Note]:
    """读侧车。**永不抛**：读不出来就当这场没有注释，工作台照样打得开。"""
    path = config_path(session_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(raw, dict):
        raw = raw.get("notes")
    if not isinstance(raw, list):
        return []
    out: list[Note] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = clean_text(item.get("text"))
        try:
            when = float(item.get("time"))
        except (TypeError, ValueError):
            continue
        if text and np.isfinite(when):
            out.append(Note(time=when, text=text))
    out.sort(key=lambda note: note.time)
    return out


def save_notes(session_path: str | Path, notes: list[Note]) -> Path:
    """写侧车。``.ld`` 一个字节都不动（ADR 0001）。"""
    path = config_path(session_path)
    payload = {"notes": [note.as_dict() for note in notes]}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path
