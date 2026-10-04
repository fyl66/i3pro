"""读**分隔文本**成场次：`.txt` / `.tsv`，以及分隔符不是逗号的 `.csv`。

和 CSV 是同一件事的两面，这里多两件外部表才有的麻烦：

* **分隔符要猜**——Tab、逗号、分号、竖线、连续空格。猜错时用户在导入预览里改，
  改完的选择写进 ``<场次>.map.json``，同一份文件第二次导入自动套用。
* **时间列可能压根没有**——默认拒绝（拿别的列冒充时间是灾难），但可以明确选
  "按固定采样率生成时间列"：填 Hz 与起点，同样写进侧车，第二次读得到同一列。

装配成场次那一步仍然只有一份代码：:func:`i3pro.csvlog.session_from_frame`。
"""

from __future__ import annotations

import codecs
import csv
import re
from pathlib import Path

from . import csvlog, sidecar

__all__ = [
    "DELIMITERS", "DELIMITER_LABELS", "ENCODINGS", "ENCODING_LABELS",
    "detect_encoding", "sniff_delimiter", "scan_text", "split_line", "read_frame",
    "read_text_session", "preview",
]

#: 猜分隔符的候选，**顺序就是平手时的优先级**：逗号最常见，连续空白最不具体。
DELIMITERS = (",", "\t", ";", "|", " ")

#: ``" "`` 在这里的意思是"连续空白"，不是"一个空格"——单空格分隔的表也归它管。
DELIMITER_LABELS = {",": "逗号", "\t": "Tab", ";": "分号", "|": "竖线", " ": "多空格"}

#: latin-1 放最后：它永远解得出东西，所以只有在别的都失败时才轮到它。
ENCODINGS = ("utf-8-sig", "gbk", "utf-16", "latin-1")
ENCODING_LABELS = {"utf-8-sig": "UTF-8", "utf-8": "UTF-8", "gbk": "GBK",
                   "utf-16": "UTF-16", "latin-1": "Latin-1"}

#: 嗅探 / 认表头 / 预览各读多少。表头行与单位行只可能出现在开头（和 CSV 那边
#: ``scan_rows`` 的 40 行同一个道理）；预览只读前面这一截——列名不会读到第 5000
#: 行才变，而真正导入时读全份。
SAMPLE_BYTES = 1 << 16
HEAD_ROWS = 200
PREVIEW_ROWS = 5000
SNIFF_LINES = 60


def _decodes(blob: bytes, encoding: str) -> bool:
    """这一段字节能按 ``encoding`` 解出来吗（不喂 final：尾部截断的字符不算错）。"""
    decoder = codecs.getincrementaldecoder(encoding)("strict")
    try:
        decoder.decode(blob)
        return True
    except UnicodeDecodeError:
        return False


def detect_encoding(path: str | Path, limit: int = SAMPLE_BYTES) -> str:
    """这份文件该按什么编码读。BOM 说了算，其次 UTF-8，再其次 GBK。"""
    with Path(path).open("rb") as handle:
        head = handle.read(limit)
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"
    for encoding in ("utf-8-sig", "gbk"):
        if _decodes(head, encoding):
            return encoding
    return "latin-1"


def split_line(line: str, delimiter: str) -> list[str]:
    """按分隔符切**一行**；``" "`` 走"连续空白"，其余交给 csv 的引号规则。"""
    if delimiter == " ":
        return re.split(r"\s+", line.strip()) if line.strip() else []
    try:
        return next(csv.reader([line], delimiter=delimiter))
    except csv.Error:
        # 引号没配对的脏行：退回按分隔符硬切，总比整个文件读不出来好。
        return line.split(delimiter)


def sniff_delimiter(text: str, lines: int = SNIFF_LINES) -> tuple[str, list[dict]]:
    """猜分隔符，返回 ``(选中的, 各候选的打分)``。

    打分 = "有多少行真的被切成了多于 1 格" × 众数宽度。光看"切出多少格"不够：
    逗号表用空格切也能切出好几格，但只有逗号那份能**每一行都一样宽**。
    """
    sample = [line for line in text.splitlines()[:lines * 2] if line.strip()][:lines]
    scores: list[dict] = []
    for delimiter in DELIMITERS:
        counts = [len(split_line(line, delimiter)) for line in sample]
        widths = [count for count in counts if count > 1]
        if not widths:
            scores.append({"value": delimiter, "label": DELIMITER_LABELS[delimiter],
                           "score": 0.0, "width": 0})
            continue
        width = max(set(widths), key=widths.count)
        share = widths.count(width) / max(len(sample), 1)
        scores.append({"value": delimiter, "label": DELIMITER_LABELS[delimiter],
                       "score": round(share * min(width, 64), 3), "width": width})
    best = max(scores, key=lambda row: row["score"])
    # 一份"一行一列"的表（没有任何分隔符）也要能进来：全 0 分时按逗号处理，
    # 那时它会以"没有可用的通道列"报出来，比抛一个嗅探错误好懂。
    chosen = str(best["value"]) if best["score"] > 0 else ","
    return chosen, scores


def scan_text(
    path: str | Path,
    *,
    encoding: str | None = None,
    delimiter: str | None = None,
    limit: int | None = None,
) -> tuple[list[list[str]], str, str]:
    """读成分行分列，返回 ``(行, 用上的分隔符, 用上的编码)``。

    ``delimiter`` / ``encoding`` 给 ``None`` 就是现猜。空行丢掉（和 pandas 的
    ``skip_blank_lines`` 一样），**不猜**任何东西：切不出来的格子原样留着，
    让上层按"不是数值"处理。

    按**物理行**切，一行一格（空行是 ``[]``，照样占一行）：读出来的行号要能直接
    当 pandas 的 ``skiprows`` 用。这不是洁癖——实测那份 i2 Pro 导出的 CSV，表头
    前面有几行空行，跳过它们会让行号少算两行，``skiprows`` 于是把表头当成数据，
    整份表读不出来。
    """
    path = Path(path)
    encoding = encoding or detect_encoding(path)
    if delimiter is None:
        with path.open("r", encoding=encoding, newline="") as handle:
            delimiter, _scores = sniff_delimiter(handle.read(SAMPLE_BYTES))
    rows: list[list[str]] = []
    with path.open("r", encoding=encoding, newline="") as handle:
        for line in handle:
            rows.append(split_line(line.rstrip("\r\n"), delimiter))
            if limit is not None and len(rows) >= limit:
                break
    return rows, delimiter, encoding


def read_frame(path: str | Path, *, encoding: str, delimiter: str, skip: int, limit: int | None):
    """数据表交给 pandas 读（表头/单位行那几行已经 ``skip`` 掉了）。

    为什么不自己一行行读：341 列 × 19 万行的表读成"每格一个 Python 字符串"要
    **60 秒以上**（实测），而 pandas 的同一次读取是秒级——它按列推断类型，也正是
    下游 ``_floats`` 的快车道。表头那几行仍然用 :func:`scan_text` 自己读，
    "分隔符嗅探 / 手工指定表头"这两件事才不受 pandas 的猜测影响。
    """
    import pandas as pd

    common = dict(header=None, skiprows=skip, nrows=limit, skip_blank_lines=True,
                  on_bad_lines="skip", encoding=encoding, encoding_errors="replace")
    if delimiter == " ":
        # 连续空白只能用 python 引擎的正则分隔符；这类表一般不大。
        return pd.read_csv(path, sep=r"\s+", engine="python", **common)
    return pd.read_csv(path, sep=delimiter, engine="c", **common)


def _note(delimiter: str, encoding: str, header, unit_row, generated, rate) -> str:
    bits = [f"{DELIMITER_LABELS.get(delimiter, delimiter)}分隔",
            ENCODING_LABELS.get(encoding, encoding)]
    if header is not None:
        bits.append("没有表头行" if int(header) < 0 else f"表头第 {int(header) + 1} 行")
    if unit_row:
        bits.append("单位行")
    if generated:
        bits.append(f"时间列按 {rate:g} Hz 生成")
    return " · ".join(bits)


def read_text_session(
    path: str | Path,
    *,
    delimiter: str | None = None,
    encoding: str | None = None,
    header: int | None = None,
    unit_row: bool | None = None,
    generate_rate: float | None = None,
    generate_start: float | None = None,
    renames: dict[str, str] | None = None,
    units: dict[str, str] | None = None,
    limit: int | None = None,
    fmt: str | None = None,
) -> csvlog.CsvSession:
    """把一份分隔文本读成场次。

    参数的优先级是 **调用方给的 > 侧车里记的 > 现猜**：命令行显式传的就该赢，
    侧车是"上次用的是什么"，没给也没记过才去猜。
    """
    path = Path(path)
    stored = csvlog.load_options(path)
    if delimiter is None:
        delimiter = stored.get("delimiter")
    if encoding is None:
        encoding = stored.get("encoding")
    if header is None and "header" in stored:
        header = stored["header"]
    if unit_row is None and "unit_row" in stored:
        unit_row = stored["unit_row"]
    if generate_rate is None:
        generate_rate = stored.get("generate_rate")
    if generate_start is None:
        generate_start = stored.get("generate_start")

    rows, delimiter, encoding = scan_text(
        path, encoding=encoding, delimiter=delimiter, limit=HEAD_ROWS
    )
    if not any(rows):
        raise ValueError(
            f"{path.name}: 这个文件里没有可读的行。"
            "下一步：确认它不是空的，或者换一个分隔符/编码再试。"
        )
    head, skip = csvlog.layout_of(rows, header=header, unit_row=unit_row)
    frame = read_frame(path, encoding=encoding, delimiter=delimiter, skip=skip, limit=limit)
    if frame.empty:
        raise ValueError(
            f"{path.name}: 表头之后没有数据行。"
            "下一步：在导入预览里把表头行指到真正的那一行，或确认这份导出里带着数据。"
        )
    resolved = fmt or ("csv" if path.suffix.lower() == ".csv" else "txt")
    session = csvlog.session_from_frame(
        path, rows[: skip], frame, renames=renames, units=units, fmt=resolved,
        header=head, unit_row=bool(skip == head + 2),
        generate_rate=generate_rate, generate_start=generate_start,
    )
    if resolved == "txt" or stored or generate_rate or header is not None or unit_row is not None:
        session.header["parse_note"] = _note(
            delimiter, encoding, header, bool(skip == head + 2),
            any(row.get("generated") for row in session.report),
            session.sample_rate,
        )
    return session


def preview(
    path: str | Path,
    *,
    delimiter: str | None = None,
    encoding: str | None = None,
    header: int | None = None,
    unit_row: bool | None = None,
    generate_rate: float | None = None,
    generate_start: float | None = None,
    rows_shown: int = 20,
) -> dict:
    """导入预览：前若干行的原样内容 + **按当前选择真读一遍**的结果。

    读不出来不是异常，而是预览的一部分（``error``）——用户正是靠它知道"这个分隔符
    不对"，然后在下拉框里改。改完再调一次这个函数，所以"预览里看到的"与"导入得到的"
    是同一条代码路径。
    """
    path = Path(path)
    stored = csvlog.load_options(path)
    out: dict = {
        "name": path.name,
        "size": path.stat().st_size,
        "delimiters": [{"value": value, "label": DELIMITER_LABELS[value]}
                       for value in DELIMITERS],
        "encodings": [{"value": value, "label": ENCODING_LABELS[value]}
                      for value in ENCODINGS],
        "stored": stored,
        "rows_shown": rows_shown,
    }
    try:
        probe_rows, used_delimiter, used_encoding = scan_text(
            path, encoding=encoding, delimiter=delimiter, limit=PREVIEW_ROWS
        )
    except (ValueError, OSError, UnicodeError) as exc:
        return {**out, "error": str(exc), "ready": False}
    out["encoding"] = used_encoding
    out["delimiter"] = used_delimiter
    out["delimiter_label"] = DELIMITER_LABELS.get(used_delimiter, used_delimiter)
    out["rows"] = probe_rows[:rows_shown]
    out["partial"] = len(probe_rows) >= PREVIEW_ROWS
    # 用户选的是"自动"时也得知道**最后定成了哪一行**，否则改了表头行却看不出效果。
    try:
        found_head, found_skip = csvlog.layout_of(probe_rows, header=header, unit_row=unit_row)
    except ValueError as exc:
        return {**out, "error": str(exc), "ready": False}
    out["effective_header"] = found_head
    out["effective_unit_row"] = found_skip == found_head + 2
    out["width"] = (len(probe_rows[found_head]) if found_head >= 0
                    else max((len(row) for row in probe_rows), default=0))
    try:
        session = read_text_session(
            path, delimiter=delimiter, encoding=encoding, header=header,
            unit_row=unit_row, generate_rate=generate_rate,
            generate_start=generate_start, limit=PREVIEW_ROWS,
        )
    except (ValueError, OSError, UnicodeError, sidecar.SidecarError) as exc:
        return {**out, "error": str(exc), "ready": False}
    out.update(
        ready=True,
        header=header,
        unit_row=unit_row,
        generated=bool(generate_rate),
        channels=[channel.name for channel in session.channels],
        units={channel.name: channel.unit for channel in session.channels},
        sample_rate=session.sample_rate,
        samples=int(session.time.size),
        parse_note=session.header.get("parse_note", ""),
        report=session.report,
    )
    return out
