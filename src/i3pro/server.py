"""A tiny local web server: ``python -m i3pro serve``.

Pure standard library (``http.server``) - no FastAPI, no Flask, nothing to
install, nothing to keep patched. It exists so a whole squad can point at one
laptop on the pit wall, and so every view is a URL you can paste into the team
chat instead of a screenshot::

    http://192.168.1.20:8731/session/20260524-%E8%80%90%E4%B9%85%E6%AD%A3%E8%B5%9B
        ?channels=Vx%20KF,G%20Force%20Long&ref=10&cmp=12

The parsed ``.ld`` files are memory mapped and kept in an LRU cache, so opening
the same session twice costs nothing.
"""

from __future__ import annotations

import json
import math
import socket
import threading
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import numpy as np

from . import csvlog, derive, importer, laps as lapsmod, render, store
from . import ld as ldmod

__all__ = ["SessionLibrary", "serve", "make_handler"]

#: Refuse anything larger than this in one upload (the biggest log here is
#: 119 MB, so 2 GB leaves room without letting a stray file fill the disk).
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024


def _json_safe(value):
    """Turn numpy scalars/arrays into JSON, mapping NaN/Inf to ``null``."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    if isinstance(value, (np.floating, float)):
        return None if not math.isfinite(float(value)) else float(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    return value


def dumps(value) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, allow_nan=False)


class SessionLibrary:
    """Discover ``.ld`` files under one or more roots and cache parsed logs."""

    def __init__(self, roots: list[str | Path], cache_size: int = 3):
        self.roots = [Path(r) for r in roots]
        self.cache_size = max(1, cache_size)
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, ldmod.LogFile] = OrderedDict()

    # ------------------------------------------------------------- discovery
    def _paths(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        for root in self.roots:
            if not root.exists():
                continue
            for pattern in ("*.ld", "*.csv"):
                for path in sorted(root.rglob(pattern)):
                    name = path.stem
                    if name in found:
                        # Two sources, one stem: keep both, so a CSV export of a
                        # session that also has its .ld is not silently hidden.
                        name = f"{name} ({path.suffix.lstrip('.').lower()})"
                    found.setdefault(name, path)
        return found

    def names(self) -> list[str]:
        return list(self._paths())

    def path_of(self, name: str) -> Path | None:
        return self._paths().get(name)

    # ------------------------------------------------------------------- load
    def get(self, name: str) -> ldmod.LogFile:
        path = self.path_of(name)
        if path is None:
            raise KeyError(name)
        key = str(path)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        log = csvlog.open_session(path)
        with self._lock:
            self._cache[key] = log
            while len(self._cache) > self.cache_size:
                _old_key, old = self._cache.popitem(last=False)
                old.close()
        return log

    def summary(self, name: str) -> dict:
        log = self.get(name)
        laps = render.detect(log)
        complete = [l for l in laps if l.complete]
        best = min((l.lap_time for l in complete), default=None)
        meta = log.metadata()
        meta["laps"] = len(laps)
        meta["complete_laps"] = len(complete)
        meta["best_lap"] = None if best is None else round(best, 3)
        meta["name"] = name
        meta["url"] = f"/session/{quote(name)}"
        return _json_safe(meta)

    def listing(self) -> list[dict]:
        out = []
        for name in self.names():
            try:
                out.append(self.summary(name))
            except Exception as exc:  # a broken file must not kill the index
                out.append({"name": name, "error": str(exc)})
        return out

    def close(self) -> None:
        with self._lock:
            for log in self._cache.values():
                log.close()
            self._cache.clear()

    def upload_dir(self) -> Path:
        """Where an uploaded log goes: the first configured data root."""
        root = self.roots[0] if self.roots else Path("i2pro_data")
        root.mkdir(parents=True, exist_ok=True)
        return root


def _csv_arg(query: dict, key: str) -> list[str]:
    raw = (query.get(key) or [""])[0]
    return [c.strip() for c in raw.split(",") if c.strip()]


def _float_arg(query: dict, key: str, default=None):
    raw = (query.get(key) or [None])[0]
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_arg(query: dict, key: str, default: int) -> int:
    value = _float_arg(query, key, None)
    return default if value is None else int(value)


def make_handler(library: SessionLibrary, buckets: int = render.DEFAULT_BUCKETS):
    class Handler(BaseHTTPRequestHandler):
        server_version = "i3pro"
        protocol_version = "HTTP/1.1"

        # ------------------------------------------------------------ helpers
        def _send(self, body: bytes, status: int = 200, ctype: str = "application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, value, status: int = 200):
            self._send(dumps(value).encode("utf-8"), status)

        def _html(self, text: str, status: int = 200):
            self._send(text.encode("utf-8"), status, "text/html; charset=utf-8")

        def _error(self, status: int, message: str):
            self._json({"error": message}, status)

        def log_message(self, fmt, *args):  # keep the console clean
            pass

        # --------------------------------------------------------------- GET
        def do_GET(self):  # noqa: N802 - http.server API
            self.dispatch("GET")

        def do_HEAD(self):  # noqa: N802 - http.server API
            self.dispatch("HEAD")

        def do_POST(self):  # noqa: N802 - http.server API
            self.dispatch("POST")

        def do_PUT(self):  # noqa: N802 - http.server API
            self.dispatch("PUT")

        def dispatch(self, method: str):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            parts = [unquote(p) for p in parsed.path.split("/") if p]
            try:
                self.route(parts, query, method)
            except KeyError as exc:
                self._error(404, f"未知场次: {exc.args[0]}")
            except FileNotFoundError as exc:
                self._error(404, str(exc))
            except ValueError as exc:
                self._error(400, str(exc))
            except Exception as exc:  # pragma: no cover - defensive
                self._error(500, f"{type(exc).__name__}: {exc}")

        # ------------------------------------------------------------- routes
        def route(self, parts: list[str], query: dict, method: str = "GET") -> None:
            if not parts:
                return self._html(index_page(library))
            if parts[0] == "api":
                return self.api(parts[1:], query, method)
            if parts[0] == "session" and len(parts) >= 2:
                return self.session_page(parts[1], query)
            if parts[0] == "favicon.ico":
                return self._send(b"", 204)
            self._error(404, "no such path")

        def session_page(self, name: str, query: dict) -> None:
            log = library.get(name)
            payload = render.build_payload(
                log,
                channels=_csv_arg(query, "channels") or None,
                ref=(query.get("ref") or [None])[0],
                cmp=(query.get("cmp") or [None])[0],
                buckets=_int_arg(query, "buckets", buckets),
                api_base="/api",
                step=_float_arg(query, "step", 1.0),
            )
            payload["session"] = name
            self._html(render.render_page(payload))

        def api(self, parts: list[str], query: dict, method: str = "GET") -> None:
            if not parts:
                return self._error(404, "no such api path")
            if parts[0] == "sessions":
                return self._json(library.listing())
            if parts[0] == "upload":
                return self.upload(query, method)
            if parts[0] != "session" or len(parts) < 3:
                return self._error(404, "no such api path")
            name, action = parts[1], parts[2]
            log = library.get(name)

            if action == "info":
                payload = render.build_payload(
                    log,
                    channels=_csv_arg(query, "channels") or None,
                    ref=(query.get("ref") or [None])[0],
                    cmp=(query.get("cmp") or [None])[0],
                    buckets=_int_arg(query, "buckets", buckets),
                    api_base="/api",
                    step=_float_arg(query, "step", 1.0),
                    with_track=False,
                )
                payload["session"] = name
                return self._json(payload)

            if action == "trace":
                names = _csv_arg(query, "channels") or render.pick_channels(log)
                time = np.arange(int(round(log.duration * log.sample_rate)) + 1) / log.sample_rate
                try:
                    distance = derive.distance_series(log)[: time.size]
                except ValueError:
                    distance = None
                out = {}
                for channel in names:
                    if not log.has(channel):
                        continue
                    out[channel] = render.trace(
                        log,
                        channel,
                        time,
                        distance,
                        _int_arg(query, "buckets", buckets),
                        start=_float_arg(query, "from"),
                        end=_float_arg(query, "to"),
                    )
                return self._json(out)

            if action == "points":
                # raw samples for the scatter component (never min/max decimated)
                names = _csv_arg(query, "channels")
                if not names:
                    return self._error(400, "points requires ?channels=")
                time = np.arange(int(round(log.duration * log.sample_rate)) + 1) / log.sample_rate
                return self._json(
                    render.points(
                        log,
                        names,
                        time,
                        start=_float_arg(query, "from"),
                        end=_float_arg(query, "to"),
                        max_points=_int_arg(query, "max", 30000),
                    )
                )

            if action == "overview":
                name = (query.get("channel") or [None])[0] or (
                    next((n for n in render.SPEED_FOR_COLORING if log.has(n)), None)
                )
                if name is None or not log.has(name):
                    return self._json(None)
                time = np.arange(int(round(log.duration * log.sample_rate)) + 1) / log.sample_rate
                payload = render.trace(
                    log, name, time, None, _int_arg(query, "buckets", 900)
                )
                payload["name"] = name
                return self._json(payload)

            if action == "overlay":
                laps = render.detect(log)
                by_label = {l.label: l for l in laps}
                ref = by_label.get((query.get("ref") or [""])[0])
                cmp = by_label.get((query.get("cmp") or [""])[0])
                channels = _csv_arg(query, "channels") or render.overlay_channels(log)
                overlay = render.build_overlay(log, [ref, cmp], channels, _float_arg(query, "step", 1.0))
                return self._json(overlay)

            if action == "track":
                return self._json(
                    render.track_payload(
                        log,
                        points=_int_arg(query, "points", 1500),
                        start=_float_arg(query, "from"),
                        end=_float_arg(query, "to"),
                    )
                )

            if action == "laps":
                # laps after the saved beacons/mode, plus the config itself
                if method == "PUT":
                    return self.save_laps(log)
                config = lapsmod.load_config(log.path)
                laps = render.detect(log)
                return self._json(
                    {
                        "config": config.as_dict(),
                        "laps": lapsmod.lap_table(log, laps),
                    }
                )

            if action == "at":
                # Distance axis -> time: a crossing is a moment, but on the
                # distance axis the cursor is metres. Answered from the distance
                # series itself, not from the downsampled plot.
                distance = _float_arg(query, "distance")
                if distance is None:
                    return self._error(400, "需要 distance= 参数（米）")
                when = lapsmod.time_at_distance(log, distance)
                if when is None:
                    return self._error(
                        400, "这个距离不在本场已行驶的范围内（或本场没有距离轴）"
                    )
                return self._json({"distance": distance, "time": when})

            if action == "export":
                return self._error(501, "export is done from the UI or the CLI")

            self._error(404, "no such api action")

        # --------------------------------------------------------- lap editing
        def save_laps(self, log) -> None:
            """PUT /api/session/<name>/laps with the beacon / mode config."""
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if not body:
                return self._error(400, "空请求体")
            try:
                data = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                return self._error(400, f"JSON 解析失败: {exc}")
            if not isinstance(data, dict):
                return self._error(400, "需要一个 JSON 对象")
            # The client sends the whole config, so the name rules (trim, empty
            # falls back, duplicate suffix, truncation) and the trusted-mark
            # migration are applied here - one implementation, every caller.
            previous = lapsmod.load_config(log.path)
            config = lapsmod.reconcile_edits(previous, lapsmod.LapConfig.from_dict(data))
            problem = lapsmod.check_new_crossings(previous, config, log.duration)
            if problem:
                return self._error(400, problem)
            before = render.detect(log)              # laps as they are right now
            path = lapsmod.save_config(log.path, config)
            laps = render.detect(log)
            notice = lapsmod.insertion_notice(previous, config, len(before), len(laps))
            self._json(
                {
                    "saved": path.name,
                    "config": config.as_dict(),
                    "laps": lapsmod.lap_table(log, laps),
                    "notice": notice,
                }
            )

        # ------------------------------------------------------------ upload
        def upload(self, query: dict, method: str) -> None:
            """PUT /api/upload?name=<file.ld> with the raw bytes as the body.

            Raw body instead of multipart keeps this dependency-free (no
            ``cgi``/``email`` parsing to get wrong) and lets the browser stream
            a 100 MB log straight from the file picker.
            """
            if method not in ("PUT", "POST"):
                self.close_connection = True
                return self._error(405, "上传请用 PUT")
            name = (query.get("name") or [""])[0]
            try:
                clean = importer.safe_name(name)
            except ValueError as exc:
                self.close_connection = True
                return self._error(400, str(exc))
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                self.close_connection = True
                return self._error(400, "请求体为空")
            if length > MAX_UPLOAD_BYTES:
                self.close_connection = True
                return self._error(
                    413, f"文件太大: {length / 1e6:.0f} MB > {MAX_UPLOAD_BYTES / 1e6:.0f} MB"
                )
            try:
                info = importer.store_stream(library.upload_dir(), clean, self.rfile, length)
            except ValueError as exc:
                self.close_connection = True
                return self._error(400, str(exc))
            except OSError as exc:
                self.close_connection = True
                return self._error(500, f"写盘失败: {exc}")

            summary: dict = {"ok": True, **info}
            target = Path(info["path"])
            if clean.lower().endswith(".ld"):
                try:
                    with ldmod.LogFile.read(target) as log:
                        meta = log.metadata()
                        tokens = render.detect(log)
                    summary.update(
                        device=meta["device"],
                        duration=round(meta["duration"], 1),
                        channels=meta["channels"],
                        complete_laps=len([l for l in tokens if l.complete]),
                        url=f"/session/{quote(info['stem'])}",
                    )
                except Exception as exc:  # stored, but not readable
                    summary["warning"] = f"文件已保存，但解析失败: {exc}"
            print(
                f"  ↑ 导入 {info['file']} ({info['bytes'] / 1e6:.1f} MB)"
                + (f" · {summary.get('channels')} 通道" if summary.get("channels") else "")
                + (f" · {summary['warning']}" if summary.get("warning") else "")
            )
            self._json(summary)

    return Handler


def _lan_addresses() -> list[str]:
    """Best-effort list of this machine's non-loopback IPv4 addresses.

    The first entry is the address of the interface that would carry the default
    route (found via ``connect`` on a UDP socket - no packets are sent), which is
    the one a team mate can actually reach. The rest are other adapters, kept
    only as a hint because laptops often have WSL / Docker / VirtualBox
    interfaces that look like LANs but are not.
    """
    primary = None
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        primary = probe.getsockname()[0]
    except OSError:
        primary = None
    finally:
        probe.close()

    others: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address.startswith("127.") or address.startswith("169.254."):
                continue
            if address == primary or address in others:
                continue
            others.append(address)
    except OSError:
        pass

    if primary and not primary.startswith("127."):
        return [primary] + others[:3]
    return others[:4]


_IMPORT_BLOCK = """
<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;
            background:#171a21;border:1px solid #2b313c;border-radius:8px;
            padding:10px 12px;margin:0 0 14px">
  <input id="files" type="file" multiple accept=".ld,.ldx" style="display:none">
  <button id="pickBtn" style="background:#1d3b52;border:1px solid #4cc2ff;color:#e6e9ef;
          border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px">
    选择 .ld 文件导入
  </button>
  <span style="color:#8b94a7;font-size:12px">
    或者把文件直接拖进这个窗口 · 也可以把 .ld 拖到 <code>导入数据.bat</code> 上
  </span>
  <span id="importMsg" style="color:#4cc2ff;font-size:12px;margin-left:auto"></span>
</div>
<script>
(function () {
  var input = document.getElementById("files");
  var msg = document.getElementById("importMsg");
  var busy = false;

  document.getElementById("pickBtn").addEventListener("click", function () {
    if (!busy) input.click();
  });

  async function upload(list) {
    var files = Array.prototype.slice.call(list);
    if (!files.length || busy) return;
    busy = true;
    for (var i = 0; i < files.length; i++) {
      var file = files[i];
      msg.textContent = "上传 " + (i + 1) + "/" + files.length + ": " + file.name
        + " (" + (file.size / 1e6).toFixed(1) + " MB) …";
      try {
        var res = await fetch("/api/upload?name=" + encodeURIComponent(file.name),
                              { method: "PUT", body: file });
        var body = await res.json().catch(function () { return {}; });
        if (!res.ok) throw new Error(body.error || ("HTTP " + res.status));
      } catch (err) {
        msg.style.color = "#ff5d6c";
        msg.textContent = "导入失败: " + file.name + " — " + err.message;
        busy = false;
        return;
      }
    }
    msg.textContent = "导入完成，正在刷新…";
    location.reload();
  }

  input.addEventListener("change", function () { upload(input.files); });
  document.addEventListener("dragover", function (e) { e.preventDefault(); });
  document.addEventListener("drop", function (e) {
    e.preventDefault();
    if (e.dataTransfer && e.dataTransfer.files) upload(e.dataTransfer.files);
  });
})();
</script>
"""


def index_page(library: SessionLibrary, error: str | None = None) -> str:
    """A no-frills session picker; the real UI is the workbench itself."""
    if error:
        return (
            "<!doctype html><meta charset='utf-8'><title>i3pro</title>"
            "<body style='font:14px system-ui;padding:32px;background:#0f1115;color:#e6e9ef'>"
            f"<h1>i3pro 本地服务</h1><p>{error}</p>"
            "</body>"
        )
    rows = []
    for entry in library.listing():
        if "error" in entry:
            rows.append(
                f"<tr><td>{entry['name']}</td><td colspan='5' style='color:#ff5d6c'>"
                f"{entry['error']}</td></tr>"
            )
            continue
        best = "--" if entry.get("best_lap") is None else f"{entry['best_lap']:.3f} s"
        rows.append(
            "<tr>"
            f"<td><a href='{entry['url']}'>{entry['name']}</a></td>"
            f"<td>{entry.get('device', '')}</td>"
            f"<td>{entry.get('log_date', '')} {entry.get('log_time', '')}</td>"
            f"<td>{entry.get('duration', 0):.0f} s</td>"
            f"<td>{entry.get('channels', 0)}</td>"
            f"<td>{entry.get('complete_laps', 0)}</td>"
            f"<td>{best}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<meta charset="utf-8">
<title>i3pro - 场次</title>
<style>
 body {{ margin:0; background:#0f1115; color:#e6e9ef;
        font:14px/1.5 "Segoe UI","Microsoft YaHei",system-ui,sans-serif; }}
 header {{ padding:22px 28px; border-bottom:1px solid #2b313c; }}
 h1 {{ margin:0 0 4px; font-size:18px; }}
 p {{ margin:0; color:#8b94a7; }}
 main {{ padding:18px 28px; }}
 table {{ border-collapse:collapse; width:100%; }}
 th, td {{ text-align:left; padding:7px 10px; border-bottom:1px solid #2b313c; }}
 th {{ color:#8b94a7; font-weight:600; }}
 a {{ color:#4cc2ff; text-decoration:none; }}
 a:hover {{ text-decoration:underline; }}
</style>
<header>
  <h1>i3pro 本地服务</h1>
  <p>选择一个试车场次开始分析；所有数据都在本机解析，不上传。</p>
</header>
<main>
{_IMPORT_BLOCK}
<table>
 <tr><th>场次</th><th>设备</th><th>日期</th><th>时长</th><th>通道</th><th>完整圈</th><th>最快圈</th></tr>
 {''.join(rows) or '<tr><td colspan="7">没有找到 .ld 文件</td></tr>'}
</table>
</main>
"""


def bind(host: str, port: int, handler, attempts: int = 10) -> ThreadingHTTPServer:
    """Bind the first free port in ``[port, port + attempts)``.

    The launcher always asks for 8731; if a previous workbench is still running
    (or something else grabbed the port) we quietly move to the next one instead
    of dying with a bind error the user has to decode.
    """
    last: OSError | None = None
    for candidate in range(port, port + max(1, attempts)):
        try:
            return ThreadingHTTPServer((host, candidate), handler)
        except OSError as exc:
            last = exc
    raise OSError(
        f"端口 {port}-{port + attempts - 1} 都被占用，用 --port {port + attempts} 换一个"
        f"（最后一次错误: {last}）"
    )


def serve(
    roots: list[str | Path],
    host: str = "127.0.0.1",
    port: int = 8731,
    buckets: int = render.DEFAULT_BUCKETS,
    cache_size: int = 3,
    open_browser: bool = False,
    ready: threading.Event | None = None,
    port_attempts: int = 10,
) -> None:
    """Run the workbench server until Ctrl-C."""
    library = SessionLibrary(roots, cache_size=cache_size)
    handler = make_handler(library, buckets)
    try:
        httpd = bind(host, port, handler, port_attempts)
    except OSError:
        library.close()
        raise
    actual_port = httpd.server_address[1]
    local = f"http://127.0.0.1:{actual_port}/"
    print(f"i3pro 本地服务已启动: {local}")
    if host in ("0.0.0.0", "::"):
        addresses = _lan_addresses()
        if addresses:
            print(f"  发给队友(局域网): http://{addresses[0]}:{actual_port}/")
            for address in addresses[1:]:
                print(f"  其他网卡(多半是虚拟网卡): http://{address}:{actual_port}/")
        else:
            print("  局域网: 没找到网卡地址，用 ipconfig 查一下本机 IP")
        print("  ⚠ 第一次运行 Windows 防火墙可能弹窗，选“允许访问”。")
    else:
        print("  只监听本机；要发给队友用 --host 0.0.0.0")
    names = library.names()
    print(f"  {len(names)} 个场次, 数据目录: {', '.join(str(r) for r in roots)}")
    if not names:
        print("  ⚠ 这些目录里没有 .ld 文件，用 --data <目录> 指定试车数据所在位置")
    print("  Ctrl-C 停止")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(local)).start()
    if ready is not None:
        ready.set()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止中...")
    finally:
        httpd.server_close()
        library.close()
