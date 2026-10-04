"""Unit + regression tests for i3pro.

The regression tests need the team's sample logs in ``i2pro_data/``; they are
skipped automatically when the data is not present.

They read a *copy* of the two golden sessions (``out/_test_data/``), never the
originals: sidecars belong to whoever drives the workbench, so a team mate
saving a beacon next to a golden log must not turn this suite red.

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import contextlib
import atexit
import csv
import dataclasses
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from i3pro import (  # noqa: E402
    canlog, channels, csvlog, dbc, derive, gpsfix, laps as lapsmod, ld, library as librarymod,
    maths as mathsmod, motec_csv,
    notes as notesmod, render, report as reportmod, sections as sectionsmod,
    server, sidecar, store, timebase,
    txtlog,
    worksheets as worksheetsmod,
    export as exportmod, xlsx as xlsxmod,
)

DATA = ROOT / "i2pro_data"


def _borrow_sidecar(session: Path) -> tuple[Path, bytes | None]:
    """Take a session's sidecar for one test, promising to give it back.

    These tests write a real ``<场次>.laps.json`` next to a real log. A user who
    has saved their own beacon edits there must get them back untouched - and an
    out-of-range beacon of theirs must not stop the test either - so the file is
    kept in memory and restored in ``finally``. Only a sidecar this test created
    is deleted.
    """
    sidecar = session.parent / f"{session.stem}.laps.json"
    kept = sidecar.read_bytes() if sidecar.exists() else None
    if kept is not None:
        sidecar.unlink()
    return sidecar, kept


def _return_sidecar(sidecar: Path, kept: bytes | None) -> None:
    """Undo whatever ``_borrow_sidecar`` did, restoring the user's own edits."""
    if kept is None:
        sidecar.unlink(missing_ok=True)
    else:
        sidecar.write_bytes(kept)


def _class_body(text: str, name: str) -> str:
    """抠出一个测试类的正文（到下一个顶层 ``class`` 为止）——给扫源码的守卫用。"""
    start = text.index(f"class {name}(")
    rest = text[start:]
    end = rest.find("\nclass ", 1)
    return rest[:end] if end > 0 else rest


#: 测试只碰这份副本，不碰 ``i2pro_data`` 里的金标准场次本身。
#:
def scratch(name: str) -> Path:
    """``out/`` 下的临时路径，名字带**进程号**。

    两个人同时跑这套测试（或者以后并行化）时，同名目录会被对方 ``rmtree`` 掉。
    实测过一次：两个进程同跑 CAN 那条用例，一边正在读 ``out/_can_library``，
    另一边刚把它删了重建，于是"第二次列表"少了一半场次（23 → 16）——
    同一份代码单独跑永远是对的，所以这条一开始被当成随机失败。

    进程号插在**扩展名之前**（``_can_plain_1234.csv``），不能接在末尾：后缀是
    分流依据（``csvlog.open_session`` 按 ``.csv`` 走 CSV 那条路），接在末尾会把它
    变成一个没有扩展名的文件——实测就是"一个 25 字节的表被当成 .ld 去解"。
    """
    path = Path(name)
    return ROOT / "out" / f"{path.stem}_{os.getpid()}{path.suffix}"


#: 侧车（信标 / 区段 / 注释 / GPS / 数学通道 / CSV 列映射）是**用户资产**，就躺在场次
#: 旁边。队员在浏览器里给金标准场次改一次信标，读原目录的用例就会红——实测过：
#: ``20260908-cjh 高避5圈.laps.json`` 里多一个 GPS 信标之后，下面两条当场变红
#: （``TestSections.test_the_golden_hill_lap_splits_into_corners_and_straights``、
#: ``TestServer.test_http_api_end_to_end``，后者是 ``/overlay`` 报 500）。所以每轮
#: 开始先清掉这份副本目录、再复制一次（实测两份共 148.2 MB、0.08 s），写侧车的用例
#: 也就写在副本里，车队数据一个字节都不动。目录名带进程号，两份测试可以同时跑；
#: 跑完就删（否则每跑一次留 148 MB 副本）。
STAGE = scratch("_test_data")


def _stage(name: str) -> Path:
    """把金标准场次复制进 ``out/_test_data``，返回副本路径。

    源文件不存在时原样返回（``@_needs`` 会据此跳过，不算通过）。
    """
    source = DATA / name
    target = STAGE / name
    if source.exists():
        shutil.copy2(source, target)
    return target


shutil.rmtree(STAGE, ignore_errors=True)
STAGE.mkdir(parents=True, exist_ok=True)
atexit.register(shutil.rmtree, STAGE, ignore_errors=True)
ENDURANCE = _stage("20260524-耐久正赛.ld")
HILL = _stage("20260908-cjh 高避5圈.ld")

#: 服务测试用的两条根目录。副本在前，所以金标准场次那个**普通名字**解析到没有侧车的
#: 那一份；``i2pro_data`` 里其它的场次（CSV 导出等）照旧可用。
LIBRARY_ROOTS = [STAGE, DATA]


def _needs(path: Path):
    return unittest.skipUnless(path.exists(), f"sample log not present: {path.name}")


class _Http:
    """一个正在跑的工作台服务，``http_session`` 交给用例的那个句柄。

    四种取数形状不是随手加的，它们对应仓库里真实存在的四种用法：

    * ``get_json`` / ``put_json`` / ``get_text``——**非 2xx 直接抛 HTTPError**。
      信标编辑那几组靠 ``assertRaises(urllib.error.HTTPError)`` 判 400。
    * ``json`` / ``raw``——**返回 ``(状态码, 内容)``**，成功与失败都要看内容
      （区段 / 注释 / GPS / 直方图 / 频谱 / 数学通道那几组逐条判错）。

    之前这九组用例各自抄一遍 ``ThreadingHTTPServer`` + ``urlopen`` + 这四个小
    函数，抄到后来同一个 400 在不同用例里被解码成了不同形状。加一个动作不该
    顺手把 HTTP 客户端也再写一遍（ticket #26）。
    """

    def __init__(self, base, quoted, session, copy, root, before, library, httpd):
        self.base = base
        self.quoted = quoted
        self.session = session
        self.copy = copy
        self.root = root
        #: 服务起来**之前** `.ld` 的字节。用例靠它判"跑一趟 HTTP 有没有动原文件"。
        self.before = before
        self.library = library
        self.httpd = httpd
        self._closed = False

    def close(self):
        """关服务与场次缓存。**幂等**：用例自己关过，夹具再兜一次底也不出错。

        用例显式写 ``finally: http.close()`` 是为了让"服务什么时候停"在测试里
        看得见；夹具的 ``finally`` 是给"还没进 try 就抛了"那种情况兜底的。
        """
        if self._closed:
            return
        self._closed = True
        self.httpd.shutdown()
        self.httpd.server_close()
        self.library.close()

    def url(self, path: str) -> str:
        return self.base + path

    def get_json(self, path: str, timeout: float = 30):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_text(self, path: str, timeout: float = 30) -> str:
        with urllib.request.urlopen(self.base + path, timeout=timeout) as response:
            return response.read().decode("utf-8")

    def get_bytes(self, path: str, timeout: float = 120):
        """``(状态码, 原始字节)``，**非 2xx 抛 HTTPError**（报表那条靠它判 400）。"""
        with urllib.request.urlopen(self.base + path, timeout=timeout) as response:
            return response.status, response.read()

    def put_json(self, path: str, payload, timeout: float = 30):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"), method="PUT",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def get(self, path: str, timeout: float = 120):
        """``(状态码, 解码后的 JSON)``；400 也照样解码返回，不抛。"""
        return self.json(path, timeout=timeout)

    def request(self, path: str, method: str = "GET", payload=None, timeout: float = 120):
        return self.json(path, method, payload, timeout)

    def raw(self, path: str, method: str = "GET", payload=None, timeout: float = 120):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def json(self, path: str, method: str = "GET", payload=None, timeout: float = 120):
        status, raw = self.raw(path, method, payload, timeout)
        return status, json.loads(raw.decode("utf-8"))

    def page_payload(self):
        """工作台页面里注入的那份 ``const DATA = {...}``。"""
        html = self.get_text(f"/session/{self.quoted}", timeout=60)
        start = html.index("const DATA = ") + len("const DATA = ")
        payload, _end = json.JSONDecoder().raw_decode(html[start:])
        return payload


@contextlib.contextmanager
def http_session(session: Path, *, buckets: int = 200, roots=None, root=None,
                 maths_root=None, worksheets_root=None, cache_size: int = 1):
    """起一个真服务（后台线程 + 随机端口），退出时关干净。

    ``session`` 是要用的场次（传 ``HILL`` / ``ENDURANCE`` 这样的 Path）。三种形态：

    * 什么都不给：把场次**复制**进一个新的临时目录，`roots` 只有它、
      `maths_root` 也是它。侧车落在临时目录里，`.ld` 一个字节不动——这是默认，
      也是大多数用例该用的那个。
    * ``roots=``：用现成的根目录（``LIBRARY_ROOTS``：副本在前、真数据在后）。
      ``root`` 缺省取 ``roots[0]``，所以 ``handle.copy`` 仍然指得到那份 `.ld`。
    * ``root=``：用你**自己准备好**的目录（里面已经有同名场次、甚至已经写好侧车）。
      这个目录不归夹具所有，退出时不删——`TestSidecar` 那条要靠它先把侧车摆好。

    每个 `with` 用完就关服务、关场次缓存；临时目录也一起删。
    """
    owned = None
    handle = None
    try:
        if root is None:
            if roots is not None:
                root = Path(roots[0])
            else:
                owned = tempfile.TemporaryDirectory(
                    prefix="i3pro-http-", ignore_cleanup_errors=True
                )
                root = Path(owned.name)
                (root / session.name).write_bytes(session.read_bytes())
                roots = [root]
                if maths_root is None:
                    maths_root = root
        root = Path(root)
        if roots is None:
            roots = [root]
        library = server.SessionLibrary(roots, cache_size=cache_size, maths_root=maths_root,
                                        worksheets_root=worksheets_root)
        httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), server.make_handler(library, buckets=buckets)
        )
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        copy = root / session.name
        handle = _Http(
            base=f"http://127.0.0.1:{httpd.server_address[1]}",
            quoted=urllib.parse.quote(session.stem),
            session=session.stem,
            copy=copy,
            root=root,
            before=copy.read_bytes() if copy.exists() else b"",
            library=library,
            httpd=httpd,
        )
        yield handle
    finally:
        if handle is not None:
            handle.close()
        if owned is not None:
            owned.cleanup()


class TestHeader(unittest.TestCase):
    @_needs(HILL)
    def test_metadata(self):
        with ld.LogFile.read(HILL) as log:
            meta = log.metadata()
            self.assertEqual(meta["device"], "C125")
            self.assertRegex(meta["log_date"], r"\d{2}/\d{2}/\d{4}")
            self.assertGreater(meta["channels"], 300)
            self.assertAlmostEqual(meta["sample_rate"], 100.0)
            self.assertGreater(meta["duration"], 400)
            self.assertTrue(meta["event"])

    @_needs(HILL)
    def test_channel_table_is_consistent(self):
        with ld.LogFile.read(HILL) as log:
            self.assertEqual(log.header["channel_table_end"] - log.header["first_channel_ptr"],
                             len(log.channels) * 124)
            first = log.channels[0]
            self.assertGreater(first.sample_count, 0)
            # data blocks are contiguous, in channel order
            for prev, nxt in zip(log.channels, log.channels[1:]):
                expected = prev.data_offset + prev.sample_count * prev.bytes_per_sample
                self.assertEqual(nxt.data_offset, expected)


class TestScaling(unittest.TestCase):
    @_needs(HILL)
    def test_values_are_scaled(self):
        with ld.LogFile.read(HILL) as log:
            ch = log.channel("G Force Lat")
            raw = log.raw(ch)
            values = log.values(ch)
            self.assertEqual(ch.decimals, 2)
            np.testing.assert_allclose(values, raw.astype(float) / 100.0)
            self.assertLess(np.abs(values).max(), 5.0)  # plausible lateral G

    @_needs(HILL)
    def test_negative_decimals_multiply(self):
        """decimals = -1 (0xffff) means x10, as used by Timestamp MTI."""
        with ld.LogFile.read(HILL) as log:
            if not log.has("Timestamp MTI"):
                self.skipTest("channel not logged")
            ch = log.channel("Timestamp MTI")
            self.assertEqual(ch.decimals, -1)
            self.assertAlmostEqual(ch.scale, 10.0)
            self.assertGreater(float(np.nanmax(log.values(ch))), 1000.0)

    @_needs(DATA / "20260522-yjw第二次直线3.72.ld")
    def test_matches_motec_csv_export(self):
        stem = "20260522-yjw第二次直线3.72"
        csv_path = DATA / f"{stem}.csv"
        if not csv_path.exists():
            self.skipTest("csv export missing")
        with ld.LogFile.read(DATA / f"{stem}.ld") as log:
            csv = motec_csv.load(csv_path, max_rows=5000)
            # the CSV export drops the Beacon channel and strips "(LoRes)" suffixes
            self.assertGreater(len(csv.channels) - 1, 300)
            self.assertLessEqual(len(csv.channels) - 1, len(log.channels))
            checked = 0
            for ch in log.channels:
                if ch.name not in csv.frame.columns:
                    continue
                if abs(ch.sample_rate - (csv.sample_rate or 100.0)) > 0.5:
                    continue
                reference = csv.values(ch.name)[:5000]
                values = log.values(ch)[: reference.size]
                tolerance = 0.5 * 10.0 ** (-ch.decimals) + 1e-9
                self.assertLessEqual(
                    float(np.max(np.abs(reference - values))), tolerance, ch.name
                )
                checked += 1
            self.assertGreater(checked, 100)


class TestDerived(unittest.TestCase):
    @_needs(HILL)
    def test_distance_is_monotonic(self):
        with ld.LogFile.read(HILL) as log:
            distance = derive.distance_series(log)
            self.assertTrue(np.all(np.diff(distance) >= -1e-9))
            # the car is stationary for the first two minutes
            self.assertLess(distance[int(60 * log.sample_rate)], 1.0)
            self.assertGreater(distance[-1], 1000.0)

    @_needs(HILL)
    def test_gps_track(self):
        with ld.LogFile.read(HILL) as log:
            track = derive.gps_track(log)
            span_x = track["x"].max() - track["x"].min()
            span_y = track["y"].max() - track["y"].min()
            self.assertGreater(span_x, 20)
            self.assertGreater(span_y, 20)
            self.assertLess(span_x, 2000)  # a test-track sized loop, not a road trip


class TestLaps(unittest.TestCase):
    @_needs(HILL)
    def test_gps_laps_hill_climb(self):
        with ld.LogFile.read(HILL) as log:
            found = lapsmod.detect_laps(log)
            complete = [l for l in found if l.complete]
            self.assertGreaterEqual(len(complete), 5)
            for lap in complete:
                self.assertGreater(lap.lap_time, 30)
                self.assertLess(lap.lap_time, 70)
                self.assertGreater(lap.distance, 600)
                self.assertLess(lap.distance, 1000)

    @_needs(ENDURANCE)
    def test_gps_laps_endurance(self):
        with ld.LogFile.read(ENDURANCE) as log:
            found = lapsmod.detect_laps(log)
            complete = [l for l in found if l.complete]
            self.assertGreaterEqual(len(complete), 20)
            best = min(l.lap_time for l in complete)
            self.assertGreater(best, 50)
            self.assertLess(best, 70)

    @_needs(HILL)
    def test_overlay_and_delta(self):
        with ld.LogFile.read(HILL) as log:
            found = [l for l in lapsmod.detect_laps(log) if l.complete]
            data = lapsmod.overlay(log, found[:2], ["Vx KF", "GPS Speed"])
            self.assertEqual(len(data["laps"]), 2)
            self.assertTrue(np.all(np.diff(data["distance"]) > 0))
            for row in data["laps"]:
                # laps shorter than the longest are NaN padded for the viewer
                diffs = np.diff(row["time"])
                self.assertTrue(np.all(diffs[~np.isnan(diffs)] >= -1e-9))
                self.assertIn("Vx KF", row)
            distance, delta = lapsmod.time_delta(data["laps"][0], data["laps"][1])
            self.assertEqual(distance.size, delta.size)
            finite = delta[~np.isnan(delta)]
            # the two laps are slightly different lengths (gate crossing vs.
            # nearest-approach sampling), so the delta at the common distance
            # must agree with the lap-time difference to within a few tenths
            self.assertLess(
                abs(
                    float(finite[-1])
                    - (data["laps"][1]["lap_time"] - data["laps"][0]["lap_time"])
                ),
                0.6,
            )


class TestStore(unittest.TestCase):
    @_needs(HILL)
    def test_parquet_roundtrip_and_sql(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with ld.LogFile.read(HILL) as log:
                parquet, meta_path = store.write_parquet(
                    log, tmp, channels=["Vx KF", "GPS Speed", "Distance"]
                )
            table = store.read_table(parquet)
            self.assertEqual(table.num_rows, 46400)
            self.assertEqual(
                sorted(table.column_names), ["Distance", "GPS Speed", "Vx KF", "time"]
            )
            meta = store.read_metadata(parquet)
            self.assertEqual(len(meta["channels"]), 3)
            frame = store.query(
                'SELECT max("Vx KF") AS vmax, min("Vx KF") AS vmin FROM log', [parquet]
            )
            self.assertEqual(len(frame), 1)
            self.assertGreater(float(frame["vmax"][0]), 50.0)
            self.assertLess(float(frame["vmin"][0]), 5.0)
            self.assertTrue(meta_path.exists())


class TestCsvReader(unittest.TestCase):
    @_needs(DATA / "20260524-耐久正赛.csv")
    def test_structure(self):
        meta, names, units, _ = motec_csv.read_structure(DATA / "20260524-耐久正赛.csv")
        self.assertEqual(names[0], "Time")
        self.assertEqual(len(names), len(units))
        self.assertEqual(meta.get("Device"), "C125")
        self.assertAlmostEqual(float(meta["Sample Rate"]), 100.0)


class TestRender(unittest.TestCase):
    @_needs(HILL)
    def test_static_payload_shape(self):
        with ld.LogFile.read(HILL) as log:
            n_channels = len(log.channels)
            payload = render.build_payload(
                log, channels=["Vx KF", "G Force Lat"], buckets=300
            )
        self.assertEqual(payload["meta"]["device"], "C125")
        self.assertEqual(len(payload["channels"]), n_channels)
        self.assertEqual(payload["selected"], ["Vx KF", "G Force Lat"])
        self.assertEqual(sorted(payload["traces"]), ["G Force Lat", "Vx KF"])
        self.assertIsNone(payload["api"])
        for spec in payload["traces"].values():
            self.assertEqual(len(spec["time"]), len(spec["value"]))
            self.assertEqual(len(spec["time"]), len(spec["distance"]))
            self.assertLessEqual(len(spec["time"]), 600)  # 2 points per bucket

    @_needs(HILL)
    def test_overlay_is_distance_aligned(self):
        with ld.LogFile.read(HILL) as log:
            payload = render.build_payload(log, channels=["Vx KF"], buckets=200)
        overlay = payload["overlay"]
        self.assertIsNotNone(overlay)
        self.assertEqual([l["lap"] for l in overlay["laps"]], [payload["ref"], payload["cmp"]])
        self.assertEqual(len(overlay["distance"]), len(overlay["delta"]))
        for row in overlay["laps"]:
            self.assertEqual(len(row["time"]), len(overlay["distance"]))
            self.assertEqual(len(row["Vx KF"]), len(overlay["distance"]))
        # laps differ, so the cumulative delta must not be flat zero
        self.assertGreater(float(np.nanmax(np.abs(overlay["delta"]))), 0.01)

    @_needs(HILL)
    def test_serve_payload_defers_traces(self):
        with ld.LogFile.read(HILL) as log:
            payload = render.build_payload(log, api_base="/api", channels=["Vx KF"])
        self.assertEqual(payload["traces"], {})
        self.assertEqual(payload["api"], "/api")
        self.assertTrue(payload["meta"]["has_distance"])
        page = render.render_page(payload)
        self.assertIn('"api": "/api"', page)
        self.assertNotIn("/*__I3PRO_DATA__*/null", page)

    @_needs(HILL)
    def test_snapshot_html_is_self_contained(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with ld.LogFile.read(HILL) as log:
                out = render.render_html(log, Path(tmp) / "view.html", buckets=200)
            text = out.read_text(encoding="utf-8")
            self.assertNotIn("/*__I3PRO_DATA__*/null", text)
            self.assertNotIn("http://", text.split("<script>")[0])  # no CDN in <head>
            self.assertIn('"laps"', text)


class TestServer(unittest.TestCase):
    @_needs(HILL)
    def test_http_api_end_to_end(self):
        with http_session(HILL, roots=LIBRARY_ROOTS, buckets=250) as http:
            base = http.base
            get_json, get_text = http.get_json, http.get_text

            sessions = get_json("/api/sessions")
            self.assertTrue(any(s["name"] == HILL.stem for s in sessions))
            quoted = urllib.parse.quote(HILL.stem)

            page = get_text(f"/session/{quoted}?channels=Vx%20KF")
            self.assertIn("i3pro", page)
            self.assertIn('"api": "/api"', page)

            traces = get_json(f"/api/session/{quoted}/trace?channels=Vx%20KF,G%20Force%20Lat&buckets=120")
            self.assertEqual(sorted(traces), ["G Force Lat", "Vx KF"])
            self.assertEqual(len(traces["Vx KF"]["time"]), len(traces["Vx KF"]["value"]))

            windowed = get_json(
                f"/api/session/{quoted}/trace?channels=Vx%20KF&from=200&to=260&buckets=120"
            )
            span = windowed["Vx KF"]["time"]
            self.assertTrue(span)
            self.assertGreaterEqual(min(span), 199.5)
            self.assertLessEqual(max(span), 260.5)
            self.assertLess(max(span) - min(span), max(traces["Vx KF"]["time"]))

            overlay = get_json(
                f"/api/session/{quoted}/overlay?ref=2&cmp=3&channels=Vx%20KF"
            )
            self.assertEqual(overlay["laps"][0]["lap"], "2")
            self.assertEqual(len(overlay["distance"]), len(overlay["delta"]))

            track = get_json(f"/api/session/{quoted}/track")
            self.assertGreater(len(track["x"]), 100)

            overview = get_json(f"/api/session/{quoted}/overview")
            self.assertEqual(overview["name"], "Vx KF")
            self.assertGreater(len(overview["time"]), 100)

            points = get_json(
                f"/api/session/{quoted}/points?channels=Vx%20KF,G%20Force%20Lat&from=200&to=210"
            )
            self.assertEqual(sorted(points["values"]), ["G Force Lat", "Vx KF"])
            self.assertEqual(len(points["time"]), len(points["values"]["Vx KF"]))
            self.assertGreaterEqual(min(points["time"]), 200.0)
            self.assertLessEqual(max(points["time"]), 210.0)

            with self.assertRaises(urllib.error.HTTPError) as missing_channel:
                urllib.request.urlopen(
                    base + f"/api/session/{quoted}/points", timeout=15
                )
            self.assertEqual(missing_channel.exception.code, 400)

            laps = get_json(f"/api/session/{quoted}/laps")
            self.assertIn("config", laps)
            self.assertGreaterEqual(len([l for l in laps["laps"] if l["complete"]]), 5)

            # a CSV session must be reachable through the same web path
            csv_stem = "20260522-yjw第二次直线3.72"
            if (DATA / f"{csv_stem}.csv").exists():
                listed = [s["name"] for s in get_json("/api/sessions")]
                self.assertIn(csv_stem, listed)          # the .ld keeps the plain name
                if (DATA / f"{csv_stem}.ld").exists():
                    # both sources exist -> the CSV must not be shadowed
                    self.assertIn(f"{csv_stem} (csv)", listed)
                    csv_stem = f"{csv_stem} (csv)"
                csv_quoted = urllib.parse.quote(csv_stem)
                csv_trace = get_json(
                    f"/api/session/{csv_quoted}/trace"
                    "?channels=GPS%20Speed&from=10&to=12&buckets=200"
                )
                self.assertTrue(csv_trace["GPS Speed"]["time"])
                csv_page = get_text(f"/session/{csv_quoted}")
                self.assertIn('"format": "csv"', csv_page)

            # windowed GPS track, for the "only the selected time range" view
            windowed = get_json(f"/api/session/{quoted}/track?from=200&to=230&points=4000")
            self.assertLessEqual(max(windowed["time"]), 230.5)
            self.assertGreaterEqual(min(windowed["time"]), 199.5)
            self.assertIn("origin", windowed)
            self.assertEqual(len(windowed["lat"]), len(windowed["x"]))

            # saving beacons writes the sidecar and re-cuts the laps
            import urllib.request as _u
            sidecar, kept_sidecar = _borrow_sidecar(HILL)
            payload = json.dumps({
                "mode": "auto",
                "beacons": [{"name": "测试信标", "lat": 22.0, "lon": 113.0}],
            }).encode("utf-8")
            try:
                request = _u.Request(
                    base + f"/api/session/{quoted}/laps", data=payload, method="PUT",
                    headers={"Content-Type": "application/json"},
                )
                with _u.urlopen(request, timeout=30) as response:
                    saved = json.loads(response.read().decode("utf-8"))
                self.assertEqual(saved["config"]["beacons"][0]["name"], "测试信标")
                self.assertNotIn("gates", saved["config"])   # the old shape is gone
                self.assertTrue(str(saved["saved"]).endswith(".laps.json"))
                self.assertTrue(sidecar.exists())
            finally:
                _return_sidecar(sidecar, kept_sidecar)

            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/api/session/nope/trace", timeout=15)
            self.assertEqual(caught.exception.code, 404)


class TestLapModes(unittest.TestCase):
    """Lap segmentation modes: auto, run, per-beacon, and the sidecar config."""

    def test_turn_direction_reads_a_loop(self):
        """A closed loop winds once; a there-and-back path does not."""
        angles = np.linspace(0.0, 2 * np.pi, 200)
        ccw_x, ccw_y = np.cos(angles), np.sin(angles)
        cw_x, cw_y = np.cos(-angles), np.sin(-angles)
        self.assertEqual(lapsmod.turn_direction(ccw_x, ccw_y), 1)
        self.assertEqual(lapsmod.turn_direction(cw_x, cw_y), -1)
        out_and_back = np.concatenate([angles, angles[::-1]])
        self.assertEqual(
            lapsmod.turn_direction(np.cos(out_and_back), np.sin(out_and_back)), 0
        )

    @_needs(HILL)
    def test_run_mode_splits_on_standstill(self):
        """`run` must return whole attempts, not laps - it is never shorter."""
        with ld.LogFile.read(HILL) as log:
            runs = lapsmod.detect_laps(log, method="run")
            laps = lapsmod.detect_laps(log, method="auto")
        self.assertTrue(runs, "run mode found nothing in a session that drove")
        self.assertLessEqual(len(runs), len(laps))
        for run in runs:
            self.assertGreater(run.lap_time, 20.0)
            self.assertGreater(run.start_distance, -1.0)
        # runs are contiguous in time and sorted
        for a, b in zip(runs, runs[1:]):
            self.assertLessEqual(a.start_time, b.start_time)

    @_needs(HILL)
    def test_figure8_mode_labels_turns(self):
        with ld.LogFile.read(HILL) as log:
            laps = lapsmod.detect_laps(log, method="figure8")
        self.assertTrue(laps)
        self.assertTrue(any(lap.turn in ("left", "right") for lap in laps),
                        "figure8 mode did not label any turn direction")

    @_needs(ENDURANCE)
    def test_two_beacons_give_two_independent_series(self):
        """One beacon per loop is how a figure-of-eight is split per loop."""
        with ld.LogFile.read(ENDURANCE) as log:
            track = derive.gps_track(log)
            i_left = int(np.argmin(track["x"]))
            i_right = int(np.argmax(track["x"]))
            config = lapsmod.LapConfig(
                beacons=[
                    lapsmod.Beacon("左环", lat=float(track["lat"][i_left]),
                                   lon=float(track["lon"][i_left])),
                    lapsmod.Beacon("右环", lat=float(track["lat"][i_right]),
                                   lon=float(track["lon"][i_right])),
                ],
            )
            laps = lapsmod.detect_from_config(log, config)
        names = {lap.label.split()[0] for lap in laps}
        self.assertEqual(names, {"左环", "右环"})
        for name in names:
            series = [l for l in laps if l.label.startswith(name)]
            self.assertTrue(series, f"{name} produced no laps")
            for lap in series:
                self.assertGreater(lap.end_time, lap.start_time)

    @_needs(ENDURANCE)
    def test_a_beacon_with_only_a_time_merges_into_the_nearest_series(self):
        """i2 Pro's "Missed Beacons": enter the time, it joins that series."""
        with ld.LogFile.read(ENDURANCE) as log:
            track = derive.gps_track(log)
            index = int(np.argmin(track["x"]))
            placed = lapsmod.Beacon("左环", lat=float(track["lat"][index]),
                                    lon=float(track["lon"][index]))
            base = lapsmod.detect_from_config(log, lapsmod.LapConfig(beacons=[placed]))
            self.assertTrue(base)
            gap = base[1].start_time if len(base) > 1 else base[0].end_time
            missed = lapsmod.Beacon("补", time=gap + 0.25)
            merged = lapsmod.detect_from_config(
                log, lapsmod.LapConfig(beacons=[placed, missed])
            )
        self.assertGreater(len(merged), len(base),
                           "a hand-inserted crossing did not add a boundary")

    def test_times_alone_cut_the_laps(self):
        """Two hand-entered crossings and nothing else still cut one lap."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.ld"
            config = lapsmod.LapConfig(
                beacons=[lapsmod.Beacon("手工1", time=10.0),
                         lapsmod.Beacon("手工2", time=40.0)]
            )
            back = lapsmod.LapConfig.from_dict(config.as_dict())
        self.assertEqual([b.time for b in back.beacons], [10.0, 40.0])
        self.assertFalse(back.beacons[0].has_position)

    def test_lap_config_round_trips_through_the_sidecar(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "20260101-test.ld"
            config = lapsmod.LapConfig(
                mode="figure8",
                beacons=[
                    lapsmod.Beacon("左环", lat=34.1, lon=113.6),
                    lapsmod.Beacon("右环", lat=34.2, lon=113.7),
                    lapsmod.Beacon("补", time=33.25),
                ],
                trusted={"左环 1": False},
            )
            path = lapsmod.save_config(log_path, config)
            self.assertEqual(path.name, "20260101-test.laps.json")
            back = lapsmod.load_config(log_path)
        self.assertEqual(back.mode, "figure8")
        self.assertEqual(back.beacons[0].name, "左环")
        self.assertAlmostEqual(back.beacons[0].lat, 34.1)
        self.assertTrue(back.beacons[0].has_position)
        self.assertIsNone(back.beacons[2].lat)
        self.assertAlmostEqual(back.beacons[2].time, 33.25)
        self.assertFalse(back.trusted["左环 1"])
        # a missing sidecar means "no manual edits", never an exception
        self.assertEqual(lapsmod.load_config(Path(tmp) / "nope.ld").mode, "auto")

    def test_every_older_sidecar_shape_still_loads(self):
        """Beacons placed before the gate/time merge must survive the upgrade."""
        legacy_shapes = [
            {"mode": "beacons", "gate": [34.5, 113.5]},
            {"mode": "beacons", "gates": [{"name": "左环", "lat": 34.1, "lon": 113.6}]},
            {"mode": "beacons", "gates": [["左环", 34.1, 113.6]]},
            {"mode": "beacons", "beacons": [12.5, 33.25]},
        ]
        for shape in legacy_shapes:
            loaded = lapsmod.LapConfig.from_dict(shape)
            self.assertEqual(len(loaded.beacons), 1 if "gate" in shape or "gates" in shape else 2,
                             f"failed to load {shape}")
            self.assertTrue(loaded.beacons[0].has_position or loaded.beacons[0].time is not None)


class TestBeaconEditing(unittest.TestCase):
    """Tickets #4 and #5: renaming a beacon, and inserting a missed crossing."""

    @staticmethod
    def _edit(old: lapsmod.LapConfig, *names: str) -> lapsmod.LapConfig:
        """Return `old` with every beacon renamed to the given name."""
        beacons = [
            lapsmod.Beacon(name, b.lat, b.lon, b.time) for name, b in zip(names, old.beacons)
        ]
        return lapsmod.reconcile_edits(old, lapsmod.LapConfig(mode=old.mode, beacons=beacons,
                                                             trusted=old.trusted))

    def test_the_four_name_rules(self):
        """Trim, empty falls back, duplicates get a suffix, over-long truncates."""
        old = lapsmod.LapConfig(beacons=[lapsmod.Beacon("左环", 34.1, 113.6),
                                        lapsmod.Beacon("右环", 34.2, 113.7)])

        self.assertEqual(self._edit(old, "  左环A  ", "右环").beacons[0].name, "左环A")
        # an empty name means "keep what it was called", not "call it nothing"
        self.assertEqual(self._edit(old, "   ", "右环").beacons[0].name, "左环")
        # two beacons may not share a name: labels are "<name> <n>"
        deduped = self._edit(old, "右环", "右环")
        self.assertEqual([b.name for b in deduped.beacons], ["右环 2", "右环"])
        long_name = "环" * 40
        self.assertEqual(len(self._edit(old, long_name, "右环").beacons[0].name),
                         lapsmod.MAX_BEACON_NAME)

    def test_a_new_beacon_is_named_uniquely_too(self):
        """Two crossings inserted in a row must not end up with the same name."""
        empty = lapsmod.LapConfig()
        first = lapsmod.reconcile_edits(
            empty, lapsmod.LapConfig(beacons=[lapsmod.Beacon("手工穿越", time=10.0)])
        )
        second = lapsmod.reconcile_edits(
            first,
            lapsmod.LapConfig(beacons=[*first.beacons, lapsmod.Beacon("手工穿越", time=40.0)]),
        )
        self.assertEqual([b.name for b in second.beacons], ["手工穿越", "手工穿越 2"])

    def test_renaming_carries_the_trusted_marks_over(self):
        """Labels are "<name> <n>", so a rename would otherwise drop every mark."""
        old = lapsmod.LapConfig(
            beacons=[lapsmod.Beacon("左环", 34.1, 113.6)],
            trusted={"左环 1": False, "左环 2": True, "别的圈 1": False},
        )
        renamed = lapsmod.reconcile_edits(
            old, lapsmod.LapConfig(beacons=[lapsmod.Beacon("左环A", 34.1, 113.6)],
                                   trusted=dict(old.trusted))
        )
        self.assertEqual(renamed.trusted,
                         {"左环A 1": False, "左环A 2": True, "别的圈 1": False})

    def test_a_name_ending_in_a_digit_is_not_mistaken_for_a_lap_number(self):
        """`左环 2` yields labels like `左环 2 1`; renaming `左环` must not take them."""
        old = lapsmod.LapConfig(
            beacons=[lapsmod.Beacon("左环", 34.1, 113.6)],
            trusted={"左环 1": False, "左环 2 1": True},
        )
        renamed = lapsmod.reconcile_edits(
            old, lapsmod.LapConfig(beacons=[lapsmod.Beacon("L", 34.1, 113.6)],
                                   trusted=dict(old.trusted))
        )
        self.assertEqual(renamed.trusted, {"L 1": False, "左环 2 1": True})

    def test_only_new_crossings_are_range_checked(self):
        """A stale out-of-range time in a sidecar must not lock the session."""
        old = lapsmod.LapConfig(beacons=[lapsmod.Beacon("手工穿越", time=9999.0)])
        self.assertIsNone(lapsmod.check_new_crossings(old, old, 100.0))
        added = lapsmod.LapConfig(beacons=[*old.beacons,
                                          lapsmod.Beacon("手工穿越 2", time=1e6)])
        message = lapsmod.check_new_crossings(old, added, 100.0)
        self.assertIsNotNone(message)
        self.assertIn("超出本场时长", message)
        # a placed beacon carries no time, so it is never range-checked
        placed = lapsmod.LapConfig(beacons=[lapsmod.Beacon("左环", 34.1, 113.6)])
        self.assertIsNone(lapsmod.check_new_crossings(old, placed, 100.0))

    @_needs(HILL)
    def test_an_inserted_crossing_splits_the_automatic_laps(self):
        """A time-only beacon adds one boundary - it never replaces the lap set."""
        with ld.LogFile.read(HILL) as log:
            auto = lapsmod.detect_laps(log, method="auto")
            self.assertGreaterEqual(len(auto), 3)
            middle = auto[len(auto) // 2]
            when = (middle.start_time + middle.end_time) / 2.0
            config = lapsmod.LapConfig(beacons=[lapsmod.Beacon("手工穿越", time=when)])
            after = lapsmod.detect_from_config(log, config)
        self.assertEqual(len(after), len(auto) + 1,
                         "the inserted crossing did not add exactly one boundary")
        self.assertTrue(any(abs(lap.start_time - when) < 0.05 for lap in after),
                        "the inserted time is not one of the lap boundaries")
        self.assertFalse([lap for lap in after if lap.label.startswith("手工穿越")],
                         "a hand-entered crossing must merge into the auto series, "
                         "not start a series of its own")

    def test_deleting_a_beacon_does_not_move_its_marks(self):
        """The whole list is sent: pairing by position reads a delete as a rename.

        Regression guard - pairing by index handed the deleted beacon's trusted
        marks to whichever beacon slid into its slot, and saved that. A beacon
        that is simply gone must carry its marks nowhere.
        """
        old = lapsmod.LapConfig(
            beacons=[lapsmod.Beacon("左环", 34.1, 113.6),
                     lapsmod.Beacon("右环", 34.2, 113.7),
                     lapsmod.Beacon("手工穿越", time=12.5)],
            trusted={"左环 1": False, "右环 1": True},
        )
        kept = lapsmod.LapConfig(beacons=[old.beacons[0], old.beacons[2]],
                                 trusted=dict(old.trusted))
        after = lapsmod.reconcile_edits(old, kept)
        self.assertEqual([b.name for b in after.beacons], ["左环", "手工穿越"])
        self.assertEqual(after.trusted, {"左环 1": False, "右环 1": True},
                         "deleting 右环 moved its trusted mark onto another series")

    def test_a_rename_in_place_still_carries_the_marks(self):
        """The same edit without the delete: the marks must follow the beacon."""
        old = lapsmod.LapConfig(
            beacons=[lapsmod.Beacon("左环", 34.1, 113.6),
                     lapsmod.Beacon("右环", 34.2, 113.7)],
            trusted={"右环 1": True},
        )
        renamed = lapsmod.LapConfig(
            beacons=[old.beacons[0], lapsmod.Beacon("右环B", 34.2, 113.7)],
            trusted=dict(old.trusted),
        )
        after = lapsmod.reconcile_edits(old, renamed)
        self.assertEqual([b.name for b in after.beacons], ["左环", "右环B"])
        self.assertEqual(after.trusted, {"右环B 1": True})

    def test_renaming_a_stale_crossing_is_not_treated_as_a_new_one(self):
        """Otherwise a stale time in a sidecar makes its entry un-renamable."""
        old = lapsmod.LapConfig(beacons=[lapsmod.Beacon("手工穿越", time=9999.0)])
        renamed = lapsmod.LapConfig(beacons=[lapsmod.Beacon("补一圈", time=9999.0)])
        self.assertIsNone(lapsmod.check_new_crossings(old, renamed, 100.0))

    @_needs(HILL)
    def test_a_crossing_on_a_boundary_that_is_already_there_changes_nothing(self):
        """Clicking i2 Pro's own boundary must not leave a hair-thin phantom lap."""
        with ld.LogFile.read(HILL) as log:
            auto = lapsmod.detect_laps(log, method="auto")
            # a sidecar carries milliseconds, so the two times never match exactly
            on_the_edge = round(auto[1].start_time, 3)
            config = lapsmod.LapConfig(beacons=[lapsmod.Beacon("手工穿越", time=on_the_edge)])
            after = lapsmod.detect_from_config(log, config)
        self.assertEqual(len(after), len(auto))
        self.assertGreater(min(lap.lap_time for lap in after), 1.0,
                           "a phantom lap was created")

    def test_a_crossing_that_split_nothing_says_so(self):
        """A button that quietly changes nothing reads as broken."""
        empty = lapsmod.LapConfig()
        crossing = lapsmod.LapConfig(beacons=[lapsmod.Beacon("手工穿越", time=10.0)])
        message = lapsmod.insertion_notice(empty, crossing, laps_before=7, laps_after=7)
        self.assertIsNotNone(message)
        self.assertIn("没有切出新圈", message)
        # a crossing that did split a lap needs no notice...
        self.assertIsNone(lapsmod.insertion_notice(empty, crossing, 7, 8))
        # ...and neither does an edit that added no crossing at all
        self.assertIsNone(lapsmod.insertion_notice(empty, lapsmod.LapConfig(), 7, 7))

    @_needs(HILL)
    def test_an_exported_snapshot_carries_the_new_name(self):
        """Criterion 5 of #4: the share link and the snapshot carry the new name.

        The *payload* is what has to carry it. The rendered file also contains
        the template's own labels and default-name code (``信标`` twelve times),
        so "the name appears in the HTML" proves nothing - that is why this looks
        inside the injected ``const DATA = ...`` literal specifically.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / HILL.name              # never beside the real data
            copy.write_bytes(HILL.read_bytes())
            out = Path(tmp) / "snap.html"
            with ld.LogFile.read(copy) as log:
                track = derive.gps_track(log)
                index = int(np.searchsorted(track["time"], 120.0))
                lapsmod.save_config(copy, lapsmod.LapConfig(
                    mode="auto",
                    beacons=[lapsmod.Beacon("弯心改名后", lat=float(track["lat"][index]),
                                            lon=float(track["lon"][index])),
                             lapsmod.Beacon("手工穿越", time=165.64)],
                    trusted={"弯心改名后 1": False},
                ))
                render.render_html(log, out)
            html = out.read_text(encoding="utf-8")
            start = html.index("const DATA = ") + len("const DATA = ")
            payload, end = json.JSONDecoder().raw_decode(html[start:])
            template = html[:start] + html[start + end:]

        self.assertEqual([b["name"] for b in payload["laps_config"]["beacons"]],
                         ["弯心改名后", "手工穿越"])
        self.assertEqual(payload["laps_config"]["trusted"], {"弯心改名后 1": False})
        self.assertTrue(payload["laps"], "the beacon produced no laps at all")
        self.assertTrue(all(row["lap"].startswith("弯心改名后 ") for row in payload["laps"]))
        self.assertNotIn("信标", json.dumps(payload, ensure_ascii=False),
                         "the payload still carries the default name")
        self.assertIn("信标", template,
                      "the template's own text moved into the payload check")


class TestBeaconEditingOverHttp(unittest.TestCase):
    """#4 / #5 as the UI reaches them: one PUT carrying the whole config."""

    @_needs(HILL)
    def test_insert_rename_and_the_range_guard(self):
        sidecar, kept_sidecar = _borrow_sidecar(HILL)
        self.addCleanup(_return_sidecar, sidecar, kept_sidecar)
        with http_session(HILL, roots=LIBRARY_ROOTS, buckets=200) as http:
            base, quoted = http.base, http.quoted
            get_json, put_json = http.get_json, http.put_json

            # ---- #5: an inserted crossing becomes a boundary of the automatic series
            before = get_json(f"/api/session/{quoted}/laps")
            auto = [row for row in before["laps"] if row["complete"]]
            self.assertGreaterEqual(len(auto), 3)
            target = auto[len(auto) // 2]
            when = (target["start_time"] + target["end_time"]) / 2.0

            inserted = put_json(f"/api/session/{quoted}/laps", {
                "mode": "auto",
                "beacons": [{"name": "手工穿越", "time": when}],
            })
            self.assertEqual(len(inserted["laps"]), len(before["laps"]) + 1)
            self.assertTrue(any(abs(row["start_time"] - when) < 0.05
                                for row in inserted["laps"]),
                            "the inserted crossing is not a lap boundary")
            self.assertIsNone(inserted["config"]["beacons"][0].get("lat"))
            self.assertTrue(sidecar.exists(), "the inserted crossing was not saved")

            # the range guard: nothing may be inserted outside the session
            with self.assertRaises(urllib.error.HTTPError) as refused:
                put_json(f"/api/session/{quoted}/laps", {
                    "mode": "auto",
                    "beacons": [{"name": "手工穿越", "time": 1e6}],
                })
            self.assertEqual(refused.exception.code, 400)
            message = json.loads(refused.exception.read().decode("utf-8"))["error"]
            self.assertIn("超出本场时长", message)
            self.assertEqual(len(get_json(f"/api/session/{quoted}/laps")["laps"]),
                             len(inserted["laps"]))

            # ---- #4: rename, with the trusted marks following the new label
            with ld.LogFile.read(HILL) as log:
                track = derive.gps_track(log)
            launch = int(np.searchsorted(track["time"], auto[0]["start_time"]))
            far = int(np.argmax(track["x"]))
            start_gate = {"lat": float(track["lat"][launch]), "lon": float(track["lon"][launch])}

            placed = put_json(f"/api/session/{quoted}/laps", {
                "mode": "auto",
                "beacons": [dict(start_gate, name="左环")],
                "trusted": {"左环 1": False},
            })
            self.assertGreaterEqual(len(placed["laps"]), 3)
            self.assertTrue(all(row["lap"].startswith("左环 ") for row in placed["laps"]))
            self.assertEqual(placed["config"]["trusted"], {"左环 1": False})

            renamed = put_json(f"/api/session/{quoted}/laps", {
                "mode": "auto",
                "beacons": [dict(start_gate, name=" 左环A  ")],
                "trusted": placed["config"]["trusted"],
            })
            self.assertEqual(renamed["config"]["beacons"][0]["name"], "左环A")
            self.assertEqual(renamed["config"]["trusted"], {"左环A 1": False},
                             "renaming lost the trusted marks")
            self.assertTrue(all(row["lap"].startswith("左环A ") for row in renamed["laps"]),
                            "the lap labels did not follow the new name")

            # two beacons may not share a name: the second one gets a suffix
            two = put_json(f"/api/session/{quoted}/laps", {
                "mode": "auto",
                "beacons": [dict(start_gate, name="左环A"),
                            {"name": "左环A", "lat": float(track["lat"][far]),
                             "lon": float(track["lon"][far])}],
                "trusted": renamed["config"]["trusted"],
            })
            self.assertEqual([b["name"] for b in two["config"]["beacons"]],
                             ["左环A", "左环A 2"])

            # it is on disk, not just in the response
            self.assertEqual([b["name"] for b in get_json(
                f"/api/session/{quoted}/laps")["config"]["beacons"]],
                ["左环A", "左环A 2"])

    @_needs(HILL)
    def test_the_distance_lookup_and_a_do_nothing_insert_over_http(self):
        """#5 needs metres -> seconds over HTTP, and a notice when nothing split."""
        sidecar, kept_sidecar = _borrow_sidecar(HILL)
        self.addCleanup(_return_sidecar, sidecar, kept_sidecar)
        with http_session(HILL, roots=LIBRARY_ROOTS, buckets=200) as http:
            base, quoted = http.base, http.quoted
            get_json, put_json = http.get_json, http.put_json

            with ld.LogFile.read(HILL) as log:
                exact = lapsmod.time_at_distance(log, 1500.0)
            self.assertIsNotNone(exact)
            self.assertAlmostEqual(
                get_json(f"/api/session/{quoted}/at?distance=1500")["time"], exact, places=6
            )
            with self.assertRaises(urllib.error.HTTPError) as refused:
                get_json(f"/api/session/{quoted}/at?distance=999999")
            self.assertEqual(refused.exception.code, 400)

            rows = get_json(f"/api/session/{quoted}/laps")["laps"]
            self.assertGreater(len(rows), 1)
            same = put_json(f"/api/session/{quoted}/laps", {
                "mode": "auto",
                "beacons": [{"name": "手工穿越", "time": rows[1]["start_time"]}],
            })
            self.assertEqual(len(same["laps"]), len(rows),
                             "a crossing on an existing boundary changed the lap set")
            self.assertIn("没有切出新圈", same["notice"] or "",
                          "the UI was given nothing to tell the user with")


class TestBeaconUndo(unittest.TestCase):
    """Ticket #6, the part that can be judged without a server.

    "撤销" is not an inverse edit: it is "submit the previous version again".
    Whether there *is* a previous version worth submitting is a pure question,
    so the button's enabled state is decided by a function rather than guessed
    by the front end.
    """

    def test_same_config_compares_what_the_sidecar_stores(self):
        base = lapsmod.LapConfig(
            mode="auto",
            beacons=[lapsmod.Beacon("左环", 34.1, 113.6),
                     lapsmod.Beacon("手工穿越", time=12.5)],
            trusted={"左环 1": False},
        )
        self.assertTrue(lapsmod.same_config(base, lapsmod.LapConfig.from_dict(base.as_dict())))
        # every field the sidecar carries counts as part of "this version"
        for changed in (
            lapsmod.LapConfig(beacons=[lapsmod.Beacon("左环A", 34.1, 113.6),
                                       lapsmod.Beacon("手工穿越", time=12.5)],
                              trusted=dict(base.trusted)),
            lapsmod.LapConfig(beacons=[base.beacons[0],
                                       lapsmod.Beacon("手工穿越", time=12.6)],
                              trusted=dict(base.trusted)),
            lapsmod.LapConfig(beacons=list(base.beacons), trusted={"左环 1": True}),
            lapsmod.LapConfig(mode="run", beacons=list(base.beacons),
                              trusted=dict(base.trusted)),
        ):
            self.assertFalse(lapsmod.same_config(base, changed))
        self.assertFalse(lapsmod.same_config(base, None), "没有上一版，就谈不上同一版")
        # as_dict rounds (7 decimals of position, 4 of time), so "the same
        # version" means "the same bytes in the sidecar": a difference finer
        # than that could never be stored, so it must not make the button lie.
        finer = lapsmod.LapConfig(
            beacons=[lapsmod.Beacon("左环", 34.1 + 1e-9, 113.6), base.beacons[1]],
            trusted=dict(base.trusted),
        )
        self.assertTrue(lapsmod.same_config(base, finer))

    def test_nothing_to_undo_is_said_out_loud(self):
        base = lapsmod.LapConfig(beacons=[lapsmod.Beacon("左环", 34.1, 113.6)])
        self.assertIsNone(lapsmod.undo_config(base, None))
        twin = lapsmod.LapConfig.from_dict(base.as_dict())
        self.assertIsNone(lapsmod.undo_config(base, twin),
                          "上一版和当前版一样时，撤销没有东西可撤")

    def test_undo_hands_the_previous_version_back_untouched(self):
        current = lapsmod.LapConfig(beacons=[lapsmod.Beacon("左环A", 34.1, 113.6)],
                                    trusted={"左环A 1": False})
        previous = lapsmod.LapConfig(beacons=[lapsmod.Beacon("左环", 34.1, 113.6)],
                                     trusted={"左环 1": False})
        back = lapsmod.undo_config(current, previous)
        self.assertIs(back, previous, "撤销要交回去的是上一版本身，不是一份重算过的近似")
        self.assertEqual([b.name for b in back.beacons], ["左环"])
        self.assertEqual(back.trusted, {"左环 1": False})


class TestBeaconUndoOverHttp(unittest.TestCase):
    """Ticket #6 as the UI reaches it: ``PUT {"undo": true}`` on the laps route."""

    @_needs(HILL)
    def test_rename_insert_and_delete_are_each_one_step_back(self):
        sidecar, kept_sidecar = _borrow_sidecar(HILL)
        self.addCleanup(_return_sidecar, sidecar, kept_sidecar)
        with http_session(HILL, roots=LIBRARY_ROOTS, buckets=200) as http:
            base, quoted = http.base, http.quoted
            get_json, put_json = http.get_json, http.put_json
            page_payload = http.page_payload

            def on_disk():
                return json.loads(sidecar.read_text(encoding="utf-8"))

            start = get_json(f"/api/session/{quoted}/laps")
            self.assertFalse(start["can_undo"],
                             "一个刚起的服务不该声称有可撤销的一步")
            first_page = page_payload()
            self.assertIn("laps_can_undo", first_page,
                          "页面没有把撤销状态告诉界面（刷新之后按钮就说不准了）")
            self.assertFalse(first_page["laps_can_undo"])

            with ld.LogFile.read(HILL) as log:
                track = derive.gps_track(log)
                auto = lapsmod.detect_laps(log)
            launch = int(np.searchsorted(track["time"], auto[0].start_time))
            gate = {"lat": float(track["lat"][launch]), "lon": float(track["lon"][launch])}

            # ---- 第 1 步：放一个信标 ----
            placed = put_json(f"/api/session/{quoted}/laps", {
                "mode": "auto", "beacons": [dict(gate, name="左环")],
                "trusted": {"左环 1": False},
            })
            self.assertTrue(placed["can_undo"], "做了一步就没有可撤销的一步？")
            self.assertTrue(page_payload()["laps_can_undo"],
                            "改了一步之后刷新页面，撤销按钮就该亮了")

            # ---- 第 2 步：改名；撤销要连可信标记一起回到旧名字上 ----
            renamed = put_json(f"/api/session/{quoted}/laps", {
                "mode": "auto", "beacons": [dict(gate, name="左环A")],
                "trusted": placed["config"]["trusted"],
            })
            self.assertEqual(renamed["config"]["trusted"], {"左环A 1": False})

            back = put_json(f"/api/session/{quoted}/laps", {"undo": True})
            self.assertEqual([b["name"] for b in back["config"]["beacons"]], ["左环"])
            self.assertEqual(back["config"]["trusted"], {"左环 1": False},
                             "撤销改名没有把可信标记迁回旧名字")
            self.assertTrue(all(row["lap"].startswith("左环 ") for row in back["laps"]),
                            "撤销改名之后圈速表的标签还挂着新名字")
            self.assertFalse(back["can_undo"], "一级撤销用掉之后不该还有下一步")
            self.assertEqual(on_disk()["trusted"], {"左环 1": False},
                             "撤销只改了内存，没有落盘")

            # ---- 第 2 步：插一次穿越；撤销后圈速表复原 ----
            rows = get_json(f"/api/session/{quoted}/laps")["laps"]
            when = (rows[0]["start_time"] + rows[0]["end_time"]) / 2.0
            inserted = put_json(f"/api/session/{quoted}/laps", {
                "mode": "auto",
                "beacons": [dict(gate, name="左环"), {"name": "手工穿越", "time": when}],
            })
            self.assertEqual(len(inserted["laps"]), len(rows) + 1)
            self.assertTrue(inserted["can_undo"])

            # 一次什么都没改的保存不该把上一步吃掉（否则用户"顺手保存一下"就撤不回来了）
            noop = put_json(f"/api/session/{quoted}/laps", inserted["config"])
            self.assertTrue(noop["can_undo"], "一次没改动的保存把上一步吃掉了")

            undone = put_json(f"/api/session/{quoted}/laps", {"undo": True})
            self.assertEqual(len(undone["laps"]), len(rows), "撤销插入没有还原圈速表")
            self.assertEqual([b["name"] for b in undone["config"]["beacons"]], ["左环"])
            self.assertFalse(undone["can_undo"])
            self.assertEqual([b["name"] for b in on_disk()["beacons"]], ["左环"],
                             "撤销插入没有落盘")

            # ---- 第 2 步：删掉信标；撤销把它放回来 ----
            deleted = put_json(f"/api/session/{quoted}/laps", {"mode": "auto", "beacons": []})
            self.assertEqual(deleted["config"]["beacons"], [])
            self.assertTrue(deleted["can_undo"])
            restored = put_json(f"/api/session/{quoted}/laps", {"undo": True})
            self.assertEqual([b["name"] for b in restored["config"]["beacons"]], ["左环"],
                             "撤销删除没有把信标放回来")
            self.assertEqual([b["name"] for b in on_disk()["beacons"]], ["左环"])
            self.assertFalse(restored["can_undo"])

            # ---- 没有可撤销的一步：明确报错，并说下一步做什么 ----
            with self.assertRaises(urllib.error.HTTPError) as refused:
                put_json(f"/api/session/{quoted}/laps", {"undo": True})
            self.assertEqual(refused.exception.code, 400)
            message = json.loads(refused.exception.read().decode("utf-8"))["error"]
            self.assertIn("没有可撤销的一步", message)
            self.assertIn("改一次信标", message, "报错没有告诉用户下一步做什么")
            self.assertEqual([b["name"] for b in on_disk()["beacons"]], ["左环"],
                             "被拒绝的撤销动了边车")


class TestDistanceAxisLookup(unittest.TestCase):
    """On the distance axis the cursor is metres, but a crossing is a moment."""

    @_needs(ENDURANCE)
    def test_a_held_distance_resolves_to_the_moment_the_car_got_there(self):
        """A parked car holds its distance for minutes; arrival is the answer.

        Interpolating (over the plotted trace, or over the distance series, which
        is flat there) answers with the moment the car *left* that distance
        instead - 127 s late on this session.
        """
        with ld.LogFile.read(ENDURANCE) as log:
            distance = np.maximum.accumulate(
                np.asarray(lapsmod.distance_on_master(log), dtype=float)
            )
            time = np.asarray(timebase.axis(log), dtype=np.float64)
            rate = log.sample_rate
            changed = np.flatnonzero(np.diff(distance) != 0)
            starts = np.concatenate([[0], changed + 1])
            ends = np.concatenate([changed, [distance.size - 1]])
            held = (ends - starts) / rate
            found = np.flatnonzero((held > 5.0) & (ends < distance.size - 2)
                                   & (distance[starts] > 1.0))
            self.assertTrue(found.size, "no held-distance stretch to test with")
            index = int(found[np.argmax(held[found])])
            arrival = float(time[starts[index]])
            departure = float(time[ends[index]])
            when = lapsmod.time_at_distance(log, float(distance[starts[index]]))
        self.assertGreater(departure - arrival, 5.0)
        self.assertAlmostEqual(when, arrival, places=3)
        self.assertLess(when, departure - 1.0,
                        "the lookup answered with the departure, not the arrival")

    @_needs(ENDURANCE)
    def test_the_lookup_is_exact_to_the_sample_not_to_the_plot(self):
        """The plotted overview has ~2 s buckets; this walks the distance series."""
        with ld.LogFile.read(ENDURANCE) as log:
            distance = np.maximum.accumulate(
                np.asarray(lapsmod.distance_on_master(log), dtype=float)
            )
            time = np.asarray(timebase.axis(log), dtype=np.float64)
            rate = log.sample_rate
            moving = np.flatnonzero(
                (np.diff(distance, prepend=distance[0]) > 0)
                & (np.diff(distance, append=distance[-1] + 1.0) > 0)
            )
            sample = moving[np.linspace(0, moving.size - 1, 200).astype(int)]
            worst = max(
                abs(lapsmod.time_at_distance(log, float(distance[i])) - time[i])
                for i in sample
            )
            bucket = float(time[-1] - time[0]) / 900.0      # the overview's bucket
        self.assertEqual(len(sample), 200)
        self.assertLessEqual(
            worst, 1.0 / rate + 1e-9,
            f"worst error {worst:.4f} s - only as good as the plot "
            f"({bucket:.2f} s per bucket)",
        )

    @_needs(HILL)
    def test_a_distance_the_car_never_drove_is_refused(self):
        with ld.LogFile.read(HILL) as log:
            distance = np.maximum.accumulate(
                np.asarray(lapsmod.distance_on_master(log), dtype=float)
            )
            far = float(distance[-1]) + 500.0
            grid = [float(value) for value in np.linspace(distance[0], distance[-1], 50)]
            walked = [lapsmod.time_at_distance(log, value) for value in grid]
            inside = lapsmod.time_at_distance(log, float(distance[-1]) / 2.0)
            self.assertIsNone(lapsmod.time_at_distance(log, far))
            self.assertIsNone(lapsmod.time_at_distance(log, -10.0))
            self.assertIsNone(lapsmod.time_at_distance(log, float("nan")))
        self.assertIsNotNone(inside)
        self.assertEqual(walked, sorted(walked), "the mapping must not run backwards")


class TestChannelGroups(unittest.TestCase):
    """i2 Pro groups channels that share a unit so they can share one axis."""

    @_needs(HILL)
    def test_every_channel_lands_in_exactly_one_group(self):
        with ld.LogFile.read(HILL) as log:
            channel_groups, status = render.groups(log)
            total = len(log.channels)
            units = {c.name: c.unit for c in log.channels}
        flat = [n for g in channel_groups for n in g["channels"]] + list(status)
        self.assertEqual(len(flat), total)
        self.assertEqual(len(set(flat)), total)
        for group in channel_groups:
            self.assertEqual({units[n] for n in group["channels"]}, {group["unit"]}, group["label"])

    @_needs(HILL)
    def test_speed_group_comes_first_and_status_is_detected(self):
        with ld.LogFile.read(HILL) as log:
            channel_groups, status = render.groups(log)
        self.assertIn(channel_groups[0]["unit"], ("km/h", "m/s"))
        # this car logs Motor/Inverter/Cell status bits; they belong in the band
        self.assertGreater(len(status), 10)
        self.assertTrue(all("error" in n.lower() or "temp" not in n for n in status))
        self.assertIn("MCU1 FR Error", status)
        self.assertNotIn("MCU1 FR TempMotor", status)


class TestCsvSession(unittest.TestCase):
    """A CSV session must be usable exactly like a `.ld` session."""

    STEM = "20260522-yjw第二次直线3.72"

    @_needs(DATA / "20260522-yjw第二次直线3.72.csv")
    def test_i2pro_export_reads_as_a_session(self):
        with csvlog.read_csv_session(DATA / f"{self.STEM}.csv") as session:
            self.assertGreater(len(session.channels), 300)
            self.assertAlmostEqual(session.sample_rate, 100.0, places=3)
            self.assertGreater(session.duration, 800)
            self.assertEqual(session.metadata()["format"], "csv")
            # the metadata block above the table is used, not ignored
            self.assertEqual(session.device, "C125")
            self.assertEqual(session.header["rate_from"], "元数据")
            self.assertTrue(session.has("GPS Speed"))
            self.assertEqual(len(session.values("GPS Speed")),
                             session.channel("GPS Speed").sample_count)
            self.assertEqual({c.name: c.unit for c in session.channels}["G Force Lat"], "G")

    @_needs(DATA / "20260522-yjw第二次直线3.72.csv")
    def test_csv_and_ld_agree_on_the_channels_they_share(self):
        """The two sources of one session must not disagree downstream."""
        if not (DATA / f"{self.STEM}.ld").exists():
            self.skipTest("matching .ld missing")
        with ld.LogFile.read(DATA / f"{self.STEM}.ld") as binary:
            with csvlog.read_csv_session(DATA / f"{self.STEM}.csv") as text:
                compared = 0
                for ch in binary.channels:
                    if not text.has(ch.name) or abs(ch.sample_rate - text.sample_rate) > 0.5:
                        continue
                    a = binary.values(ch)[:2000]
                    b = text.values(ch.name)[: a.size]
                    tolerance = 0.5 * 10.0 ** (-ch.decimals) + 1e-6
                    self.assertLessEqual(float(np.max(np.abs(a - b))), tolerance, ch.name)
                    compared += 1
                self.assertGreater(compared, 200)

    @_needs(DATA / "20260522-yjw第二次直线3.72.csv")
    def test_the_report_accounts_for_every_column(self):
        with csvlog.read_csv_session(DATA / f"{self.STEM}.csv") as session:
            report, channels = session.report, len(session.channels)
        statuses = {entry.get("matched_by") for entry in report}
        self.assertIn("时间列", statuses)
        self.assertIn("原名", statuses)
        self.assertEqual(len(report), channels + 1)      # + the time column
        for entry in report:
            self.assertTrue(entry.get("status"), entry)

    def test_a_foreign_csv_is_matched_by_name_alias_and_unit(self):
        import tempfile

        rows = [
            "Time,GPS Speed,Wheel Speed FL,Throttle Position,Left Rear Damper",
            "s,km/h,km/h,%,mm",
            "0.00,10.5,10.1,0.0,12.0",
            "0.01,20.5,19.9,55.0,13.5",
            "0.02,30.5,29.5,100.0,15.0",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "other-team.csv"
            path.write_text("\n".join(rows), encoding="utf-8")
            session = csvlog.read_csv_session(path)
        cells = {e["column"]: e for e in session.report if e.get("status") == "通道"}
        self.assertEqual(cells["GPS Speed"]["matched_by"], "原名")
        self.assertEqual(cells["Wheel Speed FL"]["name"], "SpeedFL")
        self.assertEqual(cells["Wheel Speed FL"]["matched_by"], "别名")
        self.assertEqual(cells["Throttle Position"]["name"], "TH")
        self.assertEqual(cells["Left Rear Damper"]["name"], "Left Rear Damper")
        self.assertEqual(cells["Left Rear Damper"]["unit"], "mm")
        self.assertEqual(cells["Left Rear Damper"]["matched_by"], "未匹配")

    def test_a_csv_without_a_time_column_is_refused_with_a_next_step(self):
        """Silently eating the first data column as time would be worse."""
        import tempfile

        rows = ["GPS Speed,Throttle Position", "10.5,0.0", "20.5,55.0", "30.5,100.0"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "no-time.csv"
            path.write_text("\n".join(rows), encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                csvlog.read_csv_session(path)
        message = str(caught.exception)
        self.assertIn("找不到时间列", message)
        self.assertIn("--map", message)          # says what to do next

    def test_manual_mapping_persists_in_a_sidecar(self):
        """Unmatched columns can be corrected, not just reported."""
        import tempfile

        rows = ["Time,Speed,Weird Name", "s,km/h,bar", "0.00,1.0,2.0", "0.01,2.0,3.0",
                "0.02,3.0,4.0"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.csv"
            path.write_text("\n".join(rows), encoding="utf-8")
            self.assertTrue(csvlog.read_csv_session(path).has("Weird Name"))
            sidecar = csvlog.save_map(path, {"Weird Name": "RearPress Mpa"},
                                      {"Weird Name": "MPa"})
            self.assertTrue(sidecar.exists())
            after = csvlog.read_csv_session(path)
            self.assertFalse(after.has("Weird Name"))
            self.assertEqual(after.channel("RearPress Mpa").unit, "MPa")
            self.assertEqual(
                next(e["matched_by"] for e in after.report if e["column"] == "Weird Name"),
                "手工指定",
            )
            explicit = csvlog.read_csv_session(path, renames={"Weird Name": "别的名字"})
            self.assertTrue(explicit.has("别的名字"))     # argument beats the sidecar

    @_needs(DATA / "20260522-yjw第二次直线3.72.csv")
    def test_a_csv_session_goes_through_laps_and_distance(self):
        with csvlog.read_csv_session(DATA / f"{self.STEM}.csv") as session:
            distance = derive.distance_series(session)
            laps = lapsmod.detect_laps(session)
        self.assertGreater(float(distance[-1]), 100.0)
        self.assertTrue(laps)
        self.assertGreater(max(lap.distance for lap in laps), 50.0)

    def test_unit_signal_fills_a_blank_unit_and_flags_a_mismatch(self):
        """The unit is a matching signal: it fills gaps and calls out conflicts."""
        import tempfile

        rows = ["Time,Vx KF,GPS Speed", "s,,mph", "0.00,10,1", "0.01,20,2", "0.02,30,3"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "units.csv"
            path.write_text("\n".join(rows), encoding="utf-8")
            session = csvlog.read_csv_session(path)
        cells = {e["column"]: e for e in session.report if e.get("status") == "通道"}
        self.assertEqual(session.channel("Vx KF").unit, "km/h")   # blank -> filled
        self.assertIn("单位不符", cells["GPS Speed"]["warning"])
        self.assertEqual(cells["Vx KF"]["rate_from"], "时间列")

    def test_csv_is_an_accepted_import(self):
        from i3pro import importer

        self.assertIn(".csv", importer.ALLOWED_SUFFIXES)
        self.assertEqual(importer.safe_name("别的队给的.csv"), "别的队给的.csv")
        # ``.txt`` 从 ticket #32 起是**收**的（分隔文本），所以这里换一个真的不收的
        with self.assertRaises(ValueError):
            importer.safe_name("笔记.md")

    @_needs(DATA / "20260524-耐久正赛.csv")
    def test_a_csv_session_can_compare_two_laps_on_the_distance_axis(self):
        """The ticket's criterion: a CSV session must be comparable, not just cut."""
        with csvlog.read_csv_session(DATA / "20260524-耐久正赛.csv") as session:
            laps = [l for l in lapsmod.detect_laps(session) if l.complete]
            self.assertGreaterEqual(len(laps), 2)
            channel = session.channels[1].name
            result = lapsmod.overlay(session, laps[:2], [channel], step=5.0)
            distance = np.asarray(result["distance"])
            self.assertTrue(np.all(np.diff(distance) > 0))
            for row in result["laps"]:
                self.assertEqual(len(row["time"]), distance.size)
                self.assertEqual(len(row[channel]), distance.size)
            _, delta = lapsmod.time_delta(result["laps"][0], result["laps"][1], distance)
            self.assertTrue(np.isfinite(delta).any())
            table = lapsmod.lap_table(session, laps)
        self.assertEqual(len(table), len(laps))
        self.assertIn("lap_time", table[0])


class TestPoints(unittest.TestCase):
    """The scatter component needs raw samples, never min/max decimation."""

    @_needs(HILL)
    def test_points_are_raw_and_windowed(self):
        with ld.LogFile.read(HILL) as log:
            time = timebase.axis(log)
            payload = render.points(
                log, ["Vx KF", "G Force Lat"], time, start=200.0, end=210.0, max_points=100000
            )
        self.assertEqual(sorted(payload["values"]), ["G Force Lat", "Vx KF"])
        self.assertEqual(payload["stride"], 1)
        self.assertEqual(len(payload["values"]["Vx KF"]), len(payload["time"]))
        self.assertGreaterEqual(min(payload["time"]), 200.0)
        self.assertLessEqual(max(payload["time"]), 210.0)
        # 10 s at 100 Hz must come back complete, not decimated to a few points
        self.assertGreater(len(payload["time"]), 900)

    @_needs(HILL)
    def test_points_stride_when_the_window_is_huge(self):
        with ld.LogFile.read(HILL) as log:
            time = timebase.axis(log)
            payload = render.points(log, ["Vx KF"], time, max_points=100)
        self.assertGreater(payload["stride"], 1)
        self.assertLessEqual(len(payload["time"]), 200)


class TestIndependentParsers(unittest.TestCase):
    """Cross-check the hand-written reader against other implementations."""

    @_needs(HILL)
    def test_agrees_with_gotzl_ldparser(self):
        """`vendor/ldparser` is a separate reverse-engineering of the format."""
        vendor = ROOT / "vendor" / "ldparser"
        if not (vendor / "ldparser.py").exists():
            self.skipTest("vendor/ldparser not present")
        sys.path.insert(0, str(vendor))
        try:
            import ldparser  # type: ignore
        except ImportError as exc:  # pragma: no cover - environment dependent
            self.skipTest(f"ldparser not importable: {exc}")
        try:
            reference = ldparser.ldData.fromfile(str(HILL))
        finally:
            sys.path.remove(str(vendor))

        with ld.LogFile.read(HILL) as log:
            self.assertEqual(len(log.channels), len(reference.channs))
            checked = 0
            for index, mine in enumerate(log.channels):
                theirs = reference.channs[index]
                self.assertEqual(mine.name, theirs.name)
                self.assertEqual(mine.unit, theirs.unit)
                self.assertEqual(mine.sample_count, theirs.data_len)
                if index % 37:  # sampling every 37th channel keeps this quick
                    continue
                np.testing.assert_allclose(
                    log.values(mine), theirs.data, rtol=1e-9, atol=1e-9,
                    err_msg=f"channel {mine.name}",
                )
                checked += 1
            self.assertGreater(checked, 8)

    @_needs(DATA / "20260522-yjw第二次直线3.72.ld")
    def test_agrees_with_motec_csv_export_every_channel(self):
        """Every channel exported at the log rate must match i2 Pro's own CSV."""
        stem = "20260522-yjw第二次直线3.72"
        if not (DATA / f"{stem}.csv").exists():
            self.skipTest("csv export missing")
        with ld.LogFile.read(DATA / f"{stem}.ld") as log:
            csv_export = motec_csv.load(DATA / f"{stem}.csv", max_rows=2000)
            compared = 0
            for ch in log.channels:
                if ch.name not in csv_export.frame.columns:
                    continue
                if abs(ch.sample_rate - (csv_export.sample_rate or 100.0)) > 0.5:
                    continue
                reference = csv_export.values(ch.name)[:2000]
                values = log.values(ch)[: reference.size]
                tolerance = 0.5 * 10.0 ** (-ch.decimals) + 1e-9
                self.assertLessEqual(
                    float(np.max(np.abs(reference - values))), tolerance, ch.name
                )
                compared += 1
            self.assertGreater(compared, 200)


class TestLaunchers(unittest.TestCase):
    """The one-click path: 启动.bat -> serve, 导出快照.bat -> snapshot."""

    @_needs(HILL)
    def test_snapshot_writes_html_and_index(self):
        import contextlib
        import io
        import tempfile

        from i3pro import cli

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "data"
            target = Path(tmp) / "out"
            source.mkdir()
            shutil.copy2(HILL, source / HILL.name)
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["snapshot", "--data", str(source), "--out", str(target)])
            self.assertEqual(code, 0)
            produced = target / f"{HILL.stem}.html"
            self.assertTrue(produced.exists())
            self.assertGreater(produced.stat().st_size, 200_000)
            page = produced.read_text(encoding="utf-8")
            self.assertIn('"channels"', page)
            index = (target / "index.html").read_text(encoding="utf-8")
            # 索引里的"场次"列写的是侧边栏那一套名字（不带扩展名），
            # 并起来的 CAN 记录才带 +1——两种场次同一处发现逻辑。
            self.assertIn(HILL.stem, index)
            self.assertIn("启动.bat", index)   # tells the reader how to get full detail

    def test_snapshot_reports_missing_data_dir(self):
        import contextlib
        import io
        import tempfile

        from i3pro import cli

        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(
                    ["snapshot", "--data", str(Path(tmp) / "nope"),
                     "--out", str(Path(tmp) / "out")]
                )
        self.assertEqual(code, 1)

    def test_snapshot_covers_a_raw_can_frame_table(self):
        """原始 CAN 帧表也是场次，所以 `导出快照.bat` 必须把它导出来。

        这条挡的是"场次列表里有、快照里没有"：快照那份清单以前是
        ``data.glob("*.ld")``，帧表（.csv）一个都进不去，而命令照样退出 0。
        """
        import contextlib
        import io
        import tempfile

        from i3pro import cli

        frames = CAN_DATA / "2026_10_03_201147_ID0001.csv"
        if not frames.exists() or not DBC_DIR.exists():
            self.skipTest("缺 can_data/ 或 i2pro_data/dbc/")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "data"
            target = Path(tmp) / "out"
            source.mkdir()
            shutil.copytree(DBC_DIR, source / "dbc")      # DBC 挨着场次放，跟仓库一样
            shutil.copy2(frames, source / frames.name)
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["snapshot", "--data", str(source), "--out", str(target)])
            self.assertEqual(code, 0)
            produced = target / f"{frames.stem}.html"
            self.assertTrue(produced.exists(), sorted(p.name for p in target.iterdir()))
            self.assertGreater(produced.stat().st_size, 100_000)
            index = (target / "index.html").read_text(encoding="utf-8")
            self.assertIn(frames.stem, index)
            self.assertIn("CAN", index)                    # 设备列写的是 CAN

    def test_snapshot_of_contiguous_recordings_is_one_session(self):
        """9 份是 7 次记录：一次记录的后续文件并进第一份，快照也就一份。

        以前按文件逐个导出，会得到 9 个"半截"快照（每份 1,000,000 帧处被切开）。
        """
        import contextlib
        import io
        import tempfile

        from i3pro import cli

        pair = ["2026_10_03_173345_ID0001.csv", "2026_10_03_173936_ID0001.csv"]
        missing = [name for name in pair if not (CAN_DATA / name).exists()]
        if missing or not DBC_DIR.exists():
            self.skipTest("缺 can_data/ 或 i2pro_data/dbc/")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "data"
            target = Path(tmp) / "out"
            source.mkdir()
            shutil.copytree(DBC_DIR, source / "dbc")
            for name in pair:
                shutil.copy2(CAN_DATA / name, source / name)
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["snapshot", "--data", str(source), "--out", str(target)])
            self.assertEqual(code, 0)
            pages = sorted(p.name for p in target.glob("*.html") if p.name != "index.html")
            self.assertEqual(pages, ["2026_10_03_173345_ID0001+1.html"])
            index = (target / "index.html").read_text(encoding="utf-8")
            self.assertIn("2026_10_03_173345_ID0001+1", index)
            # 索引里只有并起来的那一场，第二份文件不该单独占一行
            self.assertNotIn("2026_10_03_173936_ID0001", index)

    def test_bind_moves_to_the_next_free_port(self):
        """Starting twice must not die with a bind error."""
        import socket

        from i3pro import server

        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        busy = holder.getsockname()[1]
        library = server.SessionLibrary([])
        try:
            httpd = server.bind("127.0.0.1", busy, server.make_handler(library), attempts=5)
            try:
                self.assertNotEqual(httpd.server_address[1], busy)
            finally:
                httpd.server_close()
        finally:
            holder.close()
            library.close()


class TestViewerScript(unittest.TestCase):
    """The generated workbench must execute without throwing (needs node)."""

    def _smoke(self, hash_value=""):  # noqa: A002 - mirrors location.hash
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "view.html"
            with ld.LogFile.read(HILL) as log:
                render.render_html(log, out, buckets=150)
            env = {**os.environ, "I3PRO_HASH": hash_value}
            finished = subprocess.run(
                [node, str(ROOT / "tools" / "smoke_viewer.js"), str(out)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=env, timeout=120,
            )
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertIn("PASS", finished.stdout)

    @_needs(HILL)
    def test_runs_headless_in_time_axis(self):
        self._smoke()

    @_needs(HILL)
    def test_runs_headless_in_overlay_mode(self):
        self._smoke("mode=overlay")

    def test_template_without_data_shows_instructions(self):
        """Opening src/.../viewer.html directly must explain itself, not throw."""
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        finished = subprocess.run(
            [node, str(ROOT / "tools" / "smoke_viewer.js"),
             str(ROOT / "src" / "i3pro" / "web" / "viewer.html"), "--expect-template"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
        )
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertIn("instructions", finished.stdout)


class TestComponentRegistry(unittest.TestCase):
    """ticket #17 / #19 / #20：每种显示形式只在注册表里声明一次。

    守的是"再加一种显示形式要改几处"这件事本身——前端唯一会被反复加东西的
    地方就是它。迁移分三批走完（#17 骨架 → #19 图表类 → #20 表格与仪表类），
    现在每一种显示形式都在那张表里；代码里剩下的 `type === "graph"` 只有
    "哪个组件是图"这一类语义判断（侧边栏编辑谁、键盘作用在谁身上），不是
    显示形式的分派，所以它们留着，条数也钉住。

    这里只扫源码，跑不了注册表本身；注册表"真的管用"由无头驱动的第 32 / 33 组
    断言（`tools/smoke_viewer.js`）负责——它会新加一个只声明过的形式并走完
    添加下拉 → 标题 → 默认配置 → 渲染 → 分享链接往返，也会核对五类图表形式
    的声明与"取数只剩一条路"。
    """

    VIEWER = ROOT / "src" / "i3pro" / "web" / "viewer.html"
    DRIVER = ROOT / "tools" / "smoke_viewer.js"

    #: 注册表里声明过的全部显示形式（= 添加下拉里能看到的那几个 + 已声明的）。
    DECLARED = ("graph", "scatter", "histogram", "spectrum", "track", "gauge",
                "delta", "status", "report", "chreport")

    #: 需要服务端数据的显示形式：它们的声明里必须说清"这一屏问的问题"。
    NEEDS_DATA = ("scatter", "histogram", "spectrum", "track", "report", "chreport")

    #: 迁移之后还剩几处 `type === "..."`。实测值：9 处"哪个组件是图"
    #: （`channelColor` / `focusedComponent` / 键盘与侧边栏作用在谁身上），
    #: 1 处"哪一列是文字列"（报表排版，`column.type === "text"`）。
    #: 变多了说明新的显示形式又在自己判类型——那要么改成声明，要么在这里
    #: 写清为什么它算语义判断。
    TYPE_DISPATCH = 10

    def test_每种显示形式都在注册表里有声明(self):
        source = self.VIEWER.read_text(encoding="utf-8")
        for name in self.DECLARED:
            self.assertRegex(
                source, rf"\n  {name}: \{{",
                f"注册表里没有 {name} 的声明块——「有哪些显示形式」必须只有表一个答案",
            )

    def test_类型分派只剩哪个组件是图(self):
        source = self.VIEWER.read_text(encoding="utf-8")
        # 注释里提到 `type === "..."` 的写法不算分派。
        code = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith(("*", "//"))
        )
        hits = re.findall(r'\.type [!=]== "([^"]+)"', code)
        self.assertEqual(
            len(hits), self.TYPE_DISPATCH,
            f"工作表里还剩 {len(hits)} 处按类型分派（实测是 {self.TYPE_DISPATCH}）："
            "新的显示形式要么把这件事交给声明（defaults / needs / controls / hooks / "
            "sync / encode / decode / render / data），要么把这条数字与理由一起改。",
        )
        self.assertEqual(
            sorted(set(hits)), ["graph", "text"],
            f"剩下的分派只该是「哪个组件是图」与「哪一列是文字列」这两类语义判断，"
            f"实际还有：{sorted(set(hits))}",
        )

    def test_已迁移的类型不再留类型分派分支(self):
        source = self.VIEWER.read_text(encoding="utf-8")
        for name in self.DECLARED:
            if name == "graph":
                continue        # 见 test_类型分派只剩哪个组件是图：这是语义判断
            for pattern in (f'type === "{name}"', f"type !== \"{name}\"",
                            f"type === '{name}'"):
                self.assertNotIn(
                    pattern, source,
                    f"{name} 既然已经迁进注册表，就不该再有 {pattern} 这种分派；"
                    "把漏掉的那处也交给声明（那条路该怎么走，看表里它自己声明了什么）。",
                )

    def _spec_block(self, name):
        """注册表里某个类型的声明块（`name: { … },`）。"""
        source = self.VIEWER.read_text(encoding="utf-8")
        match = re.search(rf"\n  {name}: \{{(.*?)\n  \}},", source, re.S)
        self.assertIsNotNone(match, f"注册表里没有 {name} 的声明块")
        return match.group(1)

    def test_每种显示形式都声明了怎么画与怎么取数(self):
        """钉的是**结构**，不是字段清单。

        每一种形式都要说自己怎么画（`render`）；要服务端数据的那几种还要说清
        "这一屏问的问题"（`data`，写 `data:` 或延迟取的 `get data()` 都算）。
        字段叫什么是注册表自己的演进，不该让这条守卫变红。
        """
        for name, render_fn in (("delta", "renderDeltaComponent"),
                                ("status", "renderStatusComponent"),
                                ("track", "renderTrackComponent")):
            block = self._spec_block(name)
            self.assertIn("render:", block, f"{name} 的声明里没有 render")
            self.assertIn(render_fn, block, f"{name} 的 render 该是 {render_fn}")

        for name in self.DECLARED:
            block = self._spec_block(name)
            self.assertIn("render:", block, f"{name} 的声明里没有 render")
            if name in self.NEEDS_DATA:
                self.assertTrue(
                    "data:" in block or "get data()" in block,
                    f"{name} 要服务端的数据，却没说清自己怎么取（data 或 get data()）",
                )

        status = self._spec_block("status")
        self.assertTrue("needs:" in status or "data:" in status,
                        "状态组件的取数该由它自己的声明说了算（needs 或 data）")
        self.assertIn("hotkey", status, "状态组件占着 E 键，这也得它自己声明")

        track = self._spec_block("track")
        self.assertTrue("data:" in track or "get data()" in track,
                        "轨迹要声明自己怎么取数（data）")
        for field in ("defaults:", "controls:", "hooks:", "encode:", "decode:"):
            self.assertIn(field, track, f"轨迹的声明里少了 {field}")

    def test_取数只有一条路(self):
        """ticket #21：组件取数都得经 apiUrl / apiGet，脚本里不许自己拼自己发。"""
        source = self.VIEWER.read_text(encoding="utf-8")
        self.assertIn("function apiUrl(", source, "没有 apiUrl：地址就没有唯一出处")
        self.assertIn("function apiGet(", source, "没有 apiGet：请求与出错就没有唯一出处")
        for endpoint in ("points", "histogram", "spectrum", "track", "report", "trace"):
            self.assertNotRegex(
                source, rf'fetch\([^\n]*"/{endpoint}"',
                f"{endpoint} 还在自己发请求：它该走 apiGet(apiUrl(...))（ticket #21）",
            )

    def test_自检用的形式只在无头驱动里注册(self):
        """队员的浏览器里不许出现「只有标题（自检）」这种东西。"""
        source = self.VIEWER.read_text(encoding="utf-8")
        self.assertIn('window.__I3PRO_SELFTEST__', source,
                      "注册表里的自检形式必须挂在 __I3PRO_SELFTEST__ 上，"
                      "否则它会出现在队员的「＋ 添加组件」下拉里")
        driver = self.DRIVER.read_text(encoding="utf-8")
        self.assertIn("__I3PRO_SELFTEST__: true", driver,
                      "无头驱动没设自检标记，第 32 组断言就等于没跑")


class TestSidecar(unittest.TestCase):
    """ticket #16：六种侧车只经一个接口读写，失败策略只有一套。

    六种：信标 / 赛道区段 / GPS 校正 / 注释 / 数学通道 / CSV 列映射。
    这里钉四件事：六个领域模块里不再有文件读写；缺了=空、读坏=报错且不删文件、
    写=原子替换；新加一种只要在 `sidecar.KINDS` 里加一条；快照 / serve / 命令行
    三条路读到的同一份侧车逐字节一致。
    """

    #: 六种侧车分别住在哪个领域模块里（用来扫"还有没有自己读写文件"）。
    OWNERS = {
        "laps": "laps.py",
        "sections": "sections.py",
        "gps": "gpsfix.py",
        "notes": "notes.py",
        "maths": "maths.py",
        "csvmap": "csvlog.py",
    }

    def test_六个领域模块里不再有文件读写(self):
        """「拼路径 + 读文件 + 写文件」只该出现在 sidecar.py 里。"""
        offenders = []
        for name, filename in self.OWNERS.items():
            text = (ROOT / "src" / "i3pro" / filename).read_text(encoding="utf-8")
            for needle in ("read_text(", "write_text(", "json.load(", "json.dump("):
                if needle in text:
                    offenders.append(f"{filename}: {needle}")
        self.assertEqual(
            offenders, [],
            "这些文件还在自己碰侧车文件（该走 `sidecar`）：" + repr(offenders),
        )

    def test_六种侧车都在同一个接口上登记(self):
        expected = {
            "laps": ".laps.json", "sections": ".sections.json", "gps": ".gps.json",
            "notes": ".notes.json", "maths": ".maths.json", "csvmap": ".map.json",
        }
        self.assertEqual({name: sidecar.kind_of(name).suffix for name in expected}, expected)
        # 路径也由它算：`.ld` / `.csv` / 名字里带点的场次都得对
        self.assertEqual(sidecar.path_of("laps", "场次.ld").name, "场次.laps.json")
        self.assertEqual(sidecar.path_of("csvmap", "a.b.csv").name, "a.b.map.json")
        self.assertEqual(
            sidecar.path_of("notes", Path("目录") / "x.ld").name, "x.notes.json")

    def test_缺失等于空(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "场次.ld"
            self.assertFalse(sidecar.path_of("laps", session).exists())
            for name, kind in sidecar.KINDS.items():
                self.assertEqual(sidecar.read(name, session), kind.blank(),
                                 f"{name} 缺失时该给出它的空值")
            # 领域层看到的是"还没设过"，不是错误
            self.assertEqual(lapsmod.load_config(session).as_dict(), lapsmod.LapConfig().as_dict())
            self.assertIsNone(sectionsmod.load_config(session))
            self.assertIsNone(gpsfix.load_config(session))
            self.assertEqual(notesmod.load_notes(session), [])
            self.assertEqual(csvlog.load_map(session), {"renames": {}, "units": {}})
            self.assertEqual(mathsmod.load_local(session).definitions, [])

    def test_读坏要报错且不删文件(self):
        import tempfile

        kinds = {"laps": dict, "sections": dict, "gps": dict,
                 "notes": (list, dict), "maths": dict, "csvmap": dict}
        with tempfile.TemporaryDirectory() as tmp:
            for name, shape in kinds.items():
                session = Path(tmp) / f"{name}.ld"
                path = sidecar.path_of(name, session)
                # 坏 JSON：报错、说下一步、文件原样留着
                path.write_text("{ 这不是 JSON", encoding="utf-8")
                with self.assertRaises(sidecar.SidecarError) as caught:
                    sidecar.read(name, session)
                self.assertIn("修好这个 JSON", str(caught.exception), name)
                self.assertTrue(path.exists(), f"{name} 的坏文件被删掉了")
                self.assertEqual(path.read_text(encoding="utf-8"), "{ 这不是 JSON")
                # 顶层形状不对：也要吵，并说清该是什么
                wrong = 3 if list in (shape if isinstance(shape, tuple) else (shape,)) else []
                path.write_text(json.dumps(wrong), encoding="utf-8")
                with self.assertRaises(sidecar.SidecarError) as caught:
                    sidecar.read(name, session)
                self.assertIn("顶层", str(caught.exception), name)

    def test_写是原子的_不留临时文件(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "场次.ld"
            path = sidecar.write("laps", session, {"mode": "auto", "beacons": []})
            self.assertEqual(path.read_text(encoding="utf-8")[-1], "\n",
                             "写出来的 JSON 该以换行收尾")
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [], "原子写留下的临时文件没清掉")

    def test_新加一种侧车只要一处登记(self):
        """加一条 `KINDS` 就够了：连后缀、缺省值、报错文案都从它来。"""
        import tempfile

        name = "_自检侧车"
        sidecar.register(name, sidecar.Kind("._selftest.json", dict, "自检侧车回到空"))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                session = Path(tmp) / "场次.ld"
                self.assertEqual(sidecar.path_of(name, session).name, "场次._selftest.json")
                self.assertEqual(sidecar.read(name, session), {})
                path = sidecar.write(name, session, {"a": 1})
                self.assertEqual(sidecar.read(name, session), {"a": 1})
                path.write_text("[1, 2]", encoding="utf-8")
                with self.assertRaises(sidecar.SidecarError) as caught:
                    sidecar.read(name, session)
                self.assertIn("自检侧车回到空", str(caught.exception))
        finally:
            sidecar.KINDS.pop(name, None)

    @_needs(HILL)
    def test_三条路径读到的侧车逐字节一致(self):
        """快照（`render.build_payload`）/ serve（HTTP）/ 命令行读的是同一份。"""
        import tempfile

        # 侧车要在服务起来**之前**摆好，所以这个目录由用例自己建、自己管
        # （`root=` 那种形态：夹具不拥有它，也不删它）。
        # 服务端的会话缓存把 `.ld` 映射在内存里，夹具会先关缓存再删目录；
        # `ignore_cleanup_errors` 让这个平台差异不至于把测试判红。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            copy = root / HILL.name
            copy.write_bytes(HILL.read_bytes())
            config = lapsmod.LapConfig(
                mode="manual",
                beacons=[lapsmod.Beacon(name="起跑线", time=12.5),
                         lapsmod.Beacon(name="终点线", time=41.0)],
            )
            lapsmod.save_config(copy, config)

            # ① 命令行那条：直接读侧车
            cli_view = lapsmod.load_config(copy).as_dict()

            # ② 快照那条：页面上注入的 payload
            with ld.LogFile.read(copy) as log:
                payload = render.build_payload(log, channels=["Vx KF"])
            snapshot_view = payload["laps_config"]

            # ③ serve 那条：HTTP 拿到的
            with http_session(HILL, root=root, buckets=200) as http:
                serve_view = http.get_json(f"/api/session/{http.quoted}/laps")["config"]

            same = json.dumps(cli_view, sort_keys=True, ensure_ascii=False)
            self.assertEqual(same, json.dumps(snapshot_view, sort_keys=True, ensure_ascii=False))
            self.assertEqual(same, json.dumps(serve_view, sort_keys=True, ensure_ascii=False))
            self.assertEqual(len(cli_view["beacons"]), 2)


class TestTimebase(unittest.TestCase):
    """ticket #22：主时间基只有一处答案。

    三件事：那条规则在 `src` 里只剩一行（`tests` 里 0 行）；两份金标准场次的轴与
    改动前**逐点相同**；Parquet 与画图 / 服务取的是同一条轴，`--rate` 换的也是
    同一个答案。

    这里的金标准数字是 2026-09-13、`2a5da62` **之前**实测冻住的（当时
    `laps._master_time` 与 `report.master_time` 两条私有实现逐点 `array_equal`）。
    轴是 `arange(长度) / 步长`，所以"长度 + 首末点 + 和"都对上就等于逐点相同。
    """

    HILL_LENGTH = 46400
    HILL_RATE = 100.0
    HILL_LAST = 463.99
    HILL_SUM = 10764568.0
    ENDURANCE_LENGTH = 194300
    ENDURANCE_RATE = 100.0
    ENDURANCE_LAST = 1942.99
    ENDURANCE_SUM = 188761478.5

    #: 那条规则长什么样（写成变量，省得它在消息里被当成第二个副本）。
    RULE = re.compile(r"int\(round\(.*sample_rate.*\)\) \+ 1")

    def _rule_hits(self, folder: str) -> list[str]:
        hits = []
        for path in sorted((ROOT / folder).rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if self.RULE.search(line):
                    hits.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
        return hits

    def test_主时间基的公式在源码里只剩一处(self):
        src = self._rule_hits("src")
        self.assertEqual(
            len(src), 1,
            "主时间基的公式在 src 里只该剩一处（`timebase.py`），实际：" + repr(src),
        )
        self.assertTrue(
            src[0].startswith("src/i3pro/timebase.py:"),
            f"那一处该在 timebase.py，实际是 {src[0]}",
        )
        self.assertEqual(
            self._rule_hits("tests"), [],
            "测试里也要从 timebase 取轴：自己再算一遍就等于给这条规则又开了个副本",
        )

    @_needs(HILL)
    def test_高避的轴与改动前逐点相同(self):
        with ld.LogFile.read(HILL) as log:
            axis = timebase.axis(log)
            self.assertEqual(timebase.rate_of(log), self.HILL_RATE)
        self.assertEqual(axis.size, self.HILL_LENGTH)
        self.assertEqual(axis.dtype, np.float64)
        self.assertEqual(float(axis[0]), 0.0)
        self.assertEqual(float(axis[-1]), self.HILL_LAST)
        self.assertAlmostEqual(float(axis.sum()), self.HILL_SUM, places=3)

    @_needs(ENDURANCE)
    def test_耐久正赛的轴与改动前逐点相同(self):
        with ld.LogFile.read(ENDURANCE) as log:
            axis = timebase.axis(log)
            self.assertEqual(timebase.rate_of(log), self.ENDURANCE_RATE)
        self.assertEqual(axis.size, self.ENDURANCE_LENGTH)
        self.assertEqual(float(axis[-1]), self.ENDURANCE_LAST)
        self.assertAlmostEqual(float(axis.sum()), self.ENDURANCE_SUM, places=3)

    @_needs(HILL)
    def test_parquet_报表_取的是一条轴(self):
        """三处出口（Parquet 列 / 报表 / 画图服务用的 timebase）给的是同一条轴。"""
        with ld.LogFile.read(HILL) as log:
            table, meta = store.build_table(log, channels=["Vx KF"])
            parquet_time = np.asarray(table.column("time").to_pylist(), dtype=np.float64)
            expected = timebase.axis(log)
            np.testing.assert_array_equal(parquet_time, expected)
            np.testing.assert_array_equal(reportmod.master_time(log), expected)
            self.assertEqual(meta["rows"], expected.size)

    @_needs(HILL)
    def test_rate_换的是同一个答案(self):
        """`--rate` 让 Parquet 换采样率时，换的是同一个答案而不是第二条轴。"""
        with ld.LogFile.read(HILL) as log:
            table, meta = store.build_table(log, channels=["Vx KF"], master_rate=50.0)
            parquet_time = np.asarray(table.column("time").to_pylist(), dtype=np.float64)
            np.testing.assert_array_equal(parquet_time, timebase.axis(log, 50.0))
            self.assertEqual(meta["rows"], timebase.length(log, 50.0))
            self.assertNotEqual(meta["rows"], timebase.length(log))
            self.assertEqual(meta["sample_rate"], 50.0)


class _MathSession:
    """A minimal session, so the maths engine can be tested without a ``.ld``.

    The engine only asks a session for ``has`` / ``channel`` / ``values`` /
    ``sample_rate`` / ``duration``, and ``attach`` writes into ``derived`` (the
    same attribute a real ``LogFile`` has). Keeping this stub in the test file
    is what lets every expression test run on a machine with no team data.

    ``derived_target`` / ``derived_names`` / ``derived_units`` 是 ticket #18 定的
    显式契约：会话自己声明数学通道的列放哪、叫什么、什么单位，``channels.py``
    不去嗅探对象有哪些属性。真实的 ``LogFile`` / ``CsvSession`` 同样声明这三样。
    """

    def __init__(self, columns: dict, rate: float = 10.0, path="fake.ld", rates: dict | None = None):
        self.columns = {k: np.asarray(v, dtype=np.float64) for k, v in columns.items()}
        self.sample_rate = float(rate)
        self.derived: dict[str, np.ndarray] = {}
        self.derived_names: set[str] = set()
        self.derived_units: dict[str, str] = {}
        size = max(len(v) for v in self.columns.values())
        self.duration = (size - 1) / self.sample_rate
        self.path = Path(path)
        rates = rates or {}
        self.channels = [
            ld.Channel(
                name=name, short_name=name[:8], unit="",
                sample_rate=float(rates.get(name, self.sample_rate)),
                sample_count=len(values), data_offset=0, data_type=5, bytes_per_sample=4,
                multiplier=1, divider=1, decimals=3, shift=0, channel_id=index, index=index,
            )
            for index, (name, values) in enumerate(self.columns.items())
        ]

    def has(self, name: str) -> bool:
        return name in self.columns or name in self.derived

    @property
    def derived_target(self) -> dict:
        return self.derived

    def channel(self, name: str) -> ld.Channel:
        for ch in self.channels:
            if ch.name == name:
                return ch
        raise KeyError(name)

    def values(self, name) -> np.ndarray:
        key = name if isinstance(name, str) else name.name
        if key in self.derived:
            return self.derived[key]
        return self.columns[key]

    #: ticket #34 起，会话还要能被问到**原始样本**（判"整段有没有有效样本"用）。
    #: 这个桩的列本来就是浮点列，原始样本就是它自己。
    def raw(self, name) -> np.ndarray:
        return self.values(name)

    def time_base(self) -> np.ndarray:
        return np.arange(self.channels[0].sample_count) / self.sample_rate

    def close(self) -> None:
        pass


class TestMaths(unittest.TestCase):
    """#3 的表达式引擎：白名单求值、作用域、缓存，全部走纯函数入口。"""

    def _session(self):
        rate, count = 10.0, 51
        t = np.arange(count) / rate
        return _MathSession(
            {
                "车速": 36.0 + 4.0 * np.sin(t),
                "车轮速度": np.full(count, 36.0),
                "刹车压力": np.where((t > 1.0) & (t < 3.0), 10.0, 0.0),
                "坡度": np.linspace(0.0, 5.0, count),
            },
            rate=rate,
        )

    def _eval(self, text, session=None):
        return mathsmod.evaluate(text, session or self._session())

    # ------------------------------------------------------------ 求值本身
    def test_arithmetic_precedence_and_constants(self):
        values = self._eval("1 + 2 * 3 - 4 / 2")
        self.assertEqual(values.size, 51)
        self.assertAlmostEqual(float(values[0]), 5.0)
        self.assertAlmostEqual(float(self._eval("pi")[0]), np.pi, places=6)
        self.assertAlmostEqual(float(self._eval("2 ^ 3 ^ 2")[0]), 512.0)   # 右结合
        self.assertAlmostEqual(float(self._eval("-(3) + 1")[0]), -2.0)

    def test_channel_reference_needs_quotes_only_when_it_has_spaces(self):
        session = _MathSession({"车轮速度": np.full(11, 30.0), "车速": np.full(11, 20.0)})
        self.assertAlmostEqual(float(mathsmod.evaluate("'车轮速度' - 车速", session)[0]), 10.0)

    def test_division_by_zero_is_infinite_not_a_crash(self):
        values = self._eval("1 / 0")
        self.assertTrue(np.all(np.isinf(values)))

    def test_unknown_function_says_what_to_do(self):
        with self.assertRaises(mathsmod.MathError) as caught:
            mathsmod.compile_expr("nosuchfunc(1)")
        self.assertIn("未知函数", str(caught.exception))
        self.assertIn("函数", str(caught.exception))

    def test_unknown_channel_says_what_to_do(self):
        with self.assertRaises(mathsmod.MathError) as caught:
            self._eval("'这个通道不存在' + 1")
        self.assertIn("本场次没有这个通道", str(caught.exception))

    def test_unbalanced_and_dangling_expressions_explain_themselves(self):
        for text, fragment in (
            ("1 +", "结尾还缺一个运算数"),
            ("(1 + 2", "括号没配平"),
            ("1 + 2)", "多余的右括号"),
            ("'刹车压力", "收尾的单引号"),
            ("1 @ 2", "看不懂的字符"),
        ):
            with self.subTest(text=text):
                with self.assertRaises(mathsmod.MathError) as caught:
                    mathsmod.compile_expr(text)
                self.assertIn(fragment, str(caught.exception))

    def test_wrong_argument_count_is_caught_at_compile_time(self):
        with self.assertRaises(mathsmod.MathError) as caught:
            mathsmod.compile_expr("choose(1, 2)")
        self.assertIn("参数", str(caught.exception))
        # 可选参数：integrate 只要一个参数也能编译
        mathsmod.compile_expr("integrate('车速')")
        mathsmod.compile_expr("smooth('车速')")

    def test_whitelist_only_never_calls_eval(self):
        """验收点里唯一一条能用机器判定的"不是 eval"：拿 AST 找调用名。

        顺便证明宿主语言里那些能逃出白名单的写法在词法阶段就被挡住——
        它们连编译都过不去，根本没有机会被执行。
        """
        import ast

        source = (ROOT / "src" / "i3pro" / "maths.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        for builtin in ("eval", "exec", "compile", "__import__", "getattr", "globals"):
            self.assertNotIn(builtin, called, f"maths.py 调用了 {builtin}")
        for hostile in (
            "__import__('os').system('calc')",
            "os.system('calc')",
            "(lambda: 1)()",
            "[x for x in range(3)]",
            "{'a': 1}",
        ):
            with self.subTest(expr=hostile):
                with self.assertRaises(mathsmod.MathError):
                    mathsmod.compile_expr(hostile)

    def test_unsupported_functions_say_why_and_what_to_use_instead(self):
        with self.assertRaises(mathsmod.MathError) as caught:
            mathsmod.compile_expr("filter_cheby_lp('车速', 5)")
        message = str(caught.exception)
        self.assertIn("filter_lp", message)
        self.assertIn("numpy", message)

    # --------------------------------------------------------- 区间与滤波
    def test_interval_statistics_honour_condition_and_reset(self):
        session = _MathSession({"刹车压力": np.array([0.0, 5.0, 10.0, 0.0, 20.0, 0.0])},
                               rate=1.0)
        # range_change 是"这一段从这里开始"的脉冲：两个脉冲把时间轴切成两段
        values = mathsmod.evaluate(
            "stat_max(刹车压力, 刹车压力 > 0, range_change(0, 2) + range_change(3, 5))",
            session,
        )
        self.assertAlmostEqual(float(values[0]), 10.0)    # 第一段里有 5 与 10
        self.assertAlmostEqual(float(values[2]), 10.0)
        self.assertAlmostEqual(float(values[3]), 20.0)    # 第二段里只有 20
        self.assertAlmostEqual(float(values[5]), 20.0)

    def test_interval_statistics_reset_splits_but_condition_filters(self):
        session = _MathSession({"刹车压力": np.array([0.0, 5.0, 10.0, 0.0, 20.0, 0.0])},
                               rate=1.0)
        # 只有一个脉冲时，后面全都算同一段——条件才是筛样本的那一半
        single = mathsmod.evaluate(
            "stat_max(刹车压力, 刹车压力 > 0, range_change(1, 3))", session
        )
        self.assertTrue(np.isnan(float(single[0])), "脉冲之前那一段没有合格样本")
        self.assertAlmostEqual(float(single[4]), 20.0)
        # 不带条件：连 0 也算进去，最大值不变但"有没有样本"变了
        unfiltered = mathsmod.evaluate("stat_max(刹车压力, 1, range_change(1, 3))", session)
        self.assertAlmostEqual(float(unfiltered[0]), 0.0)

    def test_interval_statistic_without_qualified_samples_is_nan_not_zero(self):
        session = _MathSession({"信号": np.array([1.0, 2.0, 3.0, 4.0])}, rate=1.0)
        values = mathsmod.evaluate("stat_max(信号, 信号 > 99)", session)
        self.assertTrue(np.all(np.isnan(values)),
                        "空区间必须给 NaN：0 会被当成一次真实测量")

    def test_derivative_of_a_ramp_is_the_slope(self):
        session = _MathSession({"坡度": np.arange(11, dtype=np.float64)}, rate=10.0)
        values = mathsmod.evaluate("derivative(坡度, 1)", session)
        self.assertAlmostEqual(float(values[5]), 10.0, places=6)

    def test_integrate_of_a_constant_is_a_ramp(self):
        session = _MathSession({"常数": np.full(11, 2.0)}, rate=10.0)
        values = mathsmod.evaluate("integrate(常数)", session)
        self.assertAlmostEqual(float(values[0]), 0.0)
        self.assertAlmostEqual(float(values[10]), 2.0, places=6)   # 10 步 × 0.1 s × 2

    def test_smooth_reduces_ripple(self):
        session = _MathSession({"带毛刺": np.tile([0.0, 10.0], 10)}, rate=10.0)
        raw = session.values("带毛刺")
        smoothed = mathsmod.evaluate("smooth(带毛刺, 5)", session)
        self.assertLess(float(np.std(smoothed)), float(np.std(raw)))
        self.assertEqual(smoothed.size, raw.size)

    def test_low_pass_keeps_the_average_and_drops_the_ripple(self):
        rate = 100.0
        t = np.arange(501) / rate
        session = _MathSession({"带噪声": np.sin(2 * np.pi * t) + 0.2 * np.sin(2 * np.pi * 40 * t)},
                               rate=rate)
        filtered = mathsmod.evaluate("filter_lp(带噪声, 5)", session)
        self.assertLess(float(np.std(filtered)), float(np.std(session.values("带噪声"))))
        self.assertAlmostEqual(float(np.mean(filtered)), 0.0, places=1)

    def test_choose_flip_flop_and_invalid(self):
        session = _MathSession({"x": np.array([0.0, 1.0, 2.0, -1.0, 0.0])}, rate=1.0)
        chosen = mathsmod.evaluate("choose(x > 0, 10, -10)", session)
        self.assertAlmostEqual(float(chosen[1]), 10.0)
        self.assertAlmostEqual(float(chosen[3]), -10.0)
        state = mathsmod.evaluate("flip_flop(x > 0.5, x < 0)", session)
        self.assertAlmostEqual(float(state[1]), 1.0)
        self.assertAlmostEqual(float(state[3]), 0.0)
        invalid = mathsmod.evaluate("invalid()", session)
        self.assertTrue(np.all(np.isnan(invalid)))

    def test_time_range_helpers_gate_on_the_time_axis(self):
        session = _MathSession({"x": np.ones(11)}, rate=1.0)
        inside = mathsmod.evaluate("range_is(2, 4)", session)
        self.assertAlmostEqual(float(inside[0]), 0.0)
        self.assertAlmostEqual(float(inside[3]), 1.0)
        self.assertAlmostEqual(float(inside[5]), 0.0)
        edge = mathsmod.evaluate("range_change(2, 4)", session)
        self.assertEqual(int(np.sum(edge)), 1)
        self.assertAlmostEqual(float(edge[2]), 1.0)

    # ------------------------------------------------------- 作用域与缓存
    def test_local_scope_file_is_a_sibling_sidecar(self):
        self.assertEqual(mathsmod.config_path(Path("x") / "场次.ld").name, "场次.maths.json")
        self.assertEqual(mathsmod.global_path(Path("repo")).name, "global.json")
        self.assertEqual(mathsmod.global_path(Path("repo")).parent.name, "maths")

    def test_local_overrides_global_and_both_are_visible(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "场次.ld"
            glob = mathsmod.MathSet(definitions=[
                mathsmod.Definition("总G", "1", scope="global"),
                mathsmod.Definition("只有全局", "2", scope="global"),
            ])
            glob.save(mathsmod.global_path(root))
            mathsmod.MathSet(definitions=[
                mathsmod.Definition("总G", "3", scope="local"),
            ]).save(mathsmod.config_path(session))
            effective = mathsmod.load_effective(session, root)
            by_name = effective.by_name()
            self.assertEqual(by_name["总G"].expr, "3")
            self.assertEqual(by_name["总G"].scope, "local")
            self.assertEqual(by_name["只有全局"].scope, "global")
            self.assertEqual(effective.shadowed, ["总G"])
            self.assertEqual(effective.scope_of("总G"), "local")

    def test_one_broken_definition_does_not_take_the_others_down(self):
        session = self._session()
        values, errors = mathsmod.resolve_available(session, [
            mathsmod.Definition("好的", "1 + 1"),
            mathsmod.Definition("引用好的", "好的 * 2"),
            mathsmod.Definition("坏的", "'没有这个通道' + 1"),
            mathsmod.Definition("引用坏的", "坏的 + 1"),
        ])
        self.assertEqual(set(values), {"好的", "引用好的"})
        self.assertEqual({e["name"] for e in errors}, {"坏的", "引用坏的"})
        self.assertIn("没有这个通道", errors[0]["error"])

    def test_each_broken_definition_reports_its_own_missing_channel(self):
        """错因要各自算各自，不能把第一条的原因发给所有人。

        实测：一场 CAN 日志上，``maths/global.json`` 里五条定义全报"本场次没有
        ``G Force Lat``"——其中四条根本没引用它（它们缺的是 ``FSD13 Distance1``、
        ``G Force Long`` 之类）。那种报错会把人带去查一条不相干的通道。
        """
        session = self._session()
        _values, errors = mathsmod.resolve_available(session, [
            mathsmod.Definition("甲", "'缺一' + 1"),
            mathsmod.Definition("乙", "'缺二' + 1"),
            mathsmod.Definition("丙", "'缺三' + 1"),
        ])
        by_name = {row["name"]: row["error"] for row in errors}
        self.assertEqual(set(by_name), {"甲", "乙", "丙"})
        self.assertIn("缺一", by_name["甲"])
        self.assertIn("缺二", by_name["乙"])
        self.assertIn("缺三", by_name["丙"])
        for name, own in (("甲", "缺一"), ("乙", "缺二"), ("丙", "缺三")):
            for other in ("缺一", "缺二", "缺三"):
                if other != own:
                    self.assertNotIn(other, by_name[name])

    def test_a_cycle_keeps_its_real_reason_through_resolve_available(self):
        """互相引用时报的是"绕成一个圈"，不是"本场次没有这个通道"（那条会误导）。"""
        session = self._session()
        values, errors = mathsmod.resolve_available(session, [
            mathsmod.Definition("甲", "乙 + 1"),
            mathsmod.Definition("乙", "甲 + 1"),
        ])
        self.assertEqual(values, {})
        self.assertEqual({row["name"] for row in errors}, {"甲", "乙"})
        for row in errors:
            self.assertIn("圈", row["error"])

    def test_a_cycle_is_reported_instead_of_recursing(self):
        session = self._session()
        with self.assertRaises(mathsmod.MathError) as caught:
            mathsmod.resolve_all(session, [
                mathsmod.Definition("甲", "乙 + 1"),
                mathsmod.Definition("乙", "甲 + 1"),
            ])
        self.assertIn("绕成一个圈", str(caught.exception))

    def test_forward_references_resolve(self):
        session = self._session()
        out = mathsmod.resolve_all(session, [
            mathsmod.Definition("下游", "上游 * 2"),
            mathsmod.Definition("上游", "3"),
        ])
        self.assertAlmostEqual(float(out["下游"][0]), 6.0)

    def test_cache_key_changes_with_the_expression_and_the_source(self):
        first = mathsmod.DerivedCache.key("x.ld", mathsmod.Definition("A", "1"), ("车速",))
        second = mathsmod.DerivedCache.key("x.ld", mathsmod.Definition("A", "2"), ("车速",))
        third = mathsmod.DerivedCache.key("x.ld", mathsmod.Definition("A", "1"), ("车轮速度",))
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)

    def test_cache_reuses_the_column_until_the_definition_changes(self):
        import tempfile

        session = self._session()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "场次.ld"
            path.write_bytes(b"x")            # 指纹只要有这个文件就够
            cache = mathsmod.DerivedCache()
            definitions = [mathsmod.Definition("A", "'车速' * 2")]
            first = mathsmod.resolve_all(session, definitions, cache, path)["A"]
            second = mathsmod.resolve_all(session, definitions, cache, path)["A"]
            self.assertIs(first, second, "没改定义却又算了一遍")
            changed = mathsmod.resolve_all(
                session, [mathsmod.Definition("A", "'车速' * 3")], cache, path
            )["A"]
            self.assertIsNot(changed, first, "表达式改了却还在用旧列")
            self.assertAlmostEqual(float(changed[0]), float(first[0]) * 1.5, places=6)

    # ------------------------------------------------------ 下游完全等价
    def test_a_derived_column_looks_like_a_native_channel_downstream(self):
        session = self._session()
        definitions = [mathsmod.Definition("车速两倍", "车速 * 2", unit="km/h")]
        values, errors = mathsmod.resolve_available(session, definitions)
        self.assertEqual(errors, [])
        added = mathsmod.attach(session, values, definitions)
        self.assertEqual(added, ["车速两倍"])

        self.assertTrue(session.has("车速两倍"))
        channel = session.channel("车速两倍")
        self.assertEqual(channel.unit, "km/h")
        self.assertEqual(channel.sample_rate, session.sample_rate)
        self.assertEqual(channel.sample_count, values["车速两倍"].size)

        index = render.channel_index(session)
        entry = next(item for item in index if item["name"] == "车速两倍")
        self.assertTrue(entry["derived"])
        self.assertFalse(
            next(item for item in index if item["name"] == "车速")["derived"]
        )
        # 主时间基上的序列：图表、散点、切圈都按这个长度取
        self.assertEqual(
            derive.hold_to_master(session, "车速两倍").size,
            derive.hold_to_master(session, "车速").size,
        )

    def test_attaching_twice_does_not_duplicate_the_channel(self):
        session = self._session()
        definitions = [mathsmod.Definition("车速两倍", "车速 * 2")]
        before = len(session.channels)
        for _ in range(2):
            values, _errors = mathsmod.resolve_available(session, definitions)
            mathsmod.attach(session, values, definitions)
        self.assertEqual(len(session.channels), before + 1)

    def test_a_derived_channel_that_shadows_a_slow_channel_is_kept_as_is(self):
        """本地数学覆盖原生通道时最容易出的错：再按原生采样率拉一遍。

        `Lap Number` 这类原生通道常常只有 1 Hz，而派生列在 100 Hz 的主时间基上。
        如果下游仍按 1 Hz 对它做 repeat，取到的是开头一个常数值——曲线被整段毁掉，
        而且不会报任何错。
        """
        rate, count = 10.0, 6
        session = _MathSession({"计数器": np.zeros(count)}, rate=rate,
                               rates={"计数器": 1.0})       # 原生只有 1 Hz
        ramp = np.arange(count, dtype=np.float64)           # 主时间基上的斜坡
        mathsmod.attach(session, {"计数器": ramp},
                        [mathsmod.Definition("计数器", "0", unit="")])
        held = derive.hold_to_master(session, "计数器")
        self.assertEqual(held.size, count)
        np.testing.assert_allclose(held, ramp)
        entry = next(c for c in render.channel_index(session) if c["name"] == "计数器")
        self.assertEqual(entry["rate"], rate, "同名覆盖时界面还在报原生通道的采样率")
        self.assertTrue(entry["derived"])

    def test_removing_a_definition_does_not_leave_a_ghost_channel(self):
        session = self._session()
        definitions = [mathsmod.Definition("临时通道", "1")]
        values, _errors = mathsmod.resolve_available(session, definitions)
        mathsmod.attach(session, values, definitions)
        self.assertTrue(session.has("临时通道"))
        before = len(session.channels)
        # 定义被删掉 -> 下一次 apply 会用空集合再挂一次
        mathsmod.attach(session, {}, [])
        self.assertFalse(session.has("临时通道"), "删掉定义之后还留着幽灵通道")
        self.assertEqual(len(session.channels), before - 1)
        self.assertFalse(any(c["name"] == "临时通道" for c in render.channel_index(session)))

    def test_a_derived_channel_cannot_be_shadowed_by_a_stale_definition(self):
        """引用自己在定义阶段就被判成环，而不是算出一个越来越大的数列。"""
        session = self._session()
        with self.assertRaises(mathsmod.MathError):
            mathsmod.resolve_all(session, [mathsmod.Definition("车速", "车速 + 1")])

    def test_function_catalogue_covers_what_the_ticket_promised(self):
        names = {item["name"] for item in mathsmod.function_catalogue()}
        for expected in (
            "sin", "cos", "tan", "ln", "log", "exp", "sqr", "sqrt", "power",
            "int", "round", "round_down", "round_up", "frac", "sgn",
            "min", "max", "abs",
            "stat_min", "stat_max", "stat_mean", "stat_std_dev", "stat_start", "stat_end",
            "smooth", "filter_lp", "filter_hp",
            "derivative", "integrate", "choose", "invalid", "flip_flop",
            "time_shift", "time_valid", "range_is", "range_change",
            "bit_and", "bit_or", "bit_xor", "bit_not", "edge_delay", "hypot",
        ):
            self.assertIn(expected, names, f"函数表里缺 {expected}")
        self.assertEqual(len(names), 53)

    # -------------------------------------------- 通道名怎么打得出来（用户反馈）
    def test_a_channel_name_with_spaces_can_be_typed_without_quotes(self):
        """`Vx KF * 2` 以前报"两个运算数挨在一起"，用户根本猜不到要加单引号。"""
        session = _MathSession({"Vx KF": np.full(11, 30.0), "车速": np.full(11, 20.0)})
        known = mathsmod.known_names(session)
        bare = mathsmod.evaluate("Vx KF * 2", session)
        quoted = mathsmod.evaluate("'Vx KF' * 2", session)
        self.assertTrue(np.array_equal(bare, quoted))
        self.assertAlmostEqual(float(bare[0]), 60.0)
        self.assertEqual(mathsmod.compile_expr("Vx KF * 2", known=known).channels, ("Vx KF",))
        # 没给名字表时仍然要加引号——旧写法不能因为这条改动而失效
        self.assertEqual(mathsmod.compile_expr("'Vx KF' * 2").channels, ("Vx KF",))

    def test_a_channel_name_with_brackets_is_not_mistaken_for_a_function(self):
        """`Distance (2)` 以前被当成"调用函数 Distance"。"""
        session = _MathSession({"Distance (2)": np.full(11, 7.0)})
        values = mathsmod.evaluate("Distance (2) + 1", session)
        self.assertAlmostEqual(float(values[0]), 8.0)
        self.assertEqual(
            mathsmod.compile_expr("Distance (2) + 1",
                                  known=mathsmod.known_names(session)).channels,
            ("Distance (2)",),
        )
        # 真的不存在这个通道时，报的仍然是"未知函数"，不能乱猜
        with self.assertRaises(mathsmod.MathError) as caught:
            mathsmod.compile_expr("Distance (2) + 1", known=("别的通道",))
        self.assertIn("未知函数", str(caught.exception))

    def test_a_channel_name_with_a_hyphen_is_not_mistaken_for_a_subtraction(self):
        """`FSD-Distance1` 以前被当成 `FSD` 减 `Distance1`。"""
        session = _MathSession({"FSD-Distance1": np.full(11, 12.0)})
        values = mathsmod.evaluate("FSD-Distance1 * 2", session)
        self.assertAlmostEqual(float(values[0]), 24.0)

    def test_a_typo_gets_the_right_name_back(self):
        """名字真的是场次里没有的：报错要说清该换成哪一条。"""
        session = _MathSession({"FSD13 Distance1": np.full(11, 1.0),
                                "Aceinna Roll": np.full(11, 2.0)})
        with self.assertRaises(mathsmod.MathError) as caught:
            mathsmod.evaluate("FSD * 2", session)
        message = str(caught.exception)
        self.assertIn("本场次没有这个通道", message)
        self.assertIn("FSD13 Distance1", message)   # 以 `FSD` 开头的那几条
        self.assertIn("插入通道", message)
        # 完全不像的名字不要硬凑一个"最接近的"出来
        with self.assertRaises(mathsmod.MathError) as caught:
            mathsmod.evaluate("notachannel * 2", session)
        self.assertNotIn("最接近的是", str(caught.exception))

    def test_a_name_typed_without_its_space_is_still_that_channel(self):
        """用户反馈的原话：`FSD-Distance1`/`FSD13Distance1` 对不上 `FSD13 Distance1`。

        少一个空格、多一个短横线、大小写不同，都只该算"同一个名字的另一种写法"。
        下标要按**文本位置**往前走：`FSD13Distance1*2` 这种连空格都不留的写法，
        少走一格就会把 `*` 吃掉，然后报一个完全无关的语法错。
        """
        session = _MathSession({"FSD13 Distance1": np.linspace(0, 10, 11),
                                "FSD13 Distance2": np.full(11, 2.0)})
        known = mathsmod.known_names(session)
        raw = np.asarray(session.columns["FSD13 Distance1"])
        for text in ("FSD13Distance1 * 2", "FSD13Distance1*2", "fsd13distance1 * 2",
                     "FSD13-Distance1 * 2", "FSD13_Distance1 * 2"):
            with self.subTest(text=text):
                self.assertTrue(
                    np.allclose(mathsmod.evaluate(text, session), raw * 2), f"{text} 算错了"
                )
                self.assertEqual(
                    mathsmod.compile_expr(text, known=known).channels,
                    ("FSD13 Distance1",),
                )
        # 函数参数里、后面还跟着别的通道时也要按文本位置往前走
        self.assertTrue(np.allclose(
            mathsmod.evaluate("max(FSD13Distance1, 0) + FSD13Distance2", session),
            raw + 2.0,
        ))
        # 数字对不上就还是别的名字，不许往前凑
        self.assertNotIn("FSD13 Distance12", mathsmod.known_names(session))
        with self.assertRaises(mathsmod.MathError):
            mathsmod.evaluate("FSD13 Distance12 * 2", session)

    def test_case_and_separators_are_interchangeable(self):
        """`vx kf` / `VXKF` / `Vx_KF` 都是同一条 `Vx KF`。"""
        session = _MathSession({"Vx KF": np.full(11, 30.0)})
        known = mathsmod.known_names(session)
        for text in ("Vx KF * 2", "vx kf * 2", "VXKF*2", "Vx_KF * 2", "'vx kf' * 2"):
            with self.subTest(text=text):
                self.assertAlmostEqual(float(mathsmod.evaluate(text, session)[0]), 60.0)
                self.assertEqual(
                    mathsmod.compile_expr(text, known=known).channels, ("Vx KF",)
                )

    def test_exact_spelling_wins_over_a_lookalike(self):
        """两条通道只差一个分隔符时，写对哪条就是哪条。"""
        session = _MathSession({"Vx KF": np.full(11, 1.0), "Vx-KF": np.full(11, 9.0)})
        known = mathsmod.known_names(session)
        self.assertEqual(mathsmod.compile_expr("Vx-KF * 2", known=known).channels, ("Vx-KF",))
        self.assertEqual(mathsmod.compile_expr("Vx KF * 2", known=known).channels, ("Vx KF",))
        # 写法两种都对得上、又不是逐字相同：不猜，报错让用户写全
        with self.assertRaises(mathsmod.MathError) as caught:
            mathsmod.compile_expr("vxkf * 2", known=known)
        message = str(caught.exception)
        self.assertIn("Vx KF", message)
        self.assertIn("Vx-KF", message)

    def test_a_channel_called_with_brackets_can_skip_the_space(self):
        """`Distance(2) * 2` 也要认成通道 `Distance (2)`，不能报"未知函数"。"""
        session = _MathSession({"Distance (2)": np.full(11, 7.0)})
        known = mathsmod.known_names(session)
        self.assertAlmostEqual(float(mathsmod.evaluate("Distance(2) * 2", session)[0]), 14.0)
        self.assertEqual(
            mathsmod.compile_expr("Distance(2) * 2", known=known).channels, ("Distance (2)",)
        )

    def test_strict_channel_check_happens_before_anything_is_computed(self):
        """试算／保存想让编译器先说话时，缺的通道在这里就报出来。"""
        known = ("Vx KF",)
        # 不给名字表 / 不打开检查：语法过得去就先编译（旧行为不能变）
        self.assertEqual(mathsmod.compile_expr("VxKF * 2", known=known).channels, ("Vx KF",))
        self.assertEqual(mathsmod.compile_expr("Encoder9 * 2").channels, ("Encoder9",))
        with self.assertRaises(mathsmod.MathError) as caught:
            mathsmod.compile_expr("Encoder9 * 2", known=known, strict_channels=True)
        self.assertIn("本场次没有这个通道", str(caught.exception))

    def test_definition_names_are_known_too(self):
        """一条定义引用另一条时，名字同样可以不写引号。"""
        session = _MathSession({"Vx KF": np.full(11, 3.0)})
        definitions = [
            mathsmod.Definition("我的 通道", "Vx KF * 2"),
            mathsmod.Definition("下一个", "我的 通道 + 1"),
        ]
        got = mathsmod.resolve_all(session, definitions)
        self.assertAlmostEqual(float(got["下一个"][0]), 7.0)

    def test_a_changed_dependency_invalidates_the_cache(self):
        """`乙 = 甲 + 1`：甲改了，乙必须跟着重算（缓存键不能只放名字）。

        回归用例——键里原来只有被引用通道的**名字**，所以甲换了表达式之后，
        乙会一直命中旧列，偏差可以很大。
        """
        session = _MathSession({"车速": np.full(11, 3.0)})
        cache = mathsmod.DerivedCache()
        first = mathsmod.resolve_all(
            session, [mathsmod.Definition("甲", "车速 * 2"),
                      mathsmod.Definition("乙", "甲 + 1")], cache=cache)
        second = mathsmod.resolve_all(
            session, [mathsmod.Definition("甲", "车速 * 4"),
                      mathsmod.Definition("乙", "甲 + 1")], cache=cache)
        self.assertAlmostEqual(float(first["乙"][0]), 7.0)
        self.assertAlmostEqual(float(second["乙"][0]), 13.0,
                               msg="甲 改了之后 乙 仍然命中旧列")
        # 没变的定义还是要能命中（缓存不能退化成"永远重算"）
        again = mathsmod.resolve_all(
            session, [mathsmod.Definition("甲", "车速 * 4"),
                      mathsmod.Definition("乙", "甲 + 1")], cache=cache)
        self.assertIs(again["乙"], second["乙"])


class TestChannelSeam(unittest.TestCase):
    """ticket #18：数学通道与原生通道的差别只写在 ``channels.py`` 一处。

    这些断言故意写得"像删除测试"：不是测某个函数算得对，而是测**别的模块没有
    再判一遍"这是不是数学通道"**。以前这条规则在五个地方各写了一遍，本项目因此
    出过两次真错（慢通道被同名派生列盖住后采样率取错、频谱按原生采样率切窗口）。
    """

    SRC = ROOT / "src" / "i3pro"

    #: 只允许出现在 channels.py 里的写法（别处出现就说明这条规则又被抄了一份）。
    FORBIDDEN = (
        "is_derived_channel",
        'hasattr(session, "derived',
        "if derived else",
        "if is_derived else",
    )

    def _shadow_session(self):
        """数学通道**盖住一条慢的原生通道**：最容易把下游算错的那种场次。"""
        rate, count = 10.0, 6
        session = _MathSession({"计数器": np.zeros(count)}, rate=rate,
                               rates={"计数器": 1.0})       # 原生只有 1 Hz
        ramp = np.arange(count, dtype=np.float64)            # 主时间基上的斜坡
        mathsmod.attach(session, {"计数器": ramp},
                        [mathsmod.Definition("计数器", "0", unit="圈")])
        return session, ramp, rate

    @staticmethod
    def _hold_reference(log, channel) -> np.ndarray:
        """旧实现逐字抄一份，当"逐点一致"的判据用（ticket #18 之前那一版）。"""
        values = log.values(channel)
        factor = max(1, int(round(log.sample_rate / channel.sample_rate)))
        if factor > 1:
            values = np.repeat(values, factor)
        n = timebase.length(log)
        if values.size < n:
            pad = values[-1] if values.size else 0.0
            values = np.concatenate([values, np.full(n - values.size, pad)])
        return values[:n]

    # ------------------------------------------------------------ 这条缝本身
    def test_the_rule_lives_in_exactly_one_module(self):
        offenders = []
        for path in sorted(self.SRC.glob("*.py")):
            if path.name == "channels.py":
                continue
            text = path.read_text(encoding="utf-8")
            offenders += [
                f"{path.name}: {pattern}"
                for pattern in self.FORBIDDEN
                if pattern in text
            ]
        self.assertEqual(
            offenders, [],
            "这些地方又自己判了一遍「是不是数学通道」——该改成调用 channels.py",
        )

    def test_a_session_must_declare_where_derived_columns_go(self):
        """不给声明就报错并说下一步，而不是悄悄少一支分支。"""

        class Rude:
            sample_rate = 10.0
            channels: list = []

        with self.assertRaises(TypeError) as ctx:
            channels.slot(Rude())
        self.assertIn("derived_target", str(ctx.exception))
        self.assertIn("在会话类上补一个同名属性", str(ctx.exception))

        with self.assertRaises(TypeError) as ctx:
            channels.names(Rude())
        self.assertIn("derived_names", str(ctx.exception))

    def test_both_real_session_types_declare_the_same_three_things(self):
        for session in (ld.LogFile, csvlog.CsvSession):
            declared = {item.name for item in dataclasses.fields(session)}
            for attribute in ("derived_names", "derived_units"):
                self.assertIn(
                    attribute, declared,
                    f"{session.__name__} 没有声明 {attribute}——下游就得靠 hasattr 猜了",
                )
            self.assertIsInstance(
                getattr(session, "derived_target"), property,
                f"{session.__name__} 没有声明 derived_target",
            )

    # ------------------------------------------------------------ 元数据一条路
    def test_native_channels_keep_their_own_rate_and_unit(self):
        session = _MathSession({"慢": np.zeros(6), "快": np.zeros(6)},
                               rate=10.0, rates={"慢": 2.0})
        slow, fast = session.channel("慢"), session.channel("快")
        self.assertFalse(channels.is_derived(session, slow))
        self.assertEqual(channels.sample_rate(session, slow), 2.0)
        self.assertEqual(channels.hold_factor(session, slow, 10.0), 5)
        self.assertEqual(channels.hold_factor(session, fast, 10.0), 1)
        # 目标时间基比通道还慢时不去抽稀：保持用的因子最少是 1
        self.assertEqual(channels.hold_factor(session, fast, 1.0), 1)

    def test_a_derived_channel_is_held_once_even_when_it_shadows_a_slow_channel(self):
        session, ramp, rate = self._shadow_session()
        channel = session.channel("计数器")
        self.assertTrue(channels.is_derived(session, channel))
        self.assertEqual(channels.sample_rate(session, channel), rate)
        self.assertEqual(channels.hold_factor(session, channel, rate), 1)
        self.assertEqual(channels.unit(session, channel), "圈",
                         "同名覆盖时单位也要用定义里的，不是文件里那条原生通道的")
        self.assertEqual(
            render.channel_index(session),
            [{"name": "计数器", "unit": "圈", "rate": rate,
              "samples": ramp.size, "derived": True,
              #: #34 加的：界面上要能分开"本场没有这条"和"有但整段是空的"。
              "has_data": True}],
        )

    def test_a_derived_column_reaches_parquet_as_itself(self):
        """Parquet 也走同一条缝：盖住慢原生通道时不能写出重复的常数值。"""
        session, ramp, _rate = self._shadow_session()
        session.device = "C125"
        session.log_date = session.log_time = session.event_name = ""
        table, meta = store.build_table(session, channels=["计数器"])
        np.testing.assert_array_equal(table.column("计数器").to_numpy(), ramp)
        self.assertEqual(meta["channels"][0]["name"], "计数器")

    # --------------------------------------------------- 挂载 / 卸载不走嗅探
    def test_attach_and_detach_go_through_the_declaration(self):
        session = _MathSession({"车速": np.full(6, 2.0)}, rate=10.0)
        definitions = [mathsmod.Definition("两倍", "车速 * 2", unit="km/h")]
        values, errors = mathsmod.resolve_available(session, definitions)
        self.assertEqual(errors, [])
        self.assertEqual(mathsmod.attach(session, values, definitions), ["两倍"])
        self.assertEqual(channels.names(session), {"两倍"})
        self.assertEqual(channels.units(session), {"两倍": "km/h"})
        np.testing.assert_array_equal(channels.slot(session)["两倍"], values["两倍"])
        mathsmod.detach(session)
        self.assertEqual(channels.names(session), set())
        self.assertEqual(channels.units(session), {})
        self.assertEqual(channels.slot(session), {})
        self.assertFalse(session.has("两倍"))

    # ------------------------------------------------- 没挂数学通道时逐点一致
    def _assert_session_is_unchanged(self, path: Path):
        """一场真实数据：没有数学通道时，元数据与「保持到主时间基」与旧实现逐点一致。"""
        with ld.LogFile.read(path) as log:
            self.assertEqual(channels.names(log), set())
            index = render.channel_index(log)
            self.assertEqual(len(index), len(log.channels))
            for position, channel in enumerate(log.channels):
                self.assertEqual(
                    channels.hold_factor(log, channel, log.sample_rate),
                    max(1, int(round(log.sample_rate / channel.sample_rate))),
                    f"{channel.name}: 保持因子与旧公式不一致",
                )
                self.assertEqual(
                    index[position],
                    {"name": channel.name, "unit": channel.unit,
                     "rate": channel.sample_rate, "samples": channel.sample_count,
                     #: `has_data` 是 ticket #34 新加的字段（同一份通道索引，
                     #: 界面上"缺一条"与"这条是空的"要靠它分开）；除它之外
                     #: 每一项都必须与旧实现逐字段相同。
                     "has_data": True, "derived": False},
                    f"{channel.name}: 通道索引与旧实现不一致",
                )
            slowest = min(log.channels, key=lambda ch: ch.sample_rate)
            for channel in (slowest, log.channels[0], log.channels[5]):
                np.testing.assert_array_equal(
                    derive.hold_to_master(log, channel.name),
                    self._hold_reference(log, channel),
                    err_msg=f"{channel.name}: 保持到主时间基的结果变了",
                )

    @_needs(HILL)
    def test_the_hill_session_without_maths_channels_is_unchanged_point_by_point(self):
        self._assert_session_is_unchanged(HILL)

    @_needs(ENDURANCE)
    def test_the_endurance_session_without_maths_channels_is_unchanged(self):
        self._assert_session_is_unchanged(ENDURANCE)

    @_needs(DATA / "20260522-yjw第二次直线3.72.csv")
    def test_a_csv_session_uses_the_same_seam(self):
        """CSV 会话的列和原生列同住 ``columns``：挂上、取元数据、撤下都要走同一条缝。"""
        with csvlog.read_csv_session(DATA / "20260522-yjw第二次直线3.72.csv") as session:
            definitions = [mathsmod.Definition("车速两倍", "'GPS Speed' * 2", unit="km/h")]
            values, errors = mathsmod.resolve_available(session, definitions)
            self.assertEqual(errors, [])
            self.assertEqual(mathsmod.attach(session, values, definitions), ["车速两倍"])
            self.assertIn("车速两倍", session.columns)
            self.assertEqual(channels.names(session), {"车速两倍"})
            self.assertEqual(channels.units(session), {"车速两倍": "km/h"})
            channel = session.channel("车速两倍")
            self.assertTrue(channels.is_derived(session, channel))
            self.assertEqual(channels.sample_rate(session, channel), session.sample_rate)
            self.assertEqual(channels.hold_factor(session, channel, session.sample_rate), 1)
            mathsmod.detach(session)
            self.assertNotIn("车速两倍", session.columns)
            self.assertEqual(channels.names(session), set())


class TestSections(unittest.TestCase):
    """#7 赛道区段：切分本身是不依赖框架的纯函数，先用合成数据钉死它。"""

    def _lap(self, length=400.0, corners=((100.0, 160.0), (260.0, 330.0)), step=1.0):
        """一条假圈：指定距离区间里给一个"弯"的测度，其余是直道。"""
        count = int(length / step) + 1
        distance = np.arange(count, dtype=float) * step
        measure = np.full(count, 0.1)
        for start, end in corners:
            measure[(distance >= start) & (distance <= end)] = 1.6
        return distance, measure

    def test_auto_split_tiles_the_lap_without_gaps_or_overlaps(self):
        distance, measure = self._lap()
        config = sectionsmod.auto_config(distance, measure, "lateral_g", 1.0, 20.0)
        self.assertGreaterEqual(len(config.boundaries), 3)
        self.assertEqual(config.boundaries[0], 0.0)
        self.assertEqual(config.boundaries[-1], 400.0)
        self.assertEqual(len(config.kinds), len(config.boundaries) - 1)
        self.assertEqual(len(config.names), len(config.kinds))
        self.assertGreater(config.boundaries[0], -1.0)
        for left, right in zip(config.boundaries, config.boundaries[1:]):
            self.assertGreater(right, left, "边界必须严格递增（否则就是重叠）")
        rows = config.spans
        self.assertAlmostEqual(sum(row["length_m"] for row in rows), 400.0, places=1)
        # 每两条相邻区段的种类必须不同（"弯/直"交替，否则说明合并没做完）
        for before, after in zip(config.kinds, config.kinds[1:]):
            self.assertNotEqual(before, after)
        self.assertFalse(config.edited, "自动切出来的不是'手工改过'")
        self.assertEqual(config.reference_label, "")

    def test_the_two_corners_land_where_they_were_put(self):
        distance, measure = self._lap()
        config = sectionsmod.auto_config(distance, measure, "lateral_g", 1.0, 20.0)
        corners = [row for row in config.spans if row["kind"] == "corner"]
        self.assertEqual(len(corners), 2, corners)
        for row, expected in zip(corners, ((100.0, 160.0), (260.0, 330.0))):
            self.assertLess(abs(row["start_distance"] - expected[0]), 8.0)
            self.assertLess(abs(row["end_distance"] - expected[1]), 8.0)

    def test_sensitivity_only_moves_the_corner_mileage_up(self):
        distance, measure = self._lap(corners=((60.0, 140.0), (250.0, 300.0)))
        mileage = []
        for sensitivity in (0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0):
            config = sectionsmod.auto_config(distance, measure, "lateral_g", sensitivity, 20.0)
            mileage.append(
                sum(row["length_m"] for row in config.spans if row["kind"] == "corner")
            )
        for before, after in zip(mileage, mileage[1:]):
            self.assertGreaterEqual(after, before, f"灵敏度调大反而少判了弯：{mileage}")
        self.assertGreater(mileage[-1], mileage[0], f"灵敏度完全不起作用：{mileage}")

    def test_a_flat_measure_means_one_straight_not_invented_corners(self):
        """测度整场一个值（坏通道就长这样）时不许硬切出一堆假弯。"""
        distance = np.arange(0, 500, 1.0)
        measure = np.full(distance.size, 0.42)
        config = sectionsmod.auto_config(distance, measure, "lateral_g", 1.0, 25.0)
        self.assertEqual(len(config.spans), 1)
        self.assertEqual(config.kinds, ("straight",))
        self.assertEqual(config.names, ("直 1",))
        self.assertEqual(config.boundaries, (0.0, 499.0))

    def test_min_length_decides_whether_a_spike_is_a_corner(self):
        # 阈值是分位数算的：60 m 的弯（占一圈 15%）稳在 90 分位之上，
        # 4 m 的尖峰连 90 分位都够不到——那种"弯"本来就该被平滑掉。
        distance, measure = self._lap(corners=((200.0, 260.0),))
        loose = sectionsmod.auto_config(distance, measure, "lateral_g", 1.0, 80.0)
        tight = sectionsmod.auto_config(distance, measure, "lateral_g", 1.0, 2.0)
        self.assertEqual([row["kind"] for row in loose.spans], ["straight"])
        self.assertIn("corner", [row["kind"] for row in tight.spans])

    def test_absurd_parameters_say_what_to_change(self):
        distance, measure = self._lap(length=200.0)
        with self.assertRaises(ValueError) as caught:
            sectionsmod.auto_config(distance, measure, "lateral_g", 1.0, 150.0)
        self.assertIn("最短段长", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            sectionsmod.auto_config(distance, measure, "lateral_g", 0.0, 20.0)
        self.assertIn("灵敏度", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            sectionsmod.auto_config(distance, measure, "nosuch", 1.0, 20.0)
        self.assertIn("判据", str(caught.exception))

    def test_manual_edits_get_sorted_clamped_and_still_cover_the_lap(self):
        distance, measure = self._lap()
        config = sectionsmod.auto_config(distance, measure, "lateral_g", 1.0, 20.0)
        messy = replace(
            config,
            boundaries=(80.0, -30.0, 500.0, 80.4, 200.0),
            kinds=("corner",),
            names=("T1",),
        )
        fixed, notice = sectionsmod.normalize(messy, 400.0)
        self.assertEqual(fixed.boundaries[0], 0.0)
        self.assertEqual(fixed.boundaries[-1], 400.0)
        self.assertEqual(list(fixed.boundaries), sorted(fixed.boundaries))
        self.assertEqual(len(fixed.kinds), len(fixed.boundaries) - 1)
        self.assertEqual(len(fixed.names), len(fixed.kinds))
        self.assertTrue(fixed.edited, "手工整理过的必须立起 edited，否则重切会覆盖")
        self.assertEqual(fixed.names[0], "T1")
        self.assertTrue(all(name for name in fixed.names), "名字不许留空")
        self.assertIsNotNone(notice)

    def test_a_partial_boundary_list_is_completed_not_left_with_holes(self):
        """手工编辑只给一条边界时，把它补成"覆盖整圈"，而不是留一段没人管的赛道。"""
        distance, measure = self._lap()
        config = sectionsmod.auto_config(distance, measure, "lateral_g", 1.0, 20.0)
        fixed, notice = sectionsmod.normalize(replace(config, boundaries=(120.0,)), 400.0)
        self.assertEqual(fixed.boundaries[0], 0.0)
        self.assertEqual(fixed.boundaries[-1], 400.0)
        self.assertEqual(len(fixed.kinds), len(fixed.boundaries) - 1)
        self.assertIn("整理", notice or "")

    def test_same_layout_tells_a_real_edit_from_a_no_op(self):
        distance, measure = self._lap()
        config = sectionsmod.auto_config(distance, measure, "lateral_g", 1.0, 20.0)
        self.assertTrue(sectionsmod.same_layout(config, replace(config, edited=True)))
        moved = replace(config, boundaries=(0.0, *config.boundaries[1:-1], config.boundaries[-1]))
        self.assertTrue(sectionsmod.same_layout(config, moved))
        renamed = replace(config, names=("别的名字", *config.names[1:]))
        self.assertFalse(sectionsmod.same_layout(config, renamed))

    def test_duplicate_names_are_shifted_apart(self):
        distance, measure = self._lap()
        config = sectionsmod.auto_config(distance, measure, "lateral_g", 1.0, 20.0)
        same = replace(config, names=tuple(["弯"] * len(config.kinds)))
        fixed = sectionsmod.dedupe_names(same)
        self.assertEqual(len(set(fixed.names)), len(fixed.names))
        self.assertEqual(fixed.names[0], "弯")
        self.assertIn("弯 2", fixed.names)

    def test_the_sidecar_round_trips_and_refuses_nonsense(self):
        import tempfile

        distance, measure = self._lap()
        config = sectionsmod.auto_config(distance, measure, "curvature", 1.4, 30.0)
        config = replace(config, reference_label="3", length_m=400.0, edited=True)
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "场次.ld"
            path = sectionsmod.save_config(session, config)
            self.assertEqual(path.name, "场次.sections.json")
            back = sectionsmod.load_config(session)
            self.assertEqual(back, config)
            self.assertTrue(back.edited)
            path.write_text(json.dumps({"basis": "猜的", "boundaries": [0, 1]}), "utf-8")
            with self.assertRaises(ValueError) as caught:
                sectionsmod.load_config(session)
            message = str(caught.exception)
            self.assertIn("basis", message)
            self.assertIn("删掉这个文件", message)

    # ------------------------------------------------- 落在哪一段（ticket #8）

    def test_which_section_a_time_falls_in(self):
        """双击带子问的就是这一句：这一点属于哪一段？空档不算，边界归下一段。"""
        marks = [
            {"label": "1", "times": [10.0, 20.0, 30.0]},
            {"label": "2", "times": [40.0, 50.0, 60.0]},
        ]
        self.assertEqual(sectionsmod.band_at_time(marks, 10.0)["index"], 0)
        self.assertEqual(sectionsmod.band_at_time(marks, 19.999)["index"], 0)
        # 边界那一瞬间归**后**一段，和"这一段从哪儿开始"是同一件事
        self.assertEqual(sectionsmod.band_at_time(marks, 20.0)["index"], 1)
        self.assertEqual(sectionsmod.band_at_time(marks, 29.9)["index"], 1)
        # 第 1 圈的最后一段到 30.0 为止，但 30.0 已经不属于任何带子（后面还有第 2 圈）
        self.assertIsNone(sectionsmod.band_at_time(marks, 30.0))
        self.assertIsNone(sectionsmod.band_at_time(marks, 35.0))      # 圈与圈之间的空档
        hit = sectionsmod.band_at_time(marks, 40.0)
        self.assertEqual((hit["lap"], hit["index"]), ("2", 0))
        self.assertAlmostEqual(hit["duration"], 10.0)
        # 最后一条圈的最后一段**含终点**，否则双击终点线没有任何反应
        self.assertEqual(sectionsmod.band_at_time(marks, 60.0)["index"], 1)
        self.assertIsNone(sectionsmod.band_at_time(marks, 60.1))
        # 坏输入不猜：空表 / 没有时刻 / 非有限数 / 只有起点没有终点的圈
        self.assertIsNone(sectionsmod.band_at_time([], 5.0))
        self.assertIsNone(sectionsmod.band_at_time(marks, None))
        self.assertIsNone(sectionsmod.band_at_time(marks, float("nan")))
        self.assertIsNone(sectionsmod.band_at_time([{"label": "x", "times": [1.0]}], 1.0))

    def test_the_window_of_one_section_row(self):
        rows = [{"start_time": 3.0, "end_time": 7.5}, {"start_time": 7.5, "end_time": 7.5}]
        self.assertEqual(sectionsmod.band_window(rows, 0), (3.0, 7.5))
        # 零宽度的一段不给出窗口：界面要说"这一段没有能用的时间范围"，
        # 而不是把视图缩成一个点（缩了也看不出来）
        self.assertIsNone(sectionsmod.band_window(rows, 1))
        self.assertIsNone(sectionsmod.band_window(rows, 2))
        self.assertIsNone(sectionsmod.band_window(rows, -1))
        self.assertIsNone(sectionsmod.band_window([], 0))

    # ------------------------------------------------------------ 真数据
    @_needs(HILL)
    def test_the_golden_hill_lap_splits_into_corners_and_straights(self):
        with ld.LogFile.read(HILL) as log:
            laps = render.detect(log)
            lap = sectionsmod.reference_lap(laps)
            self.assertEqual(lap.label, "5", "参考圈应该是最快的完整圈")
            config = sectionsmod.auto_for_log(log, lap, "curvature", 1.0)
            summary = sectionsmod.summarize(log, lap, config)
            self.assertEqual((summary["corners"], summary["straights"]), (3, 4))
            self.assertAlmostEqual(summary["corner_m"], 408.0, delta=1.0)
            self.assertAlmostEqual(summary["straight_m"], 404.0, delta=1.0)
            self.assertAlmostEqual(
                summary["corner_m"] + summary["straight_m"], summary["lap_length_m"], delta=0.5
            )
            # 灵敏度越大，判成弯的里程越多（实测 8 条曲线全单调不降）
            mileage = [
                sectionsmod.summarize(
                    log, lap, sectionsmod.auto_for_log(log, lap, "curvature", value)
                )["corner_m"]
                for value in (0.5, 1.0, 1.5, 2.0, 3.0)
            ]
            self.assertEqual(mileage, sorted(mileage), f"灵敏度不单调：{mileage}")

    @_needs(ENDURANCE)
    def test_the_golden_endurance_lap_and_its_per_lap_marks(self):
        with ld.LogFile.read(ENDURANCE) as log:
            laps = render.detect(log)
            lap = sectionsmod.reference_lap(laps)
            config = sectionsmod.auto_for_log(log, lap, "curvature", 1.0)
            summary = sectionsmod.summarize(log, lap, config)
            self.assertEqual((summary["corners"], summary["straights"]), (6, 7))
            self.assertAlmostEqual(summary["corner_m"], 376.0, delta=2.0)
            marks = sectionsmod.lap_marks(log, laps, config)
            self.assertEqual(len(marks), len(laps))
            for row, source in zip(marks, laps):
                self.assertLess(abs(row["times"][0] - source.start_time), 0.05)
                self.assertLess(abs(row["times"][-1] - source.end_time), 0.05)
                self.assertEqual(list(row["times"]), sorted(row["times"]))
            # 每条圈的速度不一样：边界的**绝对时刻**不能拿参考圈平移出来
            first, last = marks[0]["times"], marks[-1]["times"]
            start0, start1 = laps[0].start_time, laps[-1].start_time
            self.assertNotAlmostEqual(first[1] - start0, last[1] - start1, places=2)
            self.assertNotEqual(laps[0].lap_time, laps[-1].lap_time)

    @_needs(HILL)
    def test_bands_land_inside_the_lap_they_are_asked_about(self):
        with ld.LogFile.read(HILL) as log:
            laps = render.detect(log)
            lap = laps[1]
            config = sectionsmod.auto_for_log(log, sectionsmod.reference_lap(laps), "lateral_g", 1.0)
            rows = sectionsmod.bands(log, lap, config)
            self.assertTrue(rows)
            self.assertAlmostEqual(rows[0]["start_time"], lap.start_time, delta=0.05)
            self.assertAlmostEqual(rows[-1]["end_time"], lap.end_time, delta=0.05)
            for before, after in zip(rows, rows[1:]):
                self.assertAlmostEqual(before["end_time"], after["start_time"], delta=0.05)
                self.assertAlmostEqual(before["end_distance"], after["start_distance"], delta=0.05)

    @_needs(HILL)
    def test_a_stored_split_on_another_lap_says_so(self):
        """换参考圈之后边界不在原来的距离上了：要说出来，不许悄悄按新圈用。"""
        import shutil
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / HILL.name
            shutil.copyfile(HILL, session)
            with ld.LogFile.read(session) as log:
                laps = render.detect(log)
                lap = sectionsmod.reference_lap(laps)
                config = sectionsmod.auto_for_log(log, lap, "curvature", 1.0)
                sectionsmod.save_config(session, replace(config, reference_label="1",
                                                         edited=True))
                stored, notice = sectionsmod.effective_config(log, laps)
            self.assertTrue(stored.edited)
            self.assertIn("第 1 圈", notice or "")
            self.assertIn("第 5 圈", notice or "")
            self.assertIn("重切", notice or "")

    @_needs(HILL)
    def test_double_clicking_every_band_finds_that_same_band(self):
        """#8 的真数据钉子：每条圈、每一段，拿它的起点去问，要问回它自己。"""
        with ld.LogFile.read(HILL) as log:
            laps = render.detect(log)
            config = sectionsmod.auto_for_log(log, sectionsmod.reference_lap(laps), "curvature", 1.0)
            marks = sectionsmod.lap_marks(log, laps, config)
            checked = 0
            for mark in marks:
                times = mark["times"]
                for index in range(len(times) - 1):
                    hit = sectionsmod.band_at_time(marks, times[index])
                    self.assertIsNotNone(hit, f"第 {mark['label']} 圈第 {index} 段落在空档里")
                    self.assertEqual(hit["lap"], mark["label"])
                    self.assertEqual(hit["index"], index)
                    checked += 1
            self.assertGreater(checked, 10)
            # 圈与圈之间的空档确实不属于任何一段（两条圈首尾相连时没有空档，跳过）
            tail, head = marks[0]["times"][-1], marks[1]["times"][0]
            if head > tail:
                self.assertIsNone(sectionsmod.band_at_time(marks, (tail + head) / 2))

    @_needs(HILL)
    def test_the_curvature_basis_is_not_the_dead_Curvature_channel(self):
        """场次里那条叫 `Curvature` 的通道整场是 0，不能被当成判据。"""
        with ld.LogFile.read(HILL) as log:
            raw = np.asarray(derive.hold_to_master(log, "Curvature"), dtype=float)
            self.assertEqual(float(np.nanmax(np.abs(raw))), 0.0)
            measure = sectionsmod.measure_series(log, "curvature")
            self.assertGreater(float(np.nanpercentile(measure, 90)), 0.01)
            self.assertEqual(sectionsmod.measure_unit("curvature"), "1/m")


class TestSectionsOverHttp(unittest.TestCase):
    """#7 走到界面之前的那一段：GET/PUT + 侧车 + "不许悄悄覆盖手工改动"。"""

    @_needs(HILL)
    def test_sections_are_served_saved_and_protected(self):
        with http_session(HILL, buckets=200) as http:
            root, copy, quoted = http.root, http.copy, http.quoted
            library = http.library
            before = http.before
            request = http.json

            try:
                # 没存过侧车：GET 也要给出"按缺省参数切好的一份"（不落盘）
                status, state = request(f"/api/session/{quoted}/sections")
                self.assertEqual(status, 200, state)
                self.assertEqual(state["lap"], "5")
                self.assertTrue(state["bands"])
                self.assertTrue(state["laps"])
                self.assertEqual(state["available"], ["curvature", "lateral_g"])
                self.assertFalse((root / f"{copy.stem}.sections.json").exists(),
                                 "看一眼区段不该写盘")
                first_boundaries = state["config"]["boundaries"]

                # 重切：按横向加速度、灵敏度 1.5
                status, state = request(
                    f"/api/session/{quoted}/sections", "PUT",
                    {"auto": True, "basis": "lateral_g", "sensitivity": 1.5},
                )
                self.assertEqual(status, 200, state)
                self.assertEqual(state["saved"], f"{copy.stem}.sections.json")
                self.assertEqual(state["config"]["basis"], "lateral_g")
                self.assertAlmostEqual(state["config"]["sensitivity"], 1.5, places=6)
                self.assertFalse(state["config"]["edited"])
                sidecar = json.loads((root / f"{copy.stem}.sections.json").read_text("utf-8"))
                self.assertEqual(sidecar["basis"], "lateral_g")
                self.assertEqual(copy.read_bytes(), before, ".ld 被写过了")
                self.assertNotEqual(sidecar["boundaries"], first_boundaries)

                # 手工改名字 + 挪一条边界：进侧车，edited 立起来
                names = list(state["config"]["names"])
                names[1] = "T1 入弯"
                boundaries = list(state["config"]["boundaries"])
                boundaries[1] = boundaries[1] + 7.0
                status, state = request(
                    f"/api/session/{quoted}/sections", "PUT",
                    {"boundaries": boundaries, "names": names,
                     "kinds": state["config"]["kinds"]},
                )
                self.assertEqual(status, 200, state)
                self.assertTrue(state["config"]["edited"])
                self.assertEqual(state["config"]["names"][1], "T1 入弯")
                self.assertAlmostEqual(state["config"]["boundaries"][1], boundaries[1], places=1)
                self.assertEqual(state["bands"][1]["name"], "T1 入弯")
                self.assertIn("整理", state["notice"] or "")

                # 自动重切不许悄悄覆盖：先 400（带 needs_force），再来一次带 force 才动
                status, body = request(
                    f"/api/session/{quoted}/sections", "PUT",
                    {"auto": True, "basis": "curvature", "sensitivity": 1.0},
                )
                self.assertEqual(status, 400, body)
                self.assertTrue(body["needs_force"])
                self.assertIn("手工改过", body["error"])
                kept = json.loads((root / f"{copy.stem}.sections.json").read_text("utf-8"))
                self.assertEqual(kept["names"][1], "T1 入弯", "被挡住的重切还是动了侧车")
                status, state = request(
                    f"/api/session/{quoted}/sections", "PUT",
                    {"auto": True, "basis": "curvature", "sensitivity": 1.0, "force": True},
                )
                self.assertEqual(status, 200, state)
                self.assertFalse(state["config"]["edited"])
                self.assertEqual(state["config"]["basis"], "curvature")
                self.assertNotIn("T1 入弯", state["config"]["names"])
                self.assertIn("覆盖", state["notice"] or "")

                # 存过之后重新打开：读到的还是存下来的那一份
                status, again = request(f"/api/session/{quoted}/sections")
                self.assertEqual(status, 200)
                self.assertEqual(again["config"], state["config"])

                # 坏请求要有下一步：一条边界、不认识的判据、没圈可切
                status, body = request(
                    f"/api/session/{quoted}/sections", "PUT", {"names": ["只有名字"]}
                )
                self.assertEqual(status, 400, body)
                self.assertIn("boundaries", body["error"])
                status, body = request(
                    f"/api/session/{quoted}/sections", "PUT",
                    {"auto": True, "basis": "凭感觉"},
                )
                self.assertEqual(status, 400)
                self.assertIn("判据", body["error"])
                # 只给一条边界：补成覆盖整圈的区段，并说清整理了什么
                status, state = request(
                    f"/api/session/{quoted}/sections", "PUT", {"boundaries": [120.0]}
                )
                self.assertEqual(status, 200, state)
                self.assertEqual(state["config"]["boundaries"][0], 0.0)
                self.assertEqual(state["config"]["boundaries"][-1], round(
                    float(sectionsmod.lap_distance(
                        library.get(copy.stem), sectionsmod.reference_lap(render.detect(
                            library.get(copy.stem))))[-1]), 1))
                self.assertIn("整理", state["notice"] or "")
            finally:
                http.close()


class _TableChannel:
    """报表测试用的最小通道：只带 ``report`` 真正会碰的那几个字段。"""

    def __init__(self, name, unit, rate, values):
        self.name = name
        self.unit = unit
        self.sample_rate = float(rate)
        self._values = np.asarray(values, dtype=np.float64)

    @property
    def sample_count(self):
        return int(self._values.size)


class _TableLog:
    """合成场次：通道与距离轴都摆在主时间基上，圈和区段的秒数能手算出来。

    报表的算法必须能脱离 ``.ld`` 单测——金标准数据只能证明"这份数据上是对的"。
    """

    def __init__(self, seconds, rate=10.0, channels=None, distance=None):
        self.sample_rate = float(rate)
        self.duration = float(seconds)
        self.path = Path("合成场次.ld")
        # 数学通道的安放处（ticket #18：会话自己声明，下游不去嗅探属性）
        self.derived: dict[str, np.ndarray] = {}
        self.derived_names: set[str] = set()
        self.derived_units: dict[str, str] = {}
        count = timebase.length(self)
        self._channels: dict[str, _TableChannel] = {}
        for name, spec in (channels or {}).items():
            values, unit = spec if isinstance(spec, tuple) else (spec, "")
            self._channels[name] = _TableChannel(
                name, unit, self.sample_rate, self._pad(values, count)
            )
        if distance is not None:
            self._channels["Distance"] = _TableChannel(
                "Distance", "m", self.sample_rate, self._pad(distance, count)
            )

    def _pad(self, values, count):
        values = np.asarray(values, dtype=np.float64).reshape(-1)
        if values.size >= count:
            return values[:count]
        pad = values[-1] if values.size else 0.0
        return np.concatenate([values, np.full(count - values.size, pad)])

    def has(self, name):
        return name in self._channels

    @property
    def derived_target(self):
        return self.derived

    def channel(self, name):
        return self._channels[name]

    def values(self, name):
        return self._channels[name if isinstance(name, str) else name.name]._values

    #: ticket #34：会话要能被问到原始样本（`channels.valid_count` 用）。
    def raw(self, name):
        return self.values(name)


class TestReport(unittest.TestCase):
    """#11 时间报告 / 通道报告：先用合成数据把口径钉死，再拿金标准验一遍。"""

    def _frame(self, seconds=30.0, rate=10.0):
        """一条 10 m/s 的匀速距离轴：第 n 个样本在 0.1n 秒、走了 n 米。"""
        count = int(round(seconds * rate)) + 1
        distance = np.arange(count, dtype=float)
        # 通道值就用采样时刻（秒），这样统计量能一眼看出窗口取对了没有
        speed = np.arange(count, dtype=float) / rate
        return _TableLog(
            seconds,
            rate=rate,
            channels={"Speed": (speed, "km/h")},
            distance=distance,
        )

    def _laps(self):
        # 第 1 圈 0–20 s 跑完 200 m；第 2 圈 20–30 s 只跑了 100 m（被截断）
        return [
            lapsmod.Lap(0, "1", 0.0, 20.0, 0.0, 200.0, complete=True),
            lapsmod.Lap(1, "2", 20.0, 30.0, 200.0, 300.0, complete=False),
        ]

    def _config(self):
        return sectionsmod.SectionConfig(
            basis="lateral_g",
            boundaries=(0.0, 100.0, 200.0),
            kinds=("straight", "corner"),
            names=("直 1", "弯 1"),
            reference_label="1",
            length_m=200.0,
        )

    # ------------------------------------------------------------ 纯函数
    def test_stats_use_the_first_and_last_valid_sample(self):
        found = reportmod.stats([np.nan, 1.0, 3.0, np.nan, 7.0, np.nan])
        self.assertAlmostEqual(found["min"], 1.0)
        self.assertAlmostEqual(found["max"], 7.0)
        self.assertAlmostEqual(found["abs_max"], 7.0)
        self.assertAlmostEqual(found["mean"], 11.0 / 3.0)
        self.assertAlmostEqual(found["start"], 1.0, msg="起值不能是开头的 NaN")
        self.assertAlmostEqual(found["end"], 7.0, msg="终值不能是结尾的 NaN")
        self.assertAlmostEqual(found["change"], 6.0)
        self.assertGreater(found["std_dev"], 0.0)

        empty = reportmod.stats([np.nan, np.nan])
        for key in reportmod.STAT_KEYS:
            self.assertIsNone(empty[key], f"{key} 在没有有效样本时必须是 None，0 会被当成真实测量")

    def test_band_thresholds_match_the_documented_ratios(self):
        self.assertEqual(reportmod.band_for(1.0), "best")
        self.assertEqual(reportmod.band_for(1.005), "best")
        self.assertEqual(reportmod.band_for(1.006), "close")
        self.assertEqual(reportmod.band_for(1.03), "close")
        self.assertEqual(reportmod.band_for(1.031), "fair")
        self.assertEqual(reportmod.band_for(1.08), "fair")
        self.assertEqual(reportmod.band_for(1.081), "")
        self.assertEqual(reportmod.band_for(None), "")

    # ------------------------------------------------------------ 时间报告
    def test_matrix_lists_one_column_per_lap_and_sums_to_the_lap_time(self):
        log, laps, config = self._frame(), self._laps(), self._config()
        table = reportmod.time_report(log, laps, config)
        self.assertEqual(len(table["rows"]), 2, "两条区段就该只有两行")
        self.assertEqual(
            [column["label"] for column in table["columns"]][:4],
            ["区段", "类型", "圈 1", "圈 2（未完）"],
            "表头必须点出没跑完的圈，否则一格 5 s 的'直道'会被当成神级走线",
        )
        straight, corner = table["rows"]
        self.assertEqual(straight[0], "1. 直 1")
        self.assertAlmostEqual(straight[2], 10.0)
        self.assertAlmostEqual(straight[3], 5.0)
        self.assertAlmostEqual(corner[2], 10.0)
        self.assertAlmostEqual(corner[3], 5.0)
        # 每一条圈的整行加起来必须等于那条圈的圈速（分段是无缝无叠地铺满一圈的）
        for column, lap in ((2, laps[0]), (3, laps[1])):
            total = sum(row[column] for row in table["rows"])
            self.assertAlmostEqual(total, lap.lap_time, places=6)

    def test_a_truncated_lap_cannot_win_a_section(self):
        """被截断的圈（进站 / 回维修区）不能把"理论最快圈"拉到一个跑不出来的值。"""
        log, config = self._frame(), self._config()
        partial = reportmod.time_report(log, self._laps(), config)
        self.assertEqual(partial["summary"]["based_on"], "完整圈")
        self.assertAlmostEqual(partial["summary"]["theoretical"], 20.0)
        self.assertEqual(partial["rows"][0][-2], "1", "段最快必须出自那条完整圈")

        both = [lapsmod.Lap(0, "1", 0.0, 20.0, 0.0, 200.0, complete=True),
                lapsmod.Lap(1, "2", 20.0, 30.0, 200.0, 300.0, complete=True)]
        table = reportmod.time_report(log, both, config)
        self.assertAlmostEqual(table["summary"]["theoretical"], 10.0,
                               msg="两条圈都完整时，5 s 的段才算数")
        self.assertEqual(table["rows"][0][-2], "2")

    def test_kind_filter_keeps_the_index_and_narrows_the_theoretical_lap(self):
        log, laps, config = self._frame(), self._laps(), self._config()
        table = reportmod.time_report(log, laps, config, kind="corner")
        self.assertEqual(table["section_count"], 1)
        self.assertEqual(table["filter"], "corner")
        self.assertEqual(table["rows"][0][0], "2. 弯 1", "过滤后序号仍是原序号")
        self.assertEqual(table["row_kinds"], ["corner"])
        self.assertAlmostEqual(table["summary"]["theoretical"], 10.0,
                               msg="只看弯道时理论最快圈只加弯道那些段")

    def test_theoretical_rolling_and_best_lap_line_up(self):
        log, laps, config = self._frame(), self._laps(), self._config()
        summary = reportmod.time_report(log, laps, config)["summary"]
        self.assertAlmostEqual(summary["rolling"]["duration"], 20.0,
                               msg="200 m 的窗口在这条合成数据上正好 20 s")
        self.assertAlmostEqual(summary["best_lap"]["lap_time"], 20.0)
        self.assertLessEqual(summary["theoretical"], summary["rolling"]["duration"])
        self.assertIn("参考下限", summary["note"])

    # ------------------------------------------------------------ 连续最快圈
    def test_a_stop_inside_the_window_counts_against_the_rolling_lap(self):
        """窗口跑的是**距离**：这段距离里停着的 90 s 就是这段距离的一部分。

        合成数据：0→100 m 用 10 s，在 100 m 处停 90 s，再 100→500 m 用 40 s。
        正确口径（首次到达）给 130 s；把停车的尾端也当起点会给出 40 s——那等于
        声称"400 m 只用了 40 s"，而车在那段距离里明明停着。
        """
        rate = 10.0
        count = 1401
        time = np.arange(count) / rate
        distance = np.where(
            time <= 10.0, 10.0 * time,
            np.where(time <= 100.0, 100.0, 100.0 + 10.0 * (time - 100.0)),
        )
        log = _TableLog(140.0, rate=rate, distance=distance)
        found = reportmod.rolling_best(log, 400.0)
        self.assertIsNotNone(found)
        self.assertAlmostEqual(found["duration"], 130.0, places=3, msg="停车段的 90 s 被抹掉了")
        self.assertAlmostEqual(found["start_time"], 0.0, places=3)
        self.assertAlmostEqual(found["end_time"], 130.0, places=3)

    def test_rolling_lap_needs_a_full_lap_of_track(self):
        log = _TableLog(20.0, rate=10.0, distance=np.arange(201, dtype=float))
        self.assertIsNone(reportmod.rolling_best(log, 400.0),
                          "赛道长度不到一圈时要给 None，不能给一段假成绩")
        self.assertIsNone(reportmod.rolling_best(log, 0.0))

    # ------------------------------------------------------------ 通道报告
    def test_channel_report_windows_do_not_share_the_boundary_sample(self):
        log, laps, config = self._frame(), self._laps(), self._config()
        table = reportmod.channel_report(log, laps, config, ["Speed"])
        self.assertEqual(len(table["rows"]), 2, "两条圈两条通道各一行")
        first, second = table["rows"]
        at = {column["key"]: index for index, column in enumerate(table["columns"])}
        self.assertEqual(first[:6], ["1", "1", "", "", "Speed", "km/h"])
        self.assertAlmostEqual(first[at["min"]], 0.0)
        self.assertAlmostEqual(first[at["max"]], 19.9)      # 止点那一刻算下一条圈
        self.assertAlmostEqual(first[at["change"]], 19.9)
        self.assertGreater(first[at["std_dev"]], 0.0)
        self.assertAlmostEqual(second[at["min"]], 20.0)
        self.assertAlmostEqual(second[at["start"]], 20.0)   # 起值 = 20.0 s 那个样本

    def test_channel_report_by_section_uses_that_lap_own_boundaries(self):
        log, laps, config = self._frame(), self._laps(), self._config()
        table = reportmod.channel_report(log, laps, config, ["Speed"], by="section")
        self.assertEqual(table["by"], "section")
        self.assertEqual(table["lap"], "1", "按区段分组默认跑在参考圈上")
        rows = table["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual([row[2] for row in rows], ["1. 直 1", "2. 弯 1"])
        self.assertAlmostEqual(rows[0][6], 0.0)
        self.assertAlmostEqual(rows[1][6], 10.0, msg="弯 1 从第 100 个样本（10 s）开始")

        other = reportmod.channel_report(log, laps, config, ["Speed"], by="section", lap_label="2")
        self.assertEqual(other["lap"], "2")
        self.assertEqual(other["rows"][0][1], "2")
        self.assertAlmostEqual(other["rows"][0][6], 20.0, msg="第 2 圈从 20 s 起算")

    def test_channel_report_refuses_an_unknown_grouping(self):
        log, laps, config = self._frame(), self._laps(), self._config()
        with self.assertRaises(ValueError) as caught:
            reportmod.channel_report(log, laps, config, ["Speed"], by="lapx")
        self.assertIn("lap / section", str(caught.exception))

    def test_missing_channels_are_reported_not_silently_dropped(self):
        log, laps, config = self._frame(), self._laps(), self._config()
        table = reportmod.channel_report(log, laps, config, ["Speed", "不存在的通道"])
        self.assertEqual(table["channels"], ["Speed"])
        self.assertEqual(table["missing"], ["不存在的通道"])

    # ------------------------------------------------------------ CSV
    def test_csv_header_is_the_column_labels_and_cells_are_escaped(self):
        columns = [
            {"key": "a", "label": "名称", "type": "text"},
            {"key": "b", "label": "用时", "type": "time", "decimals": 3},
        ]
        rows = [["T1, 入弯", 12.3456], ["带\"引号\"", 1.0]]
        text = reportmod.to_csv(columns, rows)
        lines = text.strip().split("\n")
        self.assertEqual(lines[0], "名称,用时")
        self.assertEqual(lines[1], '"T1, 入弯",12.346')
        self.assertEqual(lines[2], '"带""引号""",1.000')
        self.assertTrue(text.endswith("\n"))

    def test_csv_of_the_time_report_has_one_line_per_section(self):
        log, laps, config = self._frame(), self._laps(), self._config()
        table = reportmod.time_report(log, laps, config)
        text = reportmod.to_csv(table["columns"], table["rows"])
        lines = text.strip().split("\n")
        self.assertEqual(len(lines), len(table["rows"]) + 1)
        width = len(table["columns"])
        for line in lines:
            self.assertEqual(len(line.split(",")), width)

    # ------------------------------------------------------------ 金标准
    @_needs(HILL)
    def test_golden_hill_sections_sum_to_each_lap(self):
        with ld.LogFile.read(HILL) as log:
            laps = render.detect(log)
            payload = render.report_payload(log)
            table = payload["time"]
            self.assertIsNone(payload["error"], payload.get("notice"))
            # 2 个前缀列 + 每条圈一列 + 3 个尾列（段最快 / 出自 / 快慢差）
            self.assertEqual(len(table["columns"]) - 5, len(table["lap_labels"]))
            by_label = {str(lap.label): lap for lap in laps}
            for position, label in enumerate(table["lap_labels"]):
                total = 0.0
                seen = False
                for row in table["rows"]:
                    value = row[2 + position]
                    if value is None:
                        continue
                    total += value
                    seen = True
                if seen:
                    self.assertAlmostEqual(
                        total, by_label[label].lap_time, places=2,
                        msg=f"第 {label} 圈的分段加起来不等于圈速",
                    )
            summary = table["summary"]
            self.assertLessEqual(summary["theoretical"], summary["rolling"]["duration"])
            self.assertLessEqual(summary["rolling"]["duration"], summary["best_lap"]["lap_time"])

    @_needs(ENDURANCE)
    def test_golden_endurance_report_is_consistent(self):
        with ld.LogFile.read(ENDURANCE) as log:
            laps = render.detect(log)
            table = render.report_payload(log)["time"]
            summary = table["summary"]
            self.assertGreater(len(table["rows"]), 5)
            self.assertGreater(summary["theoretical"], 0)
            self.assertLess(summary["theoretical"], summary["best_lap"]["lap_time"],
                            "理论最快圈不可能比真跑出来的最快圈还慢")
            self.assertGreater(summary["rolling"]["lap_length_m"], 100.0)
            # 连续最快圈的窗口是连续数据里的一段，可以跨过起点线
            self.assertLessEqual(summary["rolling"]["duration"], summary["best_lap"]["lap_time"])

    @_needs(ENDURANCE)
    def test_golden_channel_report_covers_every_lap_and_channel(self):
        with ld.LogFile.read(ENDURANCE) as log:
            laps = render.detect(log)
            channels = ["Vx KF", "G Force Long"]
            if not all(log.has(name) for name in channels):
                self.skipTest("金标准里没有这两条通道")
            table = render.report_payload(log, channels=channels)["channels"]
            self.assertEqual(len(table["rows"]), len(laps) * len(channels))
            self.assertEqual(table["channels"], channels)
            for row in table["rows"]:
                self.assertIsNotNone(row[6], "每条圈每个通道都该有最小值")


class TestReportOverHttp(unittest.TestCase):
    """#11 走到界面之前的那一段：/report 的两种表、两种过滤、CSV 出口。"""

    @_needs(HILL)
    def test_report_endpoint_serves_both_tables_and_csv(self):
        with http_session(HILL, buckets=200) as http:
            quoted = http.quoted
            get = http.get_bytes

            try:
                status, raw = get(f"/api/session/{quoted}/report")
                self.assertEqual(status, 200)
                payload = json.loads(raw.decode("utf-8"))
                self.assertIsNone(payload["error"])
                self.assertTrue(payload["time"]["rows"])
                self.assertTrue(payload["channels"]["rows"])

                status, raw = get(f"/api/session/{quoted}/report?table=time&filter=corner")
                table = json.loads(raw.decode("utf-8"))["time"]
                self.assertEqual(table["filter"], "corner")
                self.assertTrue(all(kind == "corner" for kind in table["row_kinds"]))
                self.assertNotIn("channels", json.loads(raw.decode("utf-8")))

                status, raw = get(f"/api/session/{quoted}/report?csv=time&filter=corner")
                text = raw.decode("utf-8-sig")
                lines = text.strip().split("\n")
                self.assertEqual(status, 200)
                self.assertEqual(lines[0].split(",")[0], "区段")
                self.assertEqual(len(lines), len(table["rows"]) + 1)

                status, raw = get(f"/api/session/{quoted}/report?csv=channels&by=section&channels=Vx%20KF")
                text = raw.decode("utf-8-sig")
                self.assertEqual(text.split("\n")[0].split(",")[:6],
                                 ["分组", "圈", "区段", "类型", "通道", "单位"])
                self.assertIn("弯", text)

                with self.assertRaises(urllib.error.HTTPError) as caught:
                    get(f"/api/session/{quoted}/report?filter=nosuch")
                self.assertEqual(caught.exception.code, 400)
                message = json.loads(caught.exception.read().decode("utf-8"))["error"]
                self.assertIn("filter 只认", message)
            finally:
                http.close()


class TestNotes(unittest.TestCase):
    """#15 注释：纯函数层。位置算得对不对、规则说得清不清楚，都在这里。"""

    def test_clean_text_折行与超长(self):
        self.assertEqual(notesmod.clean_text("  这里换了\n刹车点  "), "这里换了 刹车点")
        self.assertEqual(notesmod.clean_text(None), "")
        self.assertEqual(len(notesmod.clean_text("字" * 500)), notesmod.MAX_TEXT)

    def test_normalize_去空并按时刻排序(self):
        notes = notesmod.normalize(
            [{"time": 30.0, "text": "晚"}, {"time": 10.0, "text": "早"}], 60.0
        )
        self.assertEqual([n.text for n in notes], ["早", "晚"])
        self.assertEqual(notesmod.normalize(None, 60.0), [])

    def test_normalize_空文字说下一步(self):
        with self.assertRaises(notesmod.NoteError) as caught:
            notesmod.normalize([{"time": 1.0, "text": "   "}], 60.0)
        message = str(caught.exception)
        self.assertIn("第 1 条", message)
        self.assertIn("写一句", message)          # 报错要说下一步，不只是"错了"

    def test_normalize_时刻越界报出合法区间(self):
        with self.assertRaises(notesmod.NoteError) as caught:
            notesmod.normalize([{"time": 99.0, "text": "太晚"}], 60.0)
        message = str(caught.exception)
        self.assertIn("0–60.000 s", message)
        self.assertIn("99.000", message)

    def test_normalize_时刻不是数(self):
        with self.assertRaises(notesmod.NoteError) as caught:
            notesmod.normalize([{"time": "刚才", "text": "x"}], 60.0)
        self.assertIn("鼠标移到图上", str(caught.exception))

    def test_normalize_坏结构(self):
        with self.assertRaises(notesmod.NoteError):
            notesmod.normalize({"time": 1.0}, 60.0)
        with self.assertRaises(notesmod.NoteError):
            notesmod.normalize(["第 3 条被我手写成了字符串"], 60.0)

    def test_normalize_条数上限(self):
        rows = [{"time": 1.0, "text": "x"}] * (notesmod.MAX_NOTES + 1)
        with self.assertRaises(notesmod.NoteError) as caught:
            notesmod.normalize(rows, 60.0)
        self.assertIn("先删掉几条", str(caught.exception))

    def test_add_update_remove_不改传进来的那份(self):
        base = [notesmod.Note(10.0, "早")]
        grown = notesmod.add_note(base, 5.0, "更早", 60.0)
        self.assertEqual([n.text for n in grown], ["更早", "早"])
        self.assertEqual([n.text for n in base], ["早"])          # 原表没被动
        edited = notesmod.update_note(grown, 1, " 改过 ", 60.0)
        self.assertEqual(edited[1], notesmod.Note(10.0, "改过"))  # 时刻不动
        self.assertEqual([n.text for n in notesmod.remove_note(edited, 0)], ["改过"])

    def test_update_remove_索引对不上时说下一步(self):
        notes = [notesmod.Note(10.0, "早")]
        with self.assertRaises(notesmod.NoteError) as caught:
            notesmod.update_note(notes, 3, "x", 60.0)
        self.assertIn("已经不在了", str(caught.exception))
        with self.assertRaises(notesmod.NoteError) as caught:
            notesmod.remove_note(notes, 3)
        self.assertIn("刷新", str(caught.exception))

    def test_marks_距离在主采样上插值(self):
        rows = notesmod.marks(
            [notesmod.Note(1.5, "弯心")],
            None,
            master_time=[0.0, 1.0, 2.0],
            master_distance=[0.0, 10.0, 20.0],
        )
        self.assertEqual(rows[0]["distance"], 15.0)
        self.assertIsNone(rows[0]["x"])

    def test_marks_落在序列之外不猜(self):
        rows = notesmod.marks(
            [notesmod.Note(9.0, "界外")],
            None,
            master_time=[0.0, 1.0, 2.0],
            master_distance=[0.0, 10.0, 20.0],
        )
        self.assertIsNone(rows[0]["distance"])

    def test_marks_轨迹取最近的抽稀点(self):
        track = {"time": [0.0, 10.0, 20.0], "x": [0.0, 100.0, 200.0],
                 "y": [0.0, 5.0, 12.0]}
        rows = notesmod.marks([notesmod.Note(11.0, "近的")], track)
        self.assertEqual((rows[0]["x"], rows[0]["y"]), (100.0, 5.0))
        far = notesmod.marks([notesmod.Note(60.0, "界外")], track)
        self.assertIsNone(far[0]["x"])

    def test_侧车往返_坏文件要吵且不删(self):
        """读坏了要报错、要能照做、**不能把文件删掉**（ticket #16 统一的失败策略）。

        以前这里悄悄给空表——那会让"图上看不见注释"和"文件坏了"长得一模一样，
        用户补一条再保存就把旧的覆盖掉了。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "场次.ld"
            path = notesmod.save_notes(session, [notesmod.Note(12.5, "这里换了刹车点")])
            self.assertEqual(path.name, "场次.notes.json")
            back = notesmod.load_notes(session)
            self.assertEqual([(n.time, n.text) for n in back], [(12.5, "这里换了刹车点")])
            path.write_text("{ 这不是 JSON", encoding="utf-8")
            with self.assertRaises(sidecar.SidecarError) as caught:
                notesmod.load_notes(session)
            self.assertIn("修好这个 JSON", str(caught.exception))
            self.assertTrue(path.exists(), "坏掉的侧车不能被自动删掉：那是用户的东西")
            self.assertEqual(path.read_text(encoding="utf-8"), "{ 这不是 JSON")
            self.assertFalse(session.exists())          # 侧车永远不会去写 .ld

    def test_注释不参与切圈也不改报表(self):
        """注释写坏了，圈速表一个数都不该动——它有自己的侧车，就是为了这个。"""
        if not HILL.exists():
            self.skipTest("sample log not present")
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            copy = root / HILL.name
            copy.write_bytes(HILL.read_bytes())
            with ld.LogFile.read(copy) as log:
                before = [(l.label, round(l.lap_time, 6)) for l in render.detect(log)]
                # 12.5 s 还在发车区（距离是 0），所以取圈中间的两个时刻
                notesmod.save_notes(copy, [notesmod.Note(200.0, "这里换了刹车点"),
                                           notesmod.Note(220.0, "出弯早给油")])
                after = [(l.label, round(l.lap_time, 6)) for l in render.detect(log)]
                self.assertEqual(before, after)
                self.assertTrue(before)
                # 报表面上的数也不该被注释碰到：注释不是通道，也不是区段。
                payload = render.notes_payload(log)
                self.assertEqual(len(payload), 2)
                self.assertIsNotNone(payload[0]["distance"])
                self.assertGreater(payload[1]["distance"], payload[0]["distance"])
                # 快照也要带上注释：`build_payload` 一起嵌进去，分享链接打开的
                # 是同一份（serve 模式页面 payload 走的是同一个出口）。
                full = render.build_payload(log, channels=["Vx KF"], buckets=200)
                self.assertEqual([n["text"] for n in full["notes"]],
                                 ["这里换了刹车点", "出弯早给油"])
                self.assertIsNotNone(full["notes"][0]["x"], "快照里要能画到轨迹图上")
                page = render.render_page(full)
                self.assertIn("这里换了刹车点", page)


class TestNotesOverHttp(unittest.TestCase):
    """#15 走到界面之前的那一段：/notes 的 GET / PUT、报错与落盘。"""

    @_needs(HILL)
    def test_notes_endpoint(self):
        with http_session(HILL, buckets=50) as http:
            root, copy, quoted = http.root, http.copy, http.quoted
            original = http.before
            request = http.json

            try:
                status, body = request(f"/api/session/{quoted}/notes")
                self.assertEqual(status, 200, body)
                self.assertEqual(body["notes"], [])

                status, body = request(
                    f"/api/session/{quoted}/notes", "PUT",
                    {"notes": [{"time": 231.74, "text": "这里换了刹车点"}]},
                )
                self.assertEqual(status, 200, body)
                self.assertEqual(body["saved"], f"{copy.stem}.notes.json")
                self.assertEqual(body["notes"][0]["text"], "这里换了刹车点")
                self.assertIsNotNone(body["notes"][0]["distance"], "距离轴要能落点")
                on_disk = json.loads(
                    (root / f"{copy.stem}.notes.json").read_text(encoding="utf-8")
                )
                self.assertEqual(on_disk["notes"][0]["time"], 231.74)

                status, body = request(f"/api/session/{quoted}/notes")
                self.assertEqual(len(body["notes"]), 1)

                status, body = request(
                    f"/api/session/{quoted}/notes", "PUT",
                    {"notes": [{"time": 1.0, "text": "  "}]},
                )
                self.assertEqual(status, 400, body)
                self.assertIn("写一句", body["error"])

                status, body = request(
                    f"/api/session/{quoted}/notes", "PUT",
                    {"notes": [{"time": 99999.0, "text": "太晚"}]},
                )
                self.assertEqual(status, 400, body)
                self.assertIn("0–", body["error"])

                status, body = request(f"/api/session/{quoted}/notes", "PUT", {"n": []})
                self.assertEqual(status, 400, body)
                self.assertIn("notes", body["error"])

                # 存不下的时候，盘上那一份不许被半途改掉
                self.assertEqual(
                    json.loads((root / f"{copy.stem}.notes.json").read_text("utf-8"))["notes"][0]["text"],
                    "这里换了刹车点",
                )
                self.assertEqual(copy.read_bytes(), original, ".ld 是只读的")

                status, body = request(
                    f"/api/session/{quoted}/notes", "PUT", {"notes": []}
                )
                self.assertEqual(status, 200, body)
                self.assertEqual(body["notes"], [])
            finally:
                http.close()


class TestApiLayerWithoutASocket(unittest.TestCase):
    """API 层：不起 socket、不读 socket，也能把一个动作当函数调（ticket #23）。

    拆之前，"HTTP 怎么回"和"这个动作算什么"写在同一个 769 行的请求闭包里，
    每个动作都要跑一遍 ``HTTPConnection`` 才能验证——慢，而且失败信息常常只剩
    一句"连接被重置"。现在 ``Api.handle`` 就是一个函数：``(parts, query,
    method, body) -> Response``。
    """

    def setUp(self):
        from i3pro import api, library
        self.api = api
        self.library = library.SessionLibrary(LIBRARY_ROOTS, cache_size=1)
        self.client = api.Api(self.library)

    def tearDown(self):
        self.library.close()

    def test_路由表里的每个动作都有实现(self):
        """加一个动作 = 加一行；这一行指向的方法必须真的在（打错字就是 500）。"""
        call = self.api._Call
        for action, method in call.ACTIONS.items():
            self.assertTrue(
                hasattr(call, method),
                f"ACTIONS 里 {action!r} 指向 {method!r}，但这一层没有这个方法——"
                f"改名时漏了这一行。",
            )

    def test_动作清单就是文档里那一份(self):
        expected = {
            "info", "trace", "points", "histogram", "spectrum", "overview", "overlay",
            "track", "laps", "sections", "notes", "gps", "report", "maths", "at", "export",
        }
        self.assertEqual(set(self.api._Call.ACTIONS), expected,
                         "动作清单变了：加/删动作请把这条与 docs/ACCEPTANCE.md 一起改")

    def test_每个请求都拿得到一个回复(self):
        """返回 ``None`` 的路由会把连接晾着（2026-09-14 拆服务时真的踩到过）。"""
        for parts in (["sessions"], ["nope"], ["session", "nope", "info"],
                      ["session", "nope", "nope"], []):
            response = self.client.handle(parts, {}, "GET")
            self.assertIsInstance(response, self.api.Response)
            self.assertGreaterEqual(response.status, 200)

    def test_客户端走了就早停并清掉临时目录(self):
        """点了「取消」之后，服务端不该把剩下几百 MB 写完再删。

        浏览器的 ``fetch`` 被 abort 时会关连接，HTTP 层把这个事实做成 ``Body.alive``
        探针；导出每写一块问一次，不在了就 ``ClientGone``。这条同时钉住"安静收场"
        （499 而不是假装成功）与"临时目录一个都不留"。
        """
        if not HILL.exists():
            self.skipTest("缺金标准数据")
        from i3pro import api as apimod

        temp = Path(tempfile.gettempdir())
        before = set(temp.glob("i3pro-export-*"))
        response = self.client.handle(
            ["session", HILL.stem, "export"],
            {"channels": ["all"], "rate": ["auto"], "format": ["csv"], "layout": ["wide"],
             "from": ["0"], "to": ["5"]},
            "GET",
            apimod.Body(length=0, alive=lambda: False),
        )
        self.assertEqual(response.status, 499, "客户端已断开时应当安静收场，而不是回 200")
        self.assertEqual(set(temp.glob("i3pro-export-*")), before,
                         "取消了导出，临时目录却没清掉")

    def test_未知会话与未知动作各自报自己的错(self):
        if not (DATA.exists() and any(DATA.glob("*.ld"))):
            self.skipTest("缺 i2pro_data/ 数据")
        unknown_action = self.client.handle(["session", "没有这个场次", "没有这个动作"], {}, "GET")
        self.assertEqual(unknown_action.status, 404)
        self.assertIn("未知场次", unknown_action.body.decode("utf-8"))
        name = self.library.names()[0]
        response = self.client.handle(["session", name, "没有这个动作"], {}, "GET")
        self.assertEqual(response.status, 404)
        text = response.body.decode("utf-8")
        self.assertIn("没有 '没有这个动作' 这个动作", text)
        self.assertIn("trace", text)          # 报错里带上"认得的动作"

    def test_请求体是按需读的(self):
        """上传可能 100 MB：不碰请求体的动作不该把它读进内存。"""
        from i3pro import api as apimod
        reads = []

        class Counter(apimod.Body):
            def read(self):
                reads.append(self.length)
                return super().read()

        self.client.handle(["sessions"], {}, "GET", Counter(64, None, b"x" * 64))
        self.assertEqual(reads, [], "列表动作把请求体读了")


class TestStructureOfTheSplit(unittest.TestCase):
    """结构性重构的守卫（ticket #23 / #24）：**概念住在哪个文件**只留一个答案。

    这些用例不看行为（行为由功能用例管），只看"同一件事有没有被搬回去"。
    搬回去的代价写在各自的 ticket 里：加一个 API 动作要在 700 行的闭包里翻，
    加一条切圈规则要在 1100 行的模块里翻。
    """

    SOURCE = ROOT / "src" / "i3pro"

    def _text(self, name: str) -> str:
        return (self.SOURCE / name).read_text(encoding="utf-8")

    def _code(self, name: str) -> str:
        """只有"真的会执行"的那部分：去掉模块 docstring、注释与 TYPE_CHECKING 块。

        扫源码的守卫最容易犯的错是把**注释里提到的名字**当成代码（"不 import api"
        这句话本身会被 ``assertNotIn("import api")`` 抓住）。所以先剥掉这些。
        """
        text = re.sub(r'^""".*?"""', "", self._text(name), count=1, flags=re.S)
        text = re.sub(r"\nif TYPE_CHECKING:.*?(?=\n\S)", "", text, flags=re.S)
        return "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )

    # ---------------------------------------------------------- #23 服务端
    def test_server_只剩_HTTP_管道(self):
        server = self._code("server.py")
        self.assertLess(len(self._text("server.py").splitlines()), 420,
                        "server.py 又长回来了（拆之前 1274 行，拆完 367 行）")
        for needle in ('if action == "', "def act_"):
            self.assertNotIn(
                needle, server,
                f"server.py 里还有 {needle!r}——动作分派与请求体都属于 i3pro.api，"
                f"server.py 只该收字节、发字节。",
            )
        # 读 socket 只能有一个地方（`_read_body`，而且要按需调用）：动作自己不碰
        # 请求体，所以"100 MB 的上传不会因为看一眼列表就先读进内存"。
        self.assertEqual(server.count("self.rfile.read"), 1,
                         "读 socket 的地方只该是 _read_body 这一处")
        self.assertIn("def _read_body(", server)
        # 导出中途取消要靠这一个探针早停；它丢了，"取消"就只是客户端不看而已
        self.assertIn("alive=self._client_alive", server,
                      "Body 没拿到「客户端还在不在」的探针：导出取消会退化成"
                      "「把整份写完再删」")

    def test_动作分派只有一张表(self):
        api = self._code("api.py")
        self.assertIn("    ACTIONS = {", api)
        self.assertNotIn('if action == "', api,
                         "api.py 里又出现了 if/elif 长链：加一个动作应当是加一行")

    def test_库不反向依赖服务(self):
        library = self._code("library.py")
        self.assertEqual(
            re.findall(r"^\s*(?:from \.(?:api|server)\b|import (?:api|server)\b)",
                       library, re.M),
            [], "library 被 api/server 依赖，不能反过来 import 它们（会成环）",
        )

    def test_老名字仍然可用(self):
        from i3pro import api, library
        self.assertIs(server.SessionLibrary, library.SessionLibrary)
        self.assertTrue(callable(server.make_handler))
        self.assertTrue(callable(server.bind))
        self.assertTrue(callable(library._json_safe))       # 文档与老调用方引用的名字
        self.assertEqual(api.MAX_UPLOAD_BYTES, 2 * 1024 * 1024 * 1024)

    def test_动作响应带得动附件头(self):
        from i3pro.api import Response
        response = Response(200, b"a,b\n", "text/csv; charset=utf-8",
                            (("Content-Disposition", "attachment; filename=x.csv"),))
        self.assertEqual(response.headers[0][0], "Content-Disposition")
        self.assertFalse(response.close)

    # ------------------------------------------------------------- #26 HTTP 缝
    def test_HTTP_用例共用一个夹具(self):
        """加一个动作不该顺手再抄一遍服务端脚手架（ticket #26）。

        改造前这份文件里有 **13 处** ``ThreadingHTTPServer(...)``、**20 处**
        ``urllib.request.urlopen``：同一个 400 在不同用例里被解成了不同形状，
        而"这条动作怎么回"的判据散在九组用例里各写一遍。现在只有
        ``http_session`` 里那一处。
        """
        tests = (ROOT / "tests" / "test_i3pro.py").read_text(encoding="utf-8")
        # 针尖掰成两半：整串写在源码里的话，这条断言会把自己那份字面量也数进去
        # （`_code` 那个辅助函数注释里写过同一个坑）。
        needle = "= ThreadingHTTP" + "Server("
        self.assertEqual(
            tests.count(needle), 1,
            "又有人自己起服务了：HTTP 用例请写 `with http_session(场次) as http:`",
        )
        self.assertIn("def http_session(", tests)
        for name in ("TestBeaconEditingOverHttp", "TestBeaconUndoOverHttp",
                     "TestSectionsOverHttp", "TestReportOverHttp",
                     "TestNotesOverHttp", "TestGpsFixOverHttp",
                     "TestHistogramOverHttp", "TestSpectrumOverHttp",
                     "TestMathsOverHttp"):
            body = _class_body(tests, name)
            self.assertIn("http_session(", body, f"{name} 没有走共享夹具")
            self.assertNotIn(
                "urllib.request.urlopen", body,
                f"{name} 又在自己拼 urllib 了：请求形状应当只在 `_Http` 上有一处。",
            )

    # ------------------------------------------------------------- #24 切圈
    def test_信标配置住在_beacons(self):
        beacons = self._text("beacons.py")
        for needle in ("class Beacon:", "class LapConfig:", "def reconcile_edits(",
                       "def check_new_crossings(", "def undo_config(", "def load_config(",
                       "def save_config(", "def unique_name(", "def merge_crossings("):
            self.assertIn(needle, beacons, f"beacons.py 少了 {needle!r}")

    def test_距离轴与圈差住在_axes(self):
        axes = self._text("axes.py")
        for needle in ("def overlay(", "def time_delta(", "def time_at_distance(",
                       "def distance_on_master("):
            self.assertIn(needle, axes, f"axes.py 少了 {needle!r}")

    def test_laps_只剩切圈(self):
        laps = self._text("laps.py")
        for needle in ("def overlay(", "def time_at_distance(", "def distance_on_master(",
                       "def reconcile_edits(", "def load_config(", "def save_config(",
                       "class LapConfig:", "class Beacon:"):
            self.assertNotIn(
                needle, laps,
                f"laps.py 里又有 {needle!r}——它现在只切圈；信标与配置在 beacons.py，"
                f"距离轴与圈差在 axes.py（需要的话 import 进来，别复制一份）。",
            )
        self.assertLess(len(laps.splitlines()), 800,
                        "laps.py 又长回来了（拆之前 1145 行，拆完 748 行）")

    def test_三个模块共用同一份对象(self):
        from i3pro import axes, beacons, laps
        self.assertIs(laps.LapConfig, beacons.LapConfig)
        self.assertIs(laps.Beacon, beacons.Beacon)
        self.assertIs(laps.overlay, axes.overlay)
        self.assertIs(laps.time_at_distance, axes.time_at_distance)
        self.assertIs(laps.load_config, beacons.load_config)

    def test_切圈之外的两个模块不反向依赖切圈(self):
        for name in ("beacons.py", "axes.py"):
            self.assertEqual(
                re.findall(r"^\s*(?:from \.laps\b|import laps\b)", self._code(name), re.M),
                [],
                f"{name} 被 laps 依赖，运行时不能反过来 import laps（会成环）；"
                f"只是标注用的类型请放进 if TYPE_CHECKING。",
            )


class _ExportBase(unittest.TestCase):
    """导出用例的公共底座：金标准场次的**副本** + 一个用完就删的临时目录。

    读副本而不是 ``i2pro_data`` 按仓库约定来（侧车是用户资产，队员给金标准场次
    存一次编辑不该把测试弄红）。导出这条路只读 ``.ld``，但约定一视同仁。

    ``export.write`` 一次可能写几百 MB，所以临时目录用 ``TemporaryDirectory``
    而不是 ``mkdtemp``——用例失败时也不会把垃圾留在 ``out/`` 里。
    """

    SESSION = HILL

    @classmethod
    def setUpClass(cls):
        if not cls.SESSION.exists():
            raise unittest.SkipTest(f"缺金标准数据 {cls.SESSION.name}")
        cls.log = ld.LogFile.read(cls.SESSION)

    @classmethod
    def tearDownClass(cls):
        cls.log.close()

    def request(self, **params):
        base = {"channels": "selected", "names": "Vx KF"}
        base.update(params)
        return exportmod.parse_request(self.log, base)

    def tmp(self):
        directory = tempfile.TemporaryDirectory(prefix="i3pro-exp-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name)


class TestExportRangeResolution(_ExportBase):
    """范围解析：需求里点名的那几种写法都要能落到同一段数据上（ticket #23）。

    "12:34:56.789 到 12:35:10.123" 是需求原文里的例子——第二个端点是**裸时钟**，
    日期沿用场次那一天。这条一开始漏了（只认完整日期时间），所以钉在这里。
    """

    def _request(self, **params):
        return exportmod.parse_request(self.log, params)

    def test_裸时钟沿用场次那一天的日期(self):
        exact = self._request(axis="time", absolute="1",
                              **{"from": "2026-09-08 15:48:32.5"}, to="2026-09-08 15:48:35.0")
        bare = self._request(axis="time", absolute="1",
                             **{"from": "15:48:32.5"}, to="15:48:35.0")
        self.assertAlmostEqual(exact.start, 10.5, places=3)
        self.assertAlmostEqual(bare.start, exact.start, places=6)
        self.assertAlmostEqual(bare.end, exact.end, places=6)
        self.assertAlmostEqual(bare.end - bare.start, 2.5, places=3)

    def test_范围左闭右闭(self):
        """起止点都算在里头：0–2 s、10 Hz 是 21 行（0.0 … 2.0），不是 20 行。"""
        request = self._request(axis="time", **{"from": "0"}, to="2", rate="10",
                                channels="selected", names="Vx KF")
        self.assertEqual(exportmod.plan(self.log, request)["rows"], 21)

    def test_时间范围颠倒或者越界要说下一步(self):
        with self.assertRaises(exportmod.ExportError) as caught:
            self._request(axis="time", **{"from": "20"}, to="10")
        self.assertIn("from", str(caught.exception))
        with self.assertRaises(exportmod.ExportError) as caught:
            self._request(axis="time", **{"from": "0"}, to="99999")
        self.assertIn("超出场次长度", str(caught.exception))

    def test_距离段按米换算成时刻(self):
        request = self._request(axis="distance", **{"from": "1200"}, to="1250")
        # 距离轴上 ``start`` / ``end`` 仍是**米**（范围是人填的那两个数），
        # 换算成时刻的是 ``t_start`` / ``t_end``——这一条把两个坐标都钉住，
        # 免得哪天有人把米当秒写进窗口还不报错。
        self.assertAlmostEqual(request.start, 1200.0, places=6)
        self.assertAlmostEqual(request.end, 1250.0, places=6)
        expected = lapsmod.time_at_distance(self.log, 1200.0)
        self.assertIsNotNone(expected)
        self.assertAlmostEqual(request.t_start, expected, places=6)
        self.assertGreater(request.t_end, request.t_start)
        self.assertTrue(exportmod.plan(self.log, request)["rows"] > 0)


class TestExportRanges(_ExportBase):
    """范围 → 数据：闭区间、绝对时间、距离轴（ticket #23）。"""

    def test_auto_wide_keeps_raw_samples_and_is_inclusive(self):
        """``rate=auto`` 不重采样：12.50 s 与 18.00 s 那两个样本都要在结果里。"""
        request = self.request(**{"from": "12.5", "to": "18.0"})
        path = self.tmp() / "a.csv"
        stats = exportmod.write(self.log, request, path)
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        header, rows = lines[0], lines[1:]
        self.assertEqual(header, "time_s,Vx KF [km/h]")
        self.assertEqual(stats["rows"], len(rows))
        values = self.log.values(self.log.channel("Vx KF"))
        expected = values[1250:1801]  # 左闭右闭：12.50 与 18.00 都在里面
        self.assertEqual(len(rows), expected.size)
        self.assertAlmostEqual(float(rows[0].split(",")[0]), 12.5, places=9)
        self.assertAlmostEqual(float(rows[-1].split(",")[0]), 18.0, places=9)
        for row, want in zip(rows, expected):
            self.assertAlmostEqual(float(row.split(",")[1]), float(want), places=6)

    def test_absolute_time_equals_relative_seconds(self):
        """绝对时间与相对秒指的是同一段：两份文件应当一个字节不差。"""
        from datetime import datetime, timedelta

        epoch = exportmod.epoch_of(self.log)
        self.assertIsNotNone(epoch, "这个场次的头里应该有日期时间")
        start = datetime.fromtimestamp(epoch) + timedelta(seconds=12.5)
        end = datetime.fromtimestamp(epoch) + timedelta(seconds=18.0)
        relative = self.request(**{"from": "12.5", "to": "18.0"})
        absolute = self.request(
            absolute="1",
            **{"from": start.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
               "to": end.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]},
        )
        self.assertAlmostEqual(absolute.start, relative.start, places=3)
        self.assertAlmostEqual(absolute.end, relative.end, places=3)
        one, two = self.tmp() / "r.csv", self.tmp() / "a.csv"
        exportmod.write(self.log, relative, one)
        exportmod.write(self.log, absolute, two)
        self.assertEqual(one.read_bytes(), two.read_bytes())

    def test_distance_axis_uses_metres(self):
        """距离轴：第一列是米、不倒退、范围夹在 1200–1250 之间。"""
        request = self.request(axis="distance", **{"from": "1200", "to": "1250"})
        path = self.tmp() / "d.csv"
        stats = exportmod.write(self.log, request, path)
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        self.assertTrue(lines[0].startswith("distance_m,"))
        distances = [float(row.split(",")[0]) for row in lines[1:]]
        self.assertEqual(len(distances), stats["rows"])
        self.assertGreaterEqual(min(distances), 1200.0 - 1e-6)
        self.assertLessEqual(max(distances), 1250.0 + 1e-6)
        self.assertTrue(all(b >= a for a, b in zip(distances, distances[1:])),
                        "距离轴不能倒退")


class TestExportSampling(_ExportBase):
    """采样与对齐：统一采样率、三种重采样、预计行数 = 实际行数（ticket #23）。"""

    def test_uniform_rate_gives_equal_length_columns(self):
        request = self.request(rate="10", **{"from": "0", "to": "10",
                                             "names": "Vx KF,Gear"})
        path = self.tmp() / "u.csv"
        stats = exportmod.write(self.log, request, path)
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(stats["rows"], 101)  # 0.0 … 10.0 每 0.1 秒一个点
        self.assertEqual(len(lines) - 1, 101)
        # 1 Hz 的 Gear 被拉到 10 Hz：每一格都有值，没有空的
        for row in lines[1:]:
            self.assertNotIn(",,", row)
            self.assertFalse(row.endswith(","))

    def test_resample_methods_differ_on_slow_channel(self):
        """``Gear`` 只有 1 Hz：线性插值与前值保持必须在同一时刻给不同的数。"""
        times = np.arange(0.0, 2.0, 0.1)
        linear = self.request(rate="10", **{"from": "0.2", "to": "1.2", "names": "Gear",
                                            "resample": "linear"})
        hold = self.request(rate="10", **{"from": "0.2", "to": "1.2", "names": "Gear",
                                          "resample": "hold"})
        one = exportmod._resampled(self.log, linear, "Gear", times)
        two = exportmod._resampled(self.log, hold, "Gear", times)
        values = self.log.values(self.log.channel("Gear"))
        self.assertAlmostEqual(float(one[0]), float(values[2]), places=6)  # 0.2 s 线性
        self.assertAlmostEqual(float(two[0]), float(values[0]), places=6)  # 0.2 s 前值

    def test_mean_only_changes_downsampling(self):
        """``mean`` 只对降采样有意义；升采样时与 ``linear`` 等价（契约原话）。"""
        times = np.arange(0.0, 1.0, 0.5)
        request = self.request(rate="2", names="Vx KF", resample="mean")
        mean = exportmod._resampled(self.log, request, "Vx KF", times)
        values = self.log.values(self.log.channel("Vx KF"))
        self.assertAlmostEqual(float(mean[1]), float(values[25:75].mean()), places=4)
        up = self.request(rate="200", names="Vx KF", resample="mean")
        grid = np.arange(0.0, 0.1, 0.005)
        mean_up = exportmod._resampled(self.log, up, "Vx KF", grid)
        linear_up = exportmod._resampled(
            self.log,
            self.request(rate="200", names="Vx KF", resample="linear"),
            "Vx KF",
            grid,
        )
        np.testing.assert_allclose(mean_up, linear_up, equal_nan=True)

    def test_plan_rows_match_actual_rows(self):
        """面板上的"预计行数"必须与真导出的行数一致（两种版式各验一遍）。"""
        for layout in ("wide", "long"):
            request = self.request(rate="20", layout=layout, **{"from": "5", "to": "15"})
            planned = exportmod.plan(self.log, request)
            path = self.tmp() / f"{layout}.csv"
            stats = exportmod.write(self.log, request, path)
            self.assertEqual(planned["rows"], stats["rows"],
                             f"{layout} 的预计行数对不上")


class TestExportFiles(_ExportBase):
    """落盘形状：BOM、zip 里的 metadata.json、长表、Excel（ticket #23）。"""

    def test_csv_is_utf8_with_bom_and_reads_back(self):
        import pandas as pd

        request = self.request(**{"from": "0", "to": "1"})
        path = self.tmp() / "bom.csv"
        exportmod.write(self.log, request, path)
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "CSV 要带 BOM，Excel 才不乱码")
        frame = pd.read_csv(path)
        self.assertEqual(list(frame.columns), ["time_s", "Vx KF [km/h]"])
        self.assertEqual(len(frame), 101)

    def test_bundle_carries_metadata_json(self):
        request = self.request(bundle="1", **{"from": "0", "to": "1"})
        path = self.tmp() / "b.zip"
        exportmod.write(self.log, request, path)
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            self.assertTrue(any(n.endswith(".csv") for n in names), names)
            self.assertIn("metadata.json", names)
            meta = json.loads(archive.read("metadata.json").decode("utf-8"))
        for key in ("file", "range", "rate", "resample", "channels", "exported_at", "rows"):
            self.assertIn(key, meta)
        self.assertEqual(meta["file"], self.SESSION.name)
        self.assertEqual(len(meta["channels"]), 1)
        self.assertEqual(meta["range"]["bounds"], "左闭右闭")

    def test_long_layout_only_writes_values(self):
        request = self.request(layout="long", **{"from": "0", "to": "5",
                                                 "names": "Vx KF,Gear"})
        path = self.tmp() / "l.csv"
        stats = exportmod.write(self.log, request, path)
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(lines[0], "time_s,channel,value,unit")
        self.assertEqual(len(lines) - 1, stats["rows"])
        gear = [row for row in lines[1:] if row.split(",")[1] == "Gear"]
        self.assertEqual(len(gear), 6)  # 0…5 秒，1 Hz
        self.assertFalse(any(row.split(",")[2] == "" for row in lines[1:]))

    def test_xlsx_round_trip(self):
        """Excel 的独立裁判是 openpyxl：把文件读回来逐格比对（不是自己读自己）。"""
        openpyxl = _openpyxl()
        request = self.request(format="xlsx", rate="50",
                               **{"from": "0", "to": "2", "names": "Vx KF,Gear"})
        path = self.tmp() / "x.xlsx"
        stats = exportmod.write(self.log, request, path)
        book = openpyxl.load_workbook(path)
        self.assertIn("元数据", book.sheetnames)
        self.assertIn("数据1", book.sheetnames)
        sheet = book["数据1"]
        self.assertEqual([cell.value for cell in sheet[1]],
                         ["time_s", "Vx KF [km/h]", "Gear"])
        self.assertAlmostEqual(sheet["A2"].value, 0.0, places=9)
        self.assertEqual(stats["rows"], sheet.max_row - 1)
        keys = [row[0].value for row in book["元数据"].iter_rows(min_row=2)]
        self.assertIn("日志文件", keys)
        self.assertIn("通道来源", keys)


class TestExportTempSweep(unittest.TestCase):
    """服务启动时清掉陈旧的导出临时目录。

    上一次服务被强杀会在系统临时目录里留下一个空的 `i3pro-export-*`；真浏览器验收里
    "临时目录没残留" 那两条因此会永远报红——一条永远红的断言等于没有断言。
    """

    def test_只清过时的那些(self):
        import time

        from i3pro import api as apimod

        with tempfile.TemporaryDirectory(prefix="i3pro-sweep-") as tmp:
            root = Path(tmp)
            old = root / (apimod.EXPORT_TMP_PREFIX + "old")
            fresh = root / (apimod.EXPORT_TMP_PREFIX + "fresh")
            old.mkdir()
            fresh.mkdir()
            (old / "half.csv").write_text("x", encoding="utf-8")
            stamped = time.time() - 7200
            os.utime(old, (stamped, stamped))
            removed = apimod.sweep_temp_exports(max_age_s=3600, root=root)
            # 断言要在临时目录还活着的时候做（退出 with 时整套都被删了）
            self.assertEqual([Path(p).name for p in removed], [old.name])
            self.assertFalse(old.exists(), "过时的导出临时目录没被清掉")
            self.assertTrue(fresh.exists(), "刚建的导出临时目录被误删了")


class TestExportErrors(_ExportBase):
    """坏输入说人话：每条报错都要带上"下一步改什么"（ticket #23）。"""

    def test_reversed_range_says_what_to_do(self):
        with self.assertRaises(exportmod.ExportError) as caught:
            self.request(**{"from": "18", "to": "12.5"})
        self.assertIn("from", str(caught.exception))

    def test_out_of_range_says_the_session_length(self):
        with self.assertRaises(exportmod.ExportError) as caught:
            self.request(**{"from": "0", "to": "99999"})
        self.assertIn("463", str(caught.exception))

    def test_empty_range_says_no_data(self):
        request = self.request(**{"from": "12.501", "to": "12.502", "rate": "auto"})
        with self.assertRaises(exportmod.ExportError) as caught:
            exportmod.plan(self.log, request)
        self.assertIn("无数据", str(caught.exception))

    def test_unknown_channel_names_the_first_one(self):
        with self.assertRaises(exportmod.ExportError) as caught:
            exportmod.parse_request(
                self.log, {"channels": "selected", "names": "No Such Channel"}
            )
        self.assertIn("No Such Channel", str(caught.exception))
        self.assertIn("info", str(caught.exception))

    def test_bad_enum_values_are_named(self):
        for params, needle in (
            ({"axis": "furlongs"}, "axis"),
            ({"format": "pdf"}, "format"),
            ({"layout": "tall"}, "layout"),
            ({"resample": "cubic"}, "resample"),
            ({"rate": "-5"}, "rate"),
        ):
            with self.assertRaises(exportmod.ExportError) as caught:
                self.request(**params)
            self.assertIn(needle, str(caught.exception))


class TestExportEstimate(_ExportBase):
    """「预计行数和文件大小」要对得上真文件（需求 §5）。

    预估不再靠「行数 × 列数 × 每格常数」那种一把尺子量到底的算法，而是**先按同一套格式
    真写前 200 行再外推**：原始采样模式下大部分格子是空的（比主时间基慢的通道只在少数行上
    有值），一把尺子会把 46400 行 × 438 列的表估成实际的两倍。这里真写一次文件，把
    「预估 ÷ 实际」锁在 0.75×–1.35× 之间——估算哪天被改坏，这条会红。
    """

    def test_预估与真文件在一个量级内(self):
        request = exportmod.parse_request(self.log, {
            "channels": "selected", "names": "Vx KF,G Force Lat,G Force Long",
            "from": "0", "to": "20", "rate": "10", "format": "csv", "layout": "wide",
        })
        planned = exportmod.plan(self.log, request)
        out = self.tmp() / "estimate.csv"
        stats = exportmod.write(self.log, request, out)
        self.assertEqual(planned["rows"], stats["rows"], "预估行数必须与真导出一致")
        ratio = planned["bytes"] / max(1, stats["bytes"])
        self.assertTrue(
            0.75 <= ratio <= 1.35,
            f"体积预估偏了 {ratio:.2f}×：预估 {planned['bytes']} B，实际 {stats['bytes']} B",
        )

    def test_原始采样这种稀疏表也要估得准(self):
        """全部通道 + 原始采样：大部分格子空着——这正是旧算法估成两倍的那种形状。"""
        request = exportmod.parse_request(self.log, {
            "channels": "all", "maths": "0", "from": "0", "to": "10",
            "rate": "auto", "format": "csv", "layout": "wide",
        })
        planned = exportmod.plan(self.log, request)
        stats = exportmod.write(self.log, request, self.tmp() / "sparse.csv")
        self.assertEqual(planned["rows"], stats["rows"])
        ratio = planned["bytes"] / max(1, stats["bytes"])
        self.assertTrue(
            0.75 <= ratio <= 1.35,
            f"稀疏宽表的体积预估偏了 {ratio:.2f}×："
            f"预估 {planned['bytes']} B，实际 {stats['bytes']} B",
        )


class TestExportTimestampIndex(_ExportBase):
    """主索引可以写成**绝对时间戳**（``index=timestamp``，ticket #27）。

    它是同一根时间轴的另一种写法：场次起点（``.ld`` 头里的日期时间，MoTeC 只写到秒）
    + 相对秒。三条边界钉在这里：只配时间轴、起点缺失时要说下一步、CSV 与 Excel
    两条出口写出来的必须是同一个字符串。
    """

    def stamp(self, **params):
        base = {
            "channels": "selected", "names": "Vx KF", "axis": "time",
            "index": "timestamp", "from": "10", "to": "10.2", "rate": "10",
        }
        base.update(params)
        return exportmod.parse_request(self.log, base)

    def test_时间戳列等于场次起点加相对秒(self):
        import datetime as _datetime

        request = self.stamp()
        out = self.tmp() / "stamp.csv"
        stats = exportmod.write(self.log, request, out)
        lines = out.read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(lines[0].split(",")[:2], ["timestamp", "Vx KF [km/h]"])
        epoch = exportmod.epoch_of(self.log)
        self.assertIsNotNone(epoch, "金标准场次的头里应该有日期时间")
        want = _datetime.datetime.fromtimestamp(epoch + 10.0).strftime("%Y-%m-%d %H:%M:%S.000")
        self.assertEqual(lines[1].split(",")[0], want)
        # 左闭右闭：10.0 / 10.1 / 10.2 三行，而且 plan 的行数与写出来的行数一致
        self.assertEqual(stats["rows"], 3)
        self.assertEqual(len(lines) - 1, stats["rows"])
        self.assertEqual(exportmod.plan(self.log, request)["rows"], stats["rows"])
        self.assertEqual(exportmod.plan(self.log, request)["index"], "timestamp")

    def test_长表首列就叫_timestamp(self):
        """需求里写的就是 ``timestamp, channel, value, unit``——长表的列名要照写。"""
        request = self.stamp(layout="long")
        out = self.tmp() / "stamp_long.csv"
        stats = exportmod.write(self.log, request, out)
        lines = out.read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(lines[0], "timestamp,channel,value,unit")
        self.assertEqual(stats["rows"], 3)
        self.assertEqual(len(lines) - 1, stats["rows"])
        self.assertTrue(lines[1].startswith("2026-09-08 15:48:32."), lines[1])

    def test_元数据写明时间戳的精度(self):
        """起点只有秒精度，这条必须写在元数据里，别让队友当成微秒级时钟。"""
        meta = exportmod.metadata(self.log, self.stamp(), 3, 2)
        self.assertEqual(meta["index"], "timestamp")
        self.assertIn("起点精确到秒", meta["timestamp"]["source_precision"])
        self.assertIsNotNone(meta["timestamp"]["epoch"])
        keys = [str(row[0]) for row in exportmod._metadata_rows(meta)]
        self.assertIn("时间戳列", keys)
        # 不选时间戳时不该出现这一段（免得元数据里写着不存在的东西）
        plain = exportmod.metadata(self.log, self.stamp(index="time_s"), 3, 2)
        self.assertNotIn("timestamp", plain)

    def test_距离轴配时间戳是参数错不是回退(self):
        with self.assertRaises(exportmod.ExportError) as caught:
            exportmod.parse_request(
                self.log, {"channels": "all", "axis": "distance", "index": "timestamp"}
            )
        message = str(caught.exception)
        self.assertIn("distance_m", message)
        self.assertIn("axis=time", message)

    def test_时间轴的主索引不许写_distance_m(self):
        with self.assertRaises(exportmod.ExportError) as caught:
            self.stamp(index="distance_m")
        self.assertIn("axis=distance", str(caught.exception))

    def test_没有日期时间的场次写不出时间戳(self):
        import types

        fake = types.SimpleNamespace(log_date="", log_time="")
        with self.assertRaises(exportmod.ExportError) as caught:
            exportmod._stamp_texts(fake, None, np.array([0.0]))
        message = str(caught.exception)
        self.assertIn("日期时间", message)
        self.assertIn("time_s", message)

    def test_excel_里也是文本时间戳而不是数字(self):
        """Excel 出口走的是另一条写行代码（``_wide_row_lists``），单独钉一次。"""
        openpyxl = _openpyxl()
        request = self.stamp(format="xlsx")
        out = self.tmp() / "stamp.xlsx"
        stats = exportmod.write(self.log, request, out)
        # 数据那张表带分表开关，所以名字是 数据1（元数据那张不带，才叫「元数据」）
        sheet = openpyxl.load_workbook(out)[stats["names"][-1]]
        self.assertEqual(sheet["A1"].value, "timestamp")
        self.assertEqual(sheet["A2"].value, "2026-09-08 15:48:32.000")
        self.assertIsInstance(sheet["B2"].value, (int, float))


def _openpyxl():
    """Excel 的**独立裁判**。开发机上没有它就跳过（规则 4 只豁免测试工具）。"""
    try:
        import openpyxl
    except ImportError:  # pragma: no cover - 开发机上有
        raise unittest.SkipTest("openpyxl 不在，xlsx 需要它当独立裁判") from None
    return openpyxl


class TestUploadEndpoint(unittest.TestCase):
    """浏览器上传这一步：``PUT /api/upload``（ticket #31 顺手修好的那条）。

    这之前**一条用例都没有**——于是 #26 把请求闭包拆成 ``api.py`` 那一层时，
    ``upload`` 里的 ``self.rfile`` 被留在了已经不存在的闭包作用域里，浏览器**上传
    一直 500**，而命令行的 ``i3pro import`` 照样好用，所以没人发现。这条用一份真的
    ``.xlsx`` 走一遍：落盘、进列表、概览三样都验，外加流式读的两种坏情况。
    """

    def setUp(self):
        from i3pro import api as apimod

        self.apimod = apimod
        self.work = scratch("_upload_work")
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir(parents=True)
        self.library = librarymod.SessionLibrary([self.work], cache_size=1, maths_root=ROOT)
        self.api = apimod.Api(self.library, 900)

    def tearDown(self):
        self.library.close()
        shutil.rmtree(self.work, ignore_errors=True)

    def source(self, name="源.xlsx"):
        """上传的源文件放在**数据目录之外**，不然"上传后才出现"这条就是空跑。"""
        directory = tempfile.TemporaryDirectory(prefix="i3pro-upload-src-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / name
        xlsxmod.write_workbook(path, [{
            "name": "数据", "header": ["time_s", "TH", "Vx KF [km/h]"],
            "rows": [[i / 100.0, i, i * 0.5] for i in range(50)], "split": False,
        }])
        return path

    def put(self, name, payload):
        return self.api.handle(["upload"], {"name": [name]}, "PUT",
                               self.apimod.Body.of(payload))

    def test_上传一份_xlsx_落盘并进侧边栏(self):
        payload = self.source().read_bytes()
        response = self.put("新场次.xlsx", payload)
        self.assertEqual(response.status, 200, response.body[:300])
        body = json.loads(response.body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["file"], "新场次.xlsx")
        self.assertEqual(body["bytes"], len(payload))
        self.assertEqual(body["device"], "Excel")
        self.assertEqual(body["channels"], 2)
        self.assertEqual(body["complete_laps"], 0)
        self.assertIn("url", body)
        self.assertEqual((self.work / "新场次.xlsx").read_bytes(), payload)
        self.assertIn("新场次", self.library.names())
        self.assertEqual(self.library.summary("新场次")["format"], "xlsx")

    def test_同名上传不覆盖(self):
        payload = self.source().read_bytes()
        self.put("新场次.xlsx", payload)
        second = self.put("新场次.xlsx", payload)
        self.assertEqual(json.loads(second.body)["file"], "新场次-1.xlsx")
        self.assertTrue((self.work / "新场次.xlsx").exists())
        self.assertTrue((self.work / "新场次-1.xlsx").exists())

    def test_不认识的扩展名被挡下来(self):
        response = self.put("坏文件.exe", b"MZ")
        self.assertEqual(response.status, 400)
        self.assertIn("只接受", json.loads(response.body)["error"])

    def test_上传中断要吵而且不留半截文件(self):
        chunks = [b"0123456789"]

        def reader(_n):
            return chunks.pop(0) if chunks else b""

        body = self.apimod.Body(100, reader=reader)
        response = self.api.handle(["upload"], {"name": ["半截.xlsx"]}, "PUT", body)
        self.assertEqual(response.status, 400)
        self.assertIn("中断", json.loads(response.body)["error"])
        self.assertEqual(list(self.work.iterdir()), [], "半截文件或 .part 留下来了")


class TestXlsxImport(_ExportBase):
    """读 Excel 成场次（ticket #31）。

    最硬的一条判据是 **round-trip**：同一份导出请求，CSV 与 xlsx 两种文件读回来
    必须给出同一串数字。它不需要新样例，却正好挡住"看着成功、名字或数值悄悄换了"
    那一类错——列名里的 ``[单位]`` 就是这么被抓出来的。
    """

    def book(self, name="表.xlsx", sheets=()):
        """写一份 xlsx 出来（用仓库自己的写入器，写侧另有独立裁判）。"""
        path = self.tmp() / name
        if sheets:
            xlsxmod.write_workbook(path, list(sheets))
        return path

    def simple(self, name="表.xlsx", header=("time_s", "Vx KF [km/h]", "TH"), rows=50):
        return self.book(name, [{
            "name": "数据", "header": list(header),
            "rows": iter([[i / 100.0, i * 0.5, i * 2.0] for i in range(rows)]),
            "split": False,
        }])

    # ------------------------------------------------------------ 读得回来
    def test_一份普通工作簿读成场次(self):
        session = csvlog.open_session(self.simple())
        meta = session.metadata()
        self.assertEqual(meta["format"], "xlsx")
        self.assertEqual(meta["sheet"], "数据")
        self.assertEqual(meta["device"], "Excel")
        self.assertEqual([c.name for c in session.channels], ["Vx KF", "TH"])
        self.assertEqual(session.channels[0].unit, "km/h")   # 列名里的 [单位] 拆回去了
        self.assertAlmostEqual(session.sample_rate, 100.0, places=6)
        np.testing.assert_allclose(session.values("Vx KF"), np.arange(50) * 0.5)
        np.testing.assert_allclose(session.values("TH"), np.arange(50) * 2.0)
        self.assertEqual(session.report[0]["status"], "时间轴")

    def test_硬判据_同一次导出的_csv_与_xlsx_读回来是同一串数字(self):
        import pandas as pd

        request = self.request(format="xlsx", rate="50",
                               **{"from": "0", "to": "2", "names": "Vx KF,Gear"})
        xlsx_path = self.tmp() / "x.xlsx"
        stats = exportmod.write(self.log, request, xlsx_path)
        csv_path = self.tmp() / "x.csv"
        exportmod.write(self.log, self.request(format="csv", rate="50",
                                               **{"from": "0", "to": "2",
                                                  "names": "Vx KF,Gear"}), csv_path)

        session = csvlog.open_session(xlsx_path)
        frame = pd.read_csv(csv_path)
        self.assertEqual(len(session.channels), 2)
        self.assertEqual([c.name for c in session.channels], ["Vx KF", "Gear"])
        self.assertEqual(session.channels[0].unit, "km/h")
        self.assertEqual(frame.shape[0], stats["rows"])
        self.assertEqual(session.time.size, stats["rows"])
        np.testing.assert_allclose(session.time, frame["time_s"].to_numpy(),
                                   rtol=0, atol=1e-6)
        np.testing.assert_allclose(session.values("Vx KF"),
                                   frame["Vx KF [km/h]"].to_numpy(), rtol=0, atol=1e-6)
        # 读回来的值对得上 `.ld` 本身：0.02 s 一个点，取第 50 个点（1.00 s）
        expected = self.log.values("Vx KF")[int(round(1.0 * self.log.sample_rate))]
        self.assertAlmostEqual(session.values("Vx KF")[50], expected, places=4)

    def test_csv_导出的方括号单位也拆得回去(self):
        """同一条规则对 CSV 与 Excel 一起生效（两条读取器共用一份装配代码）。"""
        request = self.request(format="csv", rate="50",
                               **{"from": "0", "to": "2", "names": "Vx KF,Gear"})
        path = self.tmp() / "x.csv"
        exportmod.write(self.log, request, path)
        session = csvlog.read_csv_session(path)
        self.assertEqual([c.name for c in session.channels], ["Vx KF", "Gear"])
        self.assertEqual(session.channels[0].unit, "km/h")

    def test_两份金标准导出的_xlsx_读回来逐点一致(self):
        """规则 ⑤：这条功能要在两份金标准上各实跑一次，不是只跑一份。"""
        measured = []
        for source in (HILL, ENDURANCE):
            if not source.exists():
                continue
            with ld.LogFile.read(source) as log:
                names = [name for name in ("Vx KF", "TH") if log.has(name)]
                request = exportmod.parse_request(log, {
                    "channels": "selected", "names": ",".join(names),
                    "format": "xlsx", "resample": "linear",
                    "rate": "50", "from": "60", "to": "80",
                })
                out = self.tmp() / (source.stem + ".xlsx")
                stats = exportmod.write(log, request, out)
                session = csvlog.open_session(out)
                self.assertEqual(session.time.size, stats["rows"], source.name)
                self.assertEqual([c.name for c in session.channels], names, source.name)
                axis = timebase.axis(log)
                for name in names:
                    expected = np.interp(session.time, axis, log.values(name))
                    np.testing.assert_allclose(
                        session.values(name), expected, rtol=0, atol=1e-6,
                        err_msg=f"{source.name} 的 {name} 读回来对不上",
                    )
                measured.append((source.stem, stats["rows"], len(names),
                                 int(out.stat().st_size)))
        self.assertTrue(measured, "两份金标准都不在，这条验不了")
        print("\n[#31 实测] " + "；".join(
            f"{stem} {rows} 行 × {cols} 通道 {size / 1024:.0f} KB"
            for stem, rows, cols, size in measured))

    def test_中间的空列或文字列不会把后面的列错位(self):
        """一列有表头、却没有数值（或写着文字）时，**名字与数值的对应关系不能挪**。

        旧写法把"整列都是 NaN"的列 drop 掉，后面每一列就往前挪一格：实测那份
        ``Time,A,Empty,B`` 里，叫 ``B`` 的通道拿到了 10/20/30，而 ``Empty`` 拿到了
        本该属于 B 的数字，报告里还写着 B「缺列」——一个错都不报，数字全配错了对。
        文字列在旧写法里则是抛一句 pandas 的 ``could not convert string to float``，
        没有下一步。
        """
        blank = self.tmp() / "blank.csv"
        blank.write_text("Time,A,Empty,B\n0,1,,10\n0.01,2,,20\n0.02,3,,30\n",
                         encoding="utf-8")
        session = csvlog.read_csv_session(blank)
        self.assertEqual([c.name for c in session.channels], ["A", "B"])
        np.testing.assert_allclose(session.values("A"), [1, 2, 3])
        np.testing.assert_allclose(session.values("B"), [10, 20, 30])
        self.assertEqual([row["column"] for row in session.report
                          if row.get("status", "").startswith("跳过")], ["Empty"])

        text = self.tmp() / "text.csv"
        text.write_text("Time,A,Note,B\n0,1,hello,10\n0.01,2,world,20\n0.02,3,x,30\n",
                        encoding="utf-8")
        session = csvlog.read_csv_session(text)
        self.assertEqual([c.name for c in session.channels], ["A", "B"])
        np.testing.assert_allclose(session.values("B"), [10, 20, 30])
        self.assertEqual([row["column"] for row in session.report
                          if row.get("status", "").startswith("跳过")], ["Note"])

    # ------------------------------------------------------------ 选哪张 sheet
    def test_默认挑第一张能当表读的_sheet(self):
        path = self.book("两页.xlsx", [
            {"name": "元数据", "header": ["项", "值"], "split": False,
             "rows": iter([["日志文件", "a.ld"], ["场次", "a"]])},
            {"name": "数据", "header": ["time_s", "TH"], "split": False,
             "rows": iter([[i / 100.0, i] for i in range(20)])},
        ])
        session = csvlog.open_session(path)
        self.assertEqual(session.metadata()["sheet"], "数据")
        self.assertEqual(session.header["sheets"], ["元数据", "数据"])
        with self.assertRaises(ValueError) as caught:
            csvlog.open_session(path, sheet="没这张")
        self.assertIn("没有叫", str(caught.exception))
        self.assertIn("数据", str(caught.exception))

    def test_选了哪张_sheet_记在侧车里_后面的列名覆盖不会把它抹掉(self):
        path = self.book("两页.xlsx", [
            {"name": "第一张", "header": ["time_s", "TH"], "split": False,
             "rows": iter([[i / 100.0, i] for i in range(20)])},
            {"name": "第二张", "header": ["time_s", "Vx KF [km/h]"], "split": False,
             "rows": iter([[i / 100.0, i] for i in range(20)])},
        ])
        self.assertEqual(csvlog.open_session(path).metadata()["sheet"], "第一张")
        csvlog.save_map(path, {}, {}, sheet="第二张")
        csvlog.save_map(path, {"TH": "节气门"}, {})          # 不带 sheet 的一次保存
        self.assertEqual(csvlog.load_sheet(path), "第二张")
        self.assertEqual(csvlog.load_map(path)["renames"], {"TH": "节气门"})
        self.assertEqual(csvlog.open_session(path).metadata()["sheet"], "第二张")

    # ------------------------------------------------------------ 读不了的形态
    def test_没有时间列就拒绝并说下一步(self):
        path = self.book("无时间.xlsx", [
            {"name": "数据", "header": ["转速", "油门"], "split": False,
             "rows": iter([[i, i * 2] for i in range(20)])},
        ])
        with self.assertRaises(ValueError) as caught:
            csvlog.open_session(path)
        message = str(caught.exception)
        self.assertIn("时间", message)
        self.assertIn("下一步", message)

    def test_宏_图表_外链各自报错并给下一步(self):
        for marker, label in (("xl/vbaProject.bin", "宏"),
                              ("xl/charts/chart1.xml", "图表"),
                              ("xl/externalLinks/externalLink1.xml", "外部链接")):
            path = self.simple(f"feat-{label}.xlsx")
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr(marker, "x")
            with self.assertRaises(ValueError) as caught:
                csvlog.open_session(path)
            message = str(caught.exception)
            self.assertIn(label, message)
            self.assertIn("下一步", message)

    def test_公式没有缓存值就报错(self):
        """别的工具生成的表常常只有公式、没有结果；读出来会是一片空白。"""
        openpyxl = _openpyxl()
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "数据"
        sheet.append(["time_s", "TH", "两倍"])
        for i in range(20):
            sheet.append([i / 100.0, i, f"=B{i + 2}*2"])
        path = self.tmp() / "公式.xlsx"
        book.save(path)
        with self.assertRaises(ValueError) as caught:
            csvlog.open_session(path)
        self.assertIn("没有存结果", str(caught.exception))

    def test_不是_zip_就说是改了后缀(self):
        path = self.tmp() / "假的.xlsx"
        path.write_text("Time,TH\n0,1\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            csvlog.open_session(path)
        message = str(caught.exception)
        self.assertIn("不是一个 .xlsx", message)
        self.assertIn("另存为", message)

    def test_日期时间列折成相对秒并带出场次日期(self):
        import datetime

        openpyxl = _openpyxl()
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "数据"
        sheet.append(["timestamp", "TH"])
        start = datetime.datetime(2026, 9, 14, 12, 34, 56, 789000)
        for i in range(20):
            sheet.append([start + datetime.timedelta(seconds=i / 100.0), i])
        path = self.tmp() / "时间戳.xlsx"
        book.save(path)
        session = csvlog.open_session(path)
        self.assertAlmostEqual(session.time[0], 0.0, places=9)
        self.assertAlmostEqual(session.time[-1], 0.19, places=6)
        self.assertAlmostEqual(session.sample_rate, 100.0, places=3)
        meta = session.metadata()
        self.assertEqual(meta["log_date"], "2026-09-14")
        self.assertEqual(meta["log_time"], "12:34:56")
        self.assertEqual(session.report[0]["detail"], "日期时间列")

    # ------------------------------------------------------------ 进得来，进侧边栏
    def test_导入_xlsx_同名不覆盖且能进侧边栏(self):
        from i3pro import importer

        source = self.simple("场次.xlsx")
        work = scratch("_xlsx_library")
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        try:
            first = importer.import_paths([source], work)
            second = importer.import_paths([source], work)
            self.assertEqual(first[0]["file"], "场次.xlsx")
            self.assertEqual(second[0]["file"], "场次-1.xlsx")
            library = librarymod.SessionLibrary([work], cache_size=1, maths_root=ROOT)
            self.assertIn("场次", library.names())
            summary = library.summary("场次")
            self.assertEqual(summary["format"], "xlsx")
            log = library.get("场次")          # 全局数学通道也会挂上来，所以不比总数
            self.assertTrue(log.has("Vx KF"))
            self.assertTrue(log.has("TH"))
        finally:
            shutil.rmtree(work, ignore_errors=True)


class TestXlsxWriter(unittest.TestCase):
    """``src/i3pro/xlsx.py``：标准库写的 OOXML，由一个**独立**读入器逐格验。

    它自己的 docstring 说"验证它的是 openpyxl"，这个类就是那句话的出处。
    分表那一段把 ``MAX_ROWS`` 临时调到 100 来跑真代码路径——不造假数据等
    104 万行，那条路一样会走到。
    """

    def _book(self, path):
        return _openpyxl().load_workbook(path)

    def test_column_name_is_zero_based(self):
        self.assertEqual(
            [xlsxmod.column_name(i) for i in (0, 25, 26, 27, 701, 702)],
            ["A", "Z", "AA", "AB", "ZZ", "AAA"],
        )

    def test_round_trip_cells(self):
        directory = tempfile.TemporaryDirectory(prefix="i3pro-xlsx-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "one.xlsx"
        rows = [[0, 1.5, "文本", None], [10, np.nan, "带 <尖括号> & 和号", 2]]
        stats = xlsxmod.write_workbook(
            path, [{"name": "数据", "header": ["数字", "小数", "文字", "空"],
                    "rows": iter(rows), "split": False}]
        )
        self.assertEqual(stats, {"sheets": 1, "rows": 2, "names": ["数据"]})
        sheet = self._book(path)["数据"]
        self.assertEqual([cell.value for cell in sheet[1]],
                         ["数字", "小数", "文字", "空"])
        self.assertEqual(sheet["A2"].value, 0)
        self.assertEqual(sheet["B2"].value, 1.5)
        self.assertEqual(sheet["C2"].value, "文本")
        self.assertIsNone(sheet["D2"].value)          # None -> 空格
        self.assertEqual(sheet["C3"].value, "带 <尖括号> & 和号")
        self.assertIsNone(sheet["B3"].value)          # NaN -> 空格

    def test_splits_when_over_the_excel_row_limit(self):
        """真分表：99 行一张，300 行 → 数据1/2/3/4，一行都不丢。"""
        directory = tempfile.TemporaryDirectory(prefix="i3pro-xlsx-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "split.xlsx"
        rows = [[i, i * 2] for i in range(300)]
        original = xlsxmod.MAX_ROWS
        xlsxmod.MAX_ROWS = 100  # 每张表装 99 行数据 + 1 行表头
        self.addCleanup(setattr, xlsxmod, "MAX_ROWS", original)
        stats = xlsxmod.write_workbook(
            path, [{"name": "数据", "header": ["i", "两倍"], "rows": iter(rows),
                    "split": True}]
        )
        self.assertEqual(stats["sheets"], 4)
        self.assertEqual(stats["names"], ["数据1", "数据2", "数据3", "数据4"])
        self.assertEqual(stats["rows"], 300)
        book = self._book(path)
        seen = []
        for name, expected_rows in zip(stats["names"], (99, 99, 99, 3)):
            sheet = book[name]
            self.assertEqual(sheet.max_row, expected_rows + 1, f"{name} 少了表头或行")
            self.assertEqual(sheet["A1"].value, "i", f"{name} 的表头没了")
            seen.extend(sheet.cell(row=r, column=1).value
                        for r in range(2, sheet.max_row + 1))
        self.assertEqual(seen, list(range(300)), "分表之间丢了行或者重了行")

    def test_split_off_is_a_loud_error(self):
        """调用方关掉分表又超行：报错要说出"用 split=True"，而不是写个坏文件。"""
        directory = tempfile.TemporaryDirectory(prefix="i3pro-xlsx-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "bad.xlsx"
        original = xlsxmod.MAX_ROWS
        xlsxmod.MAX_ROWS = 10
        self.addCleanup(setattr, xlsxmod, "MAX_ROWS", original)
        with self.assertRaises(ValueError) as caught:
            xlsxmod.write_workbook(
                path, [{"name": "数据", "header": ["i"],
                        "rows": iter([[i] for i in range(50)]), "split": False}]
            )
        self.assertIn("split=True", str(caught.exception))

    def test_too_many_columns_says_use_csv(self):
        directory = tempfile.TemporaryDirectory(prefix="i3pro-xlsx-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "wide.xlsx"
        header = [f"c{i}" for i in range(xlsxmod.MAX_COLS + 1)]
        with self.assertRaises(ValueError) as caught:
            xlsxmod.write_workbook(
                path, [{"name": "数据", "header": header, "rows": iter([]),
                        "split": False}]
            )
        self.assertIn("CSV", str(caught.exception))


class TestExportPerformance(unittest.TestCase):
    """分块写是不是真的生效：窗口拉大、列数变多，峰值内存不许跟着线性涨。

    整场 343 列的实测数字写在 ``docs/ACCEPTANCE.md`` A45 里（那条命令是
    ``i3pro export``，可复制复现）；这里留一条几秒钟能跑完的守门用例，免得每次
    回归都要多等一分钟。
    """

    @_needs(ENDURANCE)
    def test_chunked_write_keeps_memory_bounded(self):
        import time
        import tracemalloc

        with ld.LogFile.read(ENDURANCE) as log:
            names = ",".join(ch.name for ch in log.channels[:40])
            request = exportmod.parse_request(
                log, {"channels": "selected", "names": names, "rate": "100",
                      "from": "600", "to": "800"}
            )
            planned = exportmod.plan(log, request)
            self.assertEqual(planned["rows"], 20001)
            with tempfile.TemporaryDirectory(prefix="i3pro-exp-") as tmp:
                tracemalloc.start()
                start = time.time()
                stats = exportmod.write(log, request, Path(tmp) / "all.csv")
                peak = tracemalloc.get_traced_memory()[1]
                tracemalloc.stop()
                spent = time.time() - start
            self.assertEqual(stats["rows"], planned["rows"])
            print(
                f"\n[#23 实测] 耐久正赛 200 s 窗口 × 40 通道 × rate=100："
                f"{spent:.1f} s，峰值分配 {peak / 1e6:.0f} MB，"
                f"{stats['bytes'] / 1e6:.1f} MB"
            )
            self.assertLess(peak, 300e6, "峰值内存超过 300 MB：分块写没生效")


class TestExportPanelSendsTheRightRequest(unittest.TestCase):
    """导出面板拼出来的参数，必须就是 ``export.parse_request`` 认得的那几个。

    这里只钉**参数名与语义**（面板 → 查询串）；取数与落盘由 Python 一侧的导出用例
    钉。真浏览器里点「导出」→ 落一个文件，由 ``tools/verify_clicks.py`` 负责
    （假 DOM 里 fetch 永远失败，验不了下载）。
    """

    DRIVER = ROOT / "tools" / "smoke_viewer.js"
    VIEWER = ROOT / "src" / "i3pro" / "web" / "viewer.html"

    def test_无头驱动里有一组导出面板断言(self):
        driver = self.DRIVER.read_text(encoding="utf-8")
        for needle in ("exportDlg", "exportURL", "applyExportPlan", "exportMoment",
                       "exportPreset", "exportFilename", "estimate=1"):
            self.assertIn(needle, driver,
                          f"tools/smoke_viewer.js 里少了 {needle!r}；"
                          f"面板的行为要有无头断言，不能只靠肉眼点。")

    def test_采样率档位两边一致(self):
        """界面那个下拉不是手写的：它由 JS 里那张表建出来，必须和 Python 那张一样。"""
        viewer = self.VIEWER.read_text(encoding="utf-8")
        match = re.search(r"const EXPORT_RATES = \[(.*?)\];", viewer)
        self.assertIsNotNone(match, "面板里没有 EXPORT_RATES 这张表")
        numbers = [float(x) for x in match.group(1).replace(" ", "").split(",") if x]
        self.assertEqual(numbers, list(exportmod.RATES),
                         "面板的采样率档位与 export.RATES 不一致：两边说的是同一件事")

    def test_面板选项与导出模块对齐(self):
        viewer = self.VIEWER.read_text(encoding="utf-8")
        for method in exportmod.RESAMPLE_METHODS:
            self.assertIn(f'value="{method}"', viewer,
                          f"面板少了重采样方法 {method!r}（export.RESAMPLE_METHODS 里有）")
        for value in (*exportmod.FORMATS, *exportmod.LAYOUTS, *exportmod.AXES):
            self.assertIn(f'value="{value}"', viewer, f"面板少了 {value!r} 这个选项")


def _gps_session(path="fake.ld", rate: float = 10.0, gps_rate: float | None = None,
                 speed: float | None = None):
    """一条合成的 GPS 轨迹，好坏点都摆明：

    * 开头 10 点是 (0, 0)——掉星时记录仪真给这个，不能当成位置；
    * 中间一段正常走直线；
    * 第 100 个采样**整体跳到 1.1 km 外并留在那**（真数据里 214~621 m 的那类）；
    * 第 120~139 点又是 (0, 0)，也就是 2 秒空档。
    """
    n = 200
    gps_rate = rate if gps_rate is None else gps_rate
    lat = np.full(n, 22.6, dtype=np.float64)
    lon = np.full(n, 114.0, dtype=np.float64)
    lat[0:10] = 0.0
    lon[0:10] = 0.0
    for i in range(10, 100):
        lat[i] = 22.6 + (i - 10) * 1e-6
        lon[i] = 114.0 + (i - 10) * 1e-6
    for i in range(100, 120):
        lat[i] = 22.61 + (i - 100) * 1e-6
        lon[i] = 114.01 + (i - 100) * 1e-6
    lat[120:140] = 0.0
    lon[120:140] = 0.0
    for i in range(140, n):
        lat[i] = 22.61 + (i - 140) * 1e-6
        lon[i] = 114.01 + (i - 140) * 1e-6
    columns: dict[str, np.ndarray] = {"GPS Latitude": lat, "GPS Longitude": lon}
    if speed is not None:
        columns["Vx KF"] = np.full(n, float(speed))
    return _MathSession(
        columns,
        rate=rate,
        path=path,
        rates={"GPS Latitude": gps_rate, "GPS Longitude": gps_rate},
    )


class TestGpsFix(unittest.TestCase):
    """#14 GPS 校正：坏定位被标出来，修正只在开关打开时动数值。"""

    def test_bad_fix_is_dropped_not_used_as_origin(self):
        log = _gps_session()
        track = derive.gps_track(log)
        self.assertEqual(track["dropped"]["no_fix"], 30, "(0,0) 的点要计数")
        self.assertEqual(track["dropped"]["total"], 200)
        # 一个 (0,0) 都不能留在轨迹里：留下的都是 22.6°N / 114°E 附近
        self.assertGreater(float(np.min(np.abs(track["lat"]))), 20.0)
        self.assertGreater(float(np.min(np.abs(track["lon"]))), 100.0)

    def test_jump_and_hole_break_the_line(self):
        track = derive.gps_track(_gps_session())
        breaks = np.flatnonzero(track["breaks"]).tolist()
        # 170 个保留点：跳点在保留后的第 90 个，空档在第 110 个
        self.assertEqual(track["time"].size, 170)
        self.assertEqual(breaks, [90, 110])
        self.assertEqual(track["segments"], 3)
        self.assertEqual(len(track["jump_rows"]), 1)
        self.assertGreater(track["jump_rows"][0]["meters"], 1000.0, "1.1 km 的那一跳")
        self.assertEqual(len(track["holes"]), 1)
        self.assertAlmostEqual(track["holes"][0]["seconds"], 2.1, places=6)
        # 跳变的两端都算坏点：说不好哪一边错
        self.assertEqual(np.flatnonzero(track["jumps"]).tolist(), [89, 90])

    def test_off_means_identical_numbers(self):
        """关闭校正 = 数值逐点不变；只是多了几个标注字段。"""
        log = _gps_session(speed=36.0)
        base = derive.gps_track(log)
        off = derive.gps_track(log, fix=gpsfix.FixConfig(enabled=False))
        for key in ("time", "x", "y", "lat", "lon"):
            self.assertTrue(np.array_equal(base[key], off[key]), key)
        # 距离轴也不许动：关闭时仍是速度积分，而不是 GPS 路径长度
        self.assertTrue(
            np.array_equal(
                derive.distance_series(log),
                derive.distance_series(log, fix=gpsfix.FixConfig(enabled=False)),
            )
        )

    def test_offset_in_seconds_and_in_update_periods(self):
        log = _gps_session(rate=10.0)
        base = derive.gps_track(log)
        by_seconds = derive.gps_track(log, fix=gpsfix.FixConfig(enabled=True, offset_s=0.5))
        self.assertAlmostEqual(
            float(by_seconds["time"][0] - base["time"][0]), 0.5, places=9
        )
        # 2 个更新周期 @10 Hz = 0.2 s：换场次采样率变了也不用重算秒数
        by_ratio = derive.gps_track(
            log, fix=gpsfix.FixConfig(enabled=True, offset_ratio=2.0)
        )
        self.assertAlmostEqual(
            float(by_ratio["time"][0] - base["time"][0]), 0.2, places=9
        )

    def test_resample_never_bridges_a_hole(self):
        # 主采样 100 Hz、GPS 只有 10 Hz——这才是真数据的形状（C125 的 GPS 是 20/50 Hz）
        log = _gps_session(rate=100.0, gps_rate=10.0)
        fixed = derive.gps_track(
            log, fix=gpsfix.FixConfig(enabled=True, resample=True)
        )
        self.assertGreater(fixed["time"].size, 170, "10 Hz -> 100 Hz 应该更密")
        self.assertEqual(fixed["fix"]["samples_before"], 170)
        self.assertEqual(fixed["fix"]["samples_after"], fixed["time"].size)
        breaks = np.flatnonzero(fixed["breaks"])
        # 3 段 -> 2 个接缝，接缝处必须断开（插值绝不跨过空档）
        self.assertGreaterEqual(breaks.size, 2)
        # 空档里不许有点：两段之间的时间差仍然是那 2.1 秒
        gaps = np.diff(fixed["time"])
        self.assertAlmostEqual(float(gaps.max()), 2.1, places=3)

    def test_path_distance_skips_jumps_and_holes(self):
        track = derive.gps_track(_gps_session())
        distance = gpsfix.path_distance(
            track["time"], track["x"], track["y"], track["breaks"]
        )
        # 1.1 km 的那一跳不能进距离：整段路只有 ~180 m
        self.assertLess(float(distance[-1]), 500.0)
        self.assertTrue(np.all(np.diff(distance) >= 0), "里程只能单调不减")

    def test_distance_scope_switches_the_axis(self):
        log = _gps_session(speed=36.0)
        plain = derive.distance_series(log)
        gps_axis = derive.distance_series(
            log, fix=gpsfix.FixConfig(enabled=True, scope_distance=True)
        )
        self.assertTrue(np.all(np.diff(gps_axis) >= -1e-9), "距离轴要单调")
        self.assertTrue(np.all(np.diff(plain) >= -1e-9), "速度积分的距离轴也单调")
        # 36 km/h 走 19.9 s ≈ 199 m；GPS 路径只有那三段直线 ≈ 31 m。
        # 两个基准必须明显不同，否则这条测试什么也没证明。
        self.assertGreater(float(plain[-1]), 150.0)
        self.assertLess(float(gps_axis[-1]), 60.0)
        # 作用域关着 = 一个数都不动
        off = derive.distance_series(
            log, fix=gpsfix.FixConfig(enabled=True, scope_distance=False)
        )
        self.assertTrue(np.array_equal(plain, off))

    def test_scope_decides_who_gets_the_correction(self):
        log = _gps_session()
        laps_only = gpsfix.FixConfig(enabled=True, scope_track=False, scope_laps=True)
        self.assertFalse(gpsfix.resolve(log, laps_only, "track").enabled)
        self.assertTrue(gpsfix.resolve(log, laps_only, "laps").enabled)
        self.assertFalse(gpsfix.resolve(log, laps_only, "distance").enabled)
        self.assertFalse(gpsfix.resolve(log, None, "track").enabled, "没侧车就是不校正")

    def test_config_validation_speaks_chinese(self):
        with self.assertRaises(ValueError) as caught:
            gpsfix.FixConfig.from_dict({"offset_s": "快点"})
        self.assertIn("gps.offset_s", str(caught.exception))
        self.assertIn("秒", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            gpsfix.FixConfig.from_dict({"spike_kmh": 1e9})
        self.assertIn("gps.spike_kmh", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            gpsfix.FixConfig.from_dict(["not", "an", "object"])
        self.assertIn("JSON 对象", str(caught.exception))
        config = gpsfix.FixConfig.from_dict({"enabled": True, "offset_s": 0.25})
        self.assertTrue(config.enabled)
        self.assertEqual(config.scope_distance, False, "距离轴默认不动")

    def test_sidecar_roundtrip_and_corrupt_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "某场次.ld"
            self.assertFalse(gpsfix.config_path(session).exists())
            self.assertIsNone(gpsfix.load_config(session))
            config = gpsfix.FixConfig(enabled=True, offset_s=0.5, spike_kmh=800.0)
            path = gpsfix.save_config(session, config)
            self.assertEqual(path.name, "某场次.gps.json")
            self.assertEqual(gpsfix.load_config(session), config)
            # 读路径遇到坏 JSON：当没设过，而不是让工作台打不开
            path.write_text("{ 这不是 JSON", encoding="utf-8")
            with self.assertRaises(ValueError):
                gpsfix.load_config(session)
            log = _MathSession({"a": [0.0, 1.0]}, path=session)
            self.assertFalse(gpsfix.for_log(log).enabled)

    def test_summary_counts_what_is_wrong(self):
        track = derive.gps_track(_gps_session())
        info = gpsfix.summary(track)
        self.assertEqual(info["no_fix"], 30)
        self.assertEqual(info["jumps"], 1)
        self.assertEqual(info["holes"], 1)
        self.assertEqual(info["segments"], 3)
        self.assertGreater(info["worst_jump_m"], 1000.0)
        self.assertAlmostEqual(info["longest_hole_s"], 2.1, places=6)

    @_needs(ENDURANCE)
    def test_golden_endurance_spike_is_flagged(self):
        """真数据：耐久正赛结尾有一次 214 m 的错位定位。"""
        log = ld.LogFile.read(ENDURANCE)
        try:
            track = derive.gps_track(log)
            self.assertEqual(len(track["jump_rows"]), 1)
            self.assertAlmostEqual(track["jump_rows"][0]["meters"], 214.5, delta=0.5)
            self.assertEqual(track["dropped"]["no_fix"], 0)
            payload = render.track_payload(log)
            self.assertEqual(len(payload["breaks"]), 1, "抽稀之后那一跳还得断着")
            # 断的必须是**那 214 m 的幽灵线**，不是它前面那 0.2 m 的正常段：
            # 抽稀把断点错算到桶首，真机上就会把这条 214 m 直线画出来（截图抓到过）
            index = payload["breaks"][0]
            xs, ys = np.asarray(payload["x"]), np.asarray(payload["y"])
            phantom = float(np.hypot(xs[index] - xs[index - 1], ys[index] - ys[index - 1]))
            self.assertGreater(phantom, 200.0, "被断开的那一段应该就是幽灵线")
        finally:
            log.close()

    def test_downsample_keeps_the_break_on_the_right_segment(self):
        """抽稀：源下标 ``i`` 上的断点属于**第 (i-1)//step 段**。"""
        flags = np.zeros(120, dtype=bool)
        flags[51] = True
        # 25 个采样一个点 -> 断点落在第 2 段（抽稀下标 2 -> 3）上
        self.assertEqual(
            np.flatnonzero(render._downsample_breaks(flags, 25)).tolist(), [3]
        )
        flags = np.zeros(120, dtype=bool)
        flags[50] = True
        self.assertEqual(
            np.flatnonzero(render._downsample_breaks(flags, 25)).tolist(), [2]
        )
        # 不断开时一个都不该有；step<=1（没抽稀）原样返回
        self.assertEqual(render._downsample_breaks(np.zeros(120, dtype=bool), 25).sum(), 0)
        raw = np.zeros(10, dtype=bool)
        raw[4] = True
        self.assertEqual(render._downsample_breaks(raw, 1).tolist(), raw.tolist())

    @_needs(HILL)
    def test_golden_hill_has_no_jumps(self):
        """反例：高避 5 圈一个跳点都没有——阈值不是"总有东西可报"。"""
        log = ld.LogFile.read(HILL)
        try:
            track = derive.gps_track(log)
            self.assertEqual(track["jump_rows"], [])
            self.assertEqual(track["dropped"]["no_fix"], 638)
            # 开头那 12.76 秒没定位，它不算"空档"——轨迹就是从拿到定位那一刻开始的
            self.assertEqual(track["holes"], [])
            self.assertAlmostEqual(float(track["time"][0]), 12.76, delta=0.02)
            self.assertEqual(track["segments"], 1)
        finally:
            log.close()


class TestGpsFixOverHttp(unittest.TestCase):
    """#14 走到界面之前的那一段：/gps 的 GET / PUT、侧车、以及"关了就别动数"。"""

    @_needs(HILL)
    def test_gps_endpoint(self):
        with http_session(HILL, buckets=50) as http:
            root, copy, quoted = http.root, http.copy, http.quoted
            original = http.before
            request = http.json

            try:
                status, body = request(f"/api/session/{quoted}/gps")
                self.assertEqual(status, 200, body)
                self.assertFalse(body["config"]["enabled"], "没存过侧车就是不校正")
                self.assertFalse(body["stored"])
                self.assertEqual(body["summary"]["no_fix"], 638)
                self.assertEqual(body["summary"]["jumps"], 0)
                self.assertIsNone(body["applied"])

                # 关闭状态下的轨迹：PUT 前后必须逐点一致（这是 #14 的硬条件）
                status, before = request(f"/api/session/{quoted}/track?points=400")
                self.assertEqual(status, 200, before)
                status, body = request(
                    f"/api/session/{quoted}/gps", "PUT",
                    {"config": {"enabled": False, "offset_s": 0.0}},
                )
                self.assertEqual(status, 200, body)
                self.assertEqual(body["saved"], f"{copy.stem}.gps.json")
                status, after = request(f"/api/session/{quoted}/track?points=400")
                self.assertEqual(before["x"], after["x"])
                self.assertEqual(before["time"], after["time"])
                self.assertEqual(before["breaks"], after["breaks"])

                # 打开校正 + 时间偏移：轨迹的时刻整体平移，配置落进侧车
                status, body = request(
                    f"/api/session/{quoted}/gps", "PUT",
                    {"config": {"enabled": True, "offset_s": 5.0, "resample": True,
                                "scope_track": True, "scope_laps": True}},
                )
                self.assertEqual(status, 200, body)
                self.assertTrue(body["config"]["enabled"])
                self.assertEqual(body["applied"]["offset_s"], 5.0)
                self.assertTrue(body["applied"]["resampled"])
                on_disk = json.loads(
                    (root / f"{copy.stem}.gps.json").read_text(encoding="utf-8")
                )
                self.assertEqual(on_disk["offset_s"], 5.0)
                status, track = request(f"/api/session/{quoted}/track?points=400")
                self.assertTrue(track["fix"]["enabled"])
                self.assertAlmostEqual(
                    track["time"][0] - before["time"][0], 5.0, delta=1e-6
                )

                # 参数不合规：400，而且侧车一个字节都不许动
                status, body = request(
                    f"/api/session/{quoted}/gps", "PUT",
                    {"config": {"enabled": True, "offset_s": 999}},
                )
                self.assertEqual(status, 400, body)
                self.assertIn("gps.offset_s", body["error"])
                self.assertEqual(
                    json.loads(
                        (root / f"{copy.stem}.gps.json").read_text("utf-8")
                    )["offset_s"],
                    5.0,
                )
                self.assertEqual(copy.read_bytes(), original, ".ld 是只读的")

                # 关掉校正：又回到"一个数都不动"
                status, body = request(
                    f"/api/session/{quoted}/gps", "PUT", {"config": {"enabled": False}}
                )
                self.assertEqual(status, 200, body)
                status, track = request(f"/api/session/{quoted}/track?points=400")
                self.assertEqual(track["time"], before["time"])
                self.assertEqual(track["x"], before["x"])
            finally:
                http.close()


class TestHistogram(unittest.TestCase):
    """#9 直方图：数的是原始样本，被排除的样本要有个说法。"""

    def test_counts_add_up_and_edges_are_strictly_increasing(self):
        from i3pro import histogram as hist

        values = np.array([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        out = hist.histogram(values, count=4)
        self.assertEqual(len(out["bins"]), 4)
        self.assertEqual(sum(box["count"] for box in out["bins"]), 8)
        self.assertEqual(out["count"], 8)
        self.assertEqual(out["range"], [0.0, 7.0])
        edges = [out["bins"][0]["lo"]] + [box["hi"] for box in out["bins"]]
        self.assertTrue(all(b > a for a, b in zip(edges, edges[1:])),
                        f"分箱边界必须严格递增: {edges}")
        # 每格必须首尾相接：中间留缝会让柱子看起来少了一截
        for first, second in zip(out["bins"], out["bins"][1:]):
            self.assertEqual(first["hi"], second["lo"])

    def test_gate_nonzero_drops_the_zeros(self):
        from i3pro import histogram as hist

        values = np.array([1.0, 2.0, 3.0, 4.0])
        gate = np.array([0.0, 1.0, 0.0, 5.0])
        out = hist.histogram(values, count=2, gate=gate)
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["excluded"], 2)
        self.assertIn("排除 2 个样本", out["notice"])
        # 非有限数不算通过：NaN 不能当成"非零"溜进来
        out = hist.histogram(values, count=2, gate=np.array([np.nan, 1.0, 1.0, 1.0]))
        self.assertEqual(out["excluded"], 1)

    def test_gate_range_and_outside_modes(self):
        from i3pro import histogram as hist

        values = np.arange(6.0)
        gate = np.array([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
        inside = hist.histogram(values, count=3, gate=gate, gate_mode="range",
                                gate_lo=1.0, gate_hi=3.0)
        self.assertEqual(inside["count"], 3)
        outside = hist.histogram(values, count=3, gate=gate, gate_mode="outside",
                                 gate_lo=1.0, gate_hi=3.0)
        self.assertEqual(outside["count"], 3)
        with self.assertRaises(ValueError) as caught:
            hist.histogram(values, count=3, gate=gate, gate_mode="range")
        self.assertIn("gate_min", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            hist.gate_keeps(gate, "差不多就行")
        self.assertIn("门槛模式只认", str(caught.exception))

    def test_colour_is_the_mean_of_that_box(self):
        from i3pro import histogram as hist

        values = np.array([0.0, 0.0, 10.0, 10.0])
        colour = np.array([1.0, 3.0, 10.0, 20.0])
        bins = hist.histogram(values, count=4, colour=colour)["bins"]
        self.assertEqual(bins[0]["colour_mean"], 2.0)
        self.assertEqual(bins[3]["colour_mean"], 15.0)
        self.assertEqual(bins[0]["colour_min"], 1.0)
        self.assertEqual(bins[3]["colour_max"], 20.0)
        self.assertEqual(bins[0]["colour_count"], 2)
        # 空箱必须给 None，而不是 0——0 会被画成一个真实的颜色
        self.assertIsNone(bins[1]["colour_mean"])
        # 没有色值的样本不进那一格的颜色统计（NaN 在 colour_stats 里被丢掉）
        lonely = hist.histogram(np.array([5.0, 5.0]), count=4,
                                colour=np.array([1.0, np.nan]))["bins"]
        self.assertEqual(sum(box["count"] for box in lonely), 2)
        self.assertEqual(sum(box["colour_count"] for box in lonely), 1)

    def test_a_constant_channel_still_gets_a_real_range(self):
        """整段同一个值：柱子必须落在那个值上，不能塌到 0 附近。"""
        from i3pro import histogram as hist

        out = hist.histogram(np.full(100, 1000.0), count=4)
        self.assertEqual(out["count"], 100)
        self.assertLess(out["range"][0], 1000.0)
        self.assertGreater(out["range"][1], 1000.0)
        self.assertLess(abs(out["range"][0] - 1000.0), 10.0,
                        f"撑开的范围不能离真实值太远: {out['range']}")
        self.assertEqual(out["stats"]["min"], out["stats"]["max"])

    def test_nan_samples_are_reported_not_counted(self):
        from i3pro import histogram as hist

        out = hist.histogram(np.array([1.0, np.nan, 2.0]), count=4)
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["skipped"], 1)
        self.assertIn("NaN", out["notice"])

    def test_bins_are_clamped_and_said_out_loud(self):
        from i3pro import histogram as hist

        low = hist.histogram(np.arange(10.0), count=0)
        self.assertEqual(len(low["bins"]), hist.MIN_BINS)
        self.assertIn(str(hist.MIN_BINS), low["notice"])
        high = hist.histogram(np.arange(10.0), count=100000)
        self.assertEqual(len(high["bins"]), hist.MAX_BINS)
        self.assertIn(str(hist.MAX_BINS), high["notice"])

    @_needs(HILL)
    def test_golden_session_window_stats(self):
        from i3pro import histogram as hist

        with ld.LogFile.read(HILL) as log:
            time = timebase.axis(log)
            out = render.histogram(log, "Vx KF", time, bins=10, start=100.0, end=200.0,
                                   colour="G Force Lat")
            self.assertEqual(out["unit"], "km/h")
            self.assertEqual(out["count"], 10000)          # 100 Hz × 100 s
            self.assertEqual(out["excluded"], 0)
            self.assertEqual(out["window"], [100.0, 199.99])
            self.assertAlmostEqual(out["stats"]["max"], 87.41, places=2)
            self.assertEqual(sum(box["count"] for box in out["bins"]), out["count"])
            self.assertEqual(out["colour_channel"], "G Force Lat")
            self.assertTrue(all(box["colour_mean"] is not None for box in out["bins"]
                                if box["count"]), "有色值的格子必须给出均值")

    @_needs(HILL)
    def test_gate_accepts_a_channel_or_a_maths_expression(self):
        from i3pro import histogram as hist

        with ld.LogFile.read(HILL) as log:
            time = timebase.axis(log)
            by_channel = render.histogram(log, "Brake Signal", time, bins=8, gate="Vx KF")
            self.assertGreater(by_channel["excluded"], 0)
            self.assertLessEqual(by_channel["count"] + by_channel["excluded"], time.size)
            # 表达式门槛走的是数学通道那套：这里用"车速大于 40"筛
            by_expr = render.histogram(log, "Brake Signal", time, bins=8,
                                       gate="'Vx KF' > 40")
            manual = derive.hold_to_master(log, "Vx KF")[: time.size] > 40
            brake = derive.hold_to_master(log, "Brake Signal")[: time.size]
            self.assertEqual(by_expr["count"], int(np.sum(manual & np.isfinite(brake))))

    @_needs(HILL)
    def test_bad_input_says_what_to_do_next(self):
        from i3pro import histogram as hist

        with ld.LogFile.read(HILL) as toolong:
            time = timebase.axis(toolong)
            with self.assertRaises(ValueError) as caught:
                render.histogram(toolong, "根本没有这条通道", time, bins=8)
            self.assertIn("先", str(caught.exception))
            with self.assertRaises(ValueError) as caught:
                render.histogram(toolong, "Vx KF", time, bins=8, gate="nosuchfunc(1)")
            self.assertIn("数学通道", str(caught.exception))
            with self.assertRaises(ValueError) as caught:
                render.histogram(toolong, "Vx KF", time, bins=8, colour="不存在的色通道")
            self.assertIn("色", str(caught.exception))
            # 空窗口不是"分布是零"，要提示换一段
            empty = render.histogram(toolong, "Vx KF", time, bins=8, start=50.0, end=50.0)
            self.assertEqual(empty["count"], 0)
            self.assertIn("区间", empty["notice"])
            self.assertEqual(len(hist.summarize([])), 7)


class TestHistogramOverHttp(unittest.TestCase):
    """#9 走到界面之前的那一段：/histogram 的参数、单位与报错。"""

    @_needs(HILL)
    def test_histogram_endpoint(self):
        with http_session(HILL, buckets=50) as http:
            quoted = http.quoted
            get = http.get

            try:
                status, body = get(f"/api/session/{quoted}/histogram"
                                   f"?channel={urllib.parse.quote('Vx KF')}&bins=8"
                                   f"&from=100&to=200&colour={urllib.parse.quote('G Force Lat')}")
                self.assertEqual(status, 200, body)
                self.assertEqual(len(body["bins"]), 8)
                self.assertEqual(body["count"], 10000)
                self.assertEqual(body["unit"], "km/h")
                self.assertEqual(body["colour_channel"], "G Force Lat")
                self.assertIsNotNone(body["bins"][0]["colour_mean"])

                status, body = get(f"/api/session/{quoted}/histogram"
                                   f"?channel={urllib.parse.quote('Brake Signal')}"
                                   f"&gate={urllib.parse.quote('Vx KF > 40')}")
                self.assertEqual(status, 200, body)
                self.assertGreater(body["excluded"], 0)

                status, body = get(f"/api/session/{quoted}/histogram")
                self.assertEqual(status, 400)
                self.assertIn("channel=", body["error"])

                status, body = get(f"/api/session/{quoted}/histogram"
                                   f"?channel={urllib.parse.quote('没有这条')}")
                self.assertEqual(status, 400)
                self.assertIn("先", body["error"])
            finally:
                http.close()


class TestSpectrum(unittest.TestCase):
    """#10 频谱：Welch 平均周期图，按通道自己的采样率算。"""

    FS = 100.0

    @staticmethod
    def _sine(freq, seconds=100.0, fs=100.0, amplitude=1.0):
        t = np.arange(int(seconds * fs)) / fs
        return np.sin(2 * np.pi * freq * t) * amplitude

    def test_a_sine_lands_in_the_right_bin(self):
        from i3pro import spectrum

        out = spectrum.welch(self._sine(10.0), self.FS, points=1024)
        self.assertLessEqual(abs(out["peak_frequency"] - 10.0), out["resolution"])
        # 峰值格必须是整条曲线的最大值——表头报的主频不能是另算一遍的结果
        self.assertEqual(out["power"][out["peak_index"]], max(out["power"]))
        self.assertAlmostEqual(out["nyquist"], 50.0)
        self.assertEqual(len(out["power"]), 1024 // 2 + 1)

    def test_parseval_power_matches_the_variance(self):
        """Σ P·Δf 必须等于信号功率：缩放写错时这条会立刻炸。"""
        from i3pro import spectrum

        for window in ("hann", "hamming", "blackman", "rectangular"):
            out = spectrum.welch(self._sine(10.0), self.FS, points=1024, window=window)
            power = float(np.sum(out["power"]) * out["resolution"])
            # 幅值 1 的正弦功率 = 1/2
            self.assertAlmostEqual(power, 0.5, places=3, msg=f"{window} 的功率对不上")
        noise = np.random.default_rng(7).normal(size=20000)
        out = spectrum.welch(noise, self.FS, points=1024)
        self.assertAlmostEqual(float(np.sum(out["power"]) * out["resolution"]),
                               float(np.var(noise)), places=1)

    def test_hann_window_holds_the_leakage_down(self):
        """非整格频率上的正弦：矩形窗会漏出一圈旁瓣，Hann 不会。"""
        from i3pro import spectrum

        off_bin = self._sine(10.3)
        shares = {}
        for window in ("rectangular", "hann", "blackman"):
            out = spectrum.welch(off_bin, self.FS, points=1024, window=window)
            power = out["power"]
            peak = int(np.argmax(power))
            shares[window] = float(power[peak - 3:peak + 4].sum() / power.sum())
        self.assertGreater(shares["hann"], shares["rectangular"] + 0.01,
                           f"Hann 没有把泄漏压住: {shares}")
        self.assertGreater(shares["blackman"], shares["rectangular"])

    def test_points_snap_to_a_power_of_two_and_say_so(self):
        from i3pro import spectrum

        self.assertEqual(spectrum.clamp_points(1024), (1024, None))
        points, notice = spectrum.clamp_points(1000)
        self.assertEqual(points, 1024)
        self.assertIn("2 的幂", notice)
        self.assertEqual(spectrum.clamp_points(8)[0], spectrum.MIN_POINTS)
        self.assertEqual(spectrum.clamp_points(10 ** 9)[0], spectrum.MAX_POINTS)
        self.assertIn("看不懂", spectrum.clamp_points("abc")[1])

    def test_short_data_is_zero_padded_and_said_out_loud(self):
        from i3pro import spectrum

        out = spectrum.welch(np.zeros(100), self.FS, points=1024)
        self.assertTrue(out["padded"])
        self.assertEqual(out["segments"], 1)
        self.assertEqual(out["samples"], 100)
        self.assertIn("补零", out["notice"])

    def test_overlapping_segments_average(self):
        from i3pro import spectrum

        values = self._sine(10.0)
        half = spectrum.welch(values, self.FS, points=1024, overlap=0.5)
        none = spectrum.welch(values, self.FS, points=1024, overlap=0.0)
        self.assertEqual(half["segments"], 19)      # 10000 个样本、1024 点、50 % 重叠
        self.assertEqual(none["segments"], 10)
        self.assertGreater(half["segments"], none["segments"])
        self.assertLessEqual(half["overlap"], 0.95)

    def test_amplitude_scale_is_the_rms_of_the_band(self):
        from i3pro import spectrum

        out = spectrum.welch(self._sine(10.0), self.FS, points=1024,
                             window="rectangular", scale="amplitude")
        rms = float(np.sqrt(np.sum(np.square(out["power"]))))
        self.assertAlmostEqual(rms, 1.0 / np.sqrt(2.0), places=3)
        # 换一种窗也要成立：换算的是同一份数据，不该因窗而异
        hann = spectrum.welch(self._sine(10.0), self.FS, points=1024, scale="amplitude")
        self.assertAlmostEqual(float(np.sqrt(np.sum(np.square(hann["power"])))),
                               1.0 / np.sqrt(2.0), places=3)

    def test_smoothing_lowers_the_peak_but_keeps_the_power(self):
        from i3pro import spectrum

        raw = spectrum.welch(self._sine(10.0), self.FS, points=1024)
        smooth = spectrum.welch(self._sine(10.0), self.FS, points=1024, smooth=5)
        self.assertLess(max(smooth["power"]), max(raw["power"]))
        self.assertAlmostEqual(float(np.sum(smooth["power"])), float(np.sum(raw["power"])),
                               places=9)

    def test_nan_is_filled_and_reported(self):
        from i3pro import spectrum

        values = self._sine(10.0)
        values[10:20] = np.nan
        out = spectrum.welch(values, self.FS, points=1024)
        self.assertEqual(out["filled"], 10)
        self.assertFalse(np.isnan(out["power"]).any())
        with self.assertRaises(ValueError) as caught:
            spectrum.fill_gaps(np.full(100, np.nan))
        self.assertIn("换个窗口", str(caught.exception))

    def test_bad_input_says_what_to_do_next(self):
        from i3pro import spectrum

        with self.assertRaises(ValueError) as caught:
            spectrum.window_values("triangle", 64)
        self.assertIn("hann", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            spectrum.welch(self._sine(10.0), self.FS, scale="power")
        self.assertIn("psd", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            spectrum.welch(self._sine(10.0), 0.0)
        self.assertIn("采样率", str(caught.exception))

    @_needs(HILL)
    def test_golden_sessions_use_the_channel_own_sample_rate(self):
        """慢通道必须按它自己的采样率算：拿主时间基算会造出假高频。"""
        with ld.LogFile.read(HILL) as log:
            slow = render.spectrum(log, "GPS Speed", start=100.0, end=140.0)
            fast = render.spectrum(log, "Vx KF", start=100.0, end=140.0)
            self.assertEqual(slow["sample_rate"], 20.0)
            self.assertEqual(slow["nyquist"], 10.0)
            self.assertEqual(fast["sample_rate"], 100.0)
            self.assertEqual(fast["nyquist"], 50.0)
            self.assertLessEqual(slow["peak_frequency"], slow["nyquist"])
            self.assertLessEqual(fast["peak_frequency"], fast["nyquist"])
            self.assertEqual(len(fast["power"]), fast["points"] // 2 + 1)
            self.assertEqual(fast["unit"], "km/h")
            # 空窗口不是"频谱是零"：给一句能照做的话
            empty = render.spectrum(log, "Vx KF", start=200.0, end=200.0)
            self.assertEqual(empty["power"], [])
            self.assertIn("起止", empty["notice"])

    @_needs(HILL)
    def test_snapshot_spectra_only_embed_what_was_selected(self):
        from i3pro import spectrum

        with ld.LogFile.read(HILL) as log:
            payload = render.snapshot_spectra(log, ["Vx KF", "GPS Speed", "查无此通道"],
                                              points=512)
            self.assertEqual(payload["points"], 512)
            self.assertEqual(sorted(payload["series"]), ["GPS Speed", "Vx KF"])
            entry = payload["series"]["Vx KF"]
            self.assertEqual(len(entry["power"]), 512 // 2 + 1)
            self.assertAlmostEqual(entry["resolution"], 100.0 / 512)
            self.assertEqual(entry["sample_rate"], 100.0)
            # 内嵌的是功率谱密度（前端换算有效值 / dB 都从它来）
            self.assertEqual(spectrum.DEFAULT_WINDOW, payload["window"])


class TestSpectrumOverHttp(unittest.TestCase):
    """#10 走到界面之前的那一段：/spectrum 的参数、奈奎斯特频率与报错。"""

    @_needs(HILL)
    def test_spectrum_endpoint(self):
        with http_session(HILL, buckets=50) as http:
            quoted = http.quoted
            get = http.get

            try:
                status, body = get(f"/api/session/{quoted}/spectrum"
                                   f"?channel={urllib.parse.quote('GPS Speed')}"
                                   f"&points=512&window=hann&overlap=0.5&from=100&to=140")
                self.assertEqual(status, 200)
                self.assertEqual(body["channel"], "GPS Speed")
                self.assertEqual(body["points"], 512)
                self.assertEqual(body["window"], "hann")
                self.assertEqual(body["sample_rate"], 20.0)     # 通道自己那一档
                self.assertEqual(body["nyquist"], 10.0)
                self.assertAlmostEqual(body["resolution"], 20.0 / 512)
                self.assertEqual(len(body["power"]), 512 // 2 + 1)
                self.assertLessEqual(body["peak_frequency"], body["nyquist"])
                # 40 s × 20 Hz = 800 个样本；512 点一段、50 % 重叠 → 3 段
                self.assertEqual(body["samples"], 800)
                self.assertEqual(body["segments"], 3)

                # 缺参数 / 通道不存在 / 窗函数写错：都是 400，而且要说下一步
                status, body = get(f"/api/session/{quoted}/spectrum")
                self.assertEqual(status, 400)
                self.assertIn("channel", body["error"])
                status, body = get(f"/api/session/{quoted}/spectrum"
                                   f"?channel={urllib.parse.quote('查无此通道')}")
                self.assertEqual(status, 400)
                self.assertIn("「通道」", body["error"])
                status, body = get(f"/api/session/{quoted}/spectrum"
                                   f"?channel=Vx%20KF&window=triangle")
                self.assertEqual(status, 400)
                self.assertIn("hann", body["error"])
            finally:
                http.close()

class TestWorksheets(unittest.TestCase):
    """ticket #30：顶上那排按钮 = 仓库里 `worksheets/*.json` 里的文件。

    这一票之前那 7 套是 `viewer.html` 里的一段硬编码。现在它们是普通文件，所以
    这里钉的是**文件这一层的规矩**：读得出来、排得对、坏文件不连累别人、每种坏法
    都说清楚下一步做什么。至于"文件里的组件画出来长什么样"，归无头驱动
    （`tools/smoke_viewer.js` 第 38 组）——那是界面的事。

    「搬前搬后行为一字不差」也在这里钉一条：搬之前那 7 套在**高避**上的解析结果
    存成了夹具 `tests/fixtures/worksheets_before.json`（用
    `smoke_viewer.js --dump-worksheets` 倒出来的，那个模式就是为这次迁移加的），
    搬完再倒一次要逐字段对得上。
    """

    def _mkdir(self, files: dict[str, object]) -> Path:
        """造一个临时的**仓库根**（里面是 `worksheets/`），返回那个根。"""
        tmp = tempfile.mkdtemp(prefix="i3pro-worksheets-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = Path(tmp)
        (root / "worksheets").mkdir()
        for name, payload in files.items():
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            (root / "worksheets" / name).write_text(text, encoding="utf-8")
        return root

    # ------------------------------------------------------------- 仓库里那几份
    def test_默认目录就是仓库根下的_worksheets(self):
        self.assertEqual(worksheetsmod.worksheets_dir(), ROOT / "worksheets")
        self.assertEqual(worksheetsmod.worksheets_dir("/tmp/x"), Path("/tmp/x") / "worksheets")

    def test_仓库里那七套工作表都能读出来(self):
        sheets, problems = worksheetsmod.load_dir()
        self.assertEqual(problems, [], "仓库里的工作表必须全部读得出来")
        self.assertEqual(
            [s["name"] for s in sheets],
            ["分析", "对比", "动力", "底盘", "车手", "仪表台", "报表"],
        )
        # 文件名是身份（ASCII，将来要进 URL），显示名是按钮上的字
        self.assertEqual(
            [s["id"] for s in sheets],
            ["analysis", "compare", "powertrain", "chassis", "driver", "dash", "report"],
        )
        self.assertEqual([s["order"] for s in sheets], sorted(s["order"] for s in sheets))
        for sheet in sheets:
            self.assertTrue(sheet["components"], f"{sheet['name']} 一个组件都没有")
            for comp in sheet["components"]:
                self.assertTrue(comp["type"], f"{sheet['name']} 里有组件没写 type")

    def test_工作表里不会写死本场次的通道名(self):
        """挑通道要经 `pick`：写死通道名的话，换一个场次就整片空。

        `config` 里允许写死（那是用户存的配置，`worksheet` 文件是声明），但仓库里
        随版本发布的那 7 套必须靠 `pick` 挑——这条挡住"顺手把解析出来的通道名存回去"。
        """
        sheets, _ = worksheetsmod.load_dir()
        selectors = {"channels", "channel", "colour", "x", "y", "against"}
        for sheet in sheets:
            for comp in sheet["components"]:
                for key in selectors & set(comp.get("config", {})):
                    self.fail(
                        f"工作表 {sheet['id']} 的 {comp['type']} 把 {key} 写死在 config 里了；"
                        "要挑通道请用 pick（见 worksheets/README.md）"
                    )

    def test_搬进文件之后和搬之前的解析结果一致(self):
        """搬前搬后逐字段相同：夹具是硬编码那版的 dump。"""
        fixture = ROOT / "tests" / "fixtures" / "worksheets_before.json"
        self.assertTrue(fixture.exists(), f"缺少夹具 {fixture}")
        before = json.loads(fixture.read_text(encoding="utf-8"))
        after = self._resolved_on(HILL) if HILL.exists() else None
        if after is None:
            self.skipTest("没有金标准数据，跑不了这条（要在有数据的机器上跑）")
        self.assertEqual(sorted(after), sorted(before), "工作表的名字变了")
        for name in before:
            self.assertEqual(after[name], before[name], f"{name} 这套搬完不一样了")

    def _resolved_on(self, session: Path) -> dict | None:
        """用无头驱动把这一场的每套工作表倒出来（--dump-worksheets）。

        倒的是**切过去之后**的状态（`applyPreset` + 各组件自己的缺省化），不是文件里
        那一份原样——这正是用户看得见的那一层：散点自动带上前两条通道、直方图在快照
        里把窗口落成"整场"，都是这一步做的。
        """
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        out = ROOT / "out" / f"_worksheets_{session.stem}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        with ld.LogFile.read(session) as log:
            render.render_html(log, out, with_report=True)
        result = subprocess.run(
            [node, str(ROOT / "tools" / "smoke_viewer.js"), str(out), "--dump-worksheets"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode != 0:
            self.fail(f"无头驱动倒不出工作表：{result.stderr[:400]}")
        return json.loads(result.stdout)

    # ---------------------------------------------------------------- 坏文件
    def test_坏文件不连累别的工作表(self):
        root = self._mkdir({
            "good.json": {"schema": 1, "name": "好", "components": [{"type": "graph"}]},
            "broken.json": "{ 这不是 JSON",
            "future.json": {"schema": 99, "name": "未来", "components": [{"type": "graph"}]},
            "typo.json": {"schema": 1, "name": "打错了", "component": [{"type": "graph"}]},
            "empty.json": {"schema": 1, "name": "空的", "components": []},
        })
        sheets, problems = worksheetsmod.load_dir(root)
        self.assertEqual([s["name"] for s in sheets], ["好"], "好文件必须照常加载")
        self.assertEqual(len(problems), 4, problems)
        for item in problems:
            self.assertTrue(item["file"], "每条问题都要说是哪个文件")
            self.assertTrue(item["error"].endswith("。"), item["error"])

    def test_每种坏法都说清了下一步做什么(self):
        root = self._mkdir({
            "broken.json": "{",
            "future.json": {"schema": 2, "name": "未来", "components": [{"type": "graph"}]},
            "typo.json": {"schema": 1, "name": "打错了", "component": [{"type": "graph"}]},
            "weight.json": {"schema": 1, "name": "尺寸", "components": [
                {"type": "graph", "w": -1}]},
            "empty.json": {"schema": 1, "name": "空的", "components": []},
            "pickempty.json": {"schema": 1, "name": "空规则", "components": [
                {"type": "graph", "pick": {"channels": {}}}]},
            "picktypo.json": {"schema": 1, "name": "规则打错", "components": [
                {"type": "graph", "pick": {"channels": {"pattern": ["Vx"]}}}]},
        })
        _sheets, problems = worksheetsmod.load_dir(root)
        errors = {item["file"]: item["error"] for item in problems}
        self.assertEqual(len(errors), 7, errors)
        self.assertIn("worksheets/", errors["broken.json"])
        self.assertIn("schema 改回 1", errors["future.json"])
        self.assertIn("component", errors["typo.json"])
        self.assertIn("大于 0", errors["weight.json"])
        self.assertIn("至少一个", errors["empty.json"])
        self.assertIn("空规则", errors["pickempty.json"])
        self.assertIn("认不出来的键", errors["picktypo.json"])

    def test_重名会被挡下来(self):
        root = self._mkdir({
            "a.json": {"schema": 1, "name": "同一套", "components": [{"type": "graph"}]},
            "b.json": {"schema": 1, "name": "同一套", "components": [{"type": "graph"}]},
        })
        sheets, problems = worksheetsmod.load_dir(root)
        self.assertEqual(len(sheets), 1)
        self.assertEqual(len(problems), 1)
        self.assertIn("重名", problems[0]["error"])

    def test_目录不存在也有话说(self):
        with tempfile.TemporaryDirectory() as tmp:
            sheets, problems = worksheetsmod.load_dir(Path(tmp) / "没有这个目录")
        self.assertEqual(sheets, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("--worksheets", problems[0]["error"], "要告诉用户怎么换一个目录")

    def test_目录是空的也算一件事(self):
        root = self._mkdir({})
        sheets, problems = worksheetsmod.load_dir(root)
        self.assertEqual(sheets, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("没有 *.json", problems[0]["error"])

    # ------------------------------------------------------------ 装进载荷
    @_needs(HILL)
    def test_快照与本地服务都带上工作表(self):
        with ld.LogFile.read(HILL) as log:
            payload = render.build_payload(log, channels=["Vx KF"], buckets=50)
            out = scratch("_worksheets_payload.html")
            out.parent.mkdir(parents=True, exist_ok=True)
            render.render_html(log, out, channels=["Vx KF"], buckets=50)
        self.assertEqual([s["name"] for s in payload["worksheets"]][:3], ["分析", "对比", "动力"])
        self.assertEqual(payload["worksheet_problems"], [])
        html = out.read_text(encoding="utf-8")
        self.assertIn("worksheets", html)
        for name in ("分析", "仪表台", "报表"):
            self.assertIn(name, html, "快照里必须内嵌工作表，离线打开才能切")

    @_needs(HILL)
    def test_换一个工作表目录只影响那一个载荷(self):
        root = self._mkdir({
            "only.json": {"schema": 1, "name": "只有这一套", "order": 7,
                          "components": [{"type": "graph"}]},
        })
        with ld.LogFile.read(HILL) as log:
            payload = render.build_payload(log, channels=["Vx KF"], buckets=50,
                                           worksheets_dir=root)
        self.assertEqual([s["name"] for s in payload["worksheets"]], ["只有这一套"])
        self.assertEqual(payload["worksheets"][0]["order"], 7)
        self.assertEqual(payload["worksheet_problems"], [])

    @_needs(HILL)
    def test_本地服务的_info_带上工作表(self):
        with http_session(HILL, buckets=50) as http:
            status, info = http.json(f"/api/session/{http.quoted}/info")
            self.assertEqual(status, 200)
            self.assertEqual(len(info["worksheets"]), 7, info.get("worksheet_problems"))
            self.assertEqual(info["worksheet_problems"], [])
            self.assertEqual(info["worksheets"][0]["name"], "分析")


class TestWorksheetEditing(unittest.TestCase):
    """ticket #33：工作表的增删改与进出。

    判据都在**文件层**：真的多了一个文件、真的换了文件名、同名那次**没有**把旧的盖掉、
    导出的那份 JSON 原样导入得回来。界面怎么点归 `tools/verify_clicks.py`。
    """

    def _root(self, files: dict | None = None) -> Path:
        tmp = tempfile.mkdtemp(prefix="i3pro-ws-edit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = Path(tmp)
        (root / "worksheets").mkdir()
        for name, payload in (files or {}).items():
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            (root / "worksheets" / name).write_text(text, encoding="utf-8")
        return root

    def _sheet(self, name: str, order: int | None = None, components=None) -> dict:
        """一份最短的工作表；``order`` 不给就交给 create 自己排到最后。"""
        body = {
            "schema": 1, "name": name,
            "components": components or [
                {"type": "graph", "pick": {"channels": {"limit": 3}}}
            ],
        }
        if order is not None:
            body["order"] = order
        return body

    # ------------------------------------------------------------ 文件名
    def test_文件名由显示名派生_中文退回_sheet(self):
        self.assertEqual(worksheetsmod.slug("Analysis v2"), "analysis-v2")
        self.assertEqual(worksheetsmod.slug("分析"), "sheet")
        self.assertEqual(worksheetsmod.slug("分析 2"), "sheet-2")
        self.assertEqual(worksheetsmod.slug("Vx 的图"), "vx")
        self.assertEqual(worksheetsmod.slug(""), "sheet")

    def test_文件名不许乱来(self):
        """身份会拼进网址，也会拼进路径：``../`` 这类东西必须在门口挡掉。"""
        for bad in ("../x", "a/b", "a\\b", "", "  ", ".hidden", "x" * 90, "con"):
            with self.assertRaises(worksheetsmod.WorksheetError, msg=bad):
                worksheetsmod.check_stem(bad)
        for good in ("analysis", "sheet-2", "a.b", "x" * 64):
            self.assertEqual(worksheetsmod.check_stem(good), good)

    # ------------------------------------------------------------ 新建
    def test_新建写进目录而且排在最后(self):
        root = self._root({"analysis.json": self._sheet("分析", order=10)})
        directory = root / "worksheets"
        sheet = worksheetsmod.create(directory, self._sheet("我的"))
        self.assertEqual(sheet["id"], "sheet", "中文名该落到 sheet.json")
        self.assertEqual(sheet["name"], "我的")
        self.assertEqual(sheet["order"], 11, "新建的那套要排到最后")
        written = json.loads((directory / "sheet.json").read_text(encoding="utf-8"))
        self.assertEqual(written["name"], "我的")
        self.assertEqual(written["components"], sheet["components"])
        self.assertNotIn("id", written, "id 是本地身份，不该写进文件")

    def test_同名另存为加后缀_而且不覆盖(self):
        root = self._root()
        directory = root / "worksheets"
        first = worksheetsmod.create(directory, self._sheet("试验"))
        before = (directory / "sheet.json").read_bytes()
        second = worksheetsmod.create(directory, self._sheet("试验"))
        self.assertEqual(first["id"], "sheet")
        self.assertEqual(second["id"], "sheet-2", "同名该加后缀，不是覆盖")
        self.assertEqual(second["name"], "试验 2", "按钮上也要分得清两份")
        self.assertEqual((directory / "sheet.json").read_bytes(), before,
                         "同名另存为把原来那份盖掉了")
        self.assertEqual(len(list(directory.glob("*.json"))), 2)

    def test_坏文件建不出新的(self):
        root = self._root()
        with self.assertRaises(worksheetsmod.WorksheetError) as caught:
            worksheetsmod.create(root / "worksheets",
                                 {"schema": 1, "name": "空", "components": []})
        self.assertIn("至少一个", str(caught.exception))
        self.assertEqual(list((root / "worksheets").glob("*.json")), [])

    # ------------------------------------------------------------ 保存
    def test_保存换掉组件_名字与排序留在文件里(self):
        root = self._root({"dash.json": {
            "schema": 1, "name": "仪表台", "order": 7, "hints": ["Vx"],
            "components": [{"type": "graph"}]}})
        sheet = worksheetsmod.replace(root / "worksheets", "dash",
                                      [{"type": "track", "x": 1, "y": 2, "w": 3, "h": 4}])
        self.assertEqual(sheet["name"], "仪表台")
        self.assertEqual(sheet["order"], 7)
        self.assertEqual(sheet["hints"], ["Vx"])
        self.assertEqual([c["type"] for c in sheet["components"]], ["track"])
        self.assertEqual(sheet["components"][0]["w"], 3)

    def test_保存不存在的那份要说下一步(self):
        root = self._root()
        with self.assertRaises(worksheetsmod.WorksheetError) as caught:
            worksheetsmod.replace(root / "worksheets", "nope", [{"type": "graph"}])
        self.assertIn("刷新", str(caught.exception))

    # ------------------------------------------------------------ 改名
    def test_改名会把文件名也改掉(self):
        root = self._root({"analysis.json": self._sheet("分析", order=3)})
        directory = root / "worksheets"
        sheet = worksheetsmod.rename(directory, "analysis", "底盘调校")
        self.assertEqual(sheet["name"], "底盘调校")
        self.assertEqual(sheet["id"], "sheet")
        self.assertFalse((directory / "analysis.json").exists(),
                         "旧文件没删：按钮上会同时出现两套一样的")
        self.assertTrue((directory / "sheet.json").exists())
        self.assertEqual(
            json.loads((directory / "sheet.json").read_text("utf-8"))["order"], 3
        )

    def test_改名撞名被挡下_两份都不动(self):
        root = self._root({"a.json": self._sheet("甲"), "b.json": self._sheet("乙")})
        directory = root / "worksheets"
        before = (directory / "a.json").read_bytes()
        with self.assertRaises(worksheetsmod.WorksheetError) as caught:
            worksheetsmod.rename(directory, "a", "乙")
        self.assertIn("已经有一套叫", str(caught.exception))
        self.assertEqual((directory / "a.json").read_bytes(), before)
        self.assertTrue((directory / "b.json").exists())

    def test_改成一个空名字要被挡下(self):
        root = self._root({"a.json": self._sheet("甲")})
        with self.assertRaises(worksheetsmod.WorksheetError):
            worksheetsmod.rename(root / "worksheets", "a", "   ")

    # ------------------------------------------------------------ 删除
    def test_删除只删那一份并回报原来的名字(self):
        root = self._root({"a.json": self._sheet("甲"), "b.json": self._sheet("乙")})
        directory = root / "worksheets"
        self.assertEqual(worksheetsmod.remove(directory, "a"), "甲")
        self.assertEqual([p.name for p in sorted(directory.glob("*.json"))], ["b.json"])

    def test_删除不存在的要说下一步(self):
        root = self._root()
        with self.assertRaises(worksheetsmod.WorksheetError) as caught:
            worksheetsmod.remove(root / "worksheets", "nope")
        self.assertIn("找不到", str(caught.exception))

    # ------------------------------------------------------------ 导出 / 导入
    def test_导出的那份能原样导入回来(self):
        """这条就是"发给队友、队友导入后立即可用"的机械判据。"""
        source = self._root({"analysis.json": self._sheet(
            "分析", order=5,
            components=[{"type": "graph", "x": 0, "y": 0, "w": 12, "h": 10,
                         "config": {"mode": "split"},
                         "pick": {"channels": {"patterns": ["Vx"], "limit": 2}}},
                        {"type": "gauge", "config": {"subtype": "bar"}}])})
        sheet = worksheetsmod.read_sheet(source / "worksheets", "analysis")
        text = json.dumps(worksheetsmod.export_payload(sheet),
                          ensure_ascii=False, indent=2) + "\n"
        self.assertNotIn('"id"', text, "导出的文件里不该有本地 id")

        destination = self._root()
        made = worksheetsmod.import_text(destination / "worksheets", text)
        self.assertEqual(made["name"], "分析")
        self.assertEqual(made["order"], 5)
        self.assertEqual(made["components"], sheet["components"])
        self.assertEqual([c["type"] for c in made["components"]], ["graph", "gauge"])
        # 队友那边已经有一套同名的时候：加后缀，不动他那一份
        again = worksheetsmod.import_text(destination / "worksheets", text)
        self.assertEqual(again["name"], "分析 2")
        self.assertTrue((destination / "worksheets" / "sheet.json").exists())

    def test_导入一份不是工作表的文件要说下一步(self):
        root = self._root()
        with self.assertRaises(worksheetsmod.WorksheetError) as caught:
            worksheetsmod.import_text(root / "worksheets", "{ 这不是 JSON")
        self.assertIn("导出", str(caught.exception), "要告诉队友去点「导出」")
        self.assertIn("第 1 行", str(caught.exception))


class TestWorksheetEditingOverHttp(unittest.TestCase):
    """#33 的接口那一半：界面点的就是这条路，所以这里把五个动作与状态码钉住。"""

    def _worksheet_root(self) -> Path:
        tmp = tempfile.mkdtemp(prefix="i3pro-ws-http-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        base = Path(tmp)
        shutil.copytree(ROOT / "worksheets", base / "worksheets")
        return base

    @_needs(HILL)
    def test_新建保存改名删除一条龙(self):
        base = self._worksheet_root()
        directory = base / "worksheets"
        with http_session(HILL, buckets=50, worksheets_root=base) as http:
            try:
                status, state = http.json("/api/worksheets")
                self.assertEqual(status, 200)
                self.assertEqual(len(state["worksheets"]), 7)
                self.assertEqual(state["worksheet_problems"], [])

                # 新建（界面的"另存为"）：中文名落到 sheet.json，排到最后
                status, state = http.json("/api/worksheets", "POST", {
                    "sheet": {"name": "验证用", "components": [{"type": "graph"}]}})
                self.assertEqual(status, 200, state)
                self.assertEqual((state["worksheet"]["id"], state["worksheet"]["name"]),
                                 ("sheet", "验证用"))
                self.assertTrue((directory / "sheet.json").exists())
                self.assertEqual(len(state["worksheets"]), 8)
                self.assertEqual(state["worksheets"][-1]["name"], "验证用")

                # 同名再来一次：加后缀，前一份一个字节都不动
                before = (directory / "sheet.json").read_bytes()
                status, state = http.json("/api/worksheets", "POST", {
                    "sheet": {"name": "验证用", "components": [{"type": "graph"}]}})
                self.assertEqual(status, 200, state)
                self.assertEqual(state["worksheet"]["id"], "sheet-2")
                self.assertEqual(state["worksheet"]["name"], "验证用 2")
                self.assertEqual((directory / "sheet.json").read_bytes(), before)

                # 保存：只换组件，名字与排序留在文件里
                status, state = http.json(
                    "/api/worksheets/sheet", "PUT",
                    {"components": [{"type": "track", "x": 0, "y": 0, "w": 4, "h": 4}]})
                self.assertEqual(status, 200, state)
                self.assertEqual([c["type"] for c in state["worksheet"]["components"]],
                                 ["track"])
                self.assertEqual(state["worksheet"]["name"], "验证用")

                # 空的一屏不许存：页面上会出现一套画不出来的工作表
                status, body = http.json("/api/worksheets/sheet", "PUT", {"components": []})
                self.assertEqual(status, 400)
                self.assertIn("components", body["error"])

                # 改名：文件名跟着变，旧文件删掉
                status, state = http.json("/api/worksheets/sheet/rename", "POST",
                                          {"name": "renamed"})
                self.assertEqual(status, 200, state)
                self.assertEqual(state["worksheet"]["id"], "renamed")
                self.assertEqual(state["previous_id"], "sheet")
                self.assertFalse((directory / "sheet.json").exists(),
                                 "改名之后旧文件还在，按钮上会多出一套")
                self.assertTrue((directory / "renamed.json").exists())

                # 导出：发出去的字节就是文件里那一份（可以原样导入回来）
                status, raw = http.get_bytes("/api/worksheets/renamed/export")
                self.assertEqual(status, 200)
                self.assertEqual(raw, (directory / "renamed.json").read_bytes())
                exported = json.loads(raw.decode("utf-8"))
                self.assertEqual(exported["name"], "renamed")
                self.assertNotIn("id", exported)

                status, state = http.json("/api/worksheets", "POST",
                                          {"text": raw.decode("utf-8")})
                self.assertEqual(status, 200, state)
                self.assertEqual(state["worksheet"]["name"], "renamed 2",
                                 "队友导入同名的一份该加后缀，不是覆盖")
                self.assertNotEqual(state["worksheet"]["id"], "renamed")

                # 删除：说清删了哪一套、切回哪一套
                status, state = http.json("/api/worksheets/renamed", "DELETE")
                self.assertEqual(status, 200, state)
                self.assertEqual(state["deleted"], {"id": "renamed", "name": "renamed"})
                self.assertFalse((directory / "renamed.json").exists())
                self.assertEqual(state["fallback"], state["worksheets"][0]["name"])

                # 坏输入：路径穿越 / 不认得的子动作 / 不是 JSON 的请求体
                for path in ("/api/worksheets/..%2F..%2Fevil", "/api/worksheets/a%20b"):
                    status, body = http.json(path, "DELETE")
                    self.assertEqual(status, 400, (path, body))
                status, body = http.json("/api/worksheets/sheet/nope", "POST", {})
                self.assertEqual(status, 404)
                status, body = http.json("/api/worksheets", "POST", None)
                self.assertEqual(status, 400)
                self.assertIn("工作表", body["error"])
            finally:
                http.close()

    @_needs(HILL)
    def test_页面载荷里的工作表按钮跟着文件走(self):
        """改完文件之后，刷新页面看到的就是改完的那一排（同一份目录，不是缓存）。"""
        base = self._worksheet_root()
        directory = base / "worksheets"
        with http_session(HILL, buckets=50, worksheets_root=base) as http:
            try:
                payload = http.page_payload()
                self.assertEqual(payload["worksheets"][0]["name"], "分析")
                status, state = http.json("/api/worksheets", "POST", {
                    "sheet": {"name": "刚建的", "components": [{"type": "graph"}]}})
                self.assertEqual(status, 200)
                new_id = state["worksheet"]["id"]
                self.assertTrue((directory / f"{new_id}.json").exists())
                payload = http.page_payload()
                self.assertEqual(payload["worksheets"][-1]["name"], "刚建的")
                self.assertEqual(payload["worksheet_problems"], [])
            finally:
                http.close()


class TestMathsOverHttp(unittest.TestCase):
    """#3 走到界面之前的那一段：PUT/GET/POST + 侧车文件 + 作用域。"""

    @_needs(HILL)
    def test_saving_a_definition_reaches_the_viewer(self):
        with http_session(HILL, buckets=100) as http:
            root, copy, quoted = http.root, http.copy, http.quoted
            request = http.json

            try:
                # 空状态：没有定义，也没有报错
                status, state = request(f"/api/session/{quoted}/maths")
                self.assertEqual(status, 200)
                self.assertEqual(state["definitions"], [])
                self.assertEqual(state["errors"], [])
                self.assertTrue(state["functions"])

                # 存一条本地定义
                status, state = request(
                    f"/api/session/{quoted}/maths", "PUT",
                    {"definitions": [{"name": "总G",
                                      "expr": "sqrt('G Force Lat'^2 + 'G Force Long'^2)",
                                      "unit": "g"}]},
                )
                self.assertEqual(status, 200, state)
                self.assertEqual(state["saved"], f"{copy.stem}.maths.json")
                self.assertEqual([d["name"] for d in state["definitions"]], ["总G"])
                self.assertEqual(state["definitions"][0]["scope"], "local")
                self.assertEqual(state["errors"], [])
                self.assertTrue((root / f"{copy.stem}.maths.json").exists())
                self.assertFalse((root / f"{copy.stem}.ldx").exists(),
                                 "数学通道绝不能写回 MoTeC 格式")

                # 派生列要能在图表接口里取到，并带上单位
                status, trace = request(
                    f"/api/session/{quoted}/trace?channels=" + urllib.parse.quote("总G")
                    + "&buckets=10"
                )
                self.assertEqual(status, 200)
                self.assertIn("总G", trace)
                self.assertEqual(trace["总G"]["unit"], "g")
                self.assertTrue(trace["总G"]["value"])

                # 通道索引里要标出来它是算出来的
                status, info = request(f"/api/session/{quoted}/info")
                entry = next(c for c in info["channels"] if c["name"] == "总G")
                self.assertTrue(entry["derived"])

                # 试算：好式子给统计与**用到哪条通道**；语法／通道名不对直接 400
                status, preview = request(
                    f"/api/session/{quoted}/maths", "POST",
                    {"expr": "'G Force Lat' * 2"},
                )
                self.assertEqual(status, 200)
                self.assertTrue(preview["ok"])
                self.assertEqual(preview["channels"], ["G Force Lat"])
                status, preview = request(
                    f"/api/session/{quoted}/maths", "POST", {"expr": "'没有的通道' + 1"}
                )
                self.assertEqual(status, 400)
                self.assertIn("本场次没有这个通道", preview["error"])
                self.assertIn("插入通道", preview["error"])
                status, preview = request(
                    f"/api/session/{quoted}/maths", "POST", {"expr": "foo(1)"}
                )
                self.assertEqual(status, 400)
                self.assertIn("未知函数", preview["error"])

                # 坏表达式不进侧车
                status, body = request(
                    f"/api/session/{quoted}/maths", "PUT",
                    {"definitions": [{"name": "坏", "expr": "1 +"}]},
                )
                self.assertEqual(status, 400)
                self.assertIn("表达式", body["error"])
                self.assertEqual(
                    json.loads((root / f"{copy.stem}.maths.json").read_text("utf-8"))[
                        "definitions"
                    ][0]["name"],
                    "总G",
                    "被拒绝的表达式改动了已经存好的侧车",
                )

                # 同名两条要在保存前就挡住
                status, body = request(
                    f"/api/session/{quoted}/maths", "PUT",
                    {"definitions": [{"name": "X", "expr": "1"},
                                     {"name": "X", "expr": "2"}]},
                )
                self.assertEqual(status, 400)
                self.assertIn("同名", body["error"])

                # 全局作用域写进 maths/global.json，且不进本地侧车
                status, state = request(
                    f"/api/session/{quoted}/maths?scope=global", "PUT",
                    {"definitions": [{"name": "垂直G", "expr": "'G Force Vert' + 1"}]},
                )
                self.assertEqual(status, 200, state)
                self.assertEqual(state["scope"], "global")
                self.assertTrue((root / "maths" / "global.json").exists())
                local_names = [
                    d["name"] for d in
                    json.loads((root / f"{copy.stem}.maths.json").read_text("utf-8"))[
                        "definitions"
                    ]
                ]
                self.assertEqual(local_names, ["总G"],
                                 "存全局时把本地定义一起搬进了本地文件")

                # 本地同名覆盖全局：两条都在，界面能看出谁赢了
                status, state = request(
                    f"/api/session/{quoted}/maths", "PUT",
                    {"definitions": [{"name": "垂直G", "expr": "'G Force Vert' + 2"},
                                     {"name": "总G",
                                      "expr": "sqrt('G Force Lat'^2 + 'G Force Long'^2)"}]},
                )
                self.assertEqual(status, 200, state)
                self.assertEqual(state["shadowed"], ["垂直G"])
                scopes = {d["name"]: d["scope"] for d in state["definitions"]}
                self.assertEqual(scopes["垂直G"], "local")

                # 函数表
                status, catalogue = request("/api/maths/functions")
                self.assertEqual(status, 200)
                self.assertEqual(len(catalogue), 53)
            finally:
                http.close()

    @_needs(HILL)
    def test_a_bare_channel_name_with_a_space_saves_and_computes(self):
        """用户反馈的那条路：编辑器里直接打 `Vx KF * 2`，不该要求他加引号。"""
        with http_session(HILL, buckets=100) as http:
            root, copy, quoted = http.root, http.copy, http.quoted
            request = http.json

            try:
                status, state = request(
                    f"/api/session/{quoted}/maths", "PUT",
                    {"definitions": [{"name": "两倍车速", "expr": "Vx KF * 2",
                                      "unit": "km/h"}]},
                )
                self.assertEqual(status, 200, state)
                self.assertEqual(state["errors"], [])
                derived = [d for d in state["definitions"] if d["name"] == "两倍车速"]
                self.assertTrue(derived, "定义没进定义表")
                self.assertEqual(derived[0]["expr"], "Vx KF * 2")

                status, trace = request(
                    f"/api/session/{quoted}/trace?channels="
                    + urllib.parse.quote("两倍车速") + "&buckets=50"
                )
                self.assertEqual(status, 200, trace)
                self.assertIn("两倍车速", trace)
                values = [v for v in trace["两倍车速"]["value"] if v is not None]
                self.assertTrue(values, "派生列没有样本")
                self.assertLessEqual(max(values), 2 * 200.0)

                # 少一个空格、小写几个字母：还是同一条通道，存下来要能算
                status, body = request(
                    f"/api/session/{quoted}/maths", "PUT",
                    {"definitions": [{"name": "漏空格的", "expr": "vx kf * 2"}]},
                )
                self.assertEqual(status, 200, body)
                self.assertEqual(body["errors"], [], "少写一个空格不该算'没有这个通道'")
                status, trial = request(f"/api/session/{quoted}/maths/test", "POST",
                                        {"expr": "VXKF * 2"})
                self.assertEqual(status, 200, trial)
                self.assertTrue(trial["ok"], trial)
                self.assertEqual(trial["channels"], ["Vx KF"], "试算没把认到的通道说出来")

                # 真的是另一个名字：试算要说清该改成哪一条，且坏定义不进侧车
                status, trial = request(f"/api/session/{quoted}/maths/test", "POST",
                                        {"expr": "G Force Longg * 2"})
                self.assertEqual(status, 400, trial)
                self.assertIn("G Force Long", trial["error"])
                status, body = request(
                    f"/api/session/{quoted}/maths", "PUT",
                    {"definitions": [{"name": "打错的", "expr": "没有这条通道 * 2"}]},
                )
                self.assertEqual(status, 400, body)
                self.assertIn("本场次没有这个通道", body["error"])
                names = [
                    d["name"] for d in json.loads(
                        (root / f"{copy.stem}.maths.json").read_text("utf-8")
                    )["definitions"]
                ]
                self.assertNotIn("打错的", names, "通道名不对的定义不该写进侧车")

                # 全局定义是跨场次复用的：某一场缺那条通道不算"存不下"，只报出来
                status, state = request(
                    f"/api/session/{quoted}/maths?scope=global", "PUT",
                    {"definitions": [{"name": "别场才有的", "expr": "本场没有的通道 * 2"}]},
                )
                self.assertEqual(status, 200, state)
                errors = {e["name"]: e["error"] for e in state["errors"]}
                self.assertIn("别场才有的", errors)
                self.assertIn("本场次没有这个通道", errors["别场才有的"])
            finally:
                http.close()


#: 原始 CAN 帧日志与 DBC 都是车队本地数据（`.gitignore` 挡着），缺了就跳过。
CAN_DATA = ROOT / "can_data"
DBC_DIR = DATA / "dbc"
SENSORS_DBC = DBC_DIR / "Sensors.dbc"

#: CAN 那批实测数字取样的 9 份帧表（ticket #37–#40）。点名而不是扫目录：
#: 实测数字必须能复现，而数据目录里多一份新日志是常态。
CAN_FIXTURES = (
    "2026_10_03_173345_ID0001.csv",
    "2026_10_03_173936_ID0001.csv",
    "2026_10_03_174748_ID0001.csv",
    "2026_10_03_175413_ID0001.csv",
    "2026_10_03_200723_ID0001.csv",
    "2026_10_03_200755_ID0001.csv",
    "2026_10_03_201019_ID0001.csv",
    "2026_10_03_201147_ID0001.csv",
    "2026_10_03_201402_ID0001.csv",
)
DASHBOARD_DBC = DBC_DIR / "E27_Dashboard_AutoX_20260902_pjgps - Internal Display (using Display Creator).dbc"

#: 扫一遍 9 份原始帧表拿到的 ID 集合。扫描本身要几秒，所以整个测试进程只做一次。
_CAN_ID_CACHE: dict[str, object] = {}


def _can_id_counts() -> dict[int, int]:
    """``{帧 ID: 帧数}``：所有原始帧日志的并集（缺数据时是空字典）。"""
    if "counts" not in _CAN_ID_CACHE:
        counts: dict[int, int] = {}
        for path in sorted(CAN_DATA.glob("*.csv")):
            with path.open("r", encoding="gbk", errors="replace", newline="") as handle:
                handle.readline()
                for line in handle:
                    cells = line.split(",")
                    if len(cells) < 10:
                        continue
                    try:
                        frame_id = int(cells[4], 16)
                    except ValueError:
                        continue
                    counts[frame_id] = counts.get(frame_id, 0) + 1
        _CAN_ID_CACHE["counts"] = counts
    return _CAN_ID_CACHE["counts"]  # type: ignore[return-value]


def _cantools():
    """独立裁判；没装就跳过（规则 4 只允许它出现在测试里）。"""
    try:
        import cantools  # noqa: PLC0415
    except ImportError:
        return None
    return cantools


class TestDbc(unittest.TestCase):
    """DBC 解析与解码（ticket #37）。

    合成用例管"写错了会静默错"的三种：字节序（@0 锯齿走法）、有符号、超短载荷；
    真数据用例管数字（报文数 / 信号数 / 46 条通道）与 ``cantools`` 逐信号对拍。
    """

    SYNTHETIC = """
BO_ 291 Mixed_Endian: 8 Vector__XXX
 SG_ Big_At_Seven : 7|16@0+ (1,0) [0|65535] "V" Vector__XXX
 SG_ Big_At_TwentyThree : 23|16@0+ (1,0) [0|65535] "V" Vector__XXX
 SG_ Little_At_32 : 32|16@1+ (1,0) [0|65535] "V" Vector__XXX
 SG_ Signed_Big : 55|8@0- (1,0) [-128|127] "V" Vector__XXX
 SG_ Scaled : 63|8@0+ (0.5,10) [10|137.5] "deg" Vector__XXX
BO_ 2147483939 Extended_One: 2 Node
 SG_ Only_Extended : 7|16@0+ (1,0) [0|65535] "" Node
BO_ 3221225472 VECTOR__INDEPENDENT_SIG_MSG: 0 Vector__XXX
 SG_ Unused_Signal : 0|8@1+ (1,0) [0|0] "" Vector__XXX
VAL_ 291 Scaled 1 "off" 2 "on" ;
VAL_TABLE_ Ignored 1 "off" ;
BO_TX_BU_ 291 : Vector__XXX;
"""

    def test_big_endian_follows_the_sawtooth_convention(self):
        """``@0`` 的起始位是最高位，且字节内位号 0 是最低位。

        这一条是本模块最容易写反的地方：把字节内位号当成"0 是最高位"，两个 16 位值
        会各自按位反转，**不报错**。这里用 0x4A97 钉死——它反转过来是 0x52E9。
        """
        db = dbc.parse(self.SYNTHETIC)
        message = db.messages[(False, 291)]
        payload = bytes([0x4A, 0x97, 0x48, 0xE9, 0x01, 0x02, 0xFF, 0x80])
        values = dbc.decode(message, payload)
        self.assertEqual(values["Big_At_Seven"], 0x4A97)
        self.assertEqual(values["Big_At_TwentyThree"], 0x48E9)
        self.assertNotEqual(values["Big_At_Seven"], 0x52E9)

    def test_little_endian_signed_and_scaling(self):
        db = dbc.parse(self.SYNTHETIC)
        message = db.messages[(False, 291)]
        payload = bytes([0x4A, 0x97, 0x48, 0xE9, 0x34, 0x12, 0xFF, 0x80])
        values = dbc.decode(message, payload)
        self.assertEqual(values["Little_At_32"], 0x1234)
        self.assertEqual(values["Signed_Big"], -1.0)      # 0xFF 是 -1，不是 255
        self.assertEqual(values["Scaled"], 0x80 * 0.5 + 10)

    def test_value_table_is_read_and_kept_per_signal(self):
        db = dbc.parse(self.SYNTHETIC)
        signal = db.messages[(False, 291)].signal("Scaled")
        self.assertEqual(signal.choices, {1: "off", 2: "on"})
        self.assertEqual(db.messages[(False, 291)].signal("Big_At_Seven").choices, {})

    def test_extended_ids_do_not_shadow_standard_ones(self):
        db = dbc.parse(self.SYNTHETIC)
        # 同一个数字 ID 下面，标准帧与扩展帧是两条不同的报文，不许互相盖住：
        # 这份合成 DBC 里 291 两种都有（实测那份 dashboard DBC 就全是扩展帧）。
        standard = db.messages[(False, 291)]
        extended = db.messages[(True, 291)]
        self.assertFalse(standard.extended)
        self.assertTrue(extended.extended)
        self.assertEqual((standard.name, extended.name), ("Mixed_Endian", "Extended_One"))
        self.assertIs(db.find(291, extended=False), standard)
        self.assertIs(db.find(291, extended=True), extended)
        self.assertEqual(dbc.decode(extended, bytes([0x12, 0x34]))["Only_Extended"], 0x1234)

    def test_vector_pseudo_message_is_skipped(self):
        db = dbc.parse(self.SYNTHETIC)
        self.assertNotIn((False, 0xC0000000), db.messages)
        self.assertNotIn((True, 0xC0000000), db.messages)
        self.assertEqual(len(db.skipped), 1)
        self.assertIn("VECTOR__INDEPENDENT_SIG_MSG", db.skipped[0])
        self.assertEqual(db.signal_count, 6)              # 伪报文里的那条不算

    def test_multiplexed_message_is_refused_with_a_next_step(self):
        db = dbc.parse("""
BO_ 100 Muxed: 8 Node
 SG_ Selector M : 0|8@0+ (1,0) [0|255] "" Node
 SG_ On_Zero m0 : 8|8@0+ (1,0) [0|255] "" Node
""")
        message = db.messages[(False, 100)]
        self.assertTrue(message.multiplexed)
        with self.assertRaises(dbc.DbcError) as caught:
            dbc.decode(message, bytes(8))
        self.assertIn("多路复用", str(caught.exception))
        self.assertIn("下一步", str(caught.exception))

    def test_payload_length_wins_over_the_dbc_dlc(self):
        """实测 ``0x66D``：DBC 写 4，日志里发 8 字节。短了要吵，长了照解。"""
        db = dbc.parse("""
BO_ 1645 Front_Aero_Ride_Height: 4 Node
 SG_ FL : 23|16@0+ (1,0) [0|65535] "cm" Node
""")
        message = db.messages[(False, 1645)]
        self.assertEqual(dbc.decode(message, bytes([0, 0, 0x12, 0x34, 0xFF, 0xFF, 0xFF, 0xFF]))["FL"], 0x1234)
        with self.assertRaises(dbc.DbcError) as caught:
            dbc.decode(message, bytes([0x12, 0x34]))
        self.assertIn("只有 2 字节", str(caught.exception))

    def test_a_file_without_messages_says_what_to_do(self):
        with self.assertRaises(dbc.DbcError) as caught:
            dbc.parse("VERSION \"\"\n\nNS_ :\n")
        self.assertIn("BO_", str(caught.exception))

    # ------------------------------------------------------------ 真数据（跳过）
    @_needs(SENSORS_DBC)
    def test_sensors_dbc_parses_to_the_measured_counts(self):
        db = dbc.parse(SENSORS_DBC.read_text(encoding="utf-8", errors="replace"),
                       source=SENSORS_DBC.name)
        self.assertEqual(len(db.messages), 17)
        self.assertEqual(db.signal_count, 56)
        self.assertEqual(len(db.skipped), 1)
        self.assertFalse(any(m.extended for m in db.messages_only))
        self.assertFalse(any(m.multiplexed for m in db.messages_only))

    @_needs(DASHBOARD_DBC)
    def test_dashboard_dbc_parses_to_the_measured_counts(self):
        db = dbc.parse(DASHBOARD_DBC.read_text(encoding="utf-8", errors="replace"),
                       source=DASHBOARD_DBC.name)
        self.assertEqual(len(db.messages), 63)
        self.assertEqual(db.signal_count, 180)
        self.assertTrue(all(m.extended for m in db.messages_only))

    @_needs(SENSORS_DBC)
    def test_the_46_channels_are_the_signals_on_ids_the_log_really_carries(self):
        """实测：日志里出现 14 条 Sensors.dbc 的报文，共 46 条信号。"""
        if not CAN_DATA.exists():
            self.skipTest("缺 can_data/ 原始帧日志")
        db = dbc.parse(SENSORS_DBC.read_text(encoding="utf-8", errors="replace"))
        present = {frame_id for frame_id in _can_id_counts() if db.covers(frame_id)}
        signals = [s.name for m in db.messages_only if m.frame_id in present for s in m.signals]
        self.assertEqual(len(present), 14)
        self.assertEqual(len(signals), 46)

    @_needs(SENSORS_DBC)
    def test_cantools_agrees_signal_by_signal_on_real_frames(self):
        """独立裁判：cantools 解同一批真实帧，逐信号完全一致。"""
        cantools = _cantools()
        if cantools is None:
            self.skipTest("没装 cantools（测试期裁判）")
        if not CAN_DATA.exists():
            self.skipTest("缺 can_data/ 原始帧日志")
        text = SENSORS_DBC.read_text(encoding="utf-8", errors="replace")
        reference = cantools.database.load_string(text, database_format="dbc")
        mine = dbc.parse(text)
        compared = 0
        seen: set[str] = set()
        for path in sorted(CAN_DATA.glob("*.csv"))[:3]:
            with path.open("r", encoding="gbk", errors="replace", newline="") as handle:
                handle.readline()
                for index, line in enumerate(handle):
                    if index > 40000:
                        break
                    cells = line.split(",")
                    if len(cells) < 10:
                        continue
                    frame_id = int(cells[4], 16)
                    message = mine.find(frame_id)
                    if message is None:
                        continue
                    payload = bytes(int(x, 16) for x in cells[9].split("|")[1].split())
                    got = dbc.decode(message, payload)
                    want = reference.get_message_by_frame_id(frame_id).decode(
                        payload, decode_choices=False)
                    for name, value in got.items():
                        seen.add(name)
                        self.assertAlmostEqual(
                            value, float(want[name]), places=9,
                            msg=f"{path.name}:{index} {message.name}.{name}")
                    compared += 1
        self.assertGreater(compared, 20000)
        self.assertGreaterEqual(len(seen), 40)

    @_needs(DASHBOARD_DBC)
    def test_cantools_agrees_on_random_payloads_for_every_signal(self):
        """日志里没有扩展帧，所以那份 63 报文 / 180 信号用随机载荷对拍。"""
        cantools = _cantools()
        if cantools is None:
            self.skipTest("没装 cantools（测试期裁判）")
        random = __import__("random")
        random.seed(20261004)
        text = DASHBOARD_DBC.read_text(encoding="utf-8", errors="replace")
        reference = cantools.database.load_string(text, database_format="dbc")
        mine = dbc.parse(text)
        checked = 0
        for message in mine.messages_only:
            raw_id = message.frame_id | (0x80000000 if message.extended else 0)
            reference_message = reference.get_message_by_frame_id(raw_id)
            for _ in range(3):
                payload = bytes(random.randrange(256) for _ in range(message.length or 8))
                got = dbc.decode(message, payload)
                want = reference_message.decode(payload, decode_choices=False,
                                                allow_truncated=True)
                for name, value in got.items():
                    if isinstance(want[name], str):
                        continue
                    self.assertAlmostEqual(value, float(want[name]), places=9,
                                           msg=f"{message.name}.{name}")
                    checked += 1
        self.assertGreater(checked, 500)


class TestDbcMerge(unittest.TestCase):
    """多份 DBC 取并集（ticket #40）：各解各的，撞车时不猜。

    这台车把 CAN 布局拆成了 13 份小 DBC（仪表一份、每个 ECU 一份），所以"一份
    日志配多份 DBC"是常态。这些用例不带数据，纯合成——真数据那两条在
    :class:`TestCanLog` 里。
    """

    @staticmethod
    def _db(text: str, name: str) -> tuple[str, object]:
        return (name, dbc.parse(text, source=name))

    ONE = 'BO_ 100 A: 8 X\n SG_ One : 0|8@1+ (1,0) [0|255] "" X\n'
    TWO = 'BO_ 100 A: 8 X\n SG_ One : 0|8@1+ (2,0) [0|510] "" X\n'
    OTHER = 'BO_ 200 B: 8 X\n SG_ Two : 0|8@1+ (1,0) [0|255] "" X\n'

    def test_two_files_that_define_different_ids_both_get_used(self):
        """各管各的 ID：两份都要进并集，各自记下归属。"""
        merged, origin, conflicts = dbc.merge([self._db(self.ONE, "left.dbc"),
                                               self._db(self.OTHER, "right.dbc")])
        self.assertEqual(sorted(message.frame_id for message in merged.messages_only),
                         [100, 200])
        self.assertEqual(origin[(False, 100)], "left.dbc")
        self.assertEqual(origin[(False, 200)], "right.dbc")
        self.assertEqual(conflicts, [])

    def test_identical_definitions_in_two_files_are_not_a_conflict(self):
        """两份写了同一条报文、定义逐字相同：不是冲突，也不该报出来。"""
        merged, origin, conflicts = dbc.merge([self._db(self.ONE, "a.dbc"),
                                               self._db(self.ONE, "b.dbc")])
        self.assertEqual(len(merged.messages_only), 1)
        self.assertEqual(origin[(False, 100)], "a.dbc")
        self.assertEqual(conflicts, [])

    def test_conflicting_definitions_pick_by_coverage_and_say_so(self):
        """同一条 ID 两种定义：按每份 DBC 的覆盖帧数选，并把这件事写进冲突表。"""
        merged, origin, conflicts = dbc.merge(
            [self._db(self.ONE, "a.dbc"), self._db(self.TWO, "b.dbc")],
            {"a.dbc": 10, "b.dbc": 500},
        )
        self.assertEqual(origin[(False, 100)], "b.dbc")
        self.assertEqual(merged.messages[(False, 100)].signals[0].factor, 2.0)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["chosen"], "b.dbc")
        self.assertEqual(conflicts[0]["rejected"], "a.dbc")
        self.assertEqual(conflicts[0]["id"], "0x64")
        # 冲突的文字里要给出下一步（怎么改成另一份）
        self.assertIn("dbc", conflicts[0]["reason"])

    def test_a_tie_is_broken_by_file_name_so_the_result_is_reproducible(self):
        """覆盖帧数一样时按文件名定序——同样的输入必须永远给同样的结果。"""
        merged, origin, conflicts = dbc.merge(
            [self._db(self.ONE, "b.dbc"), self._db(self.TWO, "a.dbc")],
            {"b.dbc": 5, "a.dbc": 5},
        )
        # a.dbc 在字典序前面，所以它赢——哪怕它是**后**读进来的那份
        self.assertEqual(origin[(False, 100)], "a.dbc")
        self.assertEqual(merged.messages[(False, 100)].signals[0].factor, 2.0)
        self.assertEqual(len(conflicts), 1)

    def test_a_standard_frame_and_an_extended_frame_do_not_collide(self):
        """同一个数字的标准帧与扩展帧是两条报文，不许互相盖住。"""
        # BO_ 的 ID 最高位是扩展帧标记：0x80000064 -> 扩展帧 0x64，与 BO_ 100 同号
        extended = 'BO_ 2147483748 A: 8 X\n SG_ One : 0|8@1+ (1,0) [0|255] "" X\n'
        merged, origin, conflicts = dbc.merge([self._db(self.ONE, "std.dbc"),
                                               self._db(extended, "ext.dbc")])
        self.assertEqual(len(merged.messages_only), 2)
        self.assertEqual(origin[(False, 100)], "std.dbc")
        self.assertEqual(origin[(True, 100)], "ext.dbc")
        self.assertEqual(conflicts, [])


class TestCanLog(unittest.TestCase):
    """原始 CAN 帧表 -> 场次（ticket #38）与并场（ticket #39）。

    数字全部来自实测：9 份文件、3,684,850 帧、43 个 ID、Sensors.dbc 覆盖 24.6%、
    解出 46 条通道；9 份其实是 7 次记录（两次切分的墙钟差是 26 µs 与 249 µs）。
    """

    def setUp(self):
        if not CAN_DATA.exists() or not SENSORS_DBC.exists():
            self.skipTest("缺 can_data/ 或 i2pro_data/dbc/")
        # 这批数字（3,684,850 帧 / 43.42% 覆盖率 / 9 份并成 7 场）是**这 9 份文件**
        # 实测出来的，所以这里点名，而不是"目录里有什么算什么"——往 can_data/ 里丢
        # 一份新日志是正常的（2026-10-04 就丢过一份），不该让整套用例变红。
        self.files = [CAN_DATA / name for name in CAN_FIXTURES]
        missing = [path.name for path in self.files if not path.exists()]
        if missing:
            self.skipTest(f"can_data/ 里缺 {len(missing)} 份实测用的帧表：{missing[:2]}")
        self.small = CAN_DATA / "2026_10_03_201147_ID0001.csv"

    # ------------------------------------------------------------- 识别与读取
    def test_a_frame_table_is_not_mistaken_for_a_channel_table(self):
        self.assertTrue(canlog.looks_like_frames(self.small))
        plain = scratch("_can_plain.csv")
        plain.parent.mkdir(parents=True, exist_ok=True)
        plain.write_text("Time,Vx KF\n0,1\n0.01,2\n", encoding="utf-8")
        try:
            self.assertFalse(canlog.looks_like_frames(plain))
            # 一张普通的通道表照旧走 CSV 那条路（不会被帧表识别抢走）
            session = csvlog.read_csv_session(plain)
            self.assertEqual(len(session.channels), 1)
            self.assertFalse(csvlog.open_session(plain).metadata()["format"] == "can")
        finally:
            plain.unlink(missing_ok=True)

    def test_a_session_holds_the_master_timebase_and_the_true_update_rate(self):
        session = canlog.read_can_session([self.small], dbc_dir=[DBC_DIR], write_sidecar=False)
        self.assertEqual(session.sample_rate, 100.0)
        axis = timebase.axis(session)
        self.assertEqual(axis.size, int(round(session.duration * 100)) + 1)
        for channel in session.channels:
            self.assertEqual(session.values(channel).size, axis.size)
            self.assertEqual(channel.sample_rate, 100.0)          # 落在主时间基上
            self.assertIsNotNone(channel.update_rate)             # 真实更新率另有记录
            self.assertGreater(channel.update_rate, 0)
        self.assertEqual(len(session.channels), 61)

    def test_zero_order_hold_lands_every_frame_on_the_master_grid(self):
        """每条已定义报文的值变化时刻，必须与它的帧时间戳逐点对齐。

        这条挡的是"被压缩到前 69% / 变成 50 Hz"那类静默错位——把源速率交给
        ``channels.hold_factor`` 就是这么错的。测试自己按帧时间戳重算一遍保持，
        与场次里的列逐点比较（独立的一条路径，不是把实现抄一遍）。
        """
        session = canlog.read_can_session([self.small], dbc_dir=[DBC_DIR], write_sidecar=False)
        row = next(r for r in session.can["channels"] if r["message"] == "Front_Wheel_Sensors")
        signal = dbc.parse(SENSORS_DBC.read_text(encoding="utf-8", errors="replace")) \
            .messages[(False, 0x662)].signal(row["signal"])

        # 自己读一遍这份 CSV 里 0x662 的帧（不经过 canlog 的扫描）
        moments, raw = [], []
        with self.small.open("r", encoding="gbk", errors="replace", newline="") as handle:
            handle.readline()
            for line in handle:
                cells = line.split(",")
                if len(cells) < 10 or cells[4].strip().lower() != "0x662":
                    continue
                moments.append(float(cells[2]))
                payload = bytes.fromhex(cells[9].split("|", 1)[1])
                raw.append(dbc.signal_value(signal, payload))
        self.assertGreater(len(moments), 100)
        # 时间轴的原点是**第一份文件的第一帧**，不是这条报文的第一帧
        times = np.asarray(moments) - canlog.summarise(self.small)["first_t"]
        axis = timebase.axis(session)
        index = np.clip(np.searchsorted(times, axis, side="right") - 1, 0, len(raw) - 1)
        expected = np.asarray(raw, dtype=np.float64)[index]
        self.assertTrue(np.array_equal(session.values(row["name"]), expected),
                        "零阶保持的结果与按帧时间戳重算的不一致")
        # 而且真的有台阶——不然上面那条断言是空的
        self.assertGreater(np.count_nonzero(np.diff(expected)), 10)

    def test_the_report_carries_the_measured_numbers(self):
        """9 份文件 = 7 次记录；13 份 DBC **取并集**之后的实测数字。

        只挑一份的话是 Sensors.dbc 的 46 条 / 24.6%——用户后来补的 11 份 DBC
        一条都不参与解码（ticket #40 的那个 bug）。并集把它变成 61 条 / 43.42%。
        """
        groups = canlog.group_recordings(self.files)
        sessions = [canlog.read_can_session([item["path"] for item in group["items"]],
                                            dbc_dir=[DBC_DIR], write_sidecar=False,
                                            use_sidecar=False)
                    for group in groups]
        self.assertEqual(sum(session.can["frames"] for session in sessions), 3684850)
        ids = set()
        for session in sessions:
            ids |= {row["id"] for row in session.can["undecoded"]}
            ids |= set(session.can["covered_ids"])
        self.assertEqual(len(ids), 43)

        can = sessions[0].can                     # 第一场 = 前两份文件并起来的那一场
        self.assertEqual(can["frames"], 1984096)
        self.assertTrue(can["merged"])
        self.assertEqual(len(can["sources"]), 2)
        self.assertEqual(can["dbc"]["method"], "union")
        self.assertEqual(len(can["dbc"]["files"]), 13)
        self.assertEqual(can["dbc"]["messages"], 91)
        self.assertEqual(len(can["channels"]), 61)
        self.assertAlmostEqual(can["coverage"], 0.4018, places=3)
        # 每条通道 → 来自哪份 DBC；每份 DBC → 贡献了哪些 ID 与多少条通道
        self.assertTrue(all(row["dbc"] for row in can["channels"]))
        by_file = {row["file"]: row for row in can["dbc"]["files"]}
        self.assertEqual(by_file["Sensors.dbc"]["channels"], 46)
        self.assertEqual(by_file["TH.dbc"]["channels"], 5)
        self.assertEqual(by_file["TH.dbc"]["used_ids"], ["0xC1"])
        self.assertTrue(all(len(row["sha256"]) == 64 for row in can["dbc"]["files"]))
        speed_rows = [row for row in can["channels"] if row["name"] == "Vx_KF"]
        self.assertEqual(len(speed_rows), 1)
        self.assertEqual(speed_rows[0]["unit"], "kph")
        self.assertEqual(speed_rows[0]["dbc"], "TH.dbc")
        # 单份最多只给 46 条——通道数必须比它多，否则就是没取并集
        self.assertGreater(len(can["channels"]),
                           max(row["channels"] for row in can["dbc"]["files"]))
        # 未定义 ID：按帧数从多到少，6 个诊断 ID 各自标出来
        undecoded = can["undecoded"]
        self.assertEqual(sum(row["frames"] for row in undecoded) + can["covered_frames"],
                         can["frames"])
        # 全部 7 场加起来才是实测的 1,600,064 帧被覆盖（43.42%）
        covered = sum(item.can["covered_frames"] for item in sessions)
        self.assertEqual(covered, 1600064)
        self.assertAlmostEqual(covered / 3684850, 0.4342, places=3)
        totals: dict[str, int] = {}
        for item in sessions:
            for row in item.can["undecoded"]:
                totals[row["id"]] = totals.get(row["id"], 0) + row["frames"]
        # 剩下的最大一块读不懂的是 0xCC（139,050 帧，约 100 Hz）
        self.assertEqual(max(totals.items(), key=lambda pair: pair[1]), ("0xCC", 139050))
        diagnostic = {row["id"] for row in undecoded if row["diagnostic"]}
        self.assertTrue(diagnostic.issubset({"0x7E0", "0x7E2", "0x7E3", "0x7E4", "0x7E6", "0x7E7"}))
        self.assertTrue(all(row["diagnostic"] for row in undecoded
                            if row["id"].startswith("0x7E")))
        self.assertTrue(all(row["sample"] for row in undecoded))
        self.assertEqual([row["frames"] for row in undecoded],
                         sorted((row["frames"] for row in undecoded), reverse=True))
        # 那份 dashboard DBC 一条都对不上：报告要写明原因（全是扩展帧）
        self.assertEqual(by_file[DASHBOARD_DBC.name]["covered_frames"], 0)
        self.assertTrue(
            any("扩展帧" in text and DASHBOARD_DBC.name in text for text in can["notes"]),
            "报告只说了'布局对不上'，没说清那份 dashboard DBC 全是扩展帧、"
            "而日志里没有扩展帧（ticket #38 的验收点名了这条）",
        )
        self.assertTrue(any("0x9D22" in text for text in can["notes"]),
                        "报告里的扩展帧 ID 要按 DBC 文件的写法印（0x9D22xxxx）")
        # 有车速就有距离轴（Vx_KF 积分，实测 0.1–5546.8 m），而且是**算出来**的
        axis = derive.distance_series(sessions[0])
        self.assertGreater(axis[-1], 5000.0)
        self.assertTrue(np.all(np.diff(axis) >= -1e-9), "距离轴不能倒退")
        self.assertTrue(any("距离轴来源" in text for text in can["notes"]))

    def test_a_stationary_log_reports_that_there_is_no_distance_axis(self):
        """几十秒原地不动的日志：车速通道在、但值是死的，距离轴就**没有**。

        实测 ``2026_10_03_201147_ID0001.csv`` 的 ``Vx_KF`` 值域 −0.05…0.00 kph。
        "有通道但没数据"和"没有这条通道"是两回事，报告要分别说清。
        """
        session = canlog.read_can_session([self.small], dbc_dir=[DBC_DIR],
                                          write_sidecar=False, use_sidecar=False)
        self.assertIn("Vx_KF", [channel.name for channel in session.channels])
        self.assertIsNone(derive.speed_channel(session))
        with self.assertRaises(ValueError):
            derive.distance_series(session)
        self.assertTrue(any("没有距离轴" in text for text in session.can["notes"]))

    def test_pinning_one_dbc_still_works_and_is_recorded(self):
        """想只按一份解也行——但那要写进侧车（``dbc_mode: file``），不是默认行为。"""
        session = canlog.read_can_session([self.small], dbc_dir=[DBC_DIR],
                                          dbc_file="Sensors.dbc",
                                          write_sidecar=False, use_sidecar=False)
        self.assertEqual(session.can["dbc"]["method"], "file")
        self.assertEqual(session.can["dbc"]["pinned"], "Sensors.dbc")
        self.assertEqual(len(session.can["dbc"]["files"]), 1)
        self.assertEqual(len(session.channels), 46)

    def test_a_conflict_between_two_dbcs_is_reported_and_resolved(self):
        """同一条 ID 两份不同定义：按覆盖帧数选，并把冲突写进报告（不静默合并）。

        真实的 13 份之间没有冲突（实测）。这里**临时造一个**：把 0x660 用另一套
        信号写进一份新的 DBC——如果合并是静默的，通道会变成谁先读谁算数。
        """
        work = scratch("_can_conflict")
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        for path in DBC_DIR.glob("*.dbc"):
            shutil.copy2(path, work / path.name)
        (work / "zz_bogus.dbc").write_text(
            'BO_ 1632 Right_Rear_Sensors: 8 Other\n'
            ' SG_ Bogus_Channel : 0|8@1+ (1,0) [0|255] "" X\n',
            encoding="utf-8",
        )
        try:
            session = canlog.read_can_session([self.small], dbc_dir=[work],
                                              write_sidecar=False, use_sidecar=False)
            conflicts = session.can["dbc"]["conflicts"]
            self.assertEqual([row["id"] for row in conflicts], ["0x660"])
            # Sensors.dbc 覆盖的帧多得多，所以是它赢；写的每一步都要能复核
            self.assertEqual(conflicts[0]["chosen"], "Sensors.dbc")
            self.assertEqual(conflicts[0]["rejected"], "zz_bogus.dbc")
            self.assertTrue(any("0x660" in text for text in session.can["notes"]))
            signals = {row["signal"] for row in session.can["channels"]
                       if row["message"] == "Right_Rear_Sensors"}
            self.assertIn("RR_Water_Temperature", signals)      # 用的是赢的那份
            self.assertNotIn("Bogus_Channel", signals)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_the_sidecar_records_the_choices_and_column_roles_can_be_overridden(self):
        work = scratch("_can_sidecar")
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        copy = work / self.small.name
        shutil.copy2(self.small, copy)
        try:
            session = canlog.read_can_session([copy], dbc_dir=[DBC_DIR])
            self.assertEqual(session.can["dbc"]["method"], "union")
            self.assertEqual(len(session.channels), 61)
            stored = sidecar.read("canmap", copy)
            # 并集不写单个文件名，只把"这次用的是并集"记下来
            self.assertNotIn("dbc", stored)
            self.assertEqual(stored["dbc_mode"], "union")
            self.assertEqual(stored["rate"], 100.0)
            self.assertEqual(stored["roles"]["id"], "ID号")
            # 旧侧车（有 dbc、没有 dbc_mode）是按并集解，并且要说出来——
            # 那是旧版本每次导入自动写的值，和"点名固定一份"长得一样
            sidecar.write("canmap", copy, {"roles": {"id": "ID号"}, "dbc": "Sensors.dbc"})
            legacy = canlog.read_can_session([copy], dbc_dir=[DBC_DIR], write_sidecar=False)
            self.assertEqual(legacy.can["dbc"]["method"], "union")
            self.assertEqual(legacy.can["dbc"]["legacy_pin_ignored"], "Sensors.dbc")
            self.assertTrue(any("并集" in text for text in legacy.can["notes"]))
            # 换一个 CAN 工具导出的列名不一样：侧车里改掉角色就能读
            sidecar.write("canmap", copy, {"roles": {"id": "CANID"}, "dbc_mode": "union"})
            with self.assertRaises(ValueError) as caught:
                canlog.read_can_session([copy], dbc_dir=[DBC_DIR], write_sidecar=False)
            self.assertIn("表头里找不到", str(caught.exception))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_a_missing_dbc_directory_says_what_to_do(self):
        """哪儿都找不到 DBC 时要吵，并且说清把文件放哪。

        ``default_dbc_directories`` 平时会把仓库里的 ``i2pro_data/dbc`` 兜进来
        （原始帧日志在 ``can_data/``，DBC 不在它旁边），所以这里把它换成一条死路。
        """
        from unittest import mock

        nowhere = scratch("_no_such_dbc")
        with mock.patch.object(canlog, "default_dbc_directories", lambda path: [nowhere]):
            with self.assertRaises(ValueError) as caught:
                canlog.read_can_session([self.small], write_sidecar=False, use_sidecar=False)
        message = str(caught.exception)
        self.assertIn("DBC", message)
        self.assertTrue("放进去" in message or "把本车的 DBC" in message, message)

    # ------------------------------------------------------------------- 并场
    def test_contiguous_recordings_merge_into_seven(self):
        groups = canlog.group_recordings(self.files)
        self.assertEqual(len(groups), 7)
        merged = [[item["path"].name for item in group["items"]] for group in groups]
        self.assertIn(["2026_10_03_173345_ID0001.csv", "2026_10_03_173936_ID0001.csv"], merged)
        self.assertIn(["2026_10_03_174748_ID0001.csv", "2026_10_03_175413_ID0001.csv"], merged)
        evidence = " ".join(text for group in groups for text in group["evidence"])
        self.assertIn("0.026 ms", evidence)               # 实测的墙钟差
        self.assertIn("0.249 ms", evidence)
        # 关掉并场就是 9 份各自一场（判据是"两个时钟同时接上"）
        sessions = [canlog.read_can_session([path], dbc_dir=[DBC_DIR], merge=False,
                                            write_sidecar=False) for path in self.files]
        self.assertEqual(len(sessions), 9)
        self.assertEqual(sum(session.can["frames"] for session in sessions), 3684850)

    def test_the_library_shows_one_entry_per_recording(self):
        work = scratch("_can_library")
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        try:
            for path in self.files:
                shutil.copy2(path, work / path.name)
            library = librarymod.SessionLibrary([work, DATA], cache_size=1, maths_root=ROOT)
            names = [name for name in library.names() if name.startswith("2026_10_03")]
            self.assertEqual(len(names), 7)
            merged = [name for name in names if name.endswith("+1")]
            self.assertEqual(len(merged), 2)
            summary = library.summary(names[0])
            self.assertEqual(summary["format"], "can")
            # 61 条来自 DBC 并集，外加全局数学通道 `速度kmh`——它的表达式是
            # `'Vx KF'`（空格），而 CAN 那条叫 `Vx_KF`（下划线）；名字匹配允许
            # 空格/下划线互换，所以同一个定义两边的数据都能用（实测）。
            self.assertEqual(summary["channels"], 62)
            # 列表缓存：第二次不再解码（第一次要解码全部 CAN 场次，几秒）
            first = library.listing()
            second = library.listing()
            self.assertEqual(len(first), len(second))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ----------------------------------------------------------------- 性能
    def test_one_file_stays_under_five_seconds(self):
        big = CAN_DATA / "2026_10_03_173345_ID0001.csv"
        if not big.exists():
            self.skipTest("缺那份 91 MB 的帧表")
        start = time.perf_counter()
        canlog.read_can_session([self.small], dbc_dir=[DBC_DIR], merge=False,
                                write_sidecar=False)
        small_seconds = time.perf_counter() - start
        start = time.perf_counter()
        canlog.read_can_session([big], dbc_dir=[DBC_DIR], merge=False, write_sidecar=False)
        big_seconds = time.perf_counter() - start
        # 工作台走的是**默认**那条（merge=True：先看一眼同目录的邻居，判断这份是不是
        # 被切开的记录），所以判据要落在它身上，不能只测 merge=False 那条捷径。
        start = time.perf_counter()
        canlog.read_can_session([big], dbc_dir=[DBC_DIR], write_sidecar=False)
        merged_seconds = time.perf_counter() - start
        print(f"\n[#38 实测] 单份 {big.name}（91 MB）{big_seconds:.1f} s，"
              f"小份 {small_seconds:.1f} s；工作台那条（merge=True）{merged_seconds:.1f} s")
        self.assertLess(big_seconds, 5.0)
        self.assertLess(merged_seconds, 5.0)


class TestSpeedChannelResolution(unittest.TestCase):
    """同一个量在两条数据线上叫两个名字：``.ld`` 是 ``Vx KF``，CAN 是 ``Vx_KF``。

    这条缝漏过两次，所以这里把两种用途**分开**钉住：

    * 距离轴（``derive.speed_channel``）要求候选**真的在动**——拿一条死通道积分
      会得到一条假的 0 m 轴；
    * 概览条 / 轨迹着色（``render.display_speed_channel``）**不要求它在动**——
      车没动时原样画平线，比整条概览条消失更诚实。

    两处共用 ``derive.resolve_channel`` 一套名字匹配（去掉空格/下划线/大小写）。
    """

    def _can(self, name="2026_10_03_173345_ID0001.csv"):
        path = CAN_DATA / name
        if not path.exists():
            self.skipTest(f"缺 {path.name}")
        return canlog.read_can_session([path], dbc_dir=[DBC_DIR], merge=False,
                                       write_sidecar=False, use_sidecar=False)

    def test_a_squashed_name_finds_the_channel_whatever_the_spacing(self):
        if not SENSORS_DBC.exists():
            self.skipTest("缺 i2pro_data/dbc/")
        session = self._can()
        self.assertIn("Vx_KF", [channel.name for channel in session.channels])
        # 三种写法都落到同一条通道上；本场次没有的名字仍然是 None（不硬凑）
        for spelling in ("Vx_KF", "Vx KF", "vx kf"):
            self.assertEqual(derive.resolve_channel(session, spelling), "Vx_KF")
        self.assertIsNone(derive.resolve_channel(session, "Ground Speed"))
        # 原样存在时原样返回——不许把 `Vx KF` 改写成 `Vx_KF`（CSV 列名归一表也用它）
        if not HILL.exists():
            self.skipTest("缺金标准场次")
        with ld.LogFile.read(HILL) as hill:
            self.assertEqual(derive.resolve_channel(hill, "Vx KF"), "Vx KF")

    def test_the_overview_strip_shows_speed_on_both_data_lines(self):
        """``.ld`` 与 CAN 的概览条都必须画在速度上，而不是兜底的第一条通道。"""
        if not HILL.exists():
            self.skipTest("缺金标准场次")
        with ld.LogFile.read(HILL) as hill:
            self.assertEqual(render.display_speed_channel(hill), "Vx KF")
        session = self._can()
        self.assertEqual(render.display_speed_channel(session), "Vx_KF")

    def test_the_distance_axis_still_ignores_a_dead_speed_channel(self):
        """概览条认``Vx_KF``，但距离轴**不**认——那条日志的车没动（实测 −0.05…0.00）。"""
        stationary = self._can("2026_10_03_201147_ID0001.csv")
        self.assertEqual(render.display_speed_channel(stationary), "Vx_KF")
        self.assertIsNone(derive.speed_channel(stationary))
        with self.assertRaises(ValueError):
            derive.distance_series(stationary)

    def test_the_overview_channel_falls_back_only_without_any_speed(self):
        session = self._can()
        self.assertEqual(render.overview_channel(session, ["Steering_Linear"]), "Vx_KF")
        # 把速度候选都拿掉（空表）时，才轮到第一条被选中的通道
        saved = render.SPEED_FOR_COLORING
        render.SPEED_FOR_COLORING = ()
        try:
            self.assertEqual(render.overview_channel(session, ["Steering_Linear"]),
                             "Steering_Linear")
            self.assertIsNone(render.overview_channel(session, []))
        finally:
            render.SPEED_FOR_COLORING = saved

    def test_the_golden_sessions_keep_the_channel_they_always_showed(self):
        """冻结：改这条解析不许动 ``.ld`` 场次的概览条（23 个场次逐个比对过）。"""
        for path in (HILL, ENDURANCE):
            if not path.exists():
                self.skipTest(f"缺 {path.name}")
            with ld.LogFile.read(path) as log:
                self.assertEqual(render.display_speed_channel(log), "Vx KF")


class TestCanSessionSurface(unittest.TestCase):
    """CAN 场次在服务端的门面：概览条要有、报表要说清下一步（ticket #38 / #41）。

    这两条都是"看着像成功其实什么都没有"的形态：概览条返回 ``null`` 时前端只是
    不画那条横条，报表返回空表时看起来像"算出来就是零"。
    """

    def setUp(self):
        if not CAN_DATA.exists() or not SENSORS_DBC.exists():
            self.skipTest("缺 can_data/ 或 i2pro_data/dbc/")

    def _library(self):
        return librarymod.SessionLibrary(
            [CAN_DATA, DATA], maths_root=str(ROOT), worksheets_root=str(ROOT)
        )

    def test_the_overview_endpoint_is_not_empty_for_a_moving_can_log(self):
        from i3pro import api

        path = CAN_DATA / "2026_10_03_173345_ID0001.csv"
        if not path.exists():
            self.skipTest("缺那份 91 MB 的帧表")
        library = self._library()
        name = next(n for n in library.names() if n.startswith("2026_10_03_173345"))
        body = api.Api(library).handle(["session", name, "overview"], {}, "GET").body
        payload = json.loads(body)
        self.assertIsNotNone(payload, "跑起来的 CAN 场次没有概览条")
        self.assertEqual(payload["name"], "Vx_KF")
        self.assertGreater(len(payload["time"]), 100)

    def test_the_report_tells_you_to_drop_a_beacon_when_there_are_no_laps(self):
        from i3pro import api

        path = CAN_DATA / "2026_10_03_201147_ID0001.csv"
        if not path.exists():
            self.skipTest("缺那份小帧表")
        library = self._library()
        name = next(n for n in library.names() if n.startswith("2026_10_03_201147"))
        response = api.Api(library).handle(["session", name, "report"], {}, "GET")
        payload = json.loads(response.body)
        self.assertEqual(response.status, 400)
        self.assertIn("信标", payload["error"])
        # 说"报表"而不是区段模块那句"再来分区段"——用户在报表上会以为点错了地方
        self.assertIn("报表", payload["error"])

        # 快照那条路走的是 payload 里的 report 字段：**不许给一张空表**
        # （空表看起来像"算出来就是零"），要给同一条下一条指令。
        snapshot = render.build_payload(library.get(name), with_report=True)["report"]
        self.assertIsNone(snapshot["time"], "没有圈时不该给一张空表")
        self.assertIn("信标", snapshot["error"])
        self.assertIn("报表", snapshot["error"])


class TestTextImport(unittest.TestCase):
    """读分隔文本成场次（ticket #32）：`.txt` / `.tsv`，以及分隔符不是逗号的 `.csv`。

    这里最要紧的一条判据是**预览与导入一致**：预览面板显示的分隔符 / 表头行 / 通道
    列表，和点下"导入"之后真正得到的东西，必须来自同一次读取。分开写两套解析
    （前端猜一套、后端读一套）正是这类功能最常见的坏法——面板上说"8 个通道"，
    导入完是 1 个。
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="i3pro-txt-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        path = self.dir / name
        path.write_text(text, encoding=encoding)
        return path

    @staticmethod
    def body(rows: int = 6, columns: int = 3) -> list[list[str]]:
        header = ["Time", "Vx KF [km/h]", "TH"][:columns]
        return [header] + [[f"{i / 100:.3f}", str(i), str(i * 2)] for i in range(rows)]

    def table(self, name: str, delimiter: str, rows: int = 6) -> Path:
        return self.write(name, "\n".join(delimiter.join(r) for r in self.body(rows)) + "\n")

    # ------------------------------------------------------------ 四种分隔符
    def test_the_four_delimiters_all_read_as_the_same_session(self):
        wanted = None
        for i, delimiter in enumerate((",", "\t", ";", "|")):
            path = self.table(f"t{i}.txt", delimiter)
            preview = txtlog.preview(path)
            self.assertTrue(preview["ready"], preview.get("error"))
            self.assertEqual(preview["delimiter"], delimiter, preview)
            self.assertEqual(preview["channels"], ["Vx KF", "TH"], preview)
            session = csvlog.open_session(path)
            self.assertEqual([c.name for c in session.channels], ["Vx KF", "TH"])
            self.assertEqual(session.channel("Vx KF").unit, "km/h")
            self.assertAlmostEqual(session.sample_rate, 100.0)
            values = session.values("Vx KF")
            if wanted is None:
                wanted = values
            else:
                np.testing.assert_array_equal(values, wanted)

    def test_a_multi_space_file_reads_too(self):
        """一份用连续空格对齐的表（有人拿它当"表格"存）。"""
        path = self.write("space.txt", "\n".join([
            "Time    Vx      TH",
            "0.000   0       0",
            "0.010   1       2",
            "0.020   2       4",
        ]) + "\n")
        preview = txtlog.preview(path)
        self.assertEqual(preview["delimiter"], " ")
        self.assertEqual(preview["delimiter_label"], "多空格")
        self.assertEqual(preview["channels"], ["Vx", "TH"])

    # ---------------------------------------------------------- 预览 = 导入
    def test_the_preview_names_the_channels_the_import_will_produce(self):
        path = self.table("same.txt", ";")
        preview = txtlog.preview(path)
        session = csvlog.open_session(path)
        self.assertEqual(preview["channels"], [c.name for c in session.channels])
        self.assertEqual(preview["samples"], int(session.time.size))
        self.assertAlmostEqual(preview["sample_rate"], session.sample_rate)
        self.assertEqual(preview["rows"][0][0], "Time")     # 前 20 行的原样内容
        self.assertEqual(preview["width"], 3)

    def test_a_wrong_guess_is_visible_in_the_preview_and_fixable(self):
        """分隔符猜错时，预览里要**看得出来**，改一下就能读对。"""
        path = self.table("wrong.txt", ";")
        broken = txtlog.preview(path, delimiter=",")
        self.assertFalse(broken["ready"])
        self.assertEqual(broken["delimiter"], ",")
        # 报的必须是"下一步做什么"：整份表被读成一列时要说分隔符，别只说读不出来
        self.assertIn("分隔符", broken["error"])
        fixed = txtlog.preview(path, delimiter=";")
        self.assertTrue(fixed["ready"], fixed.get("error"))
        self.assertEqual(fixed["channels"], ["Vx KF", "TH"])

    # ------------------------------------------------------------ 侧车记忆
    def test_the_sidecar_remembers_how_the_file_was_read(self):
        path = self.write("two.txt", "说明行\nTime;TH\ns;\n" + "".join(
            f"{i};{i * 2}\n" for i in range(6)
        ))
        first = csvlog.open_session(path)                 # 自动认：分号 + 第 2 行是表头
        self.assertEqual([c.name for c in first.channels], ["TH"])
        csvlog.save_options(path, delimiter=";", header=1, unit_row=True)
        second = csvlog.open_session(path)                # 第二次：照侧车来
        np.testing.assert_array_equal(second.time, first.time)
        self.assertEqual(second.channel("TH").unit, "")
        # 改别的键（列名覆盖）不能把这些选项冲掉——同一个文件里两个东西在存
        csvlog.save_map(path, {"TH": "节气门"}, {})
        self.assertEqual(csvlog.load_options(path)["header"], 1)
        self.assertEqual(csvlog.open_session(path).channels[0].name, "节气门")
        # 空值 = 改回自动，不是"存了个空字符串"
        csvlog.save_options(path, header=None)
        self.assertNotIn("header", csvlog.load_options(path))

    def test_an_unknown_option_name_is_refused(self):
        path = self.table("opts.txt", ",")
        with self.assertRaises(ValueError) as caught:
            csvlog.save_options(path, delimeter=";")
        self.assertIn("delimeter", str(caught.exception))

    # -------------------------------------------------------- 没有时间列
    def test_a_table_without_a_time_column_is_refused_with_a_next_step(self):
        path = self.write("notime.txt", "Vx\tTH\n" + "".join(
            f"{i}\t{i}\n" for i in range(6)
        ))
        preview = txtlog.preview(path)
        self.assertFalse(preview["ready"])
        self.assertIn("时间列", preview["error"])
        self.assertIn("--rate", preview["error"])          # 说清下一步
        with self.assertRaises(ValueError):
            csvlog.open_session(path)

    def test_a_generated_time_column_is_reproducible_and_recorded(self):
        path = self.write("notime.txt", "Vx\tTH\n" + "".join(
            f"{i}\t{i * 2}\n" for i in range(6)
        ))
        preview = txtlog.preview(path, generate_rate=50)
        self.assertTrue(preview["ready"], preview.get("error"))
        self.assertEqual(preview["channels"], ["Vx", "TH"])   # 时间列不算通道
        self.assertIn("50 Hz", preview["parse_note"])
        csvlog.save_options(path, generate_rate=50)
        first = csvlog.open_session(path)
        np.testing.assert_allclose(first.time, np.arange(6) / 50.0)
        self.assertAlmostEqual(first.sample_rate, 50.0)
        second = csvlog.open_session(path)                    # 第二次照样复现
        np.testing.assert_array_equal(second.time, first.time)
        self.assertIn("生成", second.metadata()["parse_note"])
        self.assertTrue(any(row.get("generated") for row in second.report))

    def test_a_real_time_column_is_never_replaced_by_a_generated_one(self):
        """勾了"生成时间列"但表里本来就有时间：以文件里的为准，别把真时间盖掉。"""
        path = self.table("hastime.txt", ",")
        session = csvlog.open_session(path, generate_rate=1)
        np.testing.assert_allclose(session.time[:3], [0.0, 0.01, 0.02])
        self.assertFalse(any(row.get("generated") for row in session.report))

    # -------------------------------------------------------- 表头行 / 无表头
    def test_a_header_override_points_at_the_right_row(self):
        path = self.write("messy.txt", "导出说明\n\nTime\tTH\ns\t\n" + "".join(
            f"{i}\t{i * 3}\n" for i in range(6)
        ))
        shown = txtlog.preview(path, header=2, unit_row=True)
        self.assertTrue(shown["ready"], shown.get("error"))
        self.assertEqual(shown["effective_header"], 2)
        self.assertTrue(shown["effective_unit_row"])
        self.assertEqual(shown["channels"], ["TH"])
        # 选错行时要说清"只有几行"，而不是读出一堆莫名其妙的列
        with self.assertRaises(ValueError) as caught:
            csvlog.open_session(path, header=99)
        self.assertIn("表头行", str(caught.exception))

    def test_no_header_row_gets_positional_names(self):
        path = self.write("bare.txt", "".join(f"{i}\t{i * 2}\n" for i in range(6)))
        session = csvlog.open_session(path, header=csvlog.NO_HEADER, generate_rate=100)
        self.assertEqual([c.name for c in session.channels], ["列1", "列2"])
        np.testing.assert_allclose(session.values("列2")[:3], [0.0, 2.0, 4.0])
        self.assertIn("没有表头行", session.metadata()["parse_note"])

    # ------------------------------------------------------------ CSV 同路
    def test_csv_and_txt_of_one_table_read_the_same(self):
        """同一张表存成 CSV 与 TXT，读出来必须一模一样（ticket #32 的验收条目）。"""
        comma = self.write("same.csv", "\n".join(
            ",".join(r) for r in self.body()
        ) + "\n")
        tabs = self.write("same.txt", "\n".join(
            "\t".join(r) for r in self.body()
        ) + "\n")
        left, right = csvlog.open_session(comma), csvlog.open_session(tabs)
        self.assertEqual([c.name for c in left.channels], [c.name for c in right.channels])
        np.testing.assert_array_equal(left.time, right.time)
        np.testing.assert_array_equal(left.values("TH"), right.values("TH"))
        # 分隔符不是逗号的 CSV 也吃同一条路：`open_session` 按内容分流，不是按扩展名
        odd = self.write("odd.csv", "Time;TH\n0.000;0\n0.010;1\n0.020;2\n")
        self.assertEqual([c.name for c in csvlog.open_session(odd).channels], ["TH"])

    # ------------------------------------------------------------ 界面与接口
    def test_the_import_page_offers_the_parse_choices(self):
        library = librarymod.SessionLibrary([self.dir])
        page = server.index_page(library)
        for needle in ('accept=".ld,.ldx,.csv,.xlsx,.txt,.tsv"', 'id="cardOk"',
                       'id="optDelimiter"', 'id="optEncoding"', 'id="optHeader"',
                       'id="optUnitRow"', 'id="optRate"'):
            self.assertIn(needle, page)

    def test_the_api_stages_previews_and_commits(self):
        from i3pro import api as apimod

        library = librarymod.SessionLibrary([self.dir])
        api = apimod.Api(library)
        text = "Time;Vx KF;TH\ns;km/h;\n" + "".join(
            f"{i};{i * 2};{i}\n" for i in range(20)
        )
        staged = json.loads(api.handle(
            ["import"], {"name": ["接口.csv"]}, "PUT", text.encode("utf-8")
        ).body)
        self.assertTrue(staged["ok"])
        token = staged["token"]
        self.assertEqual(staged["preview"]["delimiter"], ";")
        # 暂存期间这份表**不许**进场次列表
        self.assertEqual(library.names(), [])
        again = json.loads(api.handle(
            ["import", "preview"], {"token": [token], "delimiter": [";"]}, "GET"
        ).body)
        self.assertEqual(again["preview"]["channels"], ["Vx KF", "TH"])
        done = api.handle(["import", "commit"],
                          {"token": [token], "delimiter": [";"]}, "POST")
        summary = json.loads(done.body)
        self.assertEqual(done.status, 200, summary)
        self.assertEqual(summary["channels"], 2)
        self.assertEqual(library.names(), ["接口"])
        # 选择的解析方式写进了侧车：换一次读取照样是分号
        self.assertEqual(csvlog.load_options(self.dir / "接口.csv")["delimiter"], ";")
        self.assertFalse(Path(staged["path"]).exists(), "提交之后暂存要收掉")

    def test_the_api_cancel_deletes_the_staged_file(self):
        from i3pro import api as apimod

        library = librarymod.SessionLibrary([self.dir])
        api = apimod.Api(library)
        staged = json.loads(api.handle(
            ["import"], {"name": ["取消.txt"]}, "PUT", b"Time\tTH\n0\t0\n1\t1\n"
        ).body)
        token = staged["token"]
        self.assertTrue(Path(staged["path"]).exists())
        cancelled = api.handle(["import", "cancel"], {"token": [token]}, "DELETE")
        self.assertEqual(cancelled.status, 200)
        self.assertFalse(Path(staged["path"]).exists())
        self.assertEqual(library.names(), [])
        # 拿一个过期的编号来提交：要说清下一步，而不是 500
        stale = api.handle(["import", "commit"], {"token": [token]}, "POST")
        self.assertEqual(stale.status, 404)
        self.assertIn("重新选", json.loads(stale.body)["error"])

    def test_a_bad_option_is_refused_with_the_list_of_good_ones(self):
        from i3pro import api as apimod

        library = librarymod.SessionLibrary([self.dir])
        api = apimod.Api(library)
        staged = json.loads(api.handle(
            ["import"], {"name": ["x.txt"]}, "PUT", b"Time\tTH\n0\t0\n1\t1\n"
        ).body)
        bad = api.handle(["import", "preview"],
                         {"token": [staged["token"]], "delimiter": ["@"]}, "GET")
        self.assertEqual(bad.status, 400)
        self.assertIn("不认识的分隔符", json.loads(bad.body)["error"])

    def test_the_cli_preview_shows_the_reading_and_imports_with_rate(self):
        from i3pro import cli

        source = self.write("命令.txt", "Vx\tTH\n" + "".join(
            f"{i}\t{i * 2}\n" for i in range(10)
        ))
        target = self.dir / "data"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["import", str(source), "--data", str(target),
                             "--preview", "--rate", "100"])
        printed = out.getvalue()
        self.assertEqual(code, 0, printed)
        self.assertIn("100 Hz", printed)
        self.assertFalse((target / "命令.txt").exists(), "--preview 不许动文件")
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["import", str(source), "--data", str(target),
                             "--rate", "100"])
        self.assertEqual(code, 0)
        session = csvlog.open_session(target / "命令.txt")
        np.testing.assert_allclose(session.time[:3], [0.0, 0.01, 0.02])

    def test_staging_is_swept_but_fresh_ones_are_kept(self):
        from i3pro import importer as importermod

        fresh = importermod.staging_dir("aabbccdd11223344")
        fresh.mkdir(parents=True, exist_ok=True)
        (fresh / "x.txt").write_text("a", encoding="utf-8")
        old = importermod.staging_dir("ffffffffffffffff")
        old.mkdir(parents=True, exist_ok=True)
        stale = time.time() - 7200
        os.utime(old, (stale, stale))

        removed = importermod.sweep_staging(max_age=3600)
        self.assertTrue(fresh.exists(), "一小时内的暂存不该被扫掉")
        self.assertFalse(old.exists(), "超过一小时的暂存要收掉")
        self.assertGreaterEqual(removed, 1)
        shutil.rmtree(fresh, ignore_errors=True)

    def test_a_sparse_gps_column_does_not_hide_the_laps(self):
        """我们自己的导出（auto 模式）会给慢通道留空格子，GPS 也一样。

        那些空格子以前**被当成有效定位**——NaN 的两次比较都是 False，所以它既不算
        "0,0 掉星"也不算"卫星不足"，一路混进轨迹：`x`/`y` 里带 NaN，切圈从 7 圈
        **静默**退化成 1 圈（1 圈还标着"不完整"）。这条是 ticket #32 的文本导入
        验收里撞出来的：导出 → 读回来 → 圈没了，中间一个错都不报。
        """
        from i3pro import derive, ld as ldmod

        source = DATA / "20260908-cjh 高避5圈.ld"
        if not source.exists():
            self.skipTest("缺金标准数据 20260908-cjh 高避5圈.ld")
        log = ldmod.LogFile.read(source)
        try:
            csv_out = self.dir / "sparse.csv"
            request = exportmod.parse_request(log, {
                "channels": "selected",
                "names": "Vx KF,GPS Speed,GPS Latitude,GPS Longitude",
                "rate": "auto", "format": "csv",
            })
            exportmod.write(log, request, csv_out)
            wanted = len(lapsmod.detect_laps(log))
        finally:
            log.close()
        # 同一张表换成 TXT（分号）读进来：跟"队友发来一份分号表"是同一件事
        text = self.dir / "sparse.txt"
        with csv_out.open(encoding="utf-8-sig", newline="") as src, \
                text.open("w", encoding="utf-8", newline="") as dst:
            csv.writer(dst, delimiter=";").writerows(csv.reader(src))
        session = csvlog.open_session(text)
        blanks = int(np.isnan(session.values("GPS Latitude")).sum())
        self.assertGreater(blanks, 0, "这份导出本该是稀疏的（慢通道留空）")
        track = derive.gps_track(session)
        self.assertEqual(int(np.isnan(track["x"]).sum()), 0, "轨迹里不该有 NaN 点")
        laps = lapsmod.detect_laps(session)
        self.assertEqual(len(laps), wanted,
                         f"空格子把圈吃掉了：{len(laps)} 圈，应该有 {wanted} 圈")
        self.assertGreaterEqual(len([l for l in laps if l.complete]), 3)


class TestChannelStates(unittest.TestCase):
    """缺失通道的**三态**（ticket #34）：present / empty / missing。

    以前"缺通道"是静默丢弃（服务端跳过 + 前端 `.filter` 再滤一遍 + 零文案），
    用户看到的是"图坏了"。这一票把它变成看得见的状态，而且**三条状态必须分开**：

    * ``missing`` —— 本场次根本没有这条通道（灰显、不画、绝不自动剔除）；
    * ``empty``   —— 通道在，但整段没有一个有效样本（正常画，文案说清是哪种"没有"）；
    * 当前窗口没样本 —— **不作提示**（那只是缩放的结果，不是数据的问题）。

    判据是"三态只有一处实现"：`channels.resolve` / `channels.state` 是那一处，
    通道索引、报告、导出元数据、页面载荷都问它。所以这里既钉行为，也钉"别人问的是它"。
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="i3pro-state-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def csv_session(self, name: str = "三态.csv"):
        """一列有数，外加一条**整段是 NaN 的数学通道**——那就是 ``empty`` 的样子。

        为什么用数学通道造这条状态：CSV 导入会把**整列空**的列直接跳过
        （``csvlog`` 的"整列没有可用数值"），所以"通道在但一个有效样本都没有"
        在导入路径上到不了界面；数学通道会（表达式算出全 NaN 时）。CAN 那条路
        同理——没有报文的信号列就是全 NaN 挂着。
        """
        path = self.dir / name
        path.write_text(
            "Time,Vx KF [km/h]\n0.000,10\n0.010,20\n0.020,30\n0.030,40\n",
            encoding="utf-8",
        )
        session = csvlog.open_session(path)
        mathsmod.attach(
            session, {"全空数学通道": np.full(session.time.size, np.nan)}, [])
        return session

    # ------------------------------------------------------------- 三态本身
    def test_三态分开_本场没有的与整段没数据的不是一回事(self):
        session = self.csv_session()
        self.assertEqual(channels.state(session, "Vx KF"), channels.PRESENT)
        self.assertEqual(channels.state(session, "全空数学通道"), channels.EMPTY)
        self.assertEqual(channels.state(session, "本场根本没有"), channels.MISSING)

    def test_state_不知道的名字与空名字都算缺(self):
        session = self.csv_session()
        for name in ("", None, "   ", " 不存在的通道 "):
            self.assertEqual(channels.state(session, name), channels.MISSING,
                             f"{name!r} 该按「本场没有」算")

    def test_resolve_保序去重_空名字跳过(self):
        session = self.csv_session()
        got = channels.resolve(
            session, ["Vx KF", "没有的", "Vx KF", "", "全空数学通道"])
        self.assertEqual(list(got["states"]), ["Vx KF", "没有的", "全空数学通道"])
        self.assertEqual(got["present"], ["Vx KF"])
        self.assertEqual(got["missing"], ["没有的"])
        self.assertEqual(got["empty"], ["全空数学通道"])

    def test_有效样本数按真的能用的行算(self):
        """``sample_count`` 是"写了几行"，不是"有几行能用"——两者必须分开。"""
        session = self.csv_session()
        self.assertEqual(channels.valid_count(session, session.channel("Vx KF")), 4)
        self.assertEqual(
            channels.valid_count(session, session.channel("全空数学通道")), 0)
        self.assertEqual(session.channel("全空数学通道").sample_count, 4,
                         "这一列确实有 4 行，只是每行都是 NaN")

    # ------------------------------------------------- 三态在载荷与出口里
    def test_通道索引带着has_data_界面不用自己扫数据(self):
        session = self.csv_session()
        index = {c["name"]: c for c in render.channel_index(session)}
        self.assertTrue(index["Vx KF"]["has_data"])
        self.assertFalse(index["全空数学通道"]["has_data"])

    def test_页面载荷把缺的通道带出去而不是静默丢掉(self):
        """#34 的修 bug 那一半：以前 `build_payload` 在这里直接 `if log.has`。"""
        session = self.csv_session()
        payload = render.build_payload(session)
        self.assertEqual(payload["missing"], [])
        payload = render.build_payload(session, channels=["Vx KF", "缺掉的通道"])
        self.assertEqual(payload["missing"], ["缺掉的通道"])
        self.assertIn("Vx KF", payload["selected"])
        self.assertNotIn("缺掉的通道", payload["selected"])

    def test_通道报告把缺失与空列分开报(self):
        session = self.csv_session()
        table = reportmod.channel_report(
            session, [], None, ["Vx KF", "全空数学通道", "缺掉的通道"], by="lap",
        )
        self.assertEqual(table["missing"], ["缺掉的通道"])
        self.assertEqual(table["empty"], ["全空数学通道"])
        self.assertEqual(table["channels"], ["Vx KF", "全空数学通道"])

    def test_金标准场次上的通道报告把缺的挑出来(self):
        """真数据上：点一条本场没有的通道，报告要把它列进 missing 而不是悄悄少一行。"""
        if not HILL.exists():
            self.skipTest(f"缺金标准数据 {HILL.name}")
        log = ld.LogFile.read(HILL)
        self.addCleanup(log.close)
        laps = lapsmod.detect_laps(log)
        table = reportmod.channel_report(
            log, laps, None, ["Vx KF", "缺掉的通道"], by="lap",
        )
        self.assertEqual(table["missing"], ["缺掉的通道"])
        self.assertEqual(table["empty"], [])
        self.assertEqual([row[4] for row in table["rows"]][:2], ["Vx KF", "Vx KF"])

    # ------------------------------------------------------- 真数据上的三态
    def test_金标准场次里没有空列(self):
        if not HILL.exists():
            self.skipTest(f"缺金标准数据 {HILL.name}")
        log = ld.LogFile.read(HILL)
        self.addCleanup(log.close)
        index = render.channel_index(log)
        dead = [c["name"] for c in index if not c["has_data"]]
        self.assertEqual(dead, [], f"这两条通道本该都是满的：{dead}")
        self.assertEqual(channels.state(log, "Vx KF"), channels.PRESENT)
        self.assertEqual(channels.state(log, "FSD13 Distance1"), channels.MISSING)


class TestMissingChannelsOnExport(_ExportBase):
    """缺失通道要一路走到出口：导出的元数据里必须有 `excluded_missing`（ticket #34）。"""

    def test_点名要了本场没有的通道_默认仍然报错并说下一步(self):
        with self.assertRaises(exportmod.ExportError) as ctx:
            self.request(names="Vx KF,缺掉的通道")
        self.assertIn("skip_missing=1", str(ctx.exception),
                      "报错要说清「确实想跳过就加这个开关」，只报「没有」等于没说")

    def test_明说跳过时导出照走_名单进元数据(self):
        request = self.request(names="Vx KF,缺掉的通道", skip_missing="1")
        self.assertEqual(request.excluded_missing, ("缺掉的通道",))
        self.assertEqual(request.channels, ("Vx KF",))
        self.assertIn("本场次没有", request.channel_source)
        meta = exportmod.metadata(self.log, request, 100, 2)
        self.assertEqual(meta["excluded_missing"], ["缺掉的通道"])
        # 空也要在：队友的脚本不该靠"有没有这个键"来猜这次缺没缺。
        clean = self.request(names="Vx KF")
        self.assertEqual(exportmod.metadata(self.log, clean, 100, 2)["excluded_missing"], [])

    def test_Excel的元数据sheet里也写着它(self):
        request = self.request(names="Vx KF,缺掉的通道", skip_missing="1")
        rows = list(exportmod._metadata_rows(
            exportmod.metadata(self.log, request, 100, 2)))
        hit = [row for row in rows if row[0] == "本场次没有（已跳过）"]
        self.assertEqual(len(hit), 1, f"Excel 元数据里没写缺了哪几条：{rows[-4:]}")
        self.assertIn("缺掉的通道", hit[0][1])

    def test_一条都不剩时要报错说下一步(self):
        with self.assertRaises(exportmod.ExportError) as ctx:
            self.request(names="缺一,缺二", skip_missing="1")
        self.assertIn("全部通道", str(ctx.exception))

    def test_导出的文件里真的没有那条通道的列(self):
        request = self.request(names="Vx KF,缺掉的通道", skip_missing="1",
                               rate="10", format="csv")
        out = self.tmp() / "kept.csv"
        exportmod.write(self.log, request, out)
        header = out.read_text(encoding="utf-8-sig").splitlines()[0]
        self.assertIn("Vx KF", header)
        self.assertNotIn("缺掉的通道", header)


if __name__ == "__main__":
    unittest.main(verbosity=2)
