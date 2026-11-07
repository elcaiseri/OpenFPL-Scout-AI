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
    differentials,
    differentials_from_json,
    differentials_to_json,
    ownership_from_bootstrap,
    parse_squad,
    pool_rows,
    stacks,
    status_line,
    with_differentials,
)
from scripts.social_reel.recap import (
    RecapError,
    attach_differentials,
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

    def test_after_deadline_freeze_is_labelled_retro(self):
        self.assertTrue(self.recap().locked_pre_deadline)
        retro = copy.deepcopy(self.payload)
        retro["freeze_method"] = "after-deadline"
        retro["forecast_captured_at"] = "2026-10-12T09:00:00+00:00"
        recap = build_recap(parse_squad(retro), points_from_live(self.live), self.info)
        self.assertFalse(recap.locked_pre_deadline)
        text = recap_caption(recap)
        self.assertIn("retro forecast", text)
        self.assertNotIn("no hindsight", text)

    def test_cta_points_at_next_open_gameweek(self):
        self.assertEqual(self.recap().cta_gameweek, 7)
        info = gameweek_info({"events": [{"id": 6}, {"id": 9, "is_next": True}]}, 6)
        recap = build_recap(parse_squad(self.payload), points_from_live(self.live), info)
        self.assertEqual(recap.cta_gameweek, 9)

    def _live_with(self, element_id, minutes, points):
        live = copy.deepcopy(self.live)
        for element in live["elements"]:
            if element["id"] == element_id:
                element["stats"].update(minutes=minutes, total_points=points)
        return points_from_live(live)

    def test_auto_sub_replaces_starter_who_did_not_play(self):
        # Saka (MID, starter) did not play: first bench outfielder Tarkowski (3 pts) comes on, 4-4-2.
        recap = build_recap(parse_squad(self.payload), self._live_with(12, 0, 0), self.info)
        self.assertEqual([(off.name, on.name) for off, on in recap.subs], [("Saka", "Tarkowski")])
        self.assertIn("Tarkowski", recap.xi_names)
        self.assertEqual(recap.total, 85 - 2 + 3)
        self.assertIn("Auto-sub: Tarkowski (3 pts) on for Saka", recap_caption(recap))

    def test_vice_takes_armband_when_captain_did_not_play(self):
        recap = build_recap(parse_squad(self.payload), self._live_with(124, 0, 0), self.info)
        self.assertEqual(recap.armband_player.name, "Haaland")
        # Groß's 6 (doubled) out, Tarkowski's 3 on, Haaland's 6 doubled.
        self.assertEqual(recap.total, 85 - 12 + 3 + 6)

    def test_recap_caption(self):
        text = recap_caption(self.recap())
        self.assertIn("Gameweek 6 recap", text)
        self.assertIn("85 pts (+27 on the 58-pt GW average)", text)
        self.assertIn("B.Fernandes 1 pt (xP 5.6)", text)
        self.assertIn("GW7 squad is live", text)



class DifferentialsTest(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.squad = parse_squad(self.payload)
        self.gw = self.squad.gameweek

    def row(self, pid, pos, xp, name=None, fixtures=1, gameweek=None):
        return {"id": pid, "element_type": pos, "web_name": name or f"P{pid}", "team_name": "Everton",
                "opponent_team_name": "Brentford", "was_home": False, "gameweek": gameweek or self.gw,
                "expected_points": xp, "fixture_count": fixtures}

    def test_top_two_per_position_under_ten_percent_outside_squad(self):
        squad_id = self.squad.players[0].id
        pool = [
            self.row(901, 1, 3.0), self.row(902, 1, 3.5), self.row(903, 1, 2.0),   # GK: 902, 901
            self.row(911, 2, 4.0), self.row(912, 2, 5.0),                          # DEF: 912 too owned -> 911 only
            self.row(921, 3, 6.0), self.row(922, 3, 5.5), self.row(923, 3, 5.9),   # MID: 923 injured
            self.row(931, 4, 4.0, fixtures=0), self.row(932, 4, 3.0),             # FWD: 931 blanks
            self.row(squad_id, 3, 9.9),                                            # in the 15
            self.row(941, 3, 9.0, gameweek=self.gw + 1),                           # other gameweek
        ]
        ownership = ownership_from_bootstrap({"elements": [
            {"id": 901, "selected_by_percent": "4.0", "status": "a"},
            {"id": 902, "selected_by_percent": "9.9", "status": "a"},
            {"id": 903, "selected_by_percent": "1.0", "status": "a"},
            {"id": 911, "selected_by_percent": "3.0", "status": "a"},
            {"id": 912, "selected_by_percent": "10.0", "status": "a"},
            {"id": 921, "selected_by_percent": "2.0", "status": "a"},
            {"id": 922, "selected_by_percent": "2.0", "status": "a"},
            {"id": 923, "selected_by_percent": "2.0", "status": "i"},
            {"id": 931, "selected_by_percent": "1.0", "status": "a"},
            {"id": 932, "selected_by_percent": "1.0", "status": "a"},
            {"id": squad_id, "selected_by_percent": "1.0", "status": "a"},
            {"id": 941, "selected_by_percent": "1.0", "status": "a"},
        ]})
        picks = differentials(pool, self.squad, ownership)
        self.assertEqual([(d.player.position, d.player.id) for d in picks],
                         [("GK", 902), ("GK", 901), ("DEF", 911), ("MID", 921), ("MID", 922), ("FWD", 932)])
        self.assertEqual(picks[0].owned, 9.9)

    def test_player_without_ownership_is_skipped(self):
        picks = differentials([self.row(950, 2, 8.0)], self.squad, {})
        self.assertEqual(picks, ())

    def test_pool_rows_prefers_first_non_empty(self):
        self.assertEqual(pool_rows(None, {"player_points": []}, {"player_points": [1]}), [1])

    def test_caption_lists_differentials(self):
        ownership = {960: (3.4, "a")}
        squad = with_differentials(self.squad, differentials([self.row(960, 3, 4.2, "Rayan")], self.squad, ownership))
        text = caption(squad)
        self.assertIn("Differentials (under 10% owned, outside the 15):", text)
        self.assertIn("MID: Rayan (EVE, 4.2 xP, 3.4%)", text)
        self.assertNotIn("Differentials", caption(self.squad))



class RecapDifferentialsTest(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.live = json.loads(LIVE_FIXTURE.read_text(encoding="utf-8"))
        self.boot = json.loads(BOOTSTRAP_FIXTURE.read_text(encoding="utf-8"))
        self.squad = parse_squad(self.payload)
        self.info = gameweek_info(self.boot, self.squad.gameweek)
        row = {"id": 970, "element_type": 3, "web_name": "Rayan", "team_name": "Bournemouth",
               "opponent_team_name": "Leeds", "was_home": True, "gameweek": self.squad.gameweek,
               "expected_points": 4.0, "fixture_count": 1}
        self.picks = differentials([row], self.squad, {970: (2.3, "a")})
        self.live["elements"].append({"id": 970, "stats": {"minutes": 87, "total_points": 3}})

    def _file(self, source, captured):
        return differentials_to_json(with_differentials(self.squad, self.picks), source, captured)

    def test_json_round_trip(self):
        picks, source, captured = differentials_from_json(self._file("preview", "2020-01-01T00:00:00Z"))
        self.assertEqual((picks[0].player.name, picks[0].player.position, picks[0].owned), ("Rayan", "MID", 2.3))
        self.assertEqual((source, captured), ("preview", "2020-01-01T00:00:00Z"))

    def test_scores_picks_and_flags_locked_only_before_deadline(self):
        recap = build_recap(self.squad, points_from_live(self.live), self.info)
        picks, _, _ = differentials_from_json(self._file("preview", "2000-01-01T00:00:00Z"))
        early = attach_differentials(recap, picks, points_from_live(self.live), "preview", "2000-01-01T00:00:00Z")
        self.assertEqual((early.diff_total, round(early.diff_xp, 1)), (3, 4.0))
        self.assertTrue(early.diffs_locked)
        late = attach_differentials(recap, picks, points_from_live(self.live), "preview", "2999-01-01T00:00:00Z")
        self.assertFalse(late.diffs_locked)
        retro = attach_differentials(recap, picks, points_from_live(self.live), "retro", None)
        self.assertFalse(retro.diffs_locked)
        text = recap_caption(retro)
        self.assertIn("retro pick", text)
        self.assertIn("MID: Rayan 3 pts (xP 4.0, 2.3%)", text)

    def test_refuses_missing_live_points(self):
        recap = build_recap(self.squad, points_from_live(self.live), self.info)
        live = copy.deepcopy(self.live)
        live["elements"] = [e for e in live["elements"] if e["id"] != 970]
        with self.assertRaises(RecapError):
            attach_differentials(recap, self.picks, points_from_live(live), "preview", None)


if __name__ == "__main__":
    unittest.main()
