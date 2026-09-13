"""最小 OOXML (``.xlsx``) 写入器：只用标准库。

为什么不引 openpyxl / xlsxwriter：仓库规则 4 规定运行期依赖只有
``numpy`` / ``pandas`` / ``pyarrow``。导出 Excel 是队友点一下就要的功能，
不值得为它把一个需要跟着 Python 版本升级的包塞进每个人的机器——而 ``.xlsx``
本来就是一个 zip 里放几段 XML，这几段 XML 我们只用到最朴素的一部分：
文本、数字、空单元格，没有公式，没有图表，没有共享字符串。

它做的事情**只有一件**（见 ``write_workbook``）：把若干张 ``(表头, 行迭代器)``
写成一张工作表；行数超过 Excel 的硬上限时按 ``名字1 / 名字2`` 分表。

**流式**是硬要求，不是优化：一场耐久赛 19.4 万行 × 445 列，先把整个矩阵端进
内存再写是 690 MB。这里的每一行都是从调用方给的迭代器现取现写，写完一张表就
落一个临时文件，最后才组装 zip。

验证它的是 ``openpyxl``（开发工具，规则 4 的豁免只对测试工具生效）：
``tests/test_i3pro.py`` 里的 ``TestXlsxWriter`` 会把文件读回来逐格比对。
"""

from __future__ import annotations

import math
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

__all__ = ["MAX_ROWS", "MAX_COLS", "column_name", "write_workbook"]

#: Excel 单张工作表的最大行数（**含表头那一行**）。
MAX_ROWS = 1_048_576
#: Excel 单张工作表的最大列数。我们没有接近它的一天，但超出时给一句人话。
MAX_COLS = 16_384
#: 多少行刷一次写缓冲。
_CHUNK_ROWS = 512


def column_name(index: int) -> str:
    """0 基列号 -> Excel 列名（0 -> ``A``，26 -> ``AA``，445 -> ``QN``）。"""
    if index < 0:
        raise ValueError(f"列号不能是负数：{index}")
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(ord("A") + rem) + name
    return name


def _number(value) -> str | None:
    """数字怎么写进 ``<v>``；不是有限数就返回 ``None``（调用方留空单元格）。"""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    number = float(value)
    if not math.isfinite(number):
        return None
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return repr(number).replace("e", "E")


def _cell(ref: str, value, style: int = 0) -> str:
    """一格。数字直接写，文本走 inline string，``None``/NaN 留空。"""
    attrs = f' r="{ref}"'
    if style:
        attrs += f' s="{style}"'
    if value is None:
        return f"<c{attrs}/>" if style else ""
    if isinstance(value, str):
        if value == "":
            return ""
        text = escape(value)
        return f'<c{attrs} t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'
    number = _number(value)
    if number is None:
        return f"<c{attrs}/>" if style else ""
    return f"<c{attrs}><v>{number}</v></c>"


def _row_xml(row_index: int, values, style: int = 0) -> str:
    cells = []
    for col, value in enumerate(values):
        if value is None and not style:
            continue
        cells.append(_cell(f"{column_name(col)}{row_index}", value, style))
    return f'<row r="{row_index}">' + "".join(cells) + "</row>"


def _sheet_parts(header, rows, progress=None) -> list[str]:
    """一张工作表的内容，按块产出 XML 片段（不整表驻留内存）。"""
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        # 冻结首行：队友打开 445 列的宽表，横向滚动时还知道每一列是什么。
        "<sheetViews><sheetView workbookViewId=\"0\">"
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>",
        '<sheetFormatPr defaultRowHeight="15"/>',
        "<sheetData>",
        _row_xml(1, header, style=1),
    ]
    yield from parts
    buffer: list[str] = []
    row_index = 1
    for row_index, row in enumerate(rows, start=2):
        if row_index > MAX_ROWS:
            raise ValueError(
                f"这张表超过 Excel 的 {MAX_ROWS} 行上限，而且调用方没有打开分表"
                f"（write_workbook 的 split=True 会自动分表）。"
            )
        buffer.append(_row_xml(row_index, row))
        if len(buffer) >= _CHUNK_ROWS:
            yield "".join(buffer)
            buffer = []
            if progress is not None:
                progress(row_index - 1, None)
    if buffer:
        yield "".join(buffer)
    if progress is not None:
        progress(row_index - 1, None)
    yield "</sheetData></worksheet>"


_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">\
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>\
<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>\
<fills count="2"><fill><patternFill patternType="none"/></fill>\
<fill><patternFill patternType="gray125"/></fill></fills>\
<borders count="1"><border/></borders>\
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>\
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>\
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>\
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>\
</styleSheet>"""


def _pick_name(base: str, index: int | None) -> str:
    """工作表名：不超过 31 个字，且不许出现 ``: \\ / ? * [ ]``。"""
    name = base if index is None else f"{base}{index}"
    for bad in ':\\/?*[]':
        name = name.replace(bad, "_")
    return name[:31]


def write_workbook(path, sheets, progress=None) -> dict:
    """把若干张表写成 ``.xlsx``。

    ``sheets`` 是 ``dict`` 的列表，每张：::

        {"name": "数据", "header": ["time_s", "Vx KF [km/h]"], "rows": <迭代器>,
         "split": True}

    ``split=True`` 时行数超过 ``MAX_ROWS - 1`` 会自动分成 ``数据1 / 数据2 / …``
    （每张都带表头）。返回 ``{"sheets": 张数, "rows": 数据行总数, "names": [...]}``。
    """
    path = Path(path)
    written: list[tuple[str, Path]] = []
    total_rows = 0
    with tempfile.TemporaryDirectory(prefix="i3pro-xlsx-") as tmp:
        tmpdir = Path(tmp)
        for sheet in sheets:
            name = str(sheet.get("name") or "数据")
            header = list(sheet.get("header") or [])
            if len(header) > MAX_COLS:
                raise ValueError(
                    f"这张表有 {len(header)} 列，超过 Excel 的 {MAX_COLS} 列上限；"
                    f"少选一些通道，或者改用 CSV。"
                )
            rows = iter(sheet.get("rows") or [])
            split = bool(sheet.get("split", True))
            if not split:
                part = tmpdir / f"sheet{len(written) + 1}.xml"
                count = _drain(part, header, rows, progress)
                total_rows += count
                written.append((_pick_name(name, None), part))
                continue
            index = 0
            pending = rows
            while True:
                index += 1
                part = tmpdir / f"sheet{len(written) + 1}.xml"
                count, pending = _drain_split(
                    part, header, pending, MAX_ROWS - 1, progress
                )
                total_rows += count
                if count or index == 1:
                    written.append((_pick_name(name, index), part))
                if pending is None:
                    break
        _assemble(path, written)
    if progress is not None:
        progress(total_rows, total_rows)
    return {"sheets": len(written), "rows": total_rows, "names": [n for n, _ in written]}


def _counted(rows):
    """包一层计数器：``_drain`` 要回报写了几行，但不能为了数行把表读进内存。"""
    box = [0]

    def generate():
        for row in rows:
            box[0] += 1
            yield row

    return generate(), box


def _drain(part: Path, header, rows, progress) -> int:
    """写一张不分表的表，返回数据行数。"""
    counted, box = _counted(rows)
    with part.open("w", encoding="utf-8", newline="") as fh:
        for piece in _sheet_parts(header, counted, progress=progress):
            fh.write(piece)
    return box[0]


def _drain_split(part: Path, header, rows, limit: int, progress):
    """写一张分表；返回 ``(本表行数, 剩余行的迭代器或 None)``。

    最多从 ``rows`` 里取 ``limit`` 行，**不多读一行**——剩下的原封不动交给下一张
    表。这是它能处理 19.4 万行 × 445 列的原因：任何时刻驻留内存的只有一行。
    """
    import itertools

    head = itertools.islice(rows, limit)
    counted, box = _counted(head)
    with part.open("w", encoding="utf-8", newline="") as fh:
        for piece in _sheet_parts(header, counted, progress=progress):
            fh.write(piece)
    count = box[0]
    if count < limit:
        return count, None
    extra = next(rows, _SENTINEL)
    if extra is _SENTINEL:
        return count, None
    # 多读出来的那一行要还回去，不然分表之间会静默丢一行。
    return count, _prepend(extra, rows)


def _prepend(first, rest):
    """把多读出来的那一行放回队首，再接上剩下的行。"""
    yield first
    yield from rest


#: ``next(rows, …)`` 的哨兵：用来区分"没有下一行"和"下一行恰好是 None"。
_SENTINEL = object()


def _assemble(path: Path, sheets: list[tuple[str, Path]]) -> None:
    """把临时写好的工作表片段组装成一个合法的 ``.xlsx``。"""
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        f'ContentType="application/vnd.openxmlformats-officedocument.'
        f'spreadsheetml.worksheet+xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
        'package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{overrides}</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    sheet_tags = "".join(
        f'<sheet name={quoteattr(name)} sheetId="{i}" r:id="rId{i}"/>'
        for i, (name, _) in enumerate(sheets, start=1)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheet_tags}</sheets></workbook>"
    )
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    style_id = len(sheets) + 1
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}"
        f'<Relationship Id="rId{style_id}" Type="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/styles.xml", _STYLES)
        for i, (_, part) in enumerate(sheets, start=1):
            # 逐块拷贝到 zip 里：19.4 万行的 sheet XML 有好几 MB，
            # zipfile.write() 走的是磁盘流，不会整份进内存。
            zf.write(part, f"xl/worksheets/sheet{i}.xml")
