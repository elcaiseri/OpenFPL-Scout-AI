"""In-process observability for the owner dashboard.

Everything here lives in memory and is per instance: a ring buffer of recent
log records, per-route request metrics, and a process snapshot. Nothing is
sent anywhere; the admin API reads it on demand. Cloud Logging keeps the
durable logs.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
PROCESS_STARTED_AT = time.time()
PROCESS_STARTED_MONOTONIC = time.monotonic()


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def percentile(values: Iterable[float], fraction: float) -> Optional[float]:
    """Nearest-rank percentile of ``values``, or None when there are none."""
    ordered = sorted(values)
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, int(round(fraction * len(ordered))) - 1))
    return ordered[index]


class LogBuffer(logging.Handler):
    """Keep the most recent log records so the dashboard can show them."""

    MESSAGE_LIMIT = 2000
    TRACEBACK_LINES = 12

    def __init__(self, capacity: int = 1000, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._records: Deque[Dict[str, Any]] = deque(maxlen=capacity)
        self._lock_records = threading.Lock()
        self._counts: Dict[str, int] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # A bad format string must not lose the record.
            message = str(record.msg)
        if len(message) > self.MESSAGE_LIMIT:
            message = f"{message[: self.MESSAGE_LIMIT - 3]}..."
        entry: Dict[str, Any] = {
            "time": _utc(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "location": f"{record.filename}:{record.lineno}",
        }
        if record.exc_info:
            try:
                formatted = logging.Formatter().formatException(record.exc_info)
            except Exception:
                formatted = ""
            lines = formatted.splitlines()
            entry["exception"] = "\n".join(lines[-self.TRACEBACK_LINES :])
        with self._lock_records:
            self._records.append(entry)
            self._counts[record.levelname] = self._counts.get(record.levelname, 0) + 1

    def records(
        self,
        level: str = "INFO",
        limit: int = 200,
        query: Optional[str] = None,
        logger_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Newest first, at or above ``level``, optionally matching ``query``."""
        threshold = LEVELS.get(str(level).upper(), logging.INFO)
        needle = (query or "").casefold()
        with self._lock_records:
            items = list(self._records)
        result = []
        for entry in reversed(items):
            if LEVELS.get(entry["level"], 0) < threshold:
                continue
            if logger_name and not entry["logger"].startswith(logger_name):
                continue
            if needle and needle not in (
                f"{entry['message']} {entry['logger']} {entry.get('exception', '')}"
            ).casefold():
                continue
            result.append(entry)
            if len(result) >= limit:
                break
        return result

    def counts(self) -> Dict[str, int]:
        with self._lock_records:
            return dict(self._counts)

    def clear(self) -> None:
        with self._lock_records:
            self._records.clear()
            self._counts.clear()


_log_buffer: Optional[LogBuffer] = None


def install_log_buffer(capacity: int = 1000) -> LogBuffer:
    """Attach one shared buffer to the root logger (idempotent).

    Module loggers propagate to the root, and handlers on ancestors are
    called whatever the ancestor's own level, so INFO records arrive.
    """
    global _log_buffer
    if _log_buffer is None:
        _log_buffer = LogBuffer(capacity)
        logging.getLogger().addHandler(_log_buffer)
    return _log_buffer


class RouteStats:
    """Counters and recent latencies for one route."""

    __slots__ = ("count", "statuses", "latencies", "last_status", "last_at", "errors")

    def __init__(self, sample_size: int) -> None:
        self.count = 0
        self.errors = 0
        self.statuses: Dict[str, int] = {}
        self.latencies: Deque[float] = deque(maxlen=sample_size)
        self.last_status: Optional[int] = None
        self.last_at: Optional[float] = None

    def snapshot(self) -> Dict[str, Any]:
        latencies = list(self.latencies)
        return {
            "count": self.count,
            "errors": self.errors,
            "error_rate": round(self.errors / self.count, 4) if self.count else 0.0,
            "statuses": dict(self.statuses),
            "last_status": self.last_status,
            "last_at": _utc(self.last_at) if self.last_at else None,
            "latency_ms": {
                "avg": round(sum(latencies) / len(latencies), 1) if latencies else None,
                "p50": _round(percentile(latencies, 0.5)),
                "p95": _round(percentile(latencies, 0.95)),
                "max": _round(max(latencies) if latencies else None),
            },
        }


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 1)


class RequestMetrics:
    """Per-route request counts, status classes, latencies and recent errors."""

    def __init__(self, sample_size: int = 256, error_capacity: int = 100) -> None:
        self.sample_size = sample_size
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._routes: Dict[str, RouteStats] = {}
        self._errors: Deque[Dict[str, Any]] = deque(maxlen=error_capacity)

    def record(
        self, method: str, route: str, status: int, duration_ms: float, path: str = ""
    ) -> None:
        key = f"{method} {route}"
        status_class = f"{int(status) // 100}xx"
        now = time.time()
        with self._lock:
            stats = self._routes.get(key)
            if stats is None:
                stats = self._routes[key] = RouteStats(self.sample_size)
            stats.count += 1
            stats.statuses[status_class] = stats.statuses.get(status_class, 0) + 1
            stats.latencies.append(float(duration_ms))
            stats.last_status = int(status)
            stats.last_at = now
            if status >= 400:
                stats.errors += 1
                self._errors.append(
                    {
                        "time": _utc(now),
                        "method": method,
                        "route": route,
                        "path": path,
                        "status": int(status),
                        "duration_ms": round(float(duration_ms), 1),
                    }
                )

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            routes = {key: stats.snapshot() for key, stats in self._routes.items()}
            errors = list(reversed(self._errors))
        total = sum(item["count"] for item in routes.values())
        failed = sum(
            item["statuses"].get("5xx", 0) for item in routes.values()
        )
        rejected = sum(
            item["statuses"].get("4xx", 0) for item in routes.values()
        )
        all_latencies = []
        with self._lock:
            for stats in self._routes.values():
                all_latencies.extend(stats.latencies)
        return {
            "since": _utc(self.started_at),
            "total_requests": total,
            "server_errors": failed,
            "client_errors": rejected,
            "error_rate": round((failed + rejected) / total, 4) if total else 0.0,
            "latency_ms": {
                "p50": _round(percentile(all_latencies, 0.5)),
                "p95": _round(percentile(all_latencies, 0.95)),
            },
            "routes": dict(
                sorted(routes.items(), key=lambda item: -item[1]["count"])
            ),
            "recent_errors": errors,
        }

    def reset(self) -> None:
        with self._lock:
            self._routes.clear()
            self._errors.clear()
            self.started_at = time.time()


class MetricsMiddleware:
    """Pure ASGI middleware that records each HTTP request's route and timing.

    Unlike BaseHTTPMiddleware it never buffers or wraps the body, so HEAD,
    streaming and GZip behave exactly as before. Static mounts are grouped,
    so thousands of file paths do not each become a route.
    """

    GROUPED_PREFIXES = ("/static/", "/assets/")

    def __init__(
        self,
        app: Any,
        metrics: RequestMetrics,
        route_resolver: Optional[Callable[[Dict[str, Any]], str]] = None,
    ) -> None:
        self.app = app
        self.metrics = metrics
        self.route_resolver = route_resolver

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                status_holder["status"] = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            try:
                duration_ms = (time.perf_counter() - started) * 1000.0
                self.metrics.record(
                    str(scope.get("method", "GET")),
                    self._route(scope),
                    status_holder["status"],
                    duration_ms,
                    str(scope.get("path", "")),
                )
            except Exception:  # Metrics must never break a request.
                pass

    def _route(self, scope: Dict[str, Any]) -> str:
        path = str(scope.get("path", ""))
        for prefix in self.GROUPED_PREFIXES:
            if path.startswith(prefix):
                return f"{prefix}*"
        route = scope.get("route")
        template = getattr(route, "path", None)
        if template:
            return str(template)
        if self.route_resolver is not None:
            try:
                return self.route_resolver(scope)
            except Exception:
                pass
        return "unmatched"


def route_resolver_for(app: Any) -> Callable[[Dict[str, Any]], str]:
    """Match a scope against ``app.routes`` and return the route template."""
    from starlette.routing import Match

    @lru_cache(maxsize=512)
    def resolve(method: str, path: str) -> str:
        scope = {"type": "http", "method": method, "path": path, "root_path": ""}
        partial = None
        for route in app.routes:
            try:
                match, _ = route.matches(scope)
            except Exception:
                continue
            template = getattr(route, "path", None) or ""
            if match == Match.FULL:
                return template or path
            if match == Match.PARTIAL and partial is None:
                partial = template
        return partial or "unmatched"

    def resolver(scope: Dict[str, Any]) -> str:
        return resolve(str(scope.get("method", "GET")), str(scope.get("path", "")))

    return resolver


def _rss_bytes() -> Optional[int]:
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _peak_rss_bytes() -> Optional[int]:
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:
        return None
    # Linux reports kilobytes; macOS reports bytes.
    return int(peak if sys.platform == "darwin" else peak * 1024)


def process_snapshot() -> Dict[str, Any]:
    """Describe this server process without third-party dependencies."""
    times = os.times()
    try:
        load = [round(value, 2) for value in os.getloadavg()]
    except (OSError, AttributeError):
        load = None
    return {
        "pid": os.getpid(),
        "python": platform.python_version(),
        "platform": platform.platform(terse=True),
        "started_at": _utc(PROCESS_STARTED_AT),
        "uptime_seconds": round(time.monotonic() - PROCESS_STARTED_MONOTONIC, 1),
        "rss_bytes": _rss_bytes(),
        "peak_rss_bytes": _peak_rss_bytes(),
        "cpu_seconds": round(times.user + times.system, 2),
        "threads": threading.active_count(),
        "cpu_count": os.cpu_count(),
        "load_average": load,
    }
