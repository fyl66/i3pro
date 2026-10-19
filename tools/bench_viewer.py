"""性能基准：把每一张性能票的"快了多少"变成一条能重跑的命令。

    python tools\\bench_viewer.py                      # 两份金标准 × 五档 × 12 通道
    python tools\\bench_viewer.py --http               # 再加一层真 HTTP 往返
    python tools\\bench_viewer.py --session <某场次>    # 只量这一份
    python tools\\bench_viewer.py --channels 8 --repeat 5
    python tools\\bench_viewer.py --json out\\bench.json

**两层数字，分开报**（`docs/spec-marple-parity.md` 的性能票都照这个口径写）：

* **计算**：走真实 ``/trace`` 路径（``SessionLibrary`` + ``Api.handle``，不起 socket），
  量服务端耗时与载荷大小（JSON 字节、gzip 字节）。
* **HTTP**：起一个临时 ``ThreadingHTTPServer``（端口自动分配），用 ``urllib`` 发同一条
  请求，量含序列化与传输的往返耗时。

带浏览器的那一层**不在这儿**——它在 ``tools\\verify_clicks.py``（A91 已有 2000 ms 判据）。
这里报的是"服务端 + HTTP"，别把它当成"用户看到的秒数"。

每个数字都是**这次运行实测**的：换一台机器、换一份数据，数字就会变。这是机器的事实，
不是脚本的毛病——所以输出要原样抄进 ACCEPTANCE，不许把上一次的数搬过去。
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import statistics
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from i3pro import api as apimod  # noqa: E402
from i3pro.cli import _utf8_console  # noqa: E402  # 控制台编码只有那一处实现
from i3pro import library as librarymod  # noqa: E402
from i3pro import render, resolution, server, timebase  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

#: 两份金标准（文件名即身份）。`I3PRO_GOLDEN` 里先找，找不到再看日常数据目录。
GOLDEN = ("20260908-cjh 高避5圈.ld", "20260524-耐久正赛.ld")

#: 曲线连接方式。基准固定用画图默认的那一档，换别的档位是另一件事，不混进来。
UPSAMPLE = render.DEFAULT_UPSAMPLE


def data_roots() -> list[Path]:
    """日常数据目录：``I3PRO_DATA`` 优先，否则仓库里的 ``i2pro_data``。"""
    env = os.environ.get("I3PRO_DATA")
    return [Path(env)] if env else [ROOT / "i2pro_data"]


def golden_paths(roots: list[Path]) -> list[Path]:
    """两份金标准的路径；缺的那份只说一句跳过（缺数据的机器照样能跑其它场次）。"""
    golden_dir = os.environ.get("I3PRO_GOLDEN")
    bases = ([Path(golden_dir)] if golden_dir else []) + list(roots)
    found: list[Path] = []
    for name in GOLDEN:
        for base in bases:
            candidate = base / name
            if candidate.exists():
                found.append(candidate)
                break
        else:
            print(f"跳过 {name}：I3PRO_GOLDEN 与数据目录里都没有这一份。",
                  file=sys.stderr)
    return found


def buckets_to_try() -> list[int]:
    """五档绘图分辨率（与界面上那个下拉同一张表，见 ``i3pro.resolution``）。"""
    return [count for _key, count, _label in resolution.PLOT_RESOLUTIONS]


def pick_channels(log, limit: int) -> list[str]:
    """这次要量的通道——**就是界面默认挑出来的那几条**（``render.pick_channels``）。

    不在工具里另写一份挑选规则：基准量的必须是用户真会看到的那套通道，否则数字
    好看也没意义（那条规则还会跳过状态通道与整段没变化的死通道）。
    """
    return [name for name in render.pick_channels(log, limit) if log.has(name)]


def trace_query(channels: list[str], buckets: int, wire: str = "json") -> dict:
    """``/trace`` 的查询参数。形状跟着 ``api.csv_arg`` / ``api.int_arg`` 走。"""
    query = {
        "channels": [",".join(channels)],
        "buckets": [str(int(buckets))],
        "upsample": [UPSAMPLE],
    }
    if wire != "json":
        query["format"] = [wire]
    return query


def resolve_name(library, path: Path) -> str:
    """这个文件在场次库里叫什么名字。

    同名 ``.ld`` 与 ``.csv`` 并存时，库会给第二个加后缀，所以不能拿 ``path.stem``
    当答案——按路径反查，查不到才回落。
    """
    for name in library.names():
        if library.path_of(name) == path:
            return name
    return path.stem


def measure_compute(api, name: str, channels: list[str], buckets: int,
                    repeat: int, wire: str = "json") -> dict:
    """量服务端计算：中位数耗时 + 载荷大小。

    先空跑一次当热身：解析、数学通道、距离积分这些是"打开这场次"的一次性代价，
    要量的是**同一个窗口被反复取数**有多快（缩放、改配置、切组件都是这一种）。
    """
    query = trace_query(channels, buckets, wire)
    api.handle(["session", name, "trace"], query, "GET")
    times: list[float] = []
    body = b""
    for _ in range(max(1, int(repeat))):
        started = time.perf_counter()
        response = api.handle(["session", name, "trace"], query, "GET")
        times.append((time.perf_counter() - started) * 1000.0)
        body = response.body
    return {
        "ms": statistics.median(times),
        "bytes": len(body),
        "gzip_bytes": len(gzip.compress(body, 6)),
    }


def measure_http(library, name: str, channels: list[str], buckets: int,
                 repeat: int, accept_encoding: str | None = None,
                 wire: str = "json") -> dict:
    """量真 HTTP 往返：临时起服务、真发请求（**不开浏览器**）。

    ``accept_encoding="gzip"`` 量的是浏览器那条路（多一层压缩的 CPU，少一大截字节）；
    不给就是同一台机器上的纯往返，用来和 ticket #51 / #53 / #63 的数字对读。

    每次都在**干净的**服务上打第一炮——那一下是冷的（要真算、真压）；后面同一条请求
    是热的（响应缓存命中，ticket #54）。两个数分开报：只说一个会把"缓存命中"当成
    "这台机器算得快"。
    """
    handler = server.make_handler(library, render.DEFAULT_BUCKETS)
    httpd = server.bind("127.0.0.1", 0, handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        query = urllib.parse.urlencode({
            "channels": ",".join(channels),
            "buckets": int(buckets),
            "upsample": UPSAMPLE,
            **({"format": wire} if wire != "json" else {}),
        })
        url = (f"http://127.0.0.1:{port}/api/session/"
               f"{urllib.parse.quote(name)}/trace?{query}")
        request = urllib.request.Request(
            url, headers={"Accept-Encoding": accept_encoding} if accept_encoding else {})
        body = b""
        cold_ms = 0.0
        times: list[float] = []
        for index in range(max(1, int(repeat)) + 1):
            started = time.perf_counter()
            with urllib.request.urlopen(request) as reply:
                body = reply.read()
            elapsed = (time.perf_counter() - started) * 1000.0
            if index == 0:
                cold_ms = elapsed
            else:
                times.append(elapsed)
        return {"cold_ms": cold_ms, "ms": statistics.median(times),
                "bytes": len(body)}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5.0)


def bench_session(path: Path, channels: int, repeat: int, with_http: bool) -> dict:
    """一个场次跑完五档，返回这次实测的全部数字。"""
    library = librarymod.SessionLibrary([path.parent], cache_size=1, maths_root=ROOT)
    try:
        name = resolve_name(library, path)
        api = apimod.Api(library, render.DEFAULT_BUCKETS)
        log = library.get(name)
        names = pick_channels(log, channels)
        rows = []
        for buckets in buckets_to_try():
            row = {"buckets": buckets}
            json_row = measure_compute(api, name, names, buckets, repeat, wire="json")
            bin_row = measure_compute(api, name, names, buckets, repeat, wire="bin")
            row.update({
                "json_ms": json_row["ms"],
                "json_bytes": json_row["bytes"],
                "json_gzip_bytes": json_row["gzip_bytes"],
                "bin_ms": bin_row["ms"],
                "bin_bytes": bin_row["bytes"],
                "bin_gzip_bytes": bin_row["gzip_bytes"],
            })
            if with_http:
                json_plain = measure_http(library, name, names, buckets, repeat,
                                          wire="json")
                json_gzip = measure_http(library, name, names, buckets, repeat,
                                         accept_encoding="gzip", wire="json")
                bin_gzip = measure_http(library, name, names, buckets, repeat,
                                        accept_encoding="gzip", wire="bin")
                row["json_http_cold_ms"] = json_plain["cold_ms"]
                row["json_http_ms"] = json_plain["ms"]
                row["json_gzip_cold_ms"] = json_gzip["cold_ms"]
                row["json_gzip_ms"] = json_gzip["ms"]
                row["json_wire_bytes"] = json_gzip["bytes"]
                row["bin_http_cold_ms"] = bin_gzip["cold_ms"]
                row["bin_http_ms"] = bin_gzip["ms"]
                row["bin_wire_bytes"] = bin_gzip["bytes"]
            rows.append(row)
            print(f"  {buckets:>6d} 桶  JSON {row['json_ms']:>7.1f} ms / "
                  f"{row['json_bytes'] / 1024:>7.1f} KB · gzip "
                  f"{row['json_gzip_bytes'] / 1024:>7.1f} KB  |  BIN "
                  f"{row['bin_ms']:>7.1f} ms / {row['bin_bytes'] / 1024:>7.1f} KB "
                  f"· gzip {row['bin_gzip_bytes'] / 1024:>7.1f} KB"
                  + (f"  |  HTTP JSON {row['json_http_cold_ms']:>7.1f}/"
                     f"{row['json_http_ms']:>6.1f} ms · BIN "
                     f"{row['bin_http_cold_ms']:>7.1f}/{row['bin_http_ms']:>6.1f} ms "
                     f"· 上线 {row['bin_wire_bytes'] / 1024:>7.1f} KB"
                     if with_http else ""))
        return {
            "path": str(path),
            "session": name,
            "channels": names,
            "channel_count": len(names),
            # 主时间基的样本数：抽稀判据 `样本数 > 2 × 桶` 靠它。
            "samples": timebase.length(log),
            "rows": rows,
        }
    finally:
        library.close()


def _fmt_header(with_http: bool) -> str:
    head = (f"  {'桶':>6s}   {'JSON 计算 ms':>12s}   {'JSON KB':>8s}"
            f"   {'BIN 计算 ms':>11s}   {'BIN KB':>8s}")
    if not with_http:
        return head
    return (head + f"   {'HTTP JSON 冷/热 ms':>18s}"
                   f"   {'HTTP BIN 冷/热 ms':>17s}   {'BIN 上线 KB':>11s}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="i3pro 性能基准：服务端计算 + HTTP 往返（两份金标准，五档分辨率）",
    )
    parser.add_argument("--session", action="append", default=None,
                        help="只量这一份场次（可重复；默认两份金标准）")
    parser.add_argument("--data", action="append", default=None,
                        help="数据目录（可重复；默认 I3PRO_DATA 或 i2pro_data）")
    parser.add_argument("--channels", type=int, default=12,
                        help="每次请求几条通道（默认 12）")
    parser.add_argument("--repeat", type=int, default=3,
                        help="每档量几次、取中位数（默认 3）")
    parser.add_argument("--http", action="store_true",
                        help="加一层临时服务的真 HTTP 往返")
    parser.add_argument("--json", dest="json_out", default=None,
                        help="把这次实测的原始数字写成 JSON（给后面几张票比对）")
    args = parser.parse_args(argv)
    # 这份表要原样抄进 ACCEPTANCE：管道按 GBK 编出去的汉字，粘到文档里就是乱码。
    _utf8_console()

    roots = [Path(p) for p in args.data] if args.data else data_roots()
    paths = ([Path(p) for p in args.session] if args.session
             else golden_paths(roots))
    missing = [str(p) for p in paths if not p.exists()]
    for path in missing:
        print(f"找不到这场数据：{path}。下一步：用 --data 指对目录，"
              f"或设 I3PRO_DATA / I3PRO_GOLDEN。", file=sys.stderr)
    paths = [p for p in paths if p.exists()]
    if not paths:
        print("没有可以量的场次，什么都没跑。", file=sys.stderr)
        return 1

    print(f"通道数上限 {args.channels} · 每档 {args.repeat} 次取中位数 · "
          f"预热一次（解析与派生列不计入）")
    report = {"channels_limit": args.channels, "repeat": args.repeat,
              "http": bool(args.http), "sessions": []}
    for path in paths:
        print(f"\n{path.name}")
        print(_fmt_header(args.http))
        report["sessions"].append(
            bench_session(path, args.channels, args.repeat, args.http))

    if args.json_out:
        target = Path(args.json_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(f"\n原始数字已写到 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
