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

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from i3pro import ld, motec_csv  # noqa: E402

DATA = ROOT / "i2pro_data"


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
    stems = sorted(
        p.stem for p in DATA.glob("*.ld") if (DATA / f"{p.stem}.csv").exists()
    )
    if not stems:
        print("no .ld/.csv pairs found in", DATA)
        return 1
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


if __name__ == "__main__":
    raise SystemExit(main())
