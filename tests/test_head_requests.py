"""Uptime monitors and crawlers send HEAD; the site's root pages must answer it.

FastAPI's @app.get rejects HEAD with 405 (Starlette's own routes and the
/static mount accept it), so a HEAD-based uptime check reported the site down.
"""

import asyncio
import unittest

from main import app

ROOT_PAGES = (
    "/",
    "/robots.txt",
    "/sitemap.xml",
    "/favicon.ico",
    "/apple-touch-icon.png",
    "/apple-touch-icon-180x180-precomposed.png",
)


def request(method, path):
    """Send one request through the whole ASGI app: middleware, mounts, routes."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
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
    headers = {name.decode().lower(): value.decode() for name, value in start["headers"]}
    return start["status"], headers, body


class HeadRequestTests(unittest.TestCase):
    def test_root_pages_answer_head_with_their_get_headers(self):
        for path in ROOT_PAGES:
            with self.subTest(path=path):
                get_status, get_headers, get_body = request("GET", path)
                head_status, head_headers, _ = request("HEAD", path)

                self.assertEqual(get_status, 200)
                self.assertEqual(head_status, 200)
                for name in ("content-type", "cache-control"):
                    self.assertEqual(head_headers.get(name), get_headers.get(name))
                # uvicorn drops a HEAD body; the length still describes the GET.
                self.assertEqual(int(head_headers["content-length"]), len(get_body))

    def test_head_on_an_unknown_icon_name_is_still_a_404(self):
        status, _, _ = request("HEAD", "/apple-touch-icon-evil.png")

        self.assertEqual(status, 404)

    def test_head_stays_out_of_the_api_catalogue(self):
        status, _, body = request("GET", "/api")

        self.assertEqual(status, 200)
        self.assertNotIn(b'"HEAD"', body)


if __name__ == "__main__":
    unittest.main()
