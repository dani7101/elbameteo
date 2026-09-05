import tempfile
from pathlib import Path
import unittest

import elbameteo as app


class ForecastTests(unittest.TestCase):
    def test_wind_and_circular_differences(self):
        self.assertEqual(app.wind(0, -5), (5, 0))
        self.assertEqual(app.wind(-5, 0), (5, 90))
        self.assertIsNone(app.wind(.1, .1)[1])
        self.assertEqual(app.angle_delta(1, 359), 2)
        self.assertEqual(app.angle_delta(359, 1), -2)

    def test_horizon_and_timezone(self):
        self.assertEqual(len(app.steps_for(app.parse_run("2026-09-03T02:00+02:00"))), 85)
        self.assertEqual(app.steps_for(app.parse_run("2026-09-03T06:00Z"))[-1], 144)
        with self.assertRaises(ValueError):
            app.parse_run("2026-09-03T01:00Z")

    def test_ensemble_confidence(self):
        tight = [dict(speed_ms=5, gust_ms=8, direction_deg=a) for a in [359, 1] * 25]
        summary = app.summarize(tight)
        self.assertGreater(summary["direction_resultant"], .99)
        self.assertEqual(summary["confidence"], "alta")
        self.assertEqual(app.summarize(tight[:2])["confidence"], "non_disponibile")
        split = [dict(speed_ms=5, gust_ms=8, direction_deg=a) for a in [0, 180] * 25]
        self.assertEqual(app.summarize(split)["confidence"], "bassa")

    def test_decode_storage_and_comparison(self):
        import eccodes as ec
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.grib2"
            run = app.parse_run("2026-09-03T00:00Z")
            with path.open("wb") as handle:
                for param, value in [(165, 3), (166, 4), (49, 8)]:
                    gid = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
                    try:
                        for key, item in dict(Ni=2, Nj=2, latitudeOfFirstGridPointInDegrees=42.75,
                                              longitudeOfFirstGridPointInDegrees=10.0,
                                              latitudeOfLastGridPointInDegrees=42.5,
                                              longitudeOfLastGridPointInDegrees=10.25,
                                              iDirectionIncrementInDegrees=.25, jDirectionIncrementInDegrees=.25,
                                              dataDate=20260903, dataTime=0,
                                              typeOfLevel="heightAboveGround", level=10).items():
                            ec.codes_set(gid, key, item)
                        ec.codes_set(gid, "stepType", "max" if param == 49 else "instant")
                        ec.codes_set(gid, "paramId", param)
                        ec.codes_set(gid, "stepRange", "0-3" if param == 49 else "3")
                        ec.codes_set_values(gid, [value] * 4)
                        ec.codes_write(gid, handle)
                    finally:
                        ec.codes_release(gid)
            points = [dict(id="test", lat=42.75, lon=10.0)]
            rows = app.decode(path, run, 3, "fc", points, 50)
            self.assertEqual(rows[0]["speed_ms"], 5)
            self.assertEqual(rows[0]["gust_ms"], 8)
            with self.assertRaises(ValueError):
                app.decode(path, run, 6, "fc", points, 50)
            config = dict(database=str(Path(directory) / "data.sqlite"))
            db = app.connect(config)
            try:
                app.save_batch(db, "test", run, "fc", 3, rows, "test", "hash")
                app.save_batch(db, "test", run, "fc", 3, rows, "test", "hash")
                self.assertEqual(db.execute("SELECT count(*) FROM forecasts").fetchone()[0], 1)
                new_run = app.parse_run("2026-09-03T06:00Z")
                # Fixture: stessa validita, finestra raffiche diversa.
                old = dict(rows[0], step=12, valid_time="2026-09-03T12:00:00Z",
                           gust_start="2026-09-03T06:00:00Z", gust_end="2026-09-03T12:00:00Z")
                new = dict(old, run=app.stamp(new_run), step=6, speed_ms=7,
                           gust_start="2026-09-03T09:00:00Z")
                app.save_batch(db, "test", run, "fc", 12, [old], "test", "hash")
                app.save_batch(db, "test", new_run, "fc", 6, [new], "test", "hash")
                compared = list(app.compare_rows(db, "test", run, new_run, "test"))
                self.assertEqual(len(compared), 1)
                self.assertEqual(compared[0]["delta_speed_kmh"], 7.2)
                self.assertIsNone(compared[0]["delta_gust_kmh"])
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
