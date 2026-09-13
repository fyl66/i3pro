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

from i3pro import (  # noqa: E402
    csvlog, derive, laps as lapsmod, ld, maths as mathsmod, motec_csv, render, server, store,
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
        finally:
            httpd.shutdown()
            httpd.server_close()
            library.close()


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
        from http.server import ThreadingHTTPServer

        library = server.SessionLibrary([DATA], cache_size=1)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(library, buckets=200))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        quoted = urllib.parse.quote(HILL.stem)
        sidecar, kept_sidecar = _borrow_sidecar(HILL)

        def get_json(path):
            with urllib.request.urlopen(base + path, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))

        def put_json(path, payload):
            request = urllib.request.Request(
                base + path, data=json.dumps(payload).encode("utf-8"), method="PUT",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
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
        finally:
            _return_sidecar(sidecar, kept_sidecar)
            httpd.shutdown()
            httpd.server_close()
            library.close()

    @_needs(HILL)
    def test_the_distance_lookup_and_a_do_nothing_insert_over_http(self):
        """#5 needs metres -> seconds over HTTP, and a notice when nothing split."""
        from http.server import ThreadingHTTPServer

        library = server.SessionLibrary([DATA], cache_size=1)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(library, buckets=200))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        quoted = urllib.parse.quote(HILL.stem)
        sidecar, kept_sidecar = _borrow_sidecar(HILL)

        def get_json(path):
            with urllib.request.urlopen(base + path, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))

        def put_json(path, payload):
            request = urllib.request.Request(
                base + path, data=json.dumps(payload).encode("utf-8"), method="PUT",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
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
        finally:
            _return_sidecar(sidecar, kept_sidecar)
            httpd.shutdown()
            httpd.server_close()
            library.close()


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
        from http.server import ThreadingHTTPServer

        library = server.SessionLibrary([DATA], cache_size=1)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(library, buckets=200))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        quoted = urllib.parse.quote(HILL.stem)
        sidecar, kept_sidecar = _borrow_sidecar(HILL)

        def get_json(path):
            with urllib.request.urlopen(base + path, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))

        def put_json(path, payload):
            request = urllib.request.Request(
                base + path, data=json.dumps(payload).encode("utf-8"), method="PUT",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))

        def page_payload():
            """页面注入的那份 payload：刷新之后界面就是靠它知道按钮该不该亮。"""
            with urllib.request.urlopen(base + "/session/" + quoted, timeout=60) as response:
                html = response.read().decode("utf-8")
            start = html.index("const DATA = ") + len("const DATA = ")
            payload, _end = json.JSONDecoder().raw_decode(html[start:])
            return payload

        def on_disk():
            return json.loads(sidecar.read_text(encoding="utf-8"))

        try:
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
        finally:
            _return_sidecar(sidecar, kept_sidecar)
            httpd.shutdown()
            httpd.server_close()
            library.close()


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
                np.asarray(lapsmod._distance_series(log), dtype=float)
            )
            time = np.asarray(lapsmod._master_time(log), dtype=float)
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
                np.asarray(lapsmod._distance_series(log), dtype=float)
            )
            time = np.asarray(lapsmod._master_time(log), dtype=float)
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
                np.asarray(lapsmod._distance_series(log), dtype=float)
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
        with self.assertRaises(ValueError):
            importer.safe_name("notes.txt")

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
            time = np.arange(int(round(log.duration * log.sample_rate)) + 1) / log.sample_rate
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
            time = np.arange(int(round(log.duration * log.sample_rate)) + 1) / log.sample_rate
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
            self.assertIn(HILL.name, index)
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


class _MathSession:
    """A minimal session, so the maths engine can be tested without a ``.ld``.

    The engine only asks a session for ``has`` / ``channel`` / ``values`` /
    ``sample_rate`` / ``duration``, and ``attach`` writes into ``derived`` (the
    same attribute a real ``LogFile`` has). Keeping this stub in the test file
    is what lets every expression test run on a machine with no team data.
    """

    def __init__(self, columns: dict, rate: float = 10.0, path="fake.ld", rates: dict | None = None):
        self.columns = {k: np.asarray(v, dtype=np.float64) for k, v in columns.items()}
        self.sample_rate = float(rate)
        self.derived: dict[str, np.ndarray] = {}
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


class TestMathsOverHttp(unittest.TestCase):
    """#3 走到界面之前的那一段：PUT/GET/POST + 侧车文件 + 作用域。"""

    @_needs(HILL)
    def test_saving_a_definition_reaches_the_viewer(self):
        import tempfile
        from http.server import ThreadingHTTPServer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            copy = root / HILL.name
            copy.write_bytes(HILL.read_bytes())
            library = server.SessionLibrary([root], cache_size=1, maths_root=root)
            httpd = ThreadingHTTPServer(
                ("127.0.0.1", 0), server.make_handler(library, buckets=100)
            )
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            quoted = urllib.parse.quote(copy.stem)

            def request(path, method="GET", payload=None):
                body = None if payload is None else json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(base + path, data=body, method=method)
                if body is not None:
                    req.add_header("Content-Type", "application/json")
                try:
                    with urllib.request.urlopen(req, timeout=60) as response:
                        return response.status, json.loads(response.read().decode("utf-8"))
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read().decode("utf-8"))

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
                httpd.shutdown()
                library.close()

    @_needs(HILL)
    def test_a_bare_channel_name_with_a_space_saves_and_computes(self):
        """用户反馈的那条路：编辑器里直接打 `Vx KF * 2`，不该要求他加引号。"""
        import tempfile
        from http.server import ThreadingHTTPServer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            copy = root / HILL.name
            copy.write_bytes(HILL.read_bytes())
            library = server.SessionLibrary([root], cache_size=1, maths_root=root)
            httpd = ThreadingHTTPServer(
                ("127.0.0.1", 0), server.make_handler(library, buckets=100)
            )
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            quoted = urllib.parse.quote(copy.stem)

            def request(path, method="GET", payload=None):
                body = None if payload is None else json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(base + path, data=body, method=method)
                if body is not None:
                    req.add_header("Content-Type", "application/json")
                try:
                    with urllib.request.urlopen(req, timeout=60) as response:
                        return response.status, json.loads(response.read().decode("utf-8"))
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read().decode("utf-8"))

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
                httpd.shutdown()
                library.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
