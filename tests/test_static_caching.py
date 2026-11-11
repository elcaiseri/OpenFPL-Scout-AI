"""A deploy's page, stylesheet and script must reach browsers together.

Without Cache-Control, browsers guessed a lifetime from Last-Modified, and a
phone kept a previous deploy's CSS and JS for days beside the new page.
"""

import asyncio
import hashlib
import re
import unittest
from pathlib import Path

from main import app


def get(target, headers=()):
    """Send one GET through the whole ASGI app: middleware, mounts, routes."""
    path, _, query = target.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(item for item in messages if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body", b"") for item in messages if item["type"] == "http.response.body"
    )
    response_headers = {
        name.decode().lower(): value.decode() for name, value in start["headers"]
    }
    return start["status"], response_headers, body


def content_version(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


class StaticCachingTests(unittest.TestCase):
    def test_page_is_revalidated_and_links_assets_by_content(self):
        status, headers, body = get("/")
        page = body.decode()

        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-cache")
        self.assertIn(
            f'href="/static/style.css?v={content_version("static/style.css")}"', page
        )
        self.assertIn(
            f'src="/static/script.js?v={content_version("static/script.js")}"', page
        )
        self.assertNotRegex(page, re.compile(r'"/static/(style\.css|script\.js)"'))

    def test_static_files_are_revalidated_with_their_etag(self):
        status, headers, body = get("/static/script.js")

        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-cache")
        self.assertEqual(body, Path("static/script.js").read_bytes())

        status, headers, body = get(
            "/static/script.js", headers=[("If-None-Match", headers["etag"])]
        )

        self.assertEqual(status, 304)
        self.assertEqual(headers["cache-control"], "no-cache")
        self.assertEqual(body, b"")

    def test_a_versioned_link_serves_the_current_file(self):
        status, headers, body = get(
            f"/static/style.css?v={content_version('static/style.css')}"
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-cache")
        self.assertEqual(body, Path("static/style.css").read_bytes())

    def test_report_page_files_are_revalidated_too(self):
        for path in ("/static/report/index.html", "/static/report/charts.js"):
            with self.subTest(path=path):
                status, headers, _ = get(path)

                self.assertEqual(status, 200)
                self.assertEqual(headers["cache-control"], "no-cache")


if __name__ == "__main__":
    unittest.main()
