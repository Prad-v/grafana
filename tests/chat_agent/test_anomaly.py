"""Unit tests of the anomaly engine (backend/services/grafana-chat-agent/chatagent/anomaly.py).

Every series is synthetic with a known injected fault, so a failure here means the
detector stopped finding something real or started inventing something.
"""

import math
import random
import unittest

from chatagent import anomaly

STEP = 60
T0 = 1_700_000_000


def series(n=180, base=60.0, noise=0.8, seed=1, fn=None):
    """`n` samples one minute apart. `fn(i, value)` can modify a sample."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        v = base + rng.gauss(0, noise)
        if fn:
            v = fn(i, v)
        out.append((T0 + i * STEP, v))
    return out


def kinds(report):
    return [f.kind for f in report.findings]


class AnalyzeTest(unittest.TestCase):
    def test_clean_noisy_series_has_no_findings(self):
        for seed in range(40):
            report = anomaly.analyze(series(seed=seed))
            self.assertEqual(report.findings, [], f"seed {seed}: {[f.as_dict() for f in report.findings]}")

    def test_spike_is_found_with_its_time_and_size(self):
        report = anomaly.analyze(series(fn=lambda i, v: v + 25 if i == 120 else v))
        spikes = [f for f in report.findings if f.kind == "spike"]
        self.assertEqual(len(spikes), 1)
        self.assertEqual(spikes[0].peak_ts, T0 + 120 * STEP)
        self.assertGreater(spikes[0].peak_value, 80)
        self.assertAlmostEqual(spikes[0].baseline, 60, delta=2)

    def test_dip_is_found(self):
        report = anomaly.analyze(series(fn=lambda i, v: v - 30 if 90 <= i < 93 else v))
        dips = [f for f in report.findings if f.kind == "dip"]
        self.assertEqual(len(dips), 1)
        self.assertIn("3 sample", dips[0].detail)

    def test_short_burst_is_one_finding_not_many(self):
        report = anomaly.analyze(series(fn=lambda i, v: v + 20 if 100 <= i < 104 else v))
        self.assertEqual(kinds(report).count("spike"), 1)

    def test_level_shift_is_found_once(self):
        report = anomaly.analyze(series(fn=lambda i, v: v + 15 if i >= 100 else v))
        shifts = [f for f in report.findings if f.kind == "level_shift"]
        self.assertEqual(len(shifts), 1)
        self.assertAlmostEqual((shifts[0].peak_ts - T0) / STEP, 100, delta=3)
        self.assertAlmostEqual(shifts[0].baseline, 60, delta=1.5)
        self.assertAlmostEqual(shifts[0].peak_value, 75, delta=1.5)
        self.assertIn("up", shifts[0].detail)

    def test_downward_level_shift_says_down(self):
        report = anomaly.analyze(series(fn=lambda i, v: v - 15 if i >= 100 else v))
        self.assertIn("down", next(f for f in report.findings if f.kind == "level_shift").detail)

    def test_flatline_is_found_in_a_series_that_normally_moves(self):
        report = anomaly.analyze(series(fn=lambda i, v: 61.5 if i >= 130 else v))
        flat = [f for f in report.findings if f.kind == "flatline"]
        self.assertEqual(len(flat), 1)
        self.assertEqual(flat[0].peak_value, 61.5)

    def test_constant_series_is_not_a_flatline(self):
        report = anomaly.analyze([(T0 + i * STEP, 100.0) for i in range(100)])
        self.assertEqual(report.findings, [])

    def test_gap_is_found(self):
        data = [p for i, p in enumerate(series()) if not 80 <= i < 100]
        report = anomaly.analyze(data)
        gaps = [f for f in report.findings if f.kind == "gap"]
        self.assertEqual(len(gaps), 1)
        self.assertGreaterEqual(gaps[0].score, 19)

    def test_gaps_do_not_count_toward_the_series_score(self):
        data = [p for i, p in enumerate(series()) if not 80 <= i < 100]
        self.assertEqual(anomaly.analyze(data).score, 0.0)

    def test_smooth_daily_curve_is_not_a_level_shift(self):
        wave = [(T0 + i * STEP, 50 + 30 * math.sin(i / 180 * 2 * math.pi)) for i in range(360)]
        report = anomaly.analyze(wave)
        self.assertNotIn("level_shift", kinds(report))
        self.assertNotIn("spike", kinds(report))

    def test_tiny_wiggles_on_a_steady_value_are_ignored(self):
        # 0.1% changes are statistically clean here but not worth a person's attention.
        data = [(T0 + i * STEP, 1000.0 + (0.5 if i == 100 else 0.0) + (i % 2) * 0.01) for i in range(180)]
        self.assertEqual([f for f in anomaly.analyze(data).findings if f.kind in {"spike", "dip"}], [])

    def test_sensitivity_changes_what_is_found(self):
        # A bump of about 4 sigma: clear at a loose threshold, ignored at a strict one.
        data = series(fn=lambda i, v: v + 3.2 if i == 100 else v)
        self.assertEqual(kinds(anomaly.analyze(data, sensitivity=8)), [])
        self.assertIn("spike", kinds(anomaly.analyze(data, sensitivity=3)))

    def test_current_anomaly_is_flagged_when_the_last_point_is_bad(self):
        report = anomaly.analyze(series(fn=lambda i, v: v + 30 if i == 179 else v))
        self.assertGreater(report.current_score, 4.5)
        self.assertLess(anomaly.analyze(series()).current_score, 4.5)

    def test_too_few_points_reports_insufficient_data(self):
        report = anomaly.analyze(series(n=5))
        self.assertTrue(report.insufficient_data)
        self.assertEqual(report.findings, [])

    def test_nan_and_infinity_are_ignored(self):
        data = series()
        data[10] = (data[10][0], float("nan"))
        data[11] = (data[11][0], float("inf"))
        self.assertEqual(anomaly.analyze(data).findings, [])

    def test_unsorted_input_is_handled(self):
        data = series(fn=lambda i, v: v + 25 if i == 120 else v)
        random.Random(3).shuffle(data)
        self.assertEqual(kinds(anomaly.analyze(data)).count("spike"), 1)

    def test_counts_need_min_scale_or_every_blip_is_an_anomaly(self):
        rng = random.Random(4)
        counts = [(T0 + i * STEP, float(rng.choice([0, 0, 1, 1, 1, 2]))) for i in range(180)]
        counts[120] = (counts[120][0], 3.0)
        noisy = anomaly.analyze(counts, min_scale=1.0)
        self.assertEqual([f for f in noisy.findings if f.kind in {"spike", "level_shift"}], [])

    def test_log_burst_is_still_found_with_min_scale(self):
        rng = random.Random(4)
        counts = [(T0 + i * STEP, float(rng.choice([0, 1, 1, 2]))) for i in range(180)]
        for i in range(140, 146):
            counts[i] = (counts[i][0], 40.0)
        report = anomaly.analyze(counts, min_scale=1.0)
        self.assertIn("spike", kinds(report))

    def test_findings_serialise_to_plain_json(self):
        import json

        report = anomaly.analyze(series(fn=lambda i, v: v + 25 if i == 120 else v))
        text = json.dumps([f.as_dict() for f in report.findings])
        self.assertIn("2023-", text)  # ISO timestamps, not floats


def square(n=240, period=24, on=16, hi=64.0, lo=40.0, noise=0.4, seed=1, jitter=True, fn=None):
    """A batch job: `on` samples hot, the rest idle, repeating. Edges wander by a sample, as in real data."""
    rng = random.Random(seed)
    out = []
    shift = 0
    for i in range(n):
        if i % period == 0 and jitter:
            shift = rng.choice([-1, 0, 0, 1])
        phase = (i + shift) % period
        v = (hi if phase < on else lo) + rng.gauss(0, noise)
        if fn:
            v = fn(i, v)
        out.append((T0 + i * STEP, v))
    return out


class CycleTest(unittest.TestCase):
    """A steady rhythm is normal. It must not drown out a real problem inside it."""

    def test_the_period_of_a_square_wave_is_found(self):
        values = [v for _, v in square()]
        self.assertEqual(anomaly.find_period(values), 24)

    def test_a_cycling_series_has_no_anomalies_and_reports_its_period(self):
        for seed in range(20):
            report = anomaly.analyze(square(seed=seed))
            self.assertEqual(report.findings, [], f"seed {seed}: {[f.as_dict() for f in report.findings]}")
            self.assertEqual(report.periodic[0]["kind"], "cycle")
            # The autocorrelation peak can land one sample off when the edges wander.
            self.assertAlmostEqual(report.periodic[0]["period_s"], 24 * STEP, delta=STEP)

    def test_edge_jitter_is_not_an_anomaly(self):
        # The same wave with edges that wander by a sample each cycle.
        self.assertEqual(anomaly.analyze(square(seed=7, jitter=True)).findings, [])

    def test_a_fault_inside_a_cycling_series_is_still_found(self):
        # The job is idle (40) at sample 170..172 of its cycle, but something heats it to 70.
        data = square(fn=lambda i, v: 70 + (v - 40) if 20 + 24 * 7 <= i < 23 + 24 * 7 else v)
        report = anomaly.analyze(data)
        self.assertIn("spike", kinds(report))
        self.assertTrue(report.periodic)  # and it still knows the series cycles

    def test_a_shift_inside_a_cycling_series_is_found_once(self):
        data = square(n=360, fn=lambda i, v: v + 14 if i >= 200 else v)
        shifts = [f for f in anomaly.analyze(data).findings if f.kind == "level_shift"]
        self.assertEqual(len(shifts), 1)
        self.assertAlmostEqual((shifts[0].peak_ts - T0) / STEP, 200, delta=4)

    def test_noise_alone_has_no_period(self):
        for seed in range(30):
            self.assertIsNone(anomaly.find_period([v for _, v in series(seed=seed)]), seed)

    def test_a_ramp_has_no_period(self):
        self.assertIsNone(anomaly.find_period([float(i) for i in range(200)]))
        self.assertIsNone(anomaly.find_period([50 + 0.3 * i + math.sin(i) * 0.1 for i in range(200)]))

    def test_fewer_than_three_cycles_is_not_a_period(self):
        self.assertIsNone(anomaly.find_period([v for _, v in square(n=60, period=24)]))

    def test_a_constant_series_has_no_period(self):
        self.assertIsNone(anomaly.find_period([5.0] * 100))

    def test_regular_spikes_are_a_rhythm_not_three_anomalies(self):
        # A cron job: a spike every 20 minutes. Sparse spikes have weak autocorrelation, so this
        # is caught by the regular-spacing check instead.
        data = series(n=240, fn=lambda i, v: v + 20 if i % 20 == 5 else v)
        report = anomaly.analyze(data)
        self.assertEqual([f for f in report.findings if f.kind == "spike"], [])
        self.assertTrue(report.periodic)

    def test_irregular_spikes_are_all_reported(self):
        hits = {30, 71, 190}
        report = anomaly.analyze(series(n=240, fn=lambda i, v: v + 20 if i in hits else v))
        self.assertEqual(kinds(report).count("spike"), 3)
        self.assertEqual(report.periodic, [])


class HelpersTest(unittest.TestCase):
    def test_sparkline_shape(self):
        line = anomaly.sparkline([1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(line[0], "▁")
        self.assertEqual(line[-1], "█")
        self.assertEqual(len(line), 8)

    def test_sparkline_downsamples_to_width(self):
        self.assertEqual(len(anomaly.sparkline(list(range(500)), width=24)), 24)

    def test_sparkline_of_flat_data_is_flat(self):
        self.assertEqual(anomaly.sparkline([5, 5, 5]), "▁▁▁")

    def test_sparkline_of_nothing_is_empty(self):
        self.assertEqual(anomaly.sparkline([]), "")

    def test_round_keeps_small_values(self):
        self.assertEqual(anomaly._round(0.000123456), 0.0001235)
        self.assertEqual(anomaly._round(12345.678), 12350.0)


class LabelConcentrationTest(unittest.TestCase):
    def fleet(self):
        return [
            {"region": r, "datacenter": f"{r}-dc{d}", "node": f"{r}-dc{d}-n{n}", "gpu": str(g)}
            for r in ("us", "eu", "ap")
            for d in (1, 2)
            for n in (1, 2)
            for g in (0, 1)
        ]

    def test_finds_the_datacenter_all_anomalies_share(self):
        fleet = self.fleet()
        bad = [labels for labels in fleet if labels["datacenter"] == "eu-dc1"]
        found = anomaly.label_concentration(fleet, bad)
        self.assertEqual(found[0]["label"], "datacenter")
        self.assertEqual(found[0]["value"], "eu-dc1")
        self.assertEqual(found[0]["coverage"], 1.0)
        # The region is also shared, but less tightly, so it comes after.
        self.assertIn(("region", "eu"), [(f["label"], f["value"]) for f in found])

    def test_scattered_anomalies_have_no_pattern(self):
        fleet = self.fleet()
        bad = [fleet[0], fleet[9], fleet[17], fleet[22]]
        self.assertEqual(anomaly.label_concentration(fleet, bad), [])

    def test_everything_anomalous_has_no_pattern(self):
        fleet = self.fleet()
        self.assertEqual(anomaly.label_concentration(fleet, fleet), [])

    def test_too_few_anomalous_series_to_say_anything(self):
        fleet = self.fleet()
        self.assertEqual(anomaly.label_concentration(fleet, fleet[:2]), [])

    def test_a_label_that_is_the_same_everywhere_is_ignored(self):
        fleet = [{"job": "dcgm", "node": f"n{i}"} for i in range(10)]
        self.assertEqual(anomaly.label_concentration(fleet, fleet[:5]), [])


class TimeClustersTest(unittest.TestCase):
    def finding(self, ts, kind="spike"):
        return anomaly.Finding(kind, ts, ts, 5.0, ts, 1.0, 0.0)

    def test_series_that_change_together_form_a_cluster(self):
        pairs = [(i, self.finding(T0 + 1000 + i * 30)) for i in range(5)] + [(9, self.finding(T0 + 50000))]
        clusters = anomaly.time_clusters(pairs, window_s=600)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["series_affected"], 5)

    def test_two_series_are_not_a_cluster(self):
        pairs = [(0, self.finding(T0)), (1, self.finding(T0 + 10))]
        self.assertEqual(anomaly.time_clusters(pairs, window_s=600), [])

    def test_the_same_series_twice_counts_once(self):
        pairs = [(0, self.finding(T0)), (0, self.finding(T0 + 10)), (1, self.finding(T0 + 20))]
        self.assertEqual(anomaly.time_clusters(pairs, window_s=600), [])

    def test_gaps_and_flatlines_are_not_clustered(self):
        pairs = [(i, self.finding(T0 + i, kind="gap")) for i in range(5)]
        self.assertEqual(anomaly.time_clusters(pairs, window_s=600), [])


if __name__ == "__main__":
    unittest.main()
