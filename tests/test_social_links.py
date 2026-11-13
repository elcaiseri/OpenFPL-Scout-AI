"""The app's TikTok, X and YouTube accounts are linked from the page and its metadata."""

import json
import re
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"
PROFILES = (
    "https://www.tiktok.com/@openfpl.kassem.dev",
    "https://x.com/OpenFPLScoutAI",
    "https://youtube.com/@openfplscoutai",
)


class SocialLinkTests(unittest.TestCase):
    def setUp(self):
        self.page = PAGE.read_text(encoding="utf-8")

    def test_structured_data_names_the_profiles(self):
        block = re.search(
            r'<script type="application/ld\+json">(.*?)</script>', self.page, re.S
        )
        graph = json.loads(block.group(1))["@graph"]
        app = next(node for node in graph if node["@type"] == "WebApplication")

        for profile in PROFILES:
            self.assertIn(profile, app["sameAs"])

    def test_x_cards_credit_the_app_account(self):
        self.assertIn('<meta name="twitter:site" content="@OpenFPLScoutAI">', self.page)

    def test_footer_links_open_each_profile_in_a_new_tab(self):
        footer = re.search(r'<nav class="credits-social".*?</nav>', self.page, re.S)
        self.assertIsNotNone(footer)
        links = re.findall(r"<a ([^>]*)>", footer.group(0))

        self.assertEqual(len(links), len(PROFILES))
        for profile, attributes in zip(PROFILES, links):
            with self.subTest(profile=profile):
                self.assertIn(f'href="{profile}"', attributes)
                self.assertIn('target="_blank"', attributes)
                self.assertIn('rel="me noopener"', attributes)


if __name__ == "__main__":
    unittest.main()
