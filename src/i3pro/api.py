"""HTTP API 层：``/api/...`` 的每个动作在哪里被回答（ticket #23）。

``server.py`` 只管 HTTP 管道（收字节、发字节、拼响应）；**判断与计算全在这里**。
这么切有两个直接好处：

* 不用起 socket 就能把一个动作当普通函数调用——
  ``Api.handle(parts, query, method, body) -> Response``。以前每个动作都要跑一遍
  ``HTTPConnection``，慢，而且失败信息常常只剩一句"连接被重置"。
* 加一个动作 = 在 ``_Call.ACTIONS`` 那张表里加一行。以前是在一个 769 行、
  25 个内嵌函数的请求闭包里翻。

``Api`` 只有"库 + 桶数"两样东西，一个进程一个；每条请求造一个 ``_Call``，
请求体（``Body``）挂在它身上——**多线程下不共享"当前请求"状态**。
"""

from __future__ import annotations

import json
import math
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote

import numpy as np

from . import (
    axes as axesmod,
    beacons as beaconsmod,
    derive,
    export as exportmod,
    gpsfix,
    histogram as histogrammod,
    importer,
    laps as lapsmod,
    maths,
    notes as notesmod,
    render,
    report as reportmod,
    sections,
    spectrum as spectrummod,
    timebase,
)
from . import ld as ldmod
from .library import dumps

__all__ = ["Api", "Body", "Response", "csv_arg", "float_arg", "int_arg", "filter_arg"]

#: 一次上传的上限（最大的场次 119 MB，2 GB 留足余量又不至于把磁盘写满）。
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class Response:
    """一次请求的回复：状态码 + 字节 + 内容类型（+ 附加头 / 是否断开）。"""

    status: int = 200
    body: bytes = b""
    ctype: str = "application/json; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = ()
    close: bool = False
    #: 要**流式**发出的文件（导出用它）。给了它就以文件长度作 ``Content-Length``、
    #: 边读边发；发完、出错、客户端断开三种情况都会把这个临时文件删掉，
    #: 所以 200 MB 的导出既不会整份进内存，也不会在磁盘上留垃圾。
    path: Path | None = None


class ClientGone(Exception):
    """客户端在导出写到一半时断开（点了「取消」）。

    它**不是错误**：没人收这份文件了，越早停越好——临时目录由 ``act_export`` 清掉。
    """


class Body:
    """请求体：**按需读**。上传可能上百 MB，不能一进 dispatch 就读进内存。

    ``alive`` 是 HTTP 管道给的一个"客户端还在不在"的探针（可选）。导出用它早停：
    队友点了「取消」，浏览器会把连接关掉，我们不必把剩下几百 MB 写完再删。
    """

    def __init__(self, length: int = 0, reader=None, data: bytes | None = None, alive=None):
        self.length = max(0, int(length or 0))
        self._reader = reader
        self._data = data
        self.alive = alive

    @classmethod
    def of(cls, value) -> "Body":
        if isinstance(value, Body):
            return value
        data = b"" if value is None else bytes(value)
        return cls(len(data), None, data)

    def read(self) -> bytes:
        if self._data is None:
            self._data = self._reader(self.length) if (self._reader and self.length) else b""
        return self._data


#: 导出用的临时目录前缀。服务每次启动会把**过时的**这类目录扫掉：上一次服务被强杀时
#: 留下的空壳会一直堆在临时目录里（本机实测撞到两个，让真浏览器验收里"临时目录没残留"
#: 那两条永远报红——一条永远红的断言等于没有断言）。
EXPORT_TMP_PREFIX = "i3pro-export-"


def sweep_temp_exports(
    max_age_s: float = 3600.0, root: str | Path | None = None
) -> list[str]:
    """删掉超过 ``max_age_s`` 没动过的导出临时目录，返回删掉的那些路径。

    ``root`` 只是为了测试能指到自己的临时目录（默认是系统的临时目录）。
    """
    import time

    removed: list[str] = []
    now = time.time()
    where = Path(root) if root is not None else Path(tempfile.gettempdir())
    for path in where.glob(EXPORT_TMP_PREFIX + "*"):
        try:
            if now - path.stat().st_mtime < max_age_s:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(str(path))
    return removed


class Api:
    """一个服务进程里只有一个；每个请求造一个 ``_Call``。"""

    def __init__(self, library, buckets: int = render.DEFAULT_BUCKETS):
        self.library = library
        self.buckets = buckets
        # 这个模块拥有导出临时目录，也由它负责收尾：上一次服务被强杀会留下空壳，
        # 攒在临时目录里会让"临时文件不残留"那条验收永远报红。只清一小时没动过的。
        sweep_temp_exports()

    def handle(self, parts, query, method: str = "GET", body=None) -> Response:
        """把一个请求变成 ``Response``；**不起 socket 也能调**（单测就这么用）。"""
        call = _Call(self, Body.of(body))
        try:
            return call.api(list(parts), dict(query), method)
        except ClientGone:
            # 客户端已经走了（导出中途取消）：没人收这份数据，安静收场。
            return Response(status=499, body=b"", close=True)
        except KeyError as exc:
            return call._error(404, f"未知场次: {exc.args[0]}")
        except FileNotFoundError as exc:
            return call._error(404, str(exc))
        except ValueError as exc:
            return call._error(400, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            return call._error(500, f"{type(exc).__name__}: {exc}")

    def can_undo(self, log, current=None) -> bool:
        """"撤销"按钮该不该亮。页面（server.session_page）与 ``/laps`` 共用一个答案。"""
        if current is None:
            current = beaconsmod.load_config(log.path)
        slot = self.library.laps_undo_slot(log.path)
        return beaconsmod.undo_config(current, slot) is not None


class _Call:
    """一次请求：路由 + 各动作。请求体挂在它身上，不与其他线程共享。"""

    #: ``/api/session/<场次>/<动作>`` 的最后一段 -> 这一层的方法名。
    #: **加一个动作 = 在这张表里加一行**，不用在 if/elif 长链里翻。
    ACTIONS = {
        "info": "act_info",
        "trace": "act_trace",
        "points": "act_points",
        "histogram": "act_histogram",
        "spectrum": "act_spectrum",
        "overview": "act_overview",
        "overlay": "act_overlay",
        "track": "act_track",
        "laps": "act_laps",
        "sections": "act_sections",
        "notes": "act_notes",
        "gps": "act_gps",
        "report": "act_report",
        "maths": "act_maths",
        "at": "act_at",
        "export": "act_export"
    }

    def __init__(self, api: Api, body: Body):
        self.library = api.library
        self.buckets = api.buckets
        self.body_source = body

    # --------------------------------------------------------------- 请求体
    @property
    def length(self) -> int:
        return self.body_source.length

    @property
    def body(self) -> bytes:
        return self.body_source.read()

    # ----------------------------------------------------------------- 回复
    def _send(
        self,
        body: bytes,
        status: int = 200,
        ctype: str = "application/json; charset=utf-8",
        headers=(),
        close: bool = False,
    ) -> Response:
        return Response(status, body, ctype, tuple(headers), close)

    def _json(self, value, status: int = 200) -> Response:
        return self._send(dumps(value).encode("utf-8"), status)

    def _html(self, text: str, status: int = 200) -> Response:
        return self._send(text.encode("utf-8"), status, "text/html; charset=utf-8")

    def _error(self, status: int, message: str, close: bool = False) -> Response:
        response = self._send(dumps({"error": message}).encode("utf-8"), status)
        return replace(response, close=True) if close else response

    # ----------------------------------------------------------------- 路由
    def api(self, parts: list[str], query: dict, method: str = "GET") -> Response:
        if not parts:
            return self._error(404, "no such api path")
        if parts[0] == "sessions":
            return self._json(self.library.listing())
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
        log = self.library.get(name)
        handler = self.ACTIONS.get(action)
        if handler is None:
            return self._error(
                404,
                f"没有 {action!r} 这个动作。这一版认得的动作是："
                f"{'、'.join(sorted(self.ACTIONS))}。",
            )
        return getattr(self, handler)(log, name, query, method)

    # ----------------------------------------------------------------- 动作
    def act_info(self, log, name: str, query: dict, method: str) -> Response:
        payload = render.build_payload(
            log,
            channels=csv_arg(query, "channels") or None,
            ref=(query.get("ref") or [None])[0],
            cmp=(query.get("cmp") or [None])[0],
            buckets=int_arg(query, "buckets", self.buckets),
            api_base="/api",
            step=float_arg(query, "step", 1.0),
            with_track=False,
            worksheets_dir=self.library.worksheets_root,
        )
        payload["session"] = name
        return self._json(payload)

    def act_trace(self, log, name: str, query: dict, method: str) -> Response:
        names = csv_arg(query, "channels") or render.pick_channels(log)
        time = timebase.axis(log)
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
                int_arg(query, "buckets", self.buckets),
                start=float_arg(query, "from"),
                end=float_arg(query, "to"),
            )
        return self._json(out)

    def act_points(self, log, name: str, query: dict, method: str) -> Response:
        # raw samples for the scatter component (never min/max decimated)
        names = csv_arg(query, "channels")
        if not names:
            return self._error(400, "points requires ?channels=")
        time = timebase.axis(log)
        return self._json(
            render.points(
                log,
                names,
                time,
                start=float_arg(query, "from"),
                end=float_arg(query, "to"),
                max_points=int_arg(query, "max", 30000),
            )
        )

    def act_histogram(self, log, name: str, query: dict, method: str) -> Response:
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
        time = timebase.axis(log)
        gate = (query.get("gate") or [None])[0] or None
        colour = (query.get("colour") or [None])[0] or None
        try:
            return self._json(
                render.histogram(
                    log,
                    channel,
                    time,
                    bins=int_arg(query, "bins", histogrammod.DEFAULT_BINS),
                    start=float_arg(query, "from"),
                    end=float_arg(query, "to"),
                    gate=gate,
                    gate_mode=(query.get("gate_mode") or ["nonzero"])[0],
                    gate_lo=float_arg(query, "gate_min"),
                    gate_hi=float_arg(query, "gate_max"),
                    colour=colour,
                )
            )
        except ValueError as exc:
            return self._error(400, str(exc))

    def act_spectrum(self, log, name: str, query: dict, method: str) -> Response:
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
                    start=float_arg(query, "from"),
                    end=float_arg(query, "to"),
                    points=int_arg(query, "points", spectrummod.DEFAULT_POINTS),
                    window=(query.get("window") or [spectrummod.DEFAULT_WINDOW])[0],
                    overlap=float_arg(query, "overlap") if query.get("overlap")
                    else spectrummod.DEFAULT_OVERLAP,
                    smooth=int_arg(query, "smooth", 1),
                    scale=(query.get("scale") or [spectrummod.DEFAULT_SCALE])[0],
                )
            )
        except ValueError as exc:
            return self._error(400, str(exc))

    def act_overview(self, log, name: str, query: dict, method: str) -> Response:
        name = (query.get("channel") or [None])[0] or (
            next((n for n in render.SPEED_FOR_COLORING if log.has(n)), None)
        )
        if name is None or not log.has(name):
            return self._json(None)
        time = timebase.axis(log)
        payload = render.trace(
            log, name, time, None, int_arg(query, "buckets", 900)
        )
        payload["name"] = name
        return self._json(payload)

    def act_overlay(self, log, name: str, query: dict, method: str) -> Response:
        laps = render.detect(log)
        by_label = {l.label: l for l in laps}
        ref = by_label.get((query.get("ref") or [""])[0])
        cmp = by_label.get((query.get("cmp") or [""])[0])
        channels = csv_arg(query, "channels") or render.overlay_channels(log)
        overlay = render.build_overlay(log, [ref, cmp], channels, float_arg(query, "step", 1.0))
        return self._json(overlay)

    def act_track(self, log, name: str, query: dict, method: str) -> Response:
        return self._json(
            render.track_payload(
                log,
                points=int_arg(query, "points", 1500),
                start=float_arg(query, "from"),
                end=float_arg(query, "to"),
            )
        )

    def act_laps(self, log, name: str, query: dict, method: str) -> Response:
        # laps after the saved beacons/mode, plus the config itself
        if method == "PUT":
            return self.save_laps(log)
        config = beaconsmod.load_config(log.path)
        laps = render.detect(log)
        return self._json(
            {
                "config": config.as_dict(),
                "laps": lapsmod.lap_table(log, laps),
                "can_undo": self._can_undo(log, config),
            }
        )

    def act_sections(self, log, name: str, query: dict, method: str) -> Response:
        # 赛道区段（i2 Pro 的 Track Sections）：GET 看当前生效的，
        # PUT 重切（auto）或手工改边界/名字。
        if method == "PUT":
            return self.save_sections(log)
        return self._json(render.sections_payload(log, render.detect(log)))

    def act_notes(self, log, name: str, query: dict, method: str) -> Response:
        # 注释（ticket #15）：GET 拿这一场的注释与落点，PUT 存整张表。
        # 它有自己的侧车 `<场次>.notes.json`，和信标 / 区段互不影响。
        if method == "PUT":
            return self.save_notes(log)
        return self._json({"notes": render.notes_payload(log, render.track_payload(log))})

    def act_gps(self, log, name: str, query: dict, method: str) -> Response:
        # GPS 校正（ticket #14）：GET 看当前配置 + "这段数据坏在哪"的计数，
        # PUT 存整份配置。侧车 `<场次>.gps.json`，`.ld` 永远只读。
        if method == "PUT":
            return self.save_gps(log)
        return self._json(render.gps_payload(log))

    def act_report(self, log, name: str, query: dict, method: str) -> Response:
        # 时间报告 / 通道报告（ticket #11）。GET 一张或两张表；
        # 带 csv=time|channels 时直接吐 CSV，方便命令行与队友核对。
        return self.session_report(log, query)

    def act_maths(self, log, name: str, query: dict, method: str) -> Response:
        # 数学通道：GET 看当前生效的定义，PUT 存，POST 试算一条式子
        if method == "PUT":
            return self.save_maths(name, log, query)
        if method == "POST":
            return self.preview_maths(log)
        return self._json(self.library.maths_state(name))

    def act_at(self, log, name: str, query: dict, method: str) -> Response:
        # Distance axis -> time: a crossing is a moment, but on the
        # distance axis the cursor is metres. Answered from the distance
        # series itself, not from the downsampled plot.
        distance = float_arg(query, "distance")
        if distance is None:
            return self._error(400, "需要 distance= 参数（米）")
        when = axesmod.time_at_distance(log, distance)
        if when is None:
            return self._error(
                400, "这个距离不在本场已行驶的范围内（或本场没有距离轴）"
            )
        return self._json({"distance": distance, "time": when})

    def act_export(self, log, name: str, query: dict, method: str) -> Response:
        """数据导出（ticket #23）。

        ``estimate=1`` 只回预计行数 / 列数 / 体积，**不产生文件**；否则写一个临时
        文件再流式回传（``Content-Length`` 就是文件长度，界面据此画真实进度）。

        参数与语义见 ``i3pro/export.py`` 与 ``out/_pending/export-contract.md``，
        命令行 ``i3pro export`` 用的是同一套。
        """
        params = {
            key: (value[0] if isinstance(value, list) and value else value)
            for key, value in query.items()
        }
        try:
            request = exportmod.parse_request(log, params)
            info = exportmod.plan(log, request)
        except exportmod.ExportError as exc:
            return self._error(400, str(exc))
        raw_estimate = str(params.get("estimate") or "0").lower()
        if raw_estimate not in ("0", "false", "no", "off", ""):
            return self._json(info)
        filename = exportmod.filename(log, request)
        ctype = (
            "text/csv; charset=utf-8"
            if request.fmt == "csv"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        directory = Path(tempfile.mkdtemp(prefix=EXPORT_TMP_PREFIX))
        target = directory / filename
        alive = getattr(self.body_source, "alive", None)

        def progress(done, total):
            """写一块就问一次"人还在吗"——点了取消就别把剩下几百 MB 写完。"""
            if alive is not None and not alive():
                raise ClientGone()

        try:
            exportmod.write(log, request, target, progress=progress)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return self._download(target, filename, ctype)

    def _download(self, path: Path, filename: str, ctype: str) -> Response:
        """一个"边读边发"的回复；临时文件的清理交给 ``server.send``（发完就删）。"""
        ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "export"
        disposition = (
            f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'
        )
        return Response(
            status=200,
            ctype=ctype,
            headers=(("Content-Disposition", disposition),),
            path=path,
        )

    # ------------------------------------------------------------- 其余方法
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
            channels=csv_arg(query, "channels") or None,
            kind=filter_arg(query, "filter"),
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
        body = self.body
        if not body:
            return self._error(400, "空请求体")
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return self._error(400, f"JSON 解析失败: {exc}")
        if not isinstance(data, dict):
            return self._error(400, "需要一个 JSON 对象")
        previous = beaconsmod.load_config(log.path)
        if data.get("undo"):
            return self.undo_laps(log, previous)
        # The client sends the whole config, so the name rules (trim, empty
        # falls back, duplicate suffix, truncation) and the trusted-mark
        # migration are applied here - one implementation, every caller.
        config = beaconsmod.reconcile_edits(previous, beaconsmod.LapConfig.from_dict(data))
        problem = beaconsmod.check_new_crossings(previous, config, log.duration)
        if problem:
            return self._error(400, problem)
        return self._commit_laps(log, config, previous)

    def undo_laps(self, log, current) -> None:
        """撤销上一步信标编辑：把内存里那一版按同一条保存路径再提交一次。

        不再跑 ``check_new_crossings``——要交回去的那一版本来就存在过、也被接受过。
        重跑一次反而有害：那条规则会放过边车里**已经存在**的越界穿越，于是用户删掉
        一条旧侧车里的越界穿越之后，就再也撤不回来了。
        """
        slot = self.library.laps_undo_slot(log.path)
        if slot is None:
            return self._error(
                400,
                "没有可撤销的一步了：上一版配置只留在内存里，服务重启过、或还没在这个"
                "页面上改过信标都会是空的。先改一次信标（改名 / ＋ 穿越 / ✕），再来撤销。",
            )
        config = beaconsmod.undo_config(current, slot)
        if config is None:
            return self._error(
                400,
                "当前这一版已经和上一版一样了，没有可撤销的一步；改一次信标再来撤销。",
            )
        return self._commit_laps(
            log, config, current, notice="已撤销上一步信标编辑", forget_undo=True
        )

    def save_notes(self, log) -> None:
        """PUT /api/session/<name>/notes：整张注释表（客户端发全量）。

        规则只有一份实现（``notes.normalize``）：文字不能为空、时刻要落在这一场里、
        最多 500 条。这里不做别的判断——注释**不参与**切圈 / 比圈 / 报表，
        所以存完只回新表，不需要重算圈速（这正是它和信标最大的区别）。
        """
        body = self.body
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
        return self._json(
            {
                "saved": path.name,
                "notes": render.notes_payload(log, render.track_payload(log)),
            }
        )

    def _commit_laps(self, log, config, previous, notice=None, forget_undo=False) -> None:
        """落盘 → 重算圈速表 → 记下"上一步"，三条编辑路径共用这一段。"""
        before = render.detect(log)              # laps as they are right now
        path = beaconsmod.save_config(log.path, config)
        laps = render.detect(log)
        if notice is None:
            notice = beaconsmod.insertion_notice(previous, config, len(before), len(laps))
        if forget_undo:
            self.library.forget_laps_undo(log.path)   # 一级撤销，用掉就没有了
        elif not beaconsmod.same_config(config, previous):
            # 只有真的改出一版新的才更新槽：一次没改动的保存不该把上一步冲掉
            self.library.remember_laps(log.path, previous)
        return self._json(
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
        return beaconsmod.undo_config(current, self.library.laps_undo_slot(log.path)) is not None

    # ------------------------------------------------------------ upload
    def save_gps(self, log) -> None:
        """PUT /api/session/<name>/gps：整份 GPS 校正配置。

        只认整份（和区段、注释一个规矩）：增量合并会让"关掉一半"这种状态
        没法表达。参数校验全在 ``gpsfix.FixConfig.from_dict`` 一处，报错带
        下一步（哪个字段、什么范围）。
        """
        try:
            data = self._read_json()
        except (ValueError, UnicodeDecodeError) as exc:
            return self._error(400, str(exc))
        if not isinstance(data, dict):
            return self._error(400, "需要一个 JSON 对象")
        raw = data.get("config") if isinstance(data.get("config"), dict) else data
        try:
            config = gpsfix.FixConfig.from_dict(raw)
        except ValueError as exc:
            return self._error(400, str(exc))
        try:
            path = gpsfix.save_config(log.path, config)
        except OSError as exc:
            return self._error(
                500,
                f"GPS 侧车写不进去（{type(exc).__name__}: {exc}）。"
                f"检查 {gpsfix.config_path(log.path)} 所在目录能不能写。",
            )
        payload = render.gps_payload(log, config)
        payload["saved"] = path.name
        return self._json(payload)

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
        return self._json(payload)

    # ------------------------------------------------------ maths editing
    def _read_json(self):
        body = self.body
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
            log, [*incoming.definitions, *self.library.maths_names(log)]
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
            else maths.global_path(self.library.maths_root)
        )
        try:
            path = incoming.save(target)
        except OSError as exc:
            return self._error(500, f"写盘失败：{exc}")
        # 定义变了 -> apply_maths 会因为指纹变化重新算一遍
        self.library.apply_maths(log.path, log)
        state = self.library.maths_state(name)
        state["saved"] = path.name
        state["scope"] = scope
        return self._json(state)

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
        return self._json(
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
                return self._error(405, "上传请用 PUT")
        name = (query.get("name") or [""])[0]
        try:
            clean = importer.safe_name(name)
        except ValueError as exc:
                return self._error(400, str(exc))
        try:
            length = self.length
        except ValueError:
            length = 0
        if length <= 0:
                return self._error(400, "请求体为空")
        if length > MAX_UPLOAD_BYTES:
                return self._error(
                413, f"文件太大: {length / 1e6:.0f} MB > {MAX_UPLOAD_BYTES / 1e6:.0f} MB"
            )
        try:
            info = importer.store_stream(self.library.upload_dir(), clean, self.rfile, length)
        except ValueError as exc:
                return self._error(400, str(exc))
        except OSError as exc:
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
        return self._json(summary)

# --------------------------------------------------------------- 查询参数
def csv_arg(query: dict, key: str) -> list[str]:
    raw = (query.get(key) or [""])[0]
    return [c.strip() for c in raw.split(",") if c.strip()]


def float_arg(query: dict, key: str, default=None):
    raw = (query.get(key) or [None])[0]
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def int_arg(query: dict, key: str, default: int) -> int:
    value = float_arg(query, key, None)
    return default if value is None else int(value)


def filter_arg(query: dict, key: str) -> str | None:
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
