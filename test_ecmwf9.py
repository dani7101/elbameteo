from datetime import datetime, timedelta
import sqlite3
import tempfile
from pathlib import Path
import io
import json
import unittest
from unittest.mock import patch, Mock

import ecmwf9 as api
import elbameteo as core
import fetch_latest
from fetch_latest import recent_runs


def fixture(run):
    return dict(latitude=42.77, longitude=10.25, utc_offset_seconds=0,
                hourly_units=dict(wind_speed_10m="m/s", wind_direction_10m="°", wind_gusts_10m="m/s"),
                hourly=dict(time=[(run + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(168)],
                            wind_speed_10m=[5] * 168, wind_direction_10m=[359] * 168,
                            wind_gusts_10m=[None] + [8] * 167))


class HourlyTests(unittest.TestCase):
    def test_latest_emission_is_selected_after_recovering_missing_point(self):
        run = core.parse_run("2026-09-02T06:00Z")
        with tempfile.TemporaryDirectory() as directory:
            config = dict(database=str(Path(directory) / "meteo.sqlite"), horizon_hours=144,
                          locations=[dict(id="a", lat=42.75, lon=10),
                                     dict(id="b", lat=42.75, lon=10.1)])
            output = io.StringIO()
            with patch("sys.argv", ["fetch_latest.py"]), \
                    patch.object(core, "load_config", return_value=config), \
                    patch.object(core, "config_id", return_value=("cfg", "{}")), \
                    patch.object(fetch_latest, "recent_runs", return_value=[run, run - timedelta(hours=6)]), \
                    patch.object(api.requests, "get", side_effect=[
                        api.requests.Timeout("simulated"), self.response(run), self.response(run)
                    ]) as get, patch.object(api.time, "sleep"), patch("sys.stdout", output):
                self.assertEqual(fetch_latest.main(), 0)
            self.assertEqual(json.loads(output.getvalue())["run"], core.stamp(run))
            self.assertEqual(get.call_count, 3)
            self.assertTrue(all(call.kwargs["params"]["run"] == "2026-09-02T06:00"
                                for call in get.call_args_list))

    def response(self, run):
        response = Mock(text="{}", content=b"{}")
        response.json.return_value = fixture(run)
        return response

    def test_timeout_does_not_skip_other_points_and_retries_only_missing(self):
        run = core.parse_run("2026-09-02T06:00Z")
        config = dict(horizon_hours=144, locations=[
            dict(id=str(i), lat=42.75, lon=10 + i / 10) for i in range(3)])
        with sqlite3.connect(":memory:") as db:
            api.init_db(db)
            with patch.object(api.requests, "get", side_effect=[
                api.requests.Timeout("simulated"), self.response(run),
                self.response(run), self.response(run),
            ]) as get, patch.object(api.time, "sleep") as sleep:
                self.assertEqual(api.collect(config, db, "cfg", [run]), 0)
                self.assertEqual([call.kwargs["params"]["longitude"] for call in get.call_args_list],
                                 [10, 10.1, 10.2, 10])
                sleep.assert_called_once_with(10)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hourly9").fetchone()[0], 435)

    def test_partial_download_survives_reopening_database(self):
        run = core.parse_run("2026-09-02T06:00Z")
        config = dict(horizon_hours=144, locations=[
            dict(id="a", lat=42.75, lon=10), dict(id="b", lat=42.75, lon=10.1)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "meteo.sqlite"
            db = sqlite3.connect(path)
            api.init_db(db)
            try:
                with patch.object(api.requests, "get", side_effect=[
                    self.response(run), api.requests.Timeout("simulated"),
                    api.requests.Timeout("simulated"), api.requests.Timeout("simulated"),
                ]) as get, patch.object(api.time, "sleep") as sleep:
                    self.assertEqual(api.collect(config, db, "cfg", [run]), 1)
                    self.assertEqual(get.call_count, 4)
                    self.assertEqual([call.args[0] for call in sleep.call_args_list], [10, 20])
            finally:
                db.close()
            db = sqlite3.connect(path)
            try:
                with patch.object(api.requests, "get", return_value=self.response(run)) as get:
                    self.assertEqual(api.collect(config, db, "cfg", [run]), 0)
                    get.assert_called_once()
                    self.assertEqual(get.call_args.kwargs["params"]["longitude"], 10.1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM hourly9").fetchone()[0], 290)
            finally:
                db.close()

    def test_invalid_data_not_saved_and_does_not_skip_next_point(self):
        run = core.parse_run("2026-09-02T06:00Z")
        invalid = self.response(run)
        invalid.json.return_value["hourly"]["wind_speed_10m"][144] = None
        config = dict(horizon_hours=144, locations=[
            dict(id="a", lat=42.75, lon=10), dict(id="b", lat=42.75, lon=10.1)])
        with sqlite3.connect(":memory:") as db:
            api.init_db(db)
            with patch.object(api.requests, "get", side_effect=[invalid, self.response(run)]) as get:
                self.assertEqual(api.collect(config, db, "cfg", [run]), 1)
                self.assertEqual(get.call_count, 2)
            self.assertEqual(db.execute("SELECT location FROM downloads9").fetchall(), [("b",)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hourly9").fetchone()[0], 145)

    def test_http_transient_errors_retried_but_400_is_not(self):
        run = core.parse_run("2026-09-02T06:00Z")
        config = dict(horizon_hours=144, locations=[dict(id="a", lat=42.75, lon=10)])
        for status in (400, 429, 503):
            with self.subTest(status=status), sqlite3.connect(":memory:") as db:
                api.init_db(db)
                error = api.requests.HTTPError(response=Mock(status_code=status))
                with patch.object(api.requests, "get", side_effect=[error, self.response(run)]) as get, \
                        patch.object(api.time, "sleep"):
                    self.assertEqual(api.collect(config, db, "cfg", [run]), int(status == 400))
                    self.assertEqual(get.call_count, 1 if status == 400 else 2)

    def test_deadline_keeps_committed_points(self):
        run = core.parse_run("2026-09-02T06:00Z")
        config = dict(horizon_hours=144, locations=[
            dict(id="a", lat=42.75, lon=10), dict(id="b", lat=42.75, lon=10.1)])
        with sqlite3.connect(":memory:") as db:
            api.init_db(db)
            with patch.object(api.requests, "get", return_value=self.response(run)), \
                    patch.object(api.time, "monotonic", side_effect=[0, 100]):
                with self.assertRaises(TimeoutError):
                    api.collect(config, db, "cfg", [run], deadline=50)
            self.assertEqual(db.execute("SELECT location FROM downloads9").fetchall(), [("a",)])

    def test_latest_candidates_start_from_current_cycle(self):
        now = datetime(2026, 9, 5, 13, 27, tzinfo=core.UTC)
        self.assertEqual(
            [core.stamp(run) for run in recent_runs(now, 12)],
            ["2026-09-05T12:00:00Z", "2026-09-05T06:00:00Z"],
        )

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
