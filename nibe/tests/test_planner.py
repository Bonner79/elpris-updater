import copy
import json
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nibe_updater as n


def records(day, step=900, fn=None):
    a, b = n.day_bounds(day)
    rows = []
    for i, t in enumerate(range(a, b, step)):
        rows.append({
            "time_start": datetime.fromtimestamp(t, n.TZ).isoformat(),
            "time_end": datetime.fromtimestamp(t + step, n.TZ).isoformat(),
            "SEK_per_kWh": float(fn(t, i) if fn else 1.0),
        })
    return rows


def config():
    cfg = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
    # Tests assume a 120 minute water window, independent of the live tuning.
    cfg["water"]["duration_minutes"] = 120
    return cfg


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 10, 11)
        self.cfg = config()
        self.a, self.b = n.day_bounds(self.day)

    def test_default_config(self):
        n.validate_config(self.cfg)
        self.assertFalse(self.cfg["heat"]["enabled"])

    def test_normal_day(self):
        self.assertEqual(len(n.parse_prices(records(self.day), self.day)), 96)

    def test_spring_day(self):
        d = date(2026, 3, 29)
        slots = n.parse_prices(records(d), d)
        self.assertEqual(len(slots), 92)
        self.assertEqual(len(n.aggregate_prices(slots, 60)), 23)

    def test_autumn_day(self):
        d = date(2026, 10, 25)
        slots = n.aggregate_prices(n.parse_prices(records(d), d), 60)
        self.assertEqual(len(slots), 25)
        twos = [s for s in slots if datetime.fromtimestamp(s.start, n.TZ).hour == 2]
        self.assertEqual(len(twos), 2)
        self.assertEqual(twos[1].start - twos[0].start, 3600)

    def test_fall_second_hour_can_be_selected_alone(self):
        d = date(2026, 10, 25)
        second = int(datetime(2026, 10, 25, 2, tzinfo=n.TZ, fold=1).timestamp())
        slots = n.parse_prices(records(d, 3600, lambda t, i: -10 if t == second else 10), d)
        rule = {"duration_minutes": 60, "start": "00:00", "end": "24:00"}
        self.assertEqual(n.cheapest(slots, rule), [second, second + 3600])

    def test_spring_block_is_real_elapsed_time(self):
        d = date(2026, 3, 29)
        a, b = n.day_bounds(d)
        slots = n.parse_prices(records(d, 3600, lambda t, i: i), d)
        w = n.cheapest(slots, {"duration_minutes": 240, "start": "00:00", "end": "24:00"})
        self.assertEqual(w, [a, a + 14400])
        self.assertEqual(datetime.fromtimestamp(w[1], n.TZ).hour, 5)

    def test_missing_quarter_rejected(self):
        rows = records(self.day)
        del rows[30]
        with self.assertRaises(n.PlanError): n.parse_prices(rows, self.day)

    def test_missing_hour_rejected_on_normal_day(self):
        rows = records(self.day, 3600)
        del rows[5]
        with self.assertRaises(n.PlanError): n.parse_prices(rows, self.day)

    def test_duplicate_rejected(self):
        rows = records(self.day)
        rows.append(rows[30])
        with self.assertRaises(n.PlanError): n.parse_prices(rows, self.day)

    def test_nan_rejected(self):
        rows = records(self.day)
        rows[0]["SEK_per_kWh"] = float("nan")
        with self.assertRaises(n.PlanError): n.parse_prices(rows, self.day)

    def test_missing_timezone_rejected(self):
        rows = records(self.day)
        rows[0]["time_start"] = "2026-10-11T00:00:00"
        with self.assertRaises(n.PlanError): n.parse_prices(rows, self.day)

    def test_wrong_day_rejected(self):
        with self.assertRaises(n.PlanError): n.parse_prices(records(self.day), self.day + timedelta(days=1))

    def test_water_chooses_latest_on_tie(self):
        slots = n.parse_prices(records(self.day), self.day)
        w = n.cheapest(slots, self.cfg["water"])
        self.assertEqual(datetime.fromtimestamp(w[0], n.TZ).hour, 16)
        self.assertEqual(datetime.fromtimestamp(w[1], n.TZ).hour, 18)

    def test_quarter_offset_can_be_cheapest(self):
        noon15 = self.a + (12 * 60 + 15) * 60
        slots = n.parse_prices(records(self.day, fn=lambda t, i: -1 if noon15 <= t < noon15+7200 else 10), self.day)
        self.assertEqual(n.cheapest(slots, self.cfg["water"]), [noon15, noon15+7200])
        w = n.cheapest(n.aggregate_prices(slots, 60), self.cfg["water"])
        self.assertEqual(w[0] % 3600, 0)

    def test_missing_weather_no_heat(self):
        self.assertEqual(n.cheapest(n.parse_prices(records(self.day), self.day), self.cfg["heat"], temperatures={}), [])

    def test_cold_weather_allows_heat(self):
        wx = {t: 5.0 for t in range(self.a-90000, self.b+3600, 3600)}
        self.assertTrue(n.cheapest(n.parse_prices(records(self.day), self.day), self.cfg["heat"], temperatures=wx))

    def test_hot_weather_no_heat(self):
        wx = {t: 20.0 for t in range(self.a-90000, self.b+3600, 3600)}
        self.assertEqual(n.cheapest(n.parse_prices(records(self.day), self.day), self.cfg["heat"], temperatures=wx), [])

    def test_heat_excludes_water_and_showers(self):
        self.cfg["heat"].update({"enabled": True, "latitude": 59.5, "longitude": 17.6})
        wx = {t: 5.0 for t in range(self.a-90000, self.b+3600, 3600)}
        now = datetime.fromtimestamp(self.a-1, n.UTC)
        d = n.build_day(self.day, self.cfg, n.parse_prices(records(self.day), self.day), wx, now)
        self.assertTrue(d["heat"])
        self.assertFalse(n.overlaps(d["heat"], d["water"]))
        self.assertFalse(n.blocked_local(*d["heat"], self.cfg["heat"]["avoid"]))

    def test_heat_needs_coordinates(self):
        self.cfg["heat"]["enabled"] = True
        with self.assertRaises(n.PlanError): n.validate_config(self.cfg)

    def test_first_run_does_not_start_expired_window(self):
        now = datetime.fromtimestamp(self.a+17*3600, n.UTC)
        d = n.build_day(self.day, self.cfg, n.parse_prices(records(self.day), self.day), {}, now)
        self.assertEqual(d["water"], [])

    def test_today_window_is_frozen(self):
        slots = n.parse_prices(records(self.day), self.day)
        d = n.build_day(self.day, self.cfg, slots, {}, datetime.fromtimestamp(self.a-1, n.UTC))
        different = n.parse_prices(records(self.day, fn=lambda t, i: -10 if i<48 else 10), self.day)
        new = n.build_day(self.day, self.cfg, different, {}, datetime.fromtimestamp(self.a+10*3600, n.UTC), d)
        self.assertEqual(d["water"], new["water"])

    def test_disable_water_clears_existing(self):
        d = {"date": self.day.isoformat(), "start": self.a, "end": self.b, "heat": [], "water": [self.a+3600, self.a+7200]}
        self.cfg["water"]["enabled"] = False
        new = n.build_day(self.day, self.cfg, None, {}, datetime.fromtimestamp(self.a+20, n.UTC), d)
        self.assertEqual(new["water"], [])

    def test_payload_small_and_two_dates(self):
        days = []
        for day in (self.day, self.day+timedelta(days=1)):
            days.append(n.build_day(day, self.cfg, n.parse_prices(records(day), day), {}, datetime.fromtimestamp(self.a-1, n.UTC)))
        p = {"system": n.SYSTEM, "area": "SE3", "generated": self.a-1, "days": days}
        self.assertLess(len(n.compact(p)), 900)
        self.assertEqual(len(p["days"]), 2)
        self.assertTrue(n.valid_day(days[0], self.day))

    def test_known_gists_protected(self):
        self.assertEqual(len(n.PROTECTED_GISTS), 4)
        self.assertIn("38a1294a14afc7b05553c2658576a721", n.PROTECTED_GISTS)


if __name__ == "__main__": unittest.main()
