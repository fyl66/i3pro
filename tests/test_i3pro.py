"""Unit + regression tests for i3pro.

The regression tests need the team's sample logs in ``i2pro_data/``; they are
skipped automatically when the data is not present.

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from i3pro import derive, laps as lapsmod, ld, motec_csv, render, server, store  # noqa: E402

DATA = ROOT / "i2pro_data"
ENDURANCE = DATA / "20260524-耐久正赛.ld"
HILL = DATA / "20260908-cjh 高避5圈.ld"


def _needs(path: Path):
    return unittest.skipUnless(path.exists(), f"sample log not present: {path.name}")


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
        from http.server import ThreadingHTTPServer

        library = server.SessionLibrary([DATA], cache_size=1)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(library, buckets=250))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def get_json(path):
            with urllib.request.urlopen(base + path, timeout=30) as response:
                self.assertEqual(response.status, 200)
                return json.loads(response.read().decode("utf-8"))

        def get_text(path):
            with urllib.request.urlopen(base + path, timeout=30) as response:
                self.assertEqual(response.status, 200)
                return response.read().decode("utf-8")

        try:
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

            laps = get_json(f"/api/session/{quoted}/laps")
            self.assertGreaterEqual(len([l for l in laps if l["complete"]]), 5)

            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/api/session/nope/trace", timeout=15)
            self.assertEqual(caught.exception.code, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()
            library.close()


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
                capture_output=True, text=True, env=env, timeout=120,
            )
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertIn("PASS", finished.stdout)

    @_needs(HILL)
    def test_runs_headless_in_time_axis(self):
        self._smoke()

    @_needs(HILL)
    def test_runs_headless_in_overlay_mode(self):
        self._smoke("mode=overlay")


if __name__ == "__main__":
    unittest.main(verbosity=2)
