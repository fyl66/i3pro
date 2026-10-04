"""造一份"别人的分号表"，走完导入 → 快照，好让无头驱动验得到分隔文本这条路。

为什么需要它：``smoke_viewer.js`` 跑在**快照 HTML** 上，而金标准快照都是 ``.ld``
的——``.ld`` 没有 ``parse_note``（"这份表是怎么读出来的"），所以 ticket #32 那条
"读法要写进抬头"的断言在它们身上是空转的。这个工具把金标准场次导成 CSV、改成分号
分隔的 ``.txt``（模拟队友发来的表），再按真实流程导入并出快照。

用法（仓库根目录）::

    python tools\\make_text_demo.py
    node tools\\smoke_viewer.js "out/_text_demo_html/金标准导出.html"

产物都在 ``out/``（已 gitignore）：``_text_demo_src/``（那份 txt）、``_text_demo/``
（导入后的数据目录，含 ``.map.json`` 侧车）、``_text_demo_html/``（快照）。
"""

from __future__ import annotations

import csv
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from i3pro import cli, export as exportmod, ld  # noqa: E402

#: 列名照抄 `.ld` 里的真名，好让列名匹配走"原名"那条路；GPS 与速度是切圈要用的。
COLUMNS = "Vx KF,GPS Speed,G Force Lat,GPS Latitude,GPS Longitude,Lap Number"
SOURCE = ROOT / "i2pro_data" / "20260908-cjh 高避5圈.ld"


def main() -> int:
    if not SOURCE.exists():
        print(f"SKIP - 缺金标准数据 {SOURCE}")
        return 0
    staging = ROOT / "out" / "_text_demo_src"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    log = ld.LogFile.read(SOURCE)
    try:
        # rate=auto：慢通道（GPS 之类）留空——**正是**当初让圈消失的那种文件
        exportmod.write(log, exportmod.parse_request(log, {
            "channels": "selected", "names": COLUMNS,
            "rate": "auto", "format": "csv",
        }), staging / "金标准导出.csv")
    finally:
        log.close()
    with (staging / "金标准导出.csv").open(encoding="utf-8-sig", newline="") as src, \
            (staging / "金标准导出.txt").open("w", encoding="utf-8", newline="") as dst:
        csv.writer(dst, delimiter=";").writerows(csv.reader(src))

    demo = ROOT / "out" / "_text_demo"
    shutil.rmtree(demo, ignore_errors=True)
    demo.mkdir(parents=True)
    text = str(staging / "金标准导出.txt")
    print("--- 预览（只看读法，不导入）---")
    cli.main(["import", text, "--data", str(demo), "--preview"])
    print("\n--- 真的导入 ---")
    cli.main(["import", text, "--data", str(demo)])
    print("\n--- 出快照 ---")
    cli.main(["snapshot", "--data", str(demo), "--out", str(ROOT / "out" / "_text_demo_html")])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
