"""Proof that the native .ld reader agrees with MoTeC's own CSV export.

For every session that has both a ``.ld`` and a ``.csv`` in ``i2pro_data`` the
script decodes the binary log, reads the CSV, and compares channel by channel.
Channels whose native rate equals the CSV export rate are compared
sample-by-sample; slow channels are skipped because the CSV writer resamples
them onto the fast grid (the rounding tolerance is the channel's own display
precision).

Run:  python tools/verify_ld_vs_csv.py
"""

from __future__ import annotations

import csv
import sys
import os
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from i3pro import ld, motec_csv  # noqa: E402

#: 裁判要的两份东西（``.ld`` 与 MoTeC 自己导出的同名 ``.csv``）放哪。默认仓库里的
#: ``i2pro_data/``，但可以另放——用户把数据挪走之后这条就没法跑了（2026-10-06 实测）：
#:
#:     $env:I3PRO_DATA = "E:\桌面\LTS-mimo\实测数据\E02"   # 有 .ld/.csv 成对的那种目录
DATA = Path(os.environ.get("I3PRO_DATA") or (ROOT / "i2pro_data"))


def compare(stem: str, sample_limit: int = 20000, verbose: bool = False) -> dict:
    log = ld.LogFile.read(DATA / f"{stem}.ld")
    csv = motec_csv.load(DATA / f"{stem}.csv", max_rows=sample_limit + 10)
    master = csv.sample_rate or log.sample_rate
    compared = 0
    exact = 0
    worst: tuple[float, str] = (0.0, "")
    mismatches: list[str] = []
    for ch in log.channels:
        if ch.name not in csv.frame.columns:
            continue
        if abs(ch.sample_rate - master) > 0.5:
            continue  # the CSV resamples slow channels; index alignment is not meaningful
        reference = csv.values(ch.name)[:sample_limit]
        values = log.values(ch)[: sample_limit]
        n = min(reference.size, values.size)
        if n < 10:
            continue
        reference, values = reference[:n], values[:n]
        error = float(np.max(np.abs(reference - values)))
        tolerance = 0.5 * 10.0 ** (-ch.decimals) + 1e-9
        compared += 1
        if error <= tolerance:
            exact += 1
        else:
            mismatches.append(
                f"{ch.name!r} unit={ch.unit!r} max_err={error:g} tol={tolerance:g}"
            )
        if error > worst[0]:
            worst = (error, ch.name)
    log.close()
    result = {
        "stem": stem,
        "compared": compared,
        "within_display_precision": exact,
        "worst_error": worst[0],
        "worst_channel": worst[1],
        "mismatches": mismatches,
    }
    if verbose:
        for line in mismatches:
            print("      mismatch:", line)
    return result


def main() -> int:
    # ``<场次>.csv`` 不一定是 MoTeC 导出的那种表：``i3pro pair`` 的合并产物也叫
    # 这个名字（一个 ``.ld`` + 若干帧表并出来的宽表，表头是 ``time_s`` 而不是
    # MoTeC 的 ``Time``）。把它当导出表读会直接报 "no channel header row found"——
    # 那是**另一种形态**，不是"这条不过"。所以先按时间列名筛一遍（``time s`` /
    # ``times`` 归一化之后都是 ``times``，与 ``csvlog.TIME_NAMES`` 同一套规矩），
    # 认不出的跳过并说出来。
    stems, skipped = [], []
    for path in sorted(DATA.glob("*.ld")):
        pair = DATA / f"{path.stem}.csv"
        if not pair.exists():
            continue
        if _is_motec_export(pair):
            stems.append(path.stem)
        else:
            skipped.append(pair.name)
    if not stems:
        print("no .ld/.csv pairs found in", DATA)
        return 1
    if skipped:
        print(f"跳过 {len(skipped)} 份不是 MoTeC 导出的同名 .csv：{', '.join(skipped)}")
        print("（``i3pro pair`` 的合并场次就是这种；它验证的是合并本身，不是解析器）\n")
    failures = 0
    for stem in stems:
        result = compare(stem, verbose=True)
        print(
            f"{stem}:\n"
            f"   channels compared at export rate : {result['compared']}\n"
            f"   within MoTeC display precision   : {result['within_display_precision']}\n"
            f"   worst channel / max abs error    : {result['worst_channel']!r} / "
            f"{result['worst_error']:.3g}"
        )
        failures += len(result["mismatches"])
    print(f"\n{'PASS' if failures == 0 else 'FAIL'} - {failures} channel(s) outside tolerance")
    return 0 if failures == 0 else 1


def _is_motec_export(path: Path) -> bool:
    """这份 CSV 是 MoTeC 自己导出的那种（表头第一格是 ``Time``）吗？

    MoTeC 导出用 ``Time``；``i3pro`` 自己导出的宽表与 ``i3pro pair`` 的合并场次
    用 ``time_s``——那是**另一种形态**，不是 MoTeC 的对照物，所以这里只认 ``Time``。
    """
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            for _ in range(400):
                head = next(csv.reader([handle.readline()]), [])
                if head and str(head[0]).strip() == "Time":
                    return True
    except OSError:
        return False
    return False


if __name__ == "__main__":
    raise SystemExit(main())
