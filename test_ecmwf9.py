from datetime import timedelta
import sqlite3
import unittest
from unittest.mock import patch, Mock

import ecmwf9 as api
import elbameteo as core


def fixture(run):
    return dict(latitude=42.77, longitude=10.25, utc_offset_seconds=0,
                hourly_units=dict(wind_speed_10m="m/s", wind_direction_10m="°", wind_gusts_10m="m/s"),
                hourly=dict(time=[(run + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(168)],
                            wind_speed_10m=[5] * 168, wind_direction_10m=[359] * 168,
                            wind_gusts_10m=[None] + [8] * 167))


class HourlyTests(unittest.TestCase):
    def test_window_and_interpolation_boundaries(self):
        for hour in (0, 6, 12, 18):
            run = core.parse_run(f"2026-09-02T{hour:02d}:00Z")
            rows = api.normalize(fixture(run), run, 144)
            self.assertEqual(len(rows), 145)
            self.assertEqual(rows[0]["valid_time"], core.stamp(run))
            self.assertEqual(rows[-1]["valid_time"], core.stamp(run + timedelta(hours=144)))
            self.assertEqual([rows[i]["native_time_step"] for i in (90, 91, 92, 93, 144)], [1, 0, 0, 1, 1])
            self.assertIsNone(rows[0]["gust_ms"])

    def test_missing_end_not_complete(self):
        run = core.parse_run("2026-09-02T06:00Z")
        payload = fixture(run)
        payload["hourly"]["wind_speed_10m"][144] = None
        with self.assertRaises(ValueError):
            api.normalize(payload, run, 144)

    def test_save_resume_and_hourly_comparison(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        api.init_db(db)
        run = core.parse_run("2026-09-02T00:00Z")
        newer = run + timedelta(hours=6)
        config = dict(horizon_hours=144, locations=[dict(id="test", lat=42.75, lon=10.25)])
        try:
            for origin in (run, newer):
                response = Mock(text="{}", content=b"{}")
                response.json.return_value = fixture(origin)
                with patch.object(api.requests, "get", return_value=response) as get:
                    self.assertEqual(api.collect(config, db, "cfg", [origin]), 0)
                    self.assertEqual(api.collect(config, db, "cfg", [origin]), 0)
                    self.assertEqual(get.call_count, 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hourly9").fetchone()[0], 290)
            compared = list(api.compare_rows(db, "cfg", run, newer, "test"))
            self.assertEqual(len(compared), 139)
            self.assertEqual(compared[0]["old_lead_h"], 6)
            self.assertEqual(compared[0]["new_lead_h"], 0)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
