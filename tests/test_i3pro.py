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

from i3pro import csvlog, derive, laps as lapsmod, ld, motec_csv, render, server, store  # noqa: E402

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
            sidecar = HILL.parent / f"{HILL.stem}.laps.json"
            self.assertFalse(sidecar.exists(), "a stale sidecar would poison this test")
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
                # never leave a sidecar behind: it would change every later test
                sidecar.unlink(missing_ok=True)

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
        sidecar = HILL.parent / f"{HILL.stem}.laps.json"
        self.assertFalse(sidecar.exists(), "a stale sidecar would poison this test")

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
            # never leave a sidecar behind: it would change every later test
            sidecar.unlink(missing_ok=True)
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
        sidecar = HILL.parent / f"{HILL.stem}.laps.json"
        self.assertFalse(sidecar.exists(), "a stale sidecar would poison this test")

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
            sidecar.unlink(missing_ok=True)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
