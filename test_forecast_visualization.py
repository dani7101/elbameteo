import unittest
from datetime import datetime, timezone

from forecast_visualization import elba_coast_segments, ensemble_score, italy_time


class PredictabilityTests(unittest.TestCase):
    def test_full_agreement_is_100_percent(self):
        self.assertEqual(ensemble_score([5.0] * 51, [270.0] * 51), 100.0)

    def test_opposite_directions_reduce_predictability(self):
        score = ensemble_score([5.0] * 50, [0.0, 180.0] * 25)
        self.assertLess(score, 1.0)

    def test_small_ensemble_is_not_scored(self):
        self.assertIsNone(ensemble_score([5.0] * 10, [90.0] * 10))

    def test_italian_summer_and_winter_time(self):
        summer = italy_time(datetime(2026, 9, 4, 12, tzinfo=timezone.utc))
        winter = italy_time(datetime(2026, 12, 4, 12, tzinfo=timezone.utc))
        self.assertEqual((summer.hour, summer.tzname()), (14, "CEST"))
        self.assertEqual((winter.hour, winter.tzname()), (13, "CET"))

    def test_elba_coast_segments_have_unit_inward_normals(self):
        points = [dict(id="west", lat=42.77, lon=10.10), dict(id="east", lat=42.77, lon=10.40)]
        segments = elba_coast_segments(points)
        self.assertGreater(len(segments), 40)
        for segment in segments:
            self.assertAlmostEqual(sum(value * value for value in segment["inward"]), 1, places=3)


if __name__ == "__main__":
    unittest.main()
