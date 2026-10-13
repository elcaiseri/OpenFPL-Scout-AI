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
from scripts.social_reel.recap import (
    RecapError,
    best_xi,
    build_recap,
    gameweek_info,
    points_from_live,
    recap_caption,
)

FIXTURE = Path(__file__).parent / "fixtures" / "scout_gw06.json"
LIVE_FIXTURE = Path(__file__).parent / "fixtures" / "live_gw06.json"
BOOTSTRAP_FIXTURE = Path(__file__).parent / "fixtures" / "bootstrap_gw06.json"


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


class SocialReelRecapTest(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.live = json.loads(LIVE_FIXTURE.read_text(encoding="utf-8"))
        self.info = gameweek_info(json.loads(BOOTSTRAP_FIXTURE.read_text(encoding="utf-8")), 6)

    def recap(self):
        return build_recap(parse_squad(self.payload), points_from_live(self.live), self.info)

    def test_best_xi_matches_team_rating_rule(self):
        xi, shape = best_xi(parse_squad(self.payload).players)
        self.assertEqual(shape, (3, 5, 2))
        self.assertEqual(len(xi), 11)
        self.assertNotIn("Calvert-Lewin", {p.name for p in xi})
        self.assertIn("Donnarumma", {p.name for p in xi})

    def test_total_doubles_captain(self):
        recap = self.recap()
        self.assertEqual(recap.total, 85)
        self.assertAlmostEqual(recap.xp_total, 67.89, places=2)
        self.assertEqual(recap.formation, "3-5-2")

    def test_hauls_misses_and_hindsight(self):
        recap = self.recap()
        self.assertEqual([p.name for p in recap.hauls(2)], ["De Cuyper", "João Pedro"])
        self.assertEqual([p.name for p in recap.misses(2)], ["B.Fernandes", "Saka"])
        self.assertEqual(recap.hindsight_captain().name, "De Cuyper")
        self.assertEqual(recap.beat_count(), 6)

    def test_gameweek_info(self):
        self.assertEqual(self.info.average, 58)
        self.assertFalse(self.info.data_checked)
        self.assertIsNone(gameweek_info({}, 6).average)

    def test_rejects_unlocked_forecast(self):
        live = copy.deepcopy(self.payload)
        live["frozen"] = False
        with self.assertRaises(RecapError):
            build_recap(parse_squad(live), points_from_live(self.live), self.info)

    def test_rejects_missing_points(self):
        partial = {"elements": self.live["elements"][1:]}
        with self.assertRaises(RecapError):
            build_recap(parse_squad(self.payload), points_from_live(partial), self.info)

    def test_recap_caption(self):
        text = recap_caption(self.recap())
        self.assertIn("Gameweek 6 recap", text)
        self.assertIn("85 pts (+27 on the 58-pt GW average)", text)
        self.assertIn("B.Fernandes 1 pt (xP 5.6)", text)
        self.assertIn("GW7 squad is live", text)


if __name__ == "__main__":
    unittest.main()
