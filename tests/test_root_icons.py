"""Root icon paths that browsers, crawlers and iOS request must not 404."""

import asyncio
import unittest
from pathlib import Path

from fastapi import HTTPException

from main import (
    app,
    serve_apple_touch_icon,
    serve_apple_touch_icon_variant,
    serve_favicon,
)

# Every path that 404'd in production during Oct 23-29, 2026.
REQUESTED_PATHS = (
    "/favicon.ico",
    "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png",
    "/apple-touch-icon-240x240.png",
    "/apple-touch-icon-240x240-precomposed.png",
)


class RootIconTests(unittest.TestCase):
    def test_every_requested_icon_path_has_a_route(self):
        patterns = [
            route.path_regex for route in app.routes if hasattr(route, "path_regex")
        ]

        for path in REQUESTED_PATHS:
            with self.subTest(path=path):
                self.assertTrue(any(pattern.match(path) for pattern in patterns))

    def test_favicon_is_a_small_ico_file(self):
        response = asyncio.run(serve_favicon())

        self.assertEqual(response.media_type, "image/x-icon")
        icon = Path(response.path)
        self.assertTrue(icon.is_file())
        self.assertEqual(icon.read_bytes()[:4], b"\x00\x00\x01\x00")
        self.assertLess(icon.stat().st_size, 32_000)

    def test_apple_touch_icon_names_serve_the_home_screen_icon(self):
        default = asyncio.run(serve_apple_touch_icon())

        for variant in ("precomposed", "240x240", "240x240-precomposed"):
            with self.subTest(variant=variant):
                response = asyncio.run(serve_apple_touch_icon_variant(variant))
                self.assertEqual(response.path, default.path)
                self.assertEqual(response.media_type, "image/png")
        self.assertTrue(Path(default.path).is_file())
        self.assertIn("max-age", default.headers["cache-control"])

    def test_other_apple_touch_icon_names_still_404(self):
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(serve_apple_touch_icon_variant("../../main"))

        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
