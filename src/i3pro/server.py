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
import threading
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import numpy as np

from . import derive, laps as lapsmod, render, store
from . import ld as ldmod

__all__ = ["SessionLibrary", "serve", "make_handler"]


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
            for path in sorted(root.rglob("*.ld")):
                found.setdefault(path.stem, path)
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
        log = ldmod.LogFile.read(path)
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
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            parts = [unquote(p) for p in parsed.path.split("/") if p]
            try:
                self.route(parts, query)
            except KeyError as exc:
                self._error(404, f"未知场次: {exc.args[0]}")
            except FileNotFoundError as exc:
                self._error(404, str(exc))
            except ValueError as exc:
                self._error(400, str(exc))
            except Exception as exc:  # pragma: no cover - defensive
                self._error(500, f"{type(exc).__name__}: {exc}")

        def do_HEAD(self):  # noqa: N802 - http.server API
            self.do_GET()

        # ------------------------------------------------------------- routes
        def route(self, parts: list[str], query: dict) -> None:
            if not parts:
                return self._html(index_page(library))
            if parts[0] == "api":
                return self.api(parts[1:], query)
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

        def api(self, parts: list[str], query: dict) -> None:
            if not parts:
                return self._error(404, "no such api path")
            if parts[0] == "sessions":
                return self._json(library.listing())
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

            if action == "overlay":
                laps = render.detect(log)
                by_label = {l.label: l for l in laps}
                ref = by_label.get((query.get("ref") or [""])[0])
                cmp = by_label.get((query.get("cmp") or [""])[0])
                channels = _csv_arg(query, "channels") or render.overlay_channels(log)
                overlay = render.build_overlay(log, [ref, cmp], channels, _float_arg(query, "step", 1.0))
                return self._json(overlay)

            if action == "track":
                return self._json(render.track_payload(log))

            if action == "laps":
                return self._json(lapsmod.lap_table(log, render.detect(log)))

            if action == "export":
                return self._error(501, "export is done from the UI or the CLI")

            self._error(404, "no such api action")

    return Handler


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
<table>
 <tr><th>场次</th><th>设备</th><th>日期</th><th>时长</th><th>通道</th><th>完整圈</th><th>最快圈</th></tr>
 {''.join(rows) or '<tr><td colspan="7">没有找到 .ld 文件</td></tr>'}
</table>
</main>
"""


def serve(
    roots: list[str | Path],
    host: str = "127.0.0.1",
    port: int = 8731,
    buckets: int = render.DEFAULT_BUCKETS,
    cache_size: int = 3,
    open_browser: bool = False,
    ready: threading.Event | None = None,
) -> None:
    """Run the workbench server until Ctrl-C."""
    library = SessionLibrary(roots, cache_size=cache_size)
    httpd = ThreadingHTTPServer((host, port), make_handler(library, buckets))
    actual_port = httpd.server_address[1]
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{actual_port}/"
    print(f"i3pro 本地服务已启动: {url}")
    print(f"  {len(library.names())} 个场次, 数据目录: {', '.join(str(r) for r in roots)}")
    print("  Ctrl-C 停止")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    if ready is not None:
        ready.set()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止中...")
    finally:
        httpd.server_close()
        library.close()
