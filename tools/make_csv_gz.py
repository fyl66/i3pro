"""把宽表 CSV 压成 ``.csv.gz``（ADR-0012）。**开发工具，不是 i3pro 的功能。**

产品那条线只**读** `.csv.gz`：命令行没有这个子命令、导出面板没有这一档、前端不
认识它。压缩是仓库外的动作，这个脚本就是那个动作的唯一实现处——理由不是"懒得
写"，而是 **gzip 的时间戳**：gzip 头默认带 mtime 与原文件名，同一份 CSV 两天压
出来字节不同，于是"共享里那份 `.gz` 和本地这份是不是同一份"就只能靠大小猜。
这里用裸 zlib 自己写 gzip 头（mtime=0、不写文件名），同一份内容在任何机器上压出
来**逐字节相同**——省空间这件事要配上"能机械复核"才算数。

用法（在仓库根目录跑）::

    python tools/make_csv_gz.py i2pro_data\\某个场次.csv      # 压，留原件
    python tools/make_csv_gz.py i2pro_data --list             # 只看会做什么
    python tools/make_csv_gz.py i2pro_data --delete           # 压完校验通过再删原件

规则：

* 只压纯文本容器（``.csv`` / ``.txt`` / ``.tsv``）；``.ld`` / ``.xlsx`` 直接拒绝
  ——二进制与已经压过的容器再套 gzip 只会更慢更大；
* **原始 CAN 帧表跳过**（`canlog.looks_like_frames`）：i3pro 不认压过的帧表，而
  且场次库启动时要逐份嗅探帧表内容，压了会让那趟扫描变成逐份解压；
* 目标已存在就跳过，绝不覆盖；``--delete`` 只在"解压回来与原件哈希一致"之后才删。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from i3pro import containers  # noqa: E402

CHUNK = 8 << 20


def compress_file(source: Path, target: Path, level: int = 6) -> dict:
    """``source`` -> ``target``（gzip，可复现）。调用方负责先确认 target 不存在。"""
    compressor = zlib.compressobj(level, zlib.DEFLATED, 31)
    written = 0
    with source.open("rb") as src, target.open("wb") as dst:
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            blob = compressor.compress(chunk)
            if blob:
                dst.write(blob)
                written += len(blob)
        blob = compressor.flush()
        if blob:
            dst.write(blob)
            written += len(blob)
    return {"source": source, "target": target, "bytes": written}


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def _same_contents(source: Path, target: Path) -> bool:
    """解压 ``target`` 与 ``source`` 逐字节比（分块，不把 290 MB 读进内存）。"""
    with gzip.open(target, "rb") as handle:
        sha_out = hashlib.sha256()
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            sha_out.update(chunk)
    return digest(source) == sha_out.hexdigest()


def _wanted(path: Path) -> tuple[bool, str]:
    """这份文件该不该压：``(要不要压, 说明)``。"""
    if not path.is_file():
        return False, "不是文件"
    if containers.is_compressed(path):
        return False, "已经压过了"
    fmt = containers.format_suffix(path)
    if fmt not in containers.COMPRESSIBLE:
        return False, (f"{fmt or '认不出来的格式'} 不压——二进制，或者本身已经是压缩容器"
                       "（.ld / .xlsx / .ldx），再套 gzip 只会更慢更大")
    from i3pro import canlog

    if fmt == ".csv" and canlog.looks_like_frames(path):
        return False, "原始 CAN 帧表不压（i3pro 不认压过的帧表，见 ADR-0012）"
    return True, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把宽表 CSV 压成可复现的 .csv.gz（开发工具，不进 i3pro 产品面）")
    parser.add_argument("paths", nargs="+", help="文件或目录（目录递归找 .csv/.txt/.tsv）")
    parser.add_argument("--delete", action="store_true",
                        help="压完、并且解压回来与原件哈希一致之后，删掉原件")
    parser.add_argument("--list", action="store_true",
                        help="只打印会做什么，一个文件都不写")
    parser.add_argument("--level", type=int, default=6, help="gzip 压缩级别（默认 6）")
    args = parser.parse_args(argv)

    targets: list[Path] = []
    for raw in args.paths:
        path = Path(raw)
        if path.is_dir():
            targets.extend(sorted(p for p in path.rglob("*") if p.is_file()))
        elif path.exists():
            targets.append(path)
        else:
            print(f"  ! 找不到 {path}")

    kept = skipped = 0
    raw_total = zipped_total = 0
    for source in targets:
        ok, why = _wanted(source)
        if not ok:
            if why not in ("不是文件",):
                print(f"  - {source.name}：{why}")
                skipped += 1
            continue
        target = source.with_name(containers.with_compression(source))
        if target.exists():
            # 已经压过了（重跑这条命令是常态）：**不覆盖**，但 `--delete` 仍然该
            # 生效——前提是"解压回来与原件逐字节一致"，否则宁可留着原件。
            if args.delete and _same_contents(source, target):
                os.remove(source)
                print(f"  = {source.name}：{target.name} 已在且内容一致 · 原件已删")
                skipped += 1
            else:
                print(f"  - {source.name}：{target.name} 已经在了（不覆盖）")
                skipped += 1
            continue
        before = source.stat().st_size
        if args.list:
            print(f"  ? {source.name} -> {target.name}（{before / 1e6:.1f} MB）")
            continue
        info = compress_file(source, target, level=args.level)
        after = target.stat().st_size
        if not _same_contents(source, target):
            target.unlink(missing_ok=True)
            print(f"  ! {source.name}：压出来解不回原件（哈希不一致），已撤回")
            continue
        raw_total += before
        zipped_total += after
        kept += 1
        note = ""
        if args.delete:
            os.remove(source)
            note = " · 原件已删"
        print(f"  + {source.name} -> {target.name}  "
              f"{before / 1e6:.1f} MB -> {after / 1e6:.1f} MB"
              f"（{100.0 * after / max(before, 1):.0f}%）{note}")

    if args.list:
        print(f"\n（--list：一个文件都没写。要真压就去掉 --list）")
    elif kept:
        print(f"\n压了 {kept} 个：{raw_total / 1e6:.1f} MB -> {zipped_total / 1e6:.1f} MB"
              f"（{100.0 * zipped_total / max(raw_total, 1):.0f}%）"
              f"，跳过 {skipped} 个")
    else:
        print(f"\n没有要压的（跳过 {skipped} 个）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
