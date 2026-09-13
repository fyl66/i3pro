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
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import numpy as np

from . import (
    csvlog,
    derive,
    histogram as histogrammod,
    importer,
    laps as lapsmod,
    notes as notesmod,
    spectrum as spectrummod,
    maths,
    render,
    report as reportmod,
    sections,
    store,
)
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
    # 布尔要排在整数前面：Python 里 ``isinstance(True, int)`` 是真的，顺序反了
    # 就会把 ``{"ok": true}`` 写成 ``{"ok": 1}``，界面拿到的"真/假"变成数字。
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    return value


def dumps(value) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, allow_nan=False)


class SessionLibrary:
    """Discover ``.ld`` files under one or more roots and cache parsed logs."""

    def __init__(
        self,
        roots: list[str | Path],
        cache_size: int = 3,
        maths_root: str | Path | None = None,
    ):
        self.roots = [Path(r) for r in roots]
        self.cache_size = max(1, cache_size)
        #: 全局数学定义（``<仓库>/maths/global.json``）的根目录。
        self.maths_root = maths_root
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, ldmod.LogFile] = OrderedDict()
        self._maths_cache = maths.DerivedCache()
        #: 场次文件 -> (会话对象身份, 定义指纹)。定义或数据一改就重算。
        self._maths_attached: dict[str, tuple] = {}
        self._maths_errors: dict[str, list[dict]] = {}
        #: 场次文件 -> 上一次信标编辑之前的那一版配置，供"撤销上一步"用。
        #: 按 ticket #6 的约定**只留一版**（一个槽），而且只在内存里：服务一重启
        #: 就没了，撤的是"这个进程里刚才那一步"，不是历史。
        self._laps_undo: dict[str, lapsmod.LapConfig] = {}

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
        self.apply_maths(path, log)
        return log

    # ------------------------------------------------------------------ maths
    def apply_maths(self, path: str | Path, log) -> None:
        """把这个场次生效的数学通道算出来挂上去；没变就不重算。"""
        path = Path(path)
        key = str(path)
        stamp = (
            id(log),
            _file_stamp(maths.config_path(path)),
            _file_stamp(maths.global_path(self.maths_root)),
        )
        if self._maths_attached.get(key) == stamp:
            return
        # 定义一变，旧的缓存列就都不作数了（包括引用关系变了的那种）
        self._maths_cache.clear()
        # 定义文件本身坏了也不影响看数据：apply_to_session 把原因当成一条报错返回
        _added, errors = maths.apply_to_session(log, self.maths_root, self._maths_cache)
        self._maths_errors[key] = errors
        self._maths_attached[key] = stamp

    def maths_state(self, name: str) -> dict:
        """界面要的全部数学状态：生效的定义、谁盖住了谁、哪些算不出来。"""
        path = self.path_of(name)
        if path is None:
            raise KeyError(name)
        effective = maths.load_effective(path, self.maths_root)
        return {
            "definitions": [d.as_dict() | {"scope": d.scope} for d in effective.definitions],
            "shadowed": list(effective.shadowed),
            "errors": list(self._maths_errors.get(str(path), [])),
            "local_path": maths.config_path(path).name,
            "global_path": str(maths.global_path(self.maths_root)),
            "functions": maths.function_catalogue(),
        }

    def maths_names(self, log) -> tuple[str, ...]:
        """这个场次现在生效的数学通道名（本地 + 全局），坏定义就当没有。

        保存时要判断"用户写的名字算不算存在"：本地定义引用全局定义完全合法，
        所以不能只看正在提交的那一份。
        """
        try:
            effective = maths.load_effective(log.path, self.maths_root)
        except maths.MathError:
            return ()
        return tuple(definition.name for definition in effective.definitions)

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

    # -------------------------------------------------------------- lap edits
    def remember_laps(self, path: str | Path, config: lapsmod.LapConfig) -> None:
        """记下这次编辑**之前**的那一版，撤销时把它原样交回去。"""
        with self._lock:
            self._laps_undo[str(path)] = config

    def laps_undo_slot(self, path: str | Path) -> lapsmod.LapConfig | None:
        with self._lock:
            return self._laps_undo.get(str(path))

    def forget_laps_undo(self, path: str | Path) -> None:
        """一级撤销：用掉就清空这个槽，不做重做。"""
        with self._lock:
            self._laps_undo.pop(str(path), None)

    def close(self) -> None:
        with self._lock:
            for log in self._cache.values():
                log.close()
            self._cache.clear()
            self._laps_undo.clear()

    def upload_dir(self) -> Path:
        """Where an uploaded log goes: the first configured data root."""
        root = self.roots[0] if self.roots else Path("i2pro_data")
        root.mkdir(parents=True, exist_ok=True)
        return root


def _csv_arg(query: dict, key: str) -> list[str]:
    raw = (query.get(key) or [""])[0]
    return [c.strip() for c in raw.split(",") if c.strip()]


def _file_stamp(path: str | Path) -> tuple:
    """``(存在?, mtime_ns, size)``：判断一份侧车文件改没改过的便宜办法。"""
    try:
        stat = Path(path).stat()
    except OSError:
        return (False, 0, 0)
    return (True, stat.st_mtime_ns, stat.st_size)


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


def _filter_arg(query: dict, key: str) -> str | None:
    """``filter=all|corner|straight`` → ``None`` / ``corner`` / ``straight``。

    写错了直接拒绝：静默当成 all 会让人以为"只看弯道"生效了，而表里其实是全部。
    """
    raw = (query.get(key) or [None])[0]
    if raw in (None, "", "all"):
        return None
    if raw not in sections.KIND_LABELS:
        raise ValueError(
            f"filter 只认 all / {' / '.join(sorted(sections.KIND_LABELS))}，收到的是 {raw!r}"
        )
    return str(raw)


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
            # 撤销的上一版只在这个进程的内存里，页面自己算不出来，只能由服务告诉它
            payload["laps_can_undo"] = self._can_undo(log, lapsmod.load_config(log.path))
            self._html(render.render_page(payload))

        def api(self, parts: list[str], query: dict, method: str = "GET") -> None:
            if not parts:
                return self._error(404, "no such api path")
            if parts[0] == "sessions":
                return self._json(library.listing())
            if parts[0] == "maths":
                # /api/maths/functions —— 表达式编辑器要的函数表
                if len(parts) >= 2 and parts[1] == "functions":
                    return self._json(maths.function_catalogue())
                return self._error(404, "no such api path")
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

            if action == "histogram":
                # 一条通道在当前窗口里的分布（ticket #9）。窗口默认由界面传当前
                # 缩放区间；不传就是整场。
                channel = (query.get("channel") or [None])[0]
                if not channel:
                    return self._error(
                        400, "histogram 需要 ?channel= 参数（要统计哪条通道）"
                    )
                if not log.has(channel):
                    return self._error(
                        400,
                        f"本场次没有 {channel!r} 这条通道。先在左侧「通道」里搜一下名字，"
                        f"或者把 ?channel= 换成 /api/session/<名>/info 里列出的通道名。",
                    )
                time = np.arange(int(round(log.duration * log.sample_rate)) + 1) / log.sample_rate
                gate = (query.get("gate") or [None])[0] or None
                colour = (query.get("colour") or [None])[0] or None
                try:
                    return self._json(
                        render.histogram(
                            log,
                            channel,
                            time,
                            bins=_int_arg(query, "bins", histogrammod.DEFAULT_BINS),
                            start=_float_arg(query, "from"),
                            end=_float_arg(query, "to"),
                            gate=gate,
                            gate_mode=(query.get("gate_mode") or ["nonzero"])[0],
                            gate_lo=_float_arg(query, "gate_min"),
                            gate_hi=_float_arg(query, "gate_max"),
                            colour=colour,
                        )
                    )
                except ValueError as exc:
                    return self._error(400, str(exc))

            if action == "spectrum":
                # 一条通道在当前窗口里的频谱（ticket #10）。按通道自己的采样率算，
                # 不是主时间基——慢通道被"保持"拉长会有假高频。
                channel = (query.get("channel") or [None])[0]
                if not channel:
                    return self._error(
                        400, "spectrum 需要 ?channel= 参数（要分析哪条通道）"
                    )
                if not log.has(channel):
                    return self._error(
                        400,
                        f"本场次没有 {channel!r} 这条通道。先在左侧「通道」里搜一下名字，"
                        f"或者把 ?channel= 换成 /api/session/<名>/info 里列出的通道名。",
                    )
                try:
                    return self._json(
                        render.spectrum(
                            log,
                            channel,
                            start=_float_arg(query, "from"),
                            end=_float_arg(query, "to"),
                            points=_int_arg(query, "points", spectrummod.DEFAULT_POINTS),
                            window=(query.get("window") or [spectrummod.DEFAULT_WINDOW])[0],
                            overlap=_float_arg(query, "overlap") if query.get("overlap")
                            else spectrummod.DEFAULT_OVERLAP,
                            smooth=_int_arg(query, "smooth", 1),
                            scale=(query.get("scale") or [spectrummod.DEFAULT_SCALE])[0],
                        )
                    )
                except ValueError as exc:
                    return self._error(400, str(exc))

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
                        "can_undo": self._can_undo(log, config),
                    }
                )

            if action == "sections":
                # 赛道区段（i2 Pro 的 Track Sections）：GET 看当前生效的，
                # PUT 重切（auto）或手工改边界/名字。
                if method == "PUT":
                    return self.save_sections(log)
                return self._json(render.sections_payload(log, render.detect(log)))

            if action == "notes":
                # 注释（ticket #15）：GET 拿这一场的注释与落点，PUT 存整张表。
                # 它有自己的侧车 `<场次>.notes.json`，和信标 / 区段互不影响。
                if method == "PUT":
                    return self.save_notes(log)
                return self._json({"notes": render.notes_payload(log, render.track_payload(log))})

            if action == "report":
                # 时间报告 / 通道报告（ticket #11）。GET 一张或两张表；
                # 带 csv=time|channels 时直接吐 CSV，方便命令行与队友核对。
                return self.session_report(log, query)

            if action == "maths":
                # 数学通道：GET 看当前生效的定义，PUT 存，POST 试算一条式子
                if method == "PUT":
                    return self.save_maths(name, log, query)
                if method == "POST":
                    return self.preview_maths(log)
                return self._json(library.maths_state(name))

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
        def session_report(self, log, query: dict) -> None:
            """GET /api/session/<name>/report[?...] —— 时间报告与通道报告。

            参数：``table=time|channels``（只要一张，省一半计算）、
            ``filter=all|corner|straight``、``by=lap|section``、``lap=<圈标签>``、
            ``channels=A,B``、``csv=time|channels``（直接吐 CSV）。

            两张表都由 :func:`i3pro.render.report_payload` 算，界面、快照、命令行
            是同一个出口——不会出现"浏览器里是这个数、CLI 里是那个数"。
            """
            laps = render.detect(log)
            wanted_table = (query.get("table") or [None])[0]
            if wanted_table not in (None, "", "time", "channels"):
                raise ValueError(f"table 只认 time / channels，收到的是 {wanted_table!r}")
            wanted_csv = (query.get("csv") or [None])[0]
            if wanted_csv in ("", None):
                wanted_csv = None
            if wanted_csv is not None and wanted_csv not in ("time", "channels"):
                raise ValueError(f"csv 只认 time / channels，收到的是 {wanted_csv!r}")
            if wanted_table and wanted_csv and wanted_table != wanted_csv:
                raise ValueError(
                    f"table={wanted_table} 与 csv={wanted_csv} 不是同一张表；"
                    f"只要 CSV 的话去掉 table= 就行"
                )

            payload = render.report_payload(
                log,
                laps,
                channels=_csv_arg(query, "channels") or None,
                kind=_filter_arg(query, "filter"),
                by=(query.get("by") or ["lap"])[0],
                lap_label=(query.get("lap") or [None])[0],
            )
            if payload.get("error") is not None:
                return self._error(400, str(payload["error"]))

            only = wanted_csv or wanted_table or ""
            if only in ("time", "channels"):
                payload = {
                    "notice": payload.get("notice"),
                    "error": None,
                    only: payload[only],
                }
                if wanted_csv:
                    table = payload[only]
                    text = reportmod.to_csv(table["columns"], table["rows"])
                    return self._send(
                        ("\ufeff" + text).encode("utf-8"), 200, "text/csv; charset=utf-8"
                    )
            return self._json(payload)

        def save_laps(self, log) -> None:
            """PUT /api/session/<name>/laps：整份信标 / 切分方式配置，或 ``{"undo": true}``。

            #4 的改名、#5 的插入穿越、以及"✕"删信标都从这里过，所以"上一步"也在这里
            记：一次真的改动了配置的保存，会把**保存之前**的那一版放进内存里的槽。
            撤销就是把那一版再提交一次，走的是同一条落盘路径。
            """
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
            previous = lapsmod.load_config(log.path)
            if data.get("undo"):
                return self.undo_laps(log, previous)
            # The client sends the whole config, so the name rules (trim, empty
            # falls back, duplicate suffix, truncation) and the trusted-mark
            # migration are applied here - one implementation, every caller.
            config = lapsmod.reconcile_edits(previous, lapsmod.LapConfig.from_dict(data))
            problem = lapsmod.check_new_crossings(previous, config, log.duration)
            if problem:
                return self._error(400, problem)
            self._commit_laps(log, config, previous)

        def undo_laps(self, log, current) -> None:
            """撤销上一步信标编辑：把内存里那一版按同一条保存路径再提交一次。

            不再跑 ``check_new_crossings``——要交回去的那一版本来就存在过、也被接受过。
            重跑一次反而有害：那条规则会放过边车里**已经存在**的越界穿越，于是用户删掉
            一条旧侧车里的越界穿越之后，就再也撤不回来了。
            """
            slot = library.laps_undo_slot(log.path)
            if slot is None:
                return self._error(
                    400,
                    "没有可撤销的一步了：上一版配置只留在内存里，服务重启过、或还没在这个"
                    "页面上改过信标都会是空的。先改一次信标（改名 / ＋ 穿越 / ✕），再来撤销。",
                )
            config = lapsmod.undo_config(current, slot)
            if config is None:
                return self._error(
                    400,
                    "当前这一版已经和上一版一样了，没有可撤销的一步；改一次信标再来撤销。",
                )
            self._commit_laps(log, config, current, notice="已撤销上一步信标编辑",
                              forget_undo=True)

        def save_notes(self, log) -> None:
            """PUT /api/session/<name>/notes：整张注释表（客户端发全量）。

            规则只有一份实现（``notes.normalize``）：文字不能为空、时刻要落在这一场里、
            最多 500 条。这里不做别的判断——注释**不参与**切圈 / 比圈 / 报表，
            所以存完只回新表，不需要重算圈速（这正是它和信标最大的区别）。
            """
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if not body:
                return self._error(400, "空请求体：注释表要带 {\"notes\": [...]} 一起发过来")
            try:
                data = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                return self._error(400, f"JSON 解析失败: {exc}")
            if not isinstance(data, dict) or "notes" not in data:
                return self._error(
                    400,
                    '注释表要写成 {"notes": [{"time": 12.5, "text": "……"}]}，'
                    "现在这个请求体里没有 notes 字段。",
                )
            try:
                notes = notesmod.normalize(data.get("notes"), log.duration)
            except notesmod.NoteError as exc:
                return self._error(400, str(exc))
            path = notesmod.save_notes(log.path, notes)
            self._json(
                {
                    "saved": path.name,
                    "notes": render.notes_payload(log, render.track_payload(log)),
                }
            )

        def _commit_laps(self, log, config, previous, notice=None, forget_undo=False) -> None:
            """落盘 → 重算圈速表 → 记下"上一步"，三条编辑路径共用这一段。"""
            before = render.detect(log)              # laps as they are right now
            path = lapsmod.save_config(log.path, config)
            laps = render.detect(log)
            if notice is None:
                notice = lapsmod.insertion_notice(previous, config, len(before), len(laps))
            if forget_undo:
                library.forget_laps_undo(log.path)   # 一级撤销，用掉就没有了
            elif not lapsmod.same_config(config, previous):
                # 只有真的改出一版新的才更新槽：一次没改动的保存不该把上一步冲掉
                library.remember_laps(log.path, previous)
            self._json(
                {
                    "saved": path.name,
                    "config": config.as_dict(),
                    "laps": lapsmod.lap_table(log, laps),
                    "notice": notice,
                    "can_undo": self._can_undo(log, config),
                }
            )

        def _can_undo(self, log, current) -> bool:
            """撤销按钮该不该亮：槽里那一版和当前这一版确实不一样才算数。"""
            return lapsmod.undo_config(current, library.laps_undo_slot(log.path)) is not None

        # ------------------------------------------------------------ upload
        def save_sections(self, log) -> None:
            """PUT /api/session/<name>/sections：重切（``auto``）或手工改边界 / 名字。

            两条路落在同一个侧车里，区别只有一个：**手工改过之后 ``edited`` 就立
            起来**，再点"重切"会先被挡住——ticket #7 要的就是"自动切分不会悄悄
            覆盖手工改动"。确认要覆盖时带上 ``force``（对应 i2 Pro Track Editor
            里的"重新生成"）。
            """
            try:
                data = self._read_json()
            except (ValueError, UnicodeDecodeError) as exc:
                return self._error(400, str(exc))
            if not isinstance(data, dict):
                return self._error(400, "需要一个 JSON 对象")
            recognized = render.detect(log)
            lap = sections.reference_lap(recognized)
            if lap is None:
                return self._error(400, "本场还没有圈，先切圈（放一个信标）再来分区段")
            try:
                stored = sections.load_config(log.path)
                length = float(sections.lap_distance(log, lap)[-1])
            except ValueError as exc:
                return self._error(400, str(exc))
            notice = None

            if data.get("auto"):
                if stored is not None and stored.edited and not data.get("force"):
                    return self._json(
                        {
                            "error": "这一场的区段被手工改过，重切会覆盖你的边界与名字；"
                                     "确认要覆盖就再点一次「重切」",
                            "needs_force": True,
                        },
                        400,
                    )
                basis = str(data.get("basis") or (stored.basis if stored else "lateral_g"))
                try:
                    sensitivity = data.get("sensitivity")
                    sensitivity = (
                        float(sensitivity) if sensitivity is not None
                        else (stored.sensitivity if stored and stored.basis == basis
                              else sections.DEFAULT_SENSITIVITY)
                    )
                    raw_min = data.get("min_length_m")
                    min_length = (
                        float(raw_min) if raw_min is not None
                        else (stored.min_length_m if stored else sections.DEFAULT_MIN_SECTION_M)
                    )
                    config = sections.auto_for_log(log, lap, basis, sensitivity, min_length)
                except (TypeError, ValueError) as exc:
                    return self._error(400, str(exc))
                if stored is not None and stored.edited:
                    notice = "已按新参数重新切分，手工改过的边界与名字被覆盖了"
            else:
                try:
                    # 手工编辑必须带边界：只发一段名字的话，"改第 2 段"到底指哪一段
                    # 全靠猜。界面本来就整份发，这条规矩是给别的调用方看的。
                    if not data.get("boundaries"):
                        return self._error(
                            400,
                            "手工改区段要给出 boundaries（至少两条：0 与本圈长度）；"
                            "要按曲率／横向加速度自动切一次就带上 auto",
                        )
                    merged = {**(stored.as_dict() if stored else {}), **data}
                    # 只发了一半的 kinds / names 时，后半段沿用侧车里那一份：
                    # 否则"改第一条的名字"会把后面几条顺手打回默认编号。
                    if stored is not None:
                        for key in ("kinds", "names"):
                            if key in data:
                                incoming_list = list(data.get(key) or [])
                                kept = list(getattr(stored, key))
                                if len(incoming_list) < len(kept):
                                    merged[key] = incoming_list + kept[len(incoming_list):]
                    incoming = sections.SectionConfig.from_dict(merged)
                    config, notice = sections.normalize(
                        sections.dedupe_names(incoming), length
                    )
                except (TypeError, ValueError) as exc:
                    return self._error(400, str(exc))
                # 手工改的边界是按**当前这条参考圈**量的，记下来，换参考圈时能提醒
                unchanged = stored is not None and sections.same_layout(stored, config)
                config = replace(
                    config,
                    reference_label=lap.label,
                    edited=stored.edited if unchanged else True,
                )

            path = sections.save_config(log.path, config)
            payload = render.sections_payload(log, recognized, config, notice=notice)
            payload["saved"] = path.name
            self._json(payload)

        # ------------------------------------------------------ maths editing
        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if not body:
                raise ValueError("空请求体")
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, (dict, list)):
                raise ValueError("需要一个 JSON 对象或数组")
            return data

        def save_maths(self, name: str, log, query: dict) -> None:
            """PUT /api/session/<name>/maths[?scope=global] —— 存一份定义并重算。"""
            scope = (query.get("scope") or ["local"])[0]
            if scope not in ("local", "global"):
                return self._error(400, f"scope 只能是 local 或 global，收到 {scope!r}")
            try:
                data = self._read_json()
            except (ValueError, UnicodeDecodeError) as exc:
                return self._error(400, str(exc))
            if isinstance(data, list):
                data = {"definitions": data}
            try:
                incoming = maths.MathSet.from_dict(data, scope=scope)
            except maths.MathError as exc:
                return self._error(400, str(exc))
            # 语法在这一步就挡掉：坏式子不进侧车，省得下次打开场次才发现。
            # 本地定义还能引用另一份作用域里的定义，所以"哪些名字算存在"要把现在
            # 生效的那一份也算上，否则存本地定义时会误报"本场次没有这个通道"。
            known = maths.known_names(
                log, [*incoming.definitions, *library.maths_names(log)]
            )
            strict = scope == "local"
            for definition in incoming.definitions:
                try:
                    maths.compile_expr(
                        definition.expr, known=known, strict_channels=strict
                    )
                except maths.MathError as exc:
                    return self._error(
                        400, f"数学通道 `{definition.name}` 的表达式有问题：{exc}"
                    )
            names = [d.name for d in incoming.definitions]
            duplicates = sorted({n for n in names if names.count(n) > 1})
            if duplicates:
                return self._error(
                    400,
                    f"同名数学通道出现多次：{'、'.join(duplicates)}。"
                    f"一条定义一个名字，改掉重复的。",
                )
            target = (
                maths.config_path(log.path)
                if scope == "local"
                else maths.global_path(library.maths_root)
            )
            try:
                path = incoming.save(target)
            except OSError as exc:
                return self._error(500, f"写盘失败：{exc}")
            # 定义变了 -> apply_maths 会因为指纹变化重新算一遍
            library.apply_maths(log.path, log)
            state = library.maths_state(name)
            state["saved"] = path.name
            state["scope"] = scope
            self._json(state)

        def preview_maths(self, log) -> None:
            """POST /api/session/<n>/maths {expr} —— 存之前先试算一条式子。"""
            try:
                data = self._read_json()
            except (ValueError, UnicodeDecodeError) as exc:
                return self._error(400, str(exc))
            expr = str((data or {}).get("expr", "")).strip() if isinstance(data, dict) else ""
            if not expr:
                return self._error(400, "需要 {\"expr\": \"...\"} 这样的请求体")
            try:
                plan = maths.compile_expr(
                    expr, known=maths.known_names(log), strict_channels=True
                )
            except maths.MathError as exc:
                return self._error(400, str(exc))
            try:
                values = maths.evaluate(expr, log)
            except maths.MathError as exc:
                return self._json({"ok": False, "error": str(exc), "channels": list(plan.channels)})
            finite = values[np.isfinite(values)]
            self._json(
                {
                    "ok": True,
                    "channels": list(plan.channels),
                    "functions": list(plan.functions),
                    "notes": list(plan.notes),
                    "samples": int(values.size),
                    "finite": int(finite.size),
                    "min": None if not finite.size else float(finite.min()),
                    "max": None if not finite.size else float(finite.max()),
                    "mean": None if not finite.size else float(finite.mean()),
                }
            )

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
    maths_root: str | Path | None = None,
) -> None:
    """Run the workbench server until Ctrl-C."""
    library = SessionLibrary(roots, cache_size=cache_size, maths_root=maths_root)
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
