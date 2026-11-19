"""The analysis tools against fake metrics and logs backends.

The fleet has four datacenters. One of them, eu1, heats up 18 degrees in the last hour,
so the tools must find it, say when it started, and name the datacenter.
"""

import unittest

from chatagent.backends import Backends, BackendError, fill_missing_buckets, parse_time, pick_step, resolve_window
from chatagent.config import Settings
from chatagent.tools import build_tools

import fakes


class ToolsTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = fakes.FakeBackend()
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__, None, None, None)

    async def asyncSetUp(self):
        self.backends = Backends(self.fake.url, self.fake.url, 5)
        self.addAsyncCleanup(self.backends.aclose)
        tools = build_tools(Settings(), self.backends, clock=lambda: fakes.NOW)
        self.summarize, self.detect_metrics, self.detect_logs = tools


class SummarizeMetricTest(ToolsTestCase):
    async def test_ranks_series_and_reports_stats(self):
        out = await self.summarize("DCGM_FI_DEV_GPU_TEMP", top=3)
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["series_total"], 16)
        self.assertEqual(out["series_shown"], 3)
        hottest = out["series"][0]
        self.assertEqual(hottest["labels"]["datacenter"], "eu1")
        self.assertGreater(hottest["max"], 78)
        self.assertEqual(len(hottest["sparkline"]), 24)
        self.assertIn("more series not shown", out["note"])

    async def test_ascending_order_returns_the_coolest(self):
        out = await self.summarize("DCGM_FI_DEV_GPU_TEMP", top=2, rank_by="max", order="asc")
        self.assertNotEqual(out["series"][0]["labels"]["datacenter"], "eu1")
        self.assertLess(out["series"][0]["max"], 66)

    async def test_ranking_by_change_finds_the_series_that_moved(self):
        out = await self.summarize("DCGM_FI_DEV_GPU_TEMP", top=4, rank_by="change")
        self.assertEqual({s["labels"]["datacenter"] for s in out["series"]}, {"eu1"})
        self.assertGreater(out["series"][0]["change_pct"], 20)

    async def test_the_query_and_window_reach_the_backend(self):
        await self.summarize("up", lookback_minutes=30, end_time="2027-01-15T08:00:00Z")
        method, path, params = self.fake.requests[-1]
        self.assertEqual((method, path), ("GET", "/api/v1/query_range"))
        self.assertEqual(params["query"], "up")
        self.assertEqual(float(params["end"]) - float(params["start"]), 1800)
        self.assertEqual(float(params["end"]), parse_time("2027-01-15T08:00:00Z", 0))
        self.assertRegex(params["step"], r"^\d+s$")

    async def test_backend_error_is_returned_not_raised(self):
        self.fake.metrics_status = 422
        self.fake.metrics_body = {"status": "error", "error": "cannot parse query: unexpected token"}
        out = await self.summarize("sum(")
        self.assertEqual(out["status"], "error")
        self.assertIn("cannot parse query", out["error"])

    async def test_empty_result_explains_itself(self):
        self.fake.metric_series = []
        out = await self.summarize("nothing")
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["series"], [])
        self.assertIn("no samples", out["note"])

    async def test_empty_query_is_rejected(self):
        self.assertEqual((await self.summarize("  "))["status"], "error")

    async def test_oversized_query_is_rejected(self):
        self.assertEqual((await self.summarize("a" * 5000))["status"], "error")

    async def test_bad_time_is_a_readable_error(self):
        out = await self.summarize("up", end_time="yesterday-ish")
        self.assertEqual(out["status"], "error")
        self.assertIn("ISO 8601", out["error"])

    async def test_unreachable_backend_is_a_readable_error(self):
        dead = Backends("http://127.0.0.1:1", "http://127.0.0.1:1", 2)
        self.addAsyncCleanup(dead.aclose)
        summarize, _, _ = build_tools(Settings(), dead, clock=lambda: fakes.NOW)
        out = await summarize("up")
        self.assertEqual(out["status"], "error")
        self.assertIn("Cannot reach the metrics backend", out["error"])

    async def test_a_scalar_result_is_explained(self):
        self.fake.metrics_body = {"status": "success", "data": {"resultType": "vector", "result": []}}
        out = await self.summarize("up")
        self.assertEqual(out["status"], "error")
        self.assertIn("range vector", out["error"])


class DetectMetricAnomaliesTest(ToolsTestCase):
    async def test_finds_the_heated_datacenter(self):
        out = await self.detect_metrics("DCGM_FI_DEV_GPU_TEMP")
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["series_analyzed"], 16)
        self.assertEqual(out["series_with_anomalies"], 4)
        self.assertEqual({s["labels"]["datacenter"] for s in out["anomalous_series"]}, {"eu1"})

    async def test_reports_when_it_started_and_that_it_is_still_happening(self):
        top = (await self.detect_metrics("DCGM_FI_DEV_GPU_TEMP"))["anomalous_series"][0]
        shift = next(f for f in top["findings"] if f["kind"] == "level_shift")
        self.assertEqual(shift["peak_time"], fakes.iso(fakes.NOW - 60 * fakes.STEP))  # minute 120 of 180
        self.assertTrue(top["anomalous_now"] or top["latest_value"] > top["typical_value"])
        self.assertIn("up", shift["detail"])

    async def test_names_the_label_the_anomalies_share(self):
        out = await self.detect_metrics("DCGM_FI_DEV_GPU_TEMP")
        best = out["patterns"][0]
        self.assertEqual((best["label"], best["value"]), ("datacenter", "eu1"))
        self.assertEqual(best["coverage"], 1.0)

    async def test_reports_that_the_series_changed_together(self):
        out = await self.detect_metrics("DCGM_FI_DEV_GPU_TEMP")
        self.assertEqual(out["simultaneous"][0]["series_affected"], 4)

    async def test_healthy_fleet_says_so(self):
        self.fake.metric_series = fakes.fleet(hot_dc="none")
        out = await self.detect_metrics("DCGM_FI_DEV_GPU_TEMP")
        self.assertEqual(out["series_with_anomalies"], 0)
        self.assertIn("Nothing unusual", " ".join(out["notes"]))
        self.assertEqual(out["patterns"], [])

    async def test_max_series_limits_the_description_and_says_how_many_were_left_out(self):
        out = await self.detect_metrics("DCGM_FI_DEV_GPU_TEMP", max_series=2)
        self.assertEqual(len(out["anomalous_series"]), 2)
        self.assertIn("2 more anomalous series", " ".join(out["notes"]))

    async def test_sensitivity_is_clamped(self):
        out = await self.detect_metrics("DCGM_FI_DEV_GPU_TEMP", sensitivity=-5)
        self.assertEqual(out["sensitivity"], 1.5)

    async def test_short_series_are_skipped_with_a_note(self):
        self.fake.metric_series = [dict(s, values=s["values"][:5]) for s in fakes.fleet()]
        out = await self.detect_metrics("DCGM_FI_DEV_GPU_TEMP")
        self.assertEqual(out["series_analyzed"], 0)
        self.assertIn("fewer than", " ".join(out["notes"]))

    async def test_errors_are_returned(self):
        self.fake.metrics_status = 400
        self.fake.metrics_body = {"status": "error", "error": "bad"}
        self.assertEqual((await self.detect_metrics("x"))["status"], "error")


class DetectLogAnomaliesTest(ToolsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fake.log_hits = fakes.error_log_hits

    async def test_finds_the_noisy_node_and_ignores_quiet_ones(self):
        out = await self.detect_logs("level:error", group_by="node")
        self.assertEqual(out["status"], "success")
        names = [s["labels"]["node"] for s in out["anomalous_series"]]
        self.assertEqual(names, ["noisy-node"])
        finding = out["anomalous_series"][0]["findings"][0]
        self.assertIn(finding["kind"], {"level_shift", "spike"})
        self.assertGreater(finding["peak_value"], 30)

    async def test_total_lines_is_reported(self):
        out = await self.detect_logs("level:error", group_by="node")
        self.assertGreater(out["total_lines"], 100)

    async def test_missing_buckets_count_as_zero_not_as_gaps(self):
        out = await self.detect_logs("level:error")  # one ungrouped series, every fifth bucket missing
        self.assertEqual(out["series_with_anomalies"], 0, out)

    async def test_request_uses_whole_buckets_and_the_group_field(self):
        await self.detect_logs("level:error", group_by="node", lookback_minutes=60, bucket_seconds=120)
        method, path, params = self.fake.requests[-1]
        self.assertEqual((method, path), ("POST", "/select/logsql/hits"))
        self.assertEqual(params["field"], "node")
        self.assertEqual(params["step"], "120s")
        self.assertEqual(float(params["start"]) % 120, 0)
        self.assertEqual(float(params["end"]) % 120, 0)

    async def test_no_matching_logs_is_not_an_error(self):
        self.fake.log_hits = lambda params: []
        out = await self.detect_logs("level:never")
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["series_with_anomalies"], 0)

    async def test_window_shorter_than_a_bucket_is_an_error(self):
        out = await self.detect_logs("*", lookback_minutes=1, bucket_seconds=3600)
        self.assertEqual(out["status"], "error")


class HelpersTest(unittest.TestCase):
    def test_pick_step_gives_about_180_points_with_a_readable_size(self):
        self.assertEqual(pick_step(3600), 30)
        self.assertEqual(pick_step(3 * 3600), 60)
        self.assertEqual(pick_step(24 * 3600), 600)

    def test_pick_step_respects_a_request_but_not_beyond_the_backend_limit(self):
        self.assertEqual(pick_step(3600, requested_s=300), 300)
        self.assertEqual(pick_step(90 * 86400, requested_s=5), 7776)

    def test_resolve_window_rejects_nonsense(self):
        with self.assertRaises(BackendError):
            resolve_window(0, "", 1000)
        with self.assertRaises(BackendError):
            resolve_window(10**7, "", 1000)

    def test_parse_time_accepts_iso_and_unix_and_empty(self):
        self.assertEqual(parse_time("", 42), 42)
        self.assertEqual(parse_time("1700000000", 0), 1700000000)
        self.assertEqual(parse_time("2023-11-14T22:13:20Z", 0), 1700000000)
        self.assertEqual(parse_time("2023-11-14T22:13:20", 0), 1700000000)  # no zone means UTC
        self.assertEqual(parse_time("2023-11-14T23:13:20+01:00", 0), 1700000000)

    def test_fill_missing_buckets_inserts_zeros(self):
        filled = fill_missing_buckets([(0, 5.0), (120, 7.0)], 0, 180, 60)
        self.assertEqual(filled, [(0, 5.0), (60, 0.0), (120, 7.0), (180, 0.0)])


if __name__ == "__main__":
    unittest.main()
