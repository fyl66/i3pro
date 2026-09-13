"""Command line interface: ``python -m i3pro <command>``."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from . import laps as lapsmod
from . import derive
from . import ld as ldmod
from . import render as rendermod
from . import store


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _print_table(rows: list[dict], columns: list[str] | None = None) -> None:
    if not rows:
        print("(no rows)")
        return
    columns = columns or list(rows[0])
    widths = {c: max(len(str(c)), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    print("  ".join(str(c).ljust(widths[c]) for c in columns))
    print("  ".join("-" * widths[c] for c in columns))
    for row in rows:
        print("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns))


def cmd_info(args: argparse.Namespace) -> int:
    rows = []
    for path in args.files:
        with ldmod.LogFile.read(path) as log:
            meta = log.metadata()
            rows.append(
                {
                    "file": meta["file"],
                    "device": meta["device"],
                    "date": meta["log_date"],
                    "time": meta["log_time"],
                    "event": meta["event"],
                    "rate": f"{meta['sample_rate']:g} Hz",
                    "duration": f"{meta['duration']:.1f} s",
                    "channels": meta["channels"],
                    "size": f"{meta['file_size'] / 1e6:.1f} MB",
                }
            )
    _print_table(rows)
    return 0


def cmd_channels(args: argparse.Namespace) -> int:
    with ldmod.LogFile.read(args.file) as log:
        rows = []
        for ch in log.channels:
            if args.filter and args.filter.lower() not in ch.name.lower():
                continue
            rows.append(
                {
                    "idx": ch.index,
                    "channel": ch.name,
                    "unit": ch.unit,
                    "rate": f"{ch.sample_rate:g}",
                    "samples": ch.sample_count,
                    "scale": f"{ch.scale:g}",
                    "dec": ch.decimals,
                }
            )
    if args.limit:
        rows = rows[: args.limit]
    _print_table(rows)
    print(f"\n{len(rows)} channel(s)")
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    channels = [c.strip() for c in args.channels.split(",")] if args.channels else None
    for path in args.files:
        with ldmod.LogFile.read(path) as log:
            pq_path, meta_path = store.write_parquet(
                log, args.out, channels=channels, master_rate=args.rate
            )
            print(f"{Path(path).name} -> {pq_path.name} ({pq_path.stat().st_size / 1e6:.1f} MB) + {meta_path.name}")
    return 0


def cmd_laps(args: argparse.Namespace) -> int:
    with ldmod.LogFile.read(args.file) as log:
        if args.mode or args.gate:
            config = lapsmod.load_config(args.file)
            if args.mode:
                config.mode = args.mode
            for spec in args.gate or []:
                parsed = _parse_gate(spec)
                if parsed is None:
                    print(f"# 忽略无法解析的信标: {spec!r}（格式 lat,lon[:名字]）")
                    continue
                config.gates.append(parsed)
            if args.save:
                path = lapsmod.save_config(args.file, config)
                print(f"# 已保存 {path.name}")
        else:
            config = lapsmod.load_config(args.file)
        try:
            laps = lapsmod.detect_from_config(log, config)
        except ValueError as exc:
            print(f"# {exc}")
            return 1
        _print_table(lapsmod.lap_table(log, laps),
                     ["lap", "turn", "lap_time", "delta_to_best", "distance", "start_time", "end_time"])
        if config.gates:
            print(f"\n{len(config.gates)} 个信标: "
                  + ", ".join(f"{n}({lat:.6f}, {lon:.6f})" for n, lat, lon in config.gates))
        print(f"切分方式: {config.mode}")
        if args.json:
            Path(args.json).write_text(
                json.dumps([l.as_row() for l in laps], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"\nwrote {args.json}")
    return 0


def _parse_gate(spec: str) -> tuple[str, float, float] | None:
    """``lat,lon`` or ``lat,lon:名称`` -> (name, lat, lon)."""
    name = ""
    if ":" in spec:
        spec, name = spec.rsplit(":", 1)
    parts = [p.strip() for p in spec.split(",")]
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    return (name.strip() or f"信标{lat:.5f}", lat, lon)


def cmd_delta(args: argparse.Namespace) -> int:
    channels = (
        [c.strip() for c in args.channels.split(",")]
        if args.channels
        else ["Ground Speed", "G Force Long", "Brake Signal", "Throttle"]
    )
    with ldmod.LogFile.read(args.file) as log:
        try:
            laps = lapsmod.detect_laps(log)
        except ValueError as exc:
            print(f"# {exc}")
            return 1
        if not laps:
            print("no laps found")
            return 1
        by_label = {l.label: l for l in laps}
        ranked = sorted((l for l in laps if l.complete), key=lambda l: l.lap_time) or laps
        ref = by_label.get(args.ref) or (
            laps[args.ref_index()] if args.ref_index() != 0 else ranked[0]
        )
        cmp = by_label.get(args.cmp) or (
            laps[args.cmp_index()] if args.cmp_index() != -1 else (ranked[1] if len(ranked) > 1 else laps[-1])
        )
        if cmp is ref:
            cmp = next((l for l in ranked if l is not ref), ref)
        available = [c for c in channels if log.has(c)]
        missing = [c for c in channels if not log.has(c)]
        if missing:
            print(f"# note: not in this log: {', '.join(missing)}")
        result = lapsmod.overlay(log, [ref, cmp], available, step=args.step)
        distance, delta = lapsmod.time_delta(
            result["laps"][0], result["laps"][1], result["distance"]
        )
        delta_nan = np.isnan(delta)
        if delta_nan.all():
            print("no overlapping distance between the two laps")
            return 1
        payload = {
            "source": log.path.name,
            "reference": ref.label,
            "compare": cmp.label,
            "step": args.step,
            "distance": distance.tolist(),
            "delta": delta.tolist(),
            "laps": [
                {
                    key: (value.tolist() if hasattr(value, "tolist") else value)
                    for key, value in lap.items()
                }
                for lap in result["laps"]
            ],
        }
        out = Path(args.out) if args.out else Path(log.path).with_suffix(".delta.json")
        out.write_text(json.dumps(payload), encoding="utf-8")
        finite = np.flatnonzero(~np.isnan(delta))
        if finite.size == 0:
            print("the two laps share no overlapping distance")
            return 1
        k = int(finite[-1])
        worst = distance[int(finite[np.nanargmax(delta[finite])])]
        best = distance[int(finite[np.nanargmin(delta[finite])])]
        print(
            f"{log.path.name}: lap {ref.label} ({ref.lap_time:.3f}s) vs lap {cmp.label} "
            f"({cmp.lap_time:.3f}s), lap-time difference {cmp.lap_time - ref.lap_time:+.3f}s\n"
            f"  delta at {distance[k]:.0f} m (common distance): {delta[k]:+.3f}s\n"
            f"  biggest loss @ {worst:.0f} m ({np.nanmax(delta):+.3f}s), "
            f"biggest gain @ {best:.0f} m ({np.nanmin(delta):+.3f}s)\n  wrote {out}"
        )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    import numpy as np

    with ldmod.LogFile.read(args.file) as log:
        names = [c.strip() for c in args.channels.split(",")] if args.channels else ["Time"] + [c.name for c in log.channels]
        explicit_time = any(n.lower() == "time" for n in names)
        cols: dict[str, np.ndarray] = {}
        if not explicit_time:
            cols["Time"] = np.arange(0, int(round(log.duration * log.sample_rate)) + 1) / log.sample_rate
        for name in names:
            if name.lower() == "time":
                continue
            if not log.has(name):
                print(f"# skipped (not in log): {name}")
                continue
            ch = log.channel(name)
            factor = max(1, int(round(log.sample_rate / ch.sample_rate)))
            values = log.values(ch)
            if factor > 1:
                values = np.repeat(values, factor)
            cols[name] = values
    out = Path(args.out)
    length = min(len(v) for v in cols.values())
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(list(cols))
        for i in range(length):
            writer.writerow([f"{cols[c][i]:.6g}" for c in cols])
    print(f"wrote {out} ({length} rows x {len(cols)} columns)")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    frame = store.query(args.sql, args.parquet)
    print(frame.to_string(index=False))
    return 0


def cmd_series(args: argparse.Namespace) -> int:
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    frame = store.read_series(
        args.parquet, channels, start=args.start, end=args.end
    )
    if args.csv:
        frame.to_csv(args.csv, index=False)
        print(f"wrote {args.csv} ({len(frame)} rows x {len(frame.columns)} columns)")
        return 0
    with pd.option_context("display.max_rows", 30, "display.width", 160):
        print(frame.head(12).to_string(index=False))
    print(f"\n{len(frame)} rows x {len(frame.columns)} columns")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    channels = [c.strip() for c in args.channels.split(",")] if args.channels else None
    out = Path(args.out) if args.out else Path("out") / f"{Path(args.file).stem}.html"
    with ldmod.LogFile.read(args.file) as log:
        out = rendermod.render_html(
            log, out, channels=channels, ref=args.ref, cmp=args.cmp, buckets=args.buckets
        )
        try:
            laps = lapsmod.detect_laps(log)
            complete = [l for l in laps if l.complete]
            best = min((l.lap_time for l in complete), default=None)
            best_txt = "-" if best is None else f"{best:.3f}s"
            print(
                f"{len(laps)} 段 / {len(complete)} 完整圈, 最快 {best_txt}; "
                f"速度源 {derive.speed_channel(log)}"
            )
        except ValueError as exc:
            print(f"# {exc}")
    print(f"wrote {out} ({out.stat().st_size / 1e6:.2f} MB self-contained HTML)")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the local workbench server (stdlib only, LAN shareable)."""
    from . import server

    roots = [Path(p) for p in args.data] or [Path("i2pro_data")]
    server.serve(
        roots,
        host=args.host,
        port=args.port,
        buckets=args.buckets,
        cache_size=args.cache,
        open_browser=args.open,
    )
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Render every session in --data to a self-contained HTML, plus an index."""
    data = Path(args.data)
    out = Path(args.out)
    if not data.is_dir():
        print(f"# 数据目录不存在: {data}")
        return 1
    files = sorted(data.glob("*.ld"))
    if not files:
        print(f"# {data} 里没有 .ld 文件")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for path in files:
        target = out / f"{path.stem}.html"
        print(f"  {path.name} ...", end="", flush=True)
        try:
            with ldmod.LogFile.read(path) as log:
                laps: list = []
                try:
                    laps = lapsmod.detect_laps(log)
                except ValueError:
                    pass
                rendermod.render_html(log, target, channels=None, buckets=args.buckets)
                meta = log.metadata()
            complete = [l for l in laps if l.complete]
            best = min((l.lap_time for l in complete), default=None)
            rows.append(
                {
                    "file": path.name,
                    "target": target.name,
                    "device": meta["device"],
                    "date": meta["log_date"],
                    "duration": meta["duration"],
                    "channels": meta["channels"],
                    "complete_laps": len(complete),
                    "best_lap": best,
                    "size_mb": target.stat().st_size / 1e6,
                    "error": None,
                }
            )
            print(f" {target.stat().st_size / 1e6:.2f} MB"
                  + (f", {len(complete)} 完整圈, 最快 {best:.3f}s" if best else ""))
        except Exception as exc:  # one broken log must not stop the batch
            rows.append({"file": path.name, "error": f"{type(exc).__name__}: {exc}"})
            print(f" 失败: {exc}")

    index = out / "index.html"
    index.write_text(_snapshot_index(rows, data), encoding="utf-8")
    ok = sum(1 for r in rows if not r["error"])
    print(f"\n生成 {ok}/{len(rows)} 个快照 -> {out}")
    print(f"双击这个文件开始看: {index}")
    for row in rows:
        if row["error"]:
            print(f"  ! {row['file']}: {row['error']}")
    if args.open:
        import webbrowser

        webbrowser.open(index.resolve().as_uri())
    return 0 if ok else 1


def cmd_import(args: argparse.Namespace) -> int:
    """Copy or move .ld/.ldx files into the data folder so they show up."""
    from . import importer

    destination = Path(args.data)
    results = importer.import_paths(args.paths, destination, move=args.move)
    imported = [r for r in results if "error" not in r]
    failed = [r for r in results if "error" in r]

    for row in imported:
        note = ""
        if row["file"].lower().endswith(".ld"):
            try:
                with ldmod.LogFile.read(row["path"]) as log:
                    meta = log.metadata()
                    note = f"{meta['channels']} 通道 · {meta['duration']:.0f} s · {meta['device']}"
            except Exception as exc:  # imported but unreadable -> say so now
                note = f"⚠ 无法解析: {type(exc).__name__}: {exc}"
        print(f"  + {row['file']}  ({row['bytes'] / 1e6:.1f} MB)  {note}")
    for row in failed:
        print(f"  ! {row['source']}: {row['error']}")

    verb = "移动" if args.move else "复制"
    print(f"\n{verb} {len(imported)} 个文件到 {destination.resolve()}")
    if failed:
        print(f"{len(failed)} 个文件跳过")
    if imported:
        print("下一步: 双击 启动.bat 打开工作台，场次列表里就能看到它们。")
    return 0 if imported else 1


def _snapshot_index(rows: list[dict], data_dir: Path) -> str:
    """A plain index page so the snapshot folder is self-explanatory."""
    body = []
    for row in rows:
        if row["error"]:
            body.append(
                f"<tr><td>{row['file']}</td><td colspan='5' style='color:#ff5d6c'>"
                f"{row['error']}</td></tr>"
            )
            continue
        best = "--" if row["best_lap"] is None else f"{row['best_lap']:.3f} s"
        body.append(
            "<tr>"
            f"<td><a href=\"{row['target']}\">{row['file']}</a></td>"
            f"<td>{row['device']}</td><td>{row['date']}</td>"
            f"<td>{row['duration']:.0f} s</td><td>{row['channels']}</td>"
            f"<td>{row['complete_laps']}</td><td>{best}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<meta charset="utf-8">
<title>i3pro 快照</title>
<style>
 body {{ margin:0; background:#0f1115; color:#e6e9ef;
        font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif; }}
 header {{ padding:20px 26px; border-bottom:1px solid #2b313c; }}
 h1 {{ margin:0 0 4px; font-size:18px; }}
 p {{ margin:0; color:#8b94a7; }}
 main {{ padding:16px 26px 40px; }}
 table {{ border-collapse:collapse; width:100%; }}
 th, td {{ text-align:left; padding:6px 10px; border-bottom:1px solid #2b313c; }}
 th {{ color:#8b94a7; font-weight:600; }}
 a {{ color:#4cc2ff; text-decoration:none; }}
 a:hover {{ text-decoration:underline; }}
 code {{ background:#1e232c; padding:1px 6px; border-radius:4px; }}
</style>
<header>
  <h1>i3pro 快照</h1>
  <p>双击任意一行离线查看。数据已经嵌进每个 HTML 里，不需要服务器、不需要联网。</p>
</header>
<main>
<table>
 <tr><th>场次</th><th>设备</th><th>日期</th><th>时长</th><th>通道</th><th>完整圈</th><th>最快圈</th></tr>
 {''.join(body) or "<tr><td colspan='7'>没有可用的场次</td></tr>"}
</table>
<p style="margin-top:18px">
  快照的波形是提前抽稀好的：放大到很细的时间段会看到折线。要看全分辨率细节，回到项目根目录
  双击 <code>启动.bat</code>，用交互模式。
</p>
<p style="margin-top:6px;color:#8b94a7">数据目录: {data_dir}</p>
</main>
"""


def cmd_track(args: argparse.Namespace) -> int:
    """Print a coarse ASCII trace of the detected lap times (quick sanity check)."""
    with ldmod.LogFile.read(args.file) as log:
        try:
            laps = lapsmod.detect_laps(log)
        except ValueError as exc:
            print(f"# {exc}")
            return 1
        for row in lapsmod.lap_table(log, laps):
            bar = "#" * max(1, int(round(row["lap_time"] / 2)))
            flag = "" if row["complete"] else "  (进出场/泊车/异常段)"
            print(f"{row['lap']:>3} {row['lap_time']:8.3f}s {row['delta_to_best']:+7.3f} "
                  f"{row['distance']:7.0f}m {bar}{flag}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="i3pro", description="MoTeC i2 Pro 数据工具链 (i3pro)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("info", help="打印 .ld 文件概览")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("channels", help="列出通道")
    p.add_argument("file")
    p.add_argument("--filter", help="按名称子串过滤")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_channels)

    p = sub.add_parser("convert", help=".ld -> Parquet (+ metadata JSON)")
    p.add_argument("files", nargs="+")
    p.add_argument("--out", default="out")
    p.add_argument("--channels", help="逗号分隔的通道白名单")
    p.add_argument("--rate", type=float, help="输出采样率 (默认取日志最高采样率)")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("laps", help="圈速表")
    p.add_argument("file")
    p.add_argument("--json", help="同时写出 JSON")
    p.add_argument("--mode", choices=["auto", "run", "figure8", "beacons"],
                   help="切分方式: auto=GPS 自动挑门, run=按起步/停车分段, "
                        "figure8=八字按环分段, beacons=只用信标")
    p.add_argument("--gate", action="append", default=[],
                   help="手工信标 lat,lon[:名字]，可重复；给两个就得到两条独立圈速序列")
    p.add_argument("--save", action="store_true",
                   help="把 --mode/--gate 写进 <场次>.laps.json")
    p.set_defaults(func=cmd_laps)

    p = sub.add_parser("delta", help="距离轴双圈对比")
    p.add_argument("file")
    p.add_argument("--ref", help="基准圈标签 (如 2)")
    p.add_argument("--cmp", help="对比圈标签 (如 3)")
    p.add_argument("--ref-index", type=int, default=0)
    p.add_argument("--cmp-index", type=int, default=-1)
    p.add_argument("--channels", help="叠加的通道, 逗号分隔")
    p.add_argument("--step", type=float, default=1.0, help="距离插值步长 [m]")
    p.add_argument("--out", help="输出 JSON 路径")
    p.set_defaults(func=cmd_delta)

    p = sub.add_parser("export", help="导出 CSV")
    p.add_argument("file")
    p.add_argument("--channels", help="逗号分隔的通道 (默认全部)")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("query", help="对 Parquet 数据集执行 SQL")
    p.add_argument("sql")
    p.add_argument("--parquet", nargs="+", required=True)
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("series", help="快速抽取若干通道的时间段 (列式裁剪)")
    p.add_argument("parquet")
    p.add_argument("--channels", required=True, help="逗号分隔的通道名")
    p.add_argument("--from", dest="start", type=float, help="起始时间 [s]")
    p.add_argument("--to", dest="end", type=float, help="结束时间 [s]")
    p.add_argument("--csv", help="导出到 CSV")
    p.set_defaults(func=cmd_series)

    p = sub.add_parser("render", help="生成自包含 HTML 分析工作台")
    p.add_argument("file")
    p.add_argument("--out", help="输出 HTML (默认 out/<场次名>.html)")
    p.add_argument("--channels")
    p.add_argument("--ref", help="基准圈标签")
    p.add_argument("--cmp", help="对比圈标签")
    p.add_argument("--buckets", type=int, default=rendermod.DEFAULT_BUCKETS,
                   help="每通道下采样像素列数")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("serve", help="启动本地 Web 工作台 (局域网可共享链接)")
    p.add_argument("--data", action="append", default=[], help="数据目录 (可重复, 默认 i2pro_data)")
    p.add_argument("--host", default="127.0.0.1", help="监听地址 (0.0.0.0 = 局域网可访问)")
    p.add_argument("--port", type=int, default=8731)
    p.add_argument("--buckets", type=int, default=rendermod.DEFAULT_BUCKETS)
    p.add_argument("--cache", type=int, default=3, help="内存中保留的已解析场次数量")
    p.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("snapshot", help="把每个场次导出成离线 HTML 快照")
    p.add_argument("--data", default="i2pro_data", help=".ld 所在目录")
    p.add_argument("--out", default="out", help="输出目录")
    p.add_argument("--buckets", type=int, default=rendermod.DEFAULT_BUCKETS,
                   help="每通道下采样像素列数 (越大越清晰、文件越大)")
    p.add_argument("--open", action="store_true", help="生成后打开索引页")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("import", help="把 .ld/.ldx 导入数据目录（可拖拽到 导入数据.bat 上）")
    p.add_argument("paths", nargs="+", help="文件或目录，可多个")
    p.add_argument("--data", default="i2pro_data", help="目标数据目录")
    p.add_argument("--move", action="store_true", help="移动而不是复制")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("track", help="圈速柱状速览")
    p.add_argument("file")
    p.set_defaults(func=cmd_track)
    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
