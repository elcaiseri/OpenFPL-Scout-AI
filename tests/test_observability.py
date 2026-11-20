"""In-process logs, request metrics and upstream statistics for the dashboard."""

import logging
import unittest

import requests

from main import request_metrics
from src.observability import LogBuffer, RequestMetrics, percentile, process_snapshot
from src.official_fpl import OfficialFPLAPIError, OfficialFPLClient, OfficialFPLNotFoundError
from tests.test_static_caching import get


class LogBufferTests(unittest.TestCase):
    def setUp(self):
        self.buffer = LogBuffer(capacity=3)
        self.logger = logging.getLogger("tests.observability.buffer")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.logger.addHandler(self.buffer)
        self.addCleanup(self.logger.removeHandler, self.buffer)

    def test_keeps_the_newest_records_and_filters_them(self):
        self.logger.info("first")
        self.logger.warning("second %s", "warning")
        try:
            raise ValueError("boom")
        except ValueError:
            self.logger.exception("third failed")
        self.logger.info("fourth")

        records = self.buffer.records("INFO", limit=10)
        self.assertEqual([record["message"] for record in records], ["fourth", "third failed", "second warning"])
        warnings = self.buffer.records("WARNING")
        self.assertEqual([record["level"] for record in warnings], ["ERROR", "WARNING"])
        self.assertIn("ValueError: boom", warnings[0]["exception"])
        self.assertEqual(self.buffer.records("INFO", query="SECOND")[0]["message"], "second warning")
        self.assertEqual(self.buffer.counts()["INFO"], 2)


class RequestMetricsTests(unittest.TestCase):
    def test_percentiles_and_errors_per_route(self):
        metrics = RequestMetrics(sample_size=10)
        for duration in range(1, 21):
            metrics.record("GET", "/api/x", 200, float(duration))
        metrics.record("GET", "/api/x", 502, 50.0, "/api/x?gameweek=1")
        snapshot = metrics.snapshot()
        route = snapshot["routes"]["GET /api/x"]
        self.assertEqual(route["count"], 21)
        self.assertEqual(route["errors"], 1)
        self.assertEqual(route["statuses"], {"2xx": 20, "5xx": 1})
        self.assertEqual(route["latency_ms"]["max"], 50.0)
        self.assertEqual(snapshot["server_errors"], 1)
        self.assertEqual(snapshot["recent_errors"][0]["path"], "/api/x?gameweek=1")
        self.assertEqual(percentile([1, 2, 3, 4], 0.5), 2)
        self.assertIsNone(percentile([], 0.5))

    def test_middleware_records_route_templates_through_the_app(self):
        request_metrics.reset()
        get("/robots.txt")
        get("/static/style.css")
        get("/apple-touch-icon-180x180.png")
        get("/no-such-page")
        routes = request_metrics.snapshot()["routes"]
        self.assertEqual(routes["GET /robots.txt"]["count"], 1)
        self.assertEqual(routes["GET /static/*"]["count"], 1)
        self.assertIn("GET /apple-touch-icon-{variant}.png", routes)
        self.assertEqual(routes["GET unmatched"]["statuses"], {"4xx": 1})

    def test_process_snapshot_needs_no_dependencies(self):
        snapshot = process_snapshot()
        self.assertGreater(snapshot["uptime_seconds"], 0)
        self.assertGreater(snapshot["threads"], 0)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, timeout):
        self.calls.append(url)
        if url.endswith("/missing/"):
            return FakeResponse({}, 404)
        if url.endswith("/broken/"):
            raise requests.ConnectionError("reset by peer")
        return FakeResponse({"ok": True})


class UpstreamStatsTests(unittest.TestCase):
    def test_counts_requests_cache_hits_failures_and_not_found(self):
        session = FakeSession()
        client = OfficialFPLClient(session=session)
        client._get_json("element-summary/1/")
        client._get_json("element-summary/1/")
        client._get_json("element-summary/2/")
        with self.assertRaises(OfficialFPLNotFoundError):
            client._get_json("missing/")
        with self.assertRaises(OfficialFPLAPIError):
            client._get_json("broken/")

        stats = client.stats_snapshot()
        self.assertEqual(stats["requests"], 4)
        self.assertEqual(stats["cache_hits"], 1)
        self.assertEqual(stats["not_found"], 1)
        self.assertEqual(stats["failures"], 1)
        self.assertIn("reset by peer", stats["last_error"])
        self.assertEqual(stats["latency_by_endpoint"]["/element-summary/{id}/"]["samples"], 2)

        cache = client.cache_status()
        self.assertEqual(cache["entries"], 2)
        self.assertEqual(cache["families"]["/element-summary/{id}/"]["live"], 2)


if __name__ == "__main__":
    unittest.main()
