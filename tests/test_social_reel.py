import copy
import json
import unittest
from pathlib import Path

from scripts.social_reel.data import (
    SquadError,
    armband_line,
    caption,
    club_code,
    deadline_label,
    parse_squad,
    stacks,
    status_line,
)

FIXTURE = Path(__file__).parent / "fixtures" / "scout_gw06.json"


class SocialReelDataTest(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_parses_full_squad(self):
        squad = parse_squad(self.payload)
        self.assertEqual(squad.gameweek, 6)
        self.assertEqual(len(squad.players), 15)
        self.assertEqual(squad.captain.name, "Groß")
        self.assertEqual(squad.vice.name, "Haaland")
        self.assertEqual([p.name for p in squad.top(3)], ["Groß", "Haaland", "João Pedro"])
        self.assertEqual(squad.captain.fixture, "SUN (A)")

    def test_stacks_rank_triples_by_total_xp(self):
        squad = parse_squad(self.payload)
        self.assertEqual([(club, len(ps)) for club, ps in stacks(squad)], [("Chelsea", 3), ("Arsenal", 3)])

    def test_armband_line_uses_gap(self):
        self.assertEqual(armband_line(parse_squad(self.payload)), "Separated by just 0.04 xP")

    def test_status_line_follows_freeze_and_deadline(self):
        frozen = parse_squad(self.payload)
        self.assertIn("Locked at the deadline", status_line(frozen))
        live = copy.deepcopy(self.payload)
        live["frozen"] = False
        squad = parse_squad(live, deadline="2026-10-17T10:00:00Z")
        self.assertEqual(status_line(squad), "Deadline Sat 17 Oct · 11:00 UK · check team news")

    def test_deadline_label_handles_bad_input(self):
        self.assertIsNone(deadline_label(None))
        self.assertIsNone(deadline_label("not a date"))

    def test_rejects_wrong_squad_shape(self):
        broken = copy.deepcopy(self.payload)
        broken["scout_team"] = broken["scout_team"][:14]
        with self.assertRaises(SquadError):
            parse_squad(broken)

    def test_rejects_missing_captain(self):
        broken = copy.deepcopy(self.payload)
        broken["scout_team"][0]["role"] = ""
        with self.assertRaises(SquadError):
            parse_squad(broken)

    def test_double_gameweek_fixture_label(self):
        dgw = copy.deepcopy(self.payload)
        dgw["scout_team"][0]["opponent_team_name"] = "Sunderland, Everton"
        dgw["scout_team"][0]["fixture_count"] = 2
        player = parse_squad(dgw).captain
        self.assertEqual(player.venue, "×2")
        self.assertEqual(player.fixture, "SUN+EVE (DGW)")

    def test_unknown_club_falls_back_to_three_letters(self):
        self.assertEqual(club_code("Brand New Town"), "BRA")
        self.assertEqual(club_code("Manchester City"), "MCI")

    def test_caption_mentions_captain_and_site(self):
        text = caption(parse_squad(self.payload))
        self.assertIn("Gameweek 6", text)
        self.assertIn("Captain: Groß", text)
        self.assertIn("openfpl.kassem.dev", text)


if __name__ == "__main__":
    unittest.main()
