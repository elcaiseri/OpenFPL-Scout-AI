"""A manager's working squad: rules, money, overrides, autosubs and advice."""

import itertools
import random
import unittest

import pandas as pd

from src import my_team

# Slots 1-11 start (1 GK, 4 DEF, 4 MID, 2 FWD); 12 is the bench GK.
LAYOUT = [1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 1, 2, 3, 4]


def make_players(**changes):
    """15 squad players (ids 1-15) and spares (ids 101+), one club each."""
    players = {}
    for index, position in enumerate(LAYOUT, start=1):
        players[index] = {
            "id": index,
            "web_name": f"P{index}",
            "team_name": f"Club {index}",
            "element_type": position,
            "price": 5.0,
            "expected_points": float(2 + index % 5),
            "availability_factor": 1.0,
            "selected_by_percent": 5.0,
            "status": "a",
            "can_select": True,
        }
    for offset, position in enumerate((1, 2, 3, 4), start=101):
        players[offset] = {
            "id": offset,
            "web_name": f"S{offset}",
            "team_name": f"Club {offset}",
            "element_type": position,
            "price": 6.0,
            "expected_points": 6.0,
            "availability_factor": 1.0,
            "status": "a",
            "can_select": True,
        }
    for key, values in changes.items():
        players[int(key.lstrip("p"))].update(values)
    return players


def make_picks(captain=6, vice=7, order=None):
    order = order or list(range(1, 16))
    return [
        {"id": player_id, "position": slot, "is_captain": player_id == captain,
         "is_vice_captain": player_id == vice}
        for slot, player_id in enumerate(order, start=1)
    ]


def swap(picks, out_id, in_id):
    return [{**pick, "id": in_id} if pick["id"] == out_id else pick for pick in picks]


class PickTests(unittest.TestCase):
    def test_normalize_orders_starters_by_position_and_keeps_the_bench_order(self):
        players = make_players()
        picks = make_picks(order=[10, 2, 1, 3, 4, 5, 6, 7, 8, 9, 11, 15, 13, 12, 14])
        normalized = my_team.normalize_picks(picks, players)
        self.assertEqual([pick["id"] for pick in normalized],
                         [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 13, 14])
        self.assertEqual([pick["position"] for pick in normalized], list(range(1, 16)))

    def test_multipliers_follow_the_chip(self):
        picks = make_picks(captain=6)
        self.assertEqual(my_team.multipliers(picks)[6], 2)
        self.assertEqual(my_team.multipliers(picks)[13], 0)
        self.assertEqual(my_team.multipliers(picks, "3xc")[6], 3)
        boosted = my_team.multipliers(picks, "bboost")
        self.assertEqual((boosted[6], boosted[13], boosted[1]), (2, 1, 1))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.players = make_players()
        self.picks = make_picks()
        self.published = list(range(1, 16))

    def check(self, picks, **kwargs):
        return my_team.validate_squad(picks, self.players, published_ids=self.published, **kwargs)

    def test_a_legal_squad_passes(self):
        result = self.check(self.picks, bank_after=0.0)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["formation"], "4-4-2")

    def test_squad_rules(self):
        duplicate = swap(self.picks, 14, 8)
        self.assertTrue(any("picked 2 times" in error for error in self.check(duplicate)["errors"]))
        wrong_mix = swap(self.picks, 14, 104)  # a MID out, a FWD in
        self.assertTrue(any("2 GK, 5 DEF, 5 MID and 3 FWD" in error for error in self.check(wrong_mix)["errors"]))
        unknown = swap(self.picks, 14, 999)
        self.assertTrue(any("Unknown" in error for error in self.check(unknown)["errors"]))
        self.assertTrue(any("15 players" in error for error in self.check(self.picks[:14])["errors"]))

    def test_club_limit_is_an_error_unless_the_published_squad_was_over(self):
        for player_id in (2, 3, 4, 5):
            self.players[player_id]["team_name"] = "Club X"
        result = self.check(self.picks)
        self.assertTrue(any("Club X" in warning for warning in result["warnings"]))
        self.assertTrue(result["valid"])
        self.players[103]["team_name"] = "Club X"
        result = self.check(swap(self.picks, 14, 103))
        self.assertTrue(any("limit is 3" in error for error in result["errors"]))

    def test_an_over_limit_club_must_be_fixed_by_the_first_transfer(self):
        for player_id in (2, 3, 4, 5):
            self.players[player_id]["team_name"] = "Club X"
        # A transfer elsewhere keeps all four: FPL would not allow it.
        result = self.check(swap(self.picks, 14, 103))
        self.assertFalse(result["valid"])
        self.assertTrue(any("first transfer sell one" in error for error in result["errors"]))
        # Selling one of them resolves it.
        self.players[102]["team_name"] = "Club 102"
        self.assertTrue(self.check(swap(self.picks, 5, 102))["valid"])

    def test_lineup_and_armband_rules(self):
        two_keepers = make_picks(order=[1, 12, 3, 4, 5, 6, 7, 8, 9, 10, 11, 2, 13, 14, 15])
        self.assertTrue(any("one goalkeeper" in error for error in self.check(two_keepers)["errors"]))
        two_defenders = make_picks(order=[1, 2, 3, 14, 15, 6, 7, 8, 9, 10, 11, 12, 4, 5, 13])
        self.assertTrue(any("at least 3 DEF" in error for error in self.check(two_defenders)["errors"]))
        benched_captain = make_picks(captain=13)
        self.assertTrue(any("must be in the starting XI" in error for error in self.check(benched_captain)["errors"]))
        same = make_picks(captain=6, vice=6)
        self.assertTrue(any("different players" in error for error in self.check(same)["errors"]))
        none = make_picks(captain=None)
        self.assertTrue(any("exactly one captain" in error for error in self.check(none)["errors"]))

    def test_budget_buys_and_chips(self):
        self.assertTrue(any("Over budget by £0.3m" in error for error in self.check(self.picks, bank_after=-0.3)["errors"]))
        self.players[103].update(can_select=False)
        result = self.check(swap(self.picks, 14, 103))
        self.assertTrue(any("can't be bought" in error for error in result["errors"]))
        self.players[103].update(can_select=True, status="d", chance_of_playing_next_round=75, news="Knock")
        result = self.check(swap(self.picks, 14, 103))
        self.assertTrue(result["valid"])
        self.assertTrue(any("75% to play" in warning for warning in result["warnings"]))
        chips = {"3xc": {"played": [4], "available_this_half": False}}
        result = self.check(self.picks, chip="3xc", chip_status=chips)
        self.assertTrue(any("already played" in warning for warning in result["warnings"]))


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.players = make_players()
        self.published = make_picks()
        self.selling = {player_id: 4.8 for player_id in range(1, 16)}

    def changes(self, draft, **kwargs):
        options = dict(players=self.players, selling=self.selling, bank=1.0, free_transfers=1)
        options.update(kwargs)
        return my_team.squad_changes(self.published, draft, **options)

    def test_transfers_spend_the_bank_and_cost_hits_beyond_the_free_ones(self):
        one = swap(self.published, 14, 103)
        result = self.changes(one)
        self.assertEqual((result["count"], result["hits"], result["free_transfers_remaining"]), (1, 0, 0))
        self.assertEqual(result["bank_after"], round(1.0 + 4.8 - 6.0, 1))
        self.assertEqual(result["transfers"], [{
            "out": {"id": 14, "web_name": "P14", "team_name": "Club 14", "position": "MID", "sell": 4.8},
            "in": {"id": 103, "web_name": "S103", "team_name": "Club 103", "position": "MID", "buy": 6.0},
        }])
        two = swap(one, 15, 104)
        result = self.changes(two)
        self.assertEqual((result["count"], result["hits"], result["hit_cost"]), (2, 1, 4))
        self.assertIn("2 transfers (1 free, −4)", result["summary"])
        wildcard = self.changes(two, chip="wildcard")
        self.assertEqual((wildcard["hits"], wildcard["free_transfers_remaining"]), (0, 1))
        unlimited = self.changes(two, free_transfers=None)
        self.assertEqual((unlimited["hits"], unlimited["free_transfers_remaining"]), (0, None))
        self.assertEqual(self.changes(two, bank_override=2.5)["bank_after"], 2.5)

    def test_what_was_paid_and_transfers_made_through_others_count(self):
        one = swap(self.published, 14, 103)
        result = self.changes(one, paid={103: 5.7})
        self.assertEqual(result["bank_after"], round(1.0 + 4.8 - 5.7, 1))
        self.assertEqual(result["transfers"][0]["in"]["buy"], 5.7)
        # P14 -> someone -> S103 on FPL: two transfers, one squad change.
        result = self.changes(one, transfers_made=2)
        self.assertEqual((result["count"], result["net_count"], result["hits"]), (2, 1, 1))
        self.assertEqual(self.changes(one, transfers_made=0)["count"], 1)

    def test_lineup_bench_and_armband_changes_are_reported(self):
        same = self.changes(self.published)
        self.assertFalse(same["edited"])
        reordered = make_picks(order=list(range(1, 13)) + [15, 13, 14])
        result = self.changes(reordered)
        self.assertTrue(result["bench_reordered"])
        self.assertIn("bench reordered", result["summary"])
        promoted = make_picks(captain=9, order=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 14, 12, 13, 11, 15])
        result = self.changes(promoted)
        self.assertEqual((result["promoted"], result["benched"]), ([14], [11]))
        self.assertEqual(result["captain"], {"before": 6, "after": 9})
        self.assertIn("captain P9", result["summary"])


class OverrideTests(unittest.TestCase):
    def test_availability_and_xp_overrides(self):
        self.assertEqual(my_team.override_value(2.0, 0.5, {"availability": 100})[:2], (4.0, 1.0))
        self.assertEqual(my_team.override_value(4.0, 1.0, {"availability": 25})[:2], (1.0, 0.25))
        self.assertEqual(my_team.override_value(4.0, 1.0, {"xp_pct": 50})[:2], (2.0, 1.0))
        self.assertEqual(my_team.override_value(0.0, 0.0, {"availability": 100}, raw=5.0)[:2], (5.0, 1.0))
        value, factor, note = my_team.override_value(0.0, 0.0, {"availability": 100})
        self.assertEqual((value, factor), (0.0, 1.0))
        self.assertIn("could not be re-projected", note)
        self.assertTrue(my_team.needs_raw(0.0, {"availability": 80}))
        self.assertFalse(my_team.needs_raw(0.5, {"availability": 80}))
        self.assertFalse(my_team.needs_raw(0.0, {"availability": 0}))

    def test_records_and_model_points_scale_together(self):
        records = [
            {"id": 1, "web_name": "A", "expected_points": 2.0, "availability_factor": 0.5},
            {"id": 2, "web_name": "B", "expected_points": 0.0, "availability_factor": 0.0},
            {"id": 3, "web_name": "C", "expected_points": 3.0, "availability_factor": 1.0},
        ]
        overrides = {1: {"availability": 100}, 2: {"availability": 100}, 3: {"xp_pct": 0}}
        models = {1: {"a": 1.0, "b": 3.0}, 2: {"a": 0.0}, 3: {"a": 3.0}}
        adjusted, scaled, notes = my_team.apply_overrides(records, overrides, {2: 4.0}, models)
        by_id = {record["id"]: record for record in adjusted}
        self.assertEqual(by_id[1]["expected_points"], 4.0)
        self.assertEqual(by_id[1]["base_expected_points"], 2.0)
        self.assertEqual(by_id[2]["expected_points"], 4.0)
        self.assertEqual(by_id[3]["expected_points"], 0.0)
        self.assertEqual(scaled[1], {"a": 2.0, "b": 6.0})
        self.assertEqual(scaled[2], {"a": 4.0})
        self.assertEqual(notes, [])

    def test_horizon_frames_use_the_raw_lookup_for_players_ruled_out(self):
        points = pd.DataFrame({11: [2.0, 0.0], 12: [4.0, 0.0]}, index=[1, 2])
        factors = pd.DataFrame({11: [0.5, 0.0], 12: [1.0, 0.0]}, index=[1, 2])
        calls = []

        def lookup(gameweek, ids):
            calls.append((gameweek, list(ids)))
            return {2: 3.0 + gameweek - 11}

        result, notes = my_team.apply_overrides_frame(
            points, factors, {1: {"availability": 100}, 2: {"availability": 50}}, lookup
        )
        self.assertEqual(result.loc[1].tolist(), [4.0, 4.0])
        self.assertEqual(result.loc[2].tolist(), [1.5, 2.0])
        self.assertEqual(calls, [(11, [2]), (12, [2])])
        self.assertEqual(notes, [])


class AutosubTests(unittest.TestCase):
    def test_everyone_fit_is_the_xi_plus_the_captain(self):
        players = make_players()
        picks = make_picks(captain=6)
        result = my_team.expected_with_autosubs(players, picks)
        expected = sum(players[pid]["expected_points"] for pid in range(1, 12)) + players[6]["expected_points"]
        self.assertAlmostEqual(result["expected_points"], round(expected, 2))
        self.assertEqual(result["autosub_value"], 0.0)
        self.assertEqual(result["method"], "exact")
        self.assertEqual(result["scenarios"], 1)

    def test_a_captain_ruled_out_passes_the_armband_and_a_sub_comes_on(self):
        players = make_players(p6={"availability_factor": 0.0, "expected_points": 0.0})
        picks = make_picks(captain=6, vice=7)
        result = my_team.expected_with_autosubs(players, picks)
        # P6 (MID) misses: the first sub, P13 (DEF), keeps 5-3-2 legal.
        xi = [pid for pid in range(1, 12) if pid != 6] + [13]
        expected = sum(players[pid]["expected_points"] for pid in xi) + players[7]["expected_points"]
        self.assertAlmostEqual(result["expected_points"], round(expected, 2))
        self.assertEqual(result["bench_use"][13], 1.0)
        self.assertEqual(result["vice_armband"], 1.0)

    def test_only_a_keeper_replaces_the_keeper(self):
        players = make_players(p1={"availability_factor": 0.0, "expected_points": 0.0})
        result = my_team.expected_with_autosubs(players, make_picks())
        self.assertEqual(result["bench_use"], {12: 1.0, 13: 0.0, 14: 0.0, 15: 0.0})

    def test_a_sub_that_breaks_the_formation_is_skipped(self):
        # 3-4-3: P2-P4 defend; P2 misses; bench order MID (13), DEF (14).
        layout = [1, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 1, 3, 2, 3]
        players = {
            index: {"id": index, "web_name": f"P{index}", "element_type": position,
                    "expected_points": 3.0, "availability_factor": 1.0}
            for index, position in enumerate(layout, start=1)
        }
        players[2].update(availability_factor=0.0, expected_points=0.0)
        result = my_team.expected_with_autosubs(players, make_picks(captain=5, vice=6))
        self.assertEqual(result["bench_use"][13], 0.0)
        self.assertEqual(result["bench_use"][14], 1.0)

    def test_bench_boost_counts_everyone_without_subs(self):
        players = make_players(p6={"availability_factor": 0.0, "expected_points": 0.0})
        result = my_team.expected_with_autosubs(players, make_picks(captain=7, vice=8), "bboost")
        total = sum(player["expected_points"] for pid, player in players.items() if pid <= 15)
        self.assertAlmostEqual(result["expected_points"], round(total + players[7]["expected_points"], 2))
        self.assertEqual(sum(result["bench_use"].values()), 0)

    def test_a_doubtful_captain_is_averaged_exactly(self):
        players = make_players(p6={"availability_factor": 0.5, "expected_points": 5.0})
        result = my_team.expected_with_autosubs(players, make_picks(captain=6, vice=7))
        others = sum(players[pid]["expected_points"] for pid in range(1, 12) if pid != 6)
        plays = others + 10.0 * 2  # he scores 10 when he plays, doubled
        misses = others + players[13]["expected_points"] + players[7]["expected_points"]
        self.assertAlmostEqual(result["expected_points"], round(0.5 * plays + 0.5 * misses, 2))
        self.assertEqual(result["scenarios"], 2)

    def test_sampling_agrees_with_the_exact_answer(self):
        changes = {f"p{pid}": {"availability_factor": 0.6} for pid in (3, 6, 9, 10, 13)}
        players = make_players(**changes)
        picks = make_picks()
        exact = my_team.expected_with_autosubs(players, picks)
        sampled = my_team.expected_with_autosubs(players, picks, exact_limit=0, samples=20000)
        self.assertEqual(sampled["method"], "sampled")
        self.assertAlmostEqual(exact["expected_points"], sampled["expected_points"], delta=0.25)


class AdviceTests(unittest.TestCase):
    def test_the_bench_order_puts_the_best_eligible_sub_first(self):
        # A MID starter is out: any outfield sub keeps the formation legal.
        players = make_players(
            p6={"availability_factor": 0.0, "expected_points": 0.0},
            p13={"expected_points": 1.0}, p14={"expected_points": 2.0}, p15={"expected_points": 3.0},
        )
        picks = make_picks(captain=7, vice=8)
        order, result = my_team.best_bench_order(players, list(range(1, 12)), [12, 13, 14, 15], 7, 8)
        self.assertEqual(order, [12, 15, 14, 13])  # then the higher projection
        brute = max(
            my_team.expected_with_autosubs(
                players, my_team._picks_from(list(range(1, 12)), [12, *perm], 7, 8, players)
            )["expected_points"]
            for perm in itertools.permutations([13, 14, 15])
        )
        self.assertEqual(result["expected_points"], brute)
        advice = my_team.lineup_advice(players, picks)
        self.assertGreater(advice["gain"], 0)
        self.assertNotIn(6, advice["starting_ids"])

    def test_the_armband_pair_matches_brute_force(self):
        players = make_players(p6={"availability_factor": 0.5, "expected_points": 6.0},
                               p7={"expected_points": 5.5}, p10={"expected_points": 5.0})
        starters = list(range(1, 12))
        captain, vice = my_team.best_armband(players, starters)
        best = max(
            ((my_team._armband_value(players, c, v), c, v) for c, v in itertools.permutations(starters, 2)),
            key=lambda item: item[0],
        )
        self.assertAlmostEqual(my_team._armband_value(players, captain, vice), best[0])
        self.assertEqual(captain, 6)  # 12 points if he plays, half the time

    def test_advice_is_legal_and_never_worse_than_the_current_lineup(self):
        rng = random.Random(3)
        for _ in range(25):
            players = make_players()
            for pid in range(1, 16):
                players[pid]["expected_points"] = round(rng.uniform(0, 8), 2)
                players[pid]["availability_factor"] = rng.choice([1.0, 1.0, 1.0, 0.75, 0.5, 0.0])
                if players[pid]["availability_factor"] == 0.0:
                    players[pid]["expected_points"] = 0.0
            picks = make_picks(captain=rng.randint(1, 11), vice=None)
            picks[0 if picks[0]["is_captain"] is False else 1]["is_vice_captain"] = True
            advice = my_team.lineup_advice(players, picks)
            self.assertGreaterEqual(advice["gain"], -1e-9)
            check = my_team.validate_squad(advice["picks"], players, published_ids=range(1, 16))
            self.assertTrue(check["valid"], check)
            self.assertTrue(advice["reasons"])

    def test_captaincy_options_rank_by_armband_value_with_risk_and_ownership(self):
        players = make_players(p6={"expected_points": 9.0, "selected_by_percent": 45.0},
                               p7={"expected_points": 8.0, "availability_factor": 0.5, "selected_by_percent": 3.0})
        models = {6: {"a": 8.0, "b": 10.0}, 7: {"a": 8.0, "b": 8.0}}
        options = my_team.captaincy_options(players, make_picks(captain=6, vice=8), models, limit=3)
        self.assertEqual(options[0]["id"], 7)  # 16 if he plays, else the vice: 0.5*16 + 0.5*P8
        top = {option["id"]: option for option in options}
        self.assertEqual(top[6]["ownership_tag"], "template")
        self.assertEqual(top[6]["model_spread"], 1.0)
        self.assertEqual(top[6]["risk"], "low")
        self.assertEqual(top[7]["risk"], "minutes")
        self.assertEqual(top[7]["ownership_tag"], "differential")
        self.assertTrue(top[6]["current_captain"])
        self.assertEqual(len(options), 3)


class PriceTests(unittest.TestCase):
    def test_price_pressure_flags_heavy_net_transfers(self):
        rising = my_team.price_pressure(
            {"transfers_in_event": 60000, "transfers_out_event": 10000, "selected_by_percent": 10.0,
             "cost_change_event": 1, "cost_change_start": 3},
            total_players=10_000_000,
        )
        self.assertEqual(rising["net_transfers"], 50000)
        self.assertEqual(rising["owners"], 1_000_000)
        self.assertEqual(rising["direction"], "rise likely")
        self.assertEqual((rising["change_this_gameweek"], rising["change_since_start"]), (0.1, 0.3))
        falling = my_team.price_pressure(
            {"transfers_in_event": 0, "transfers_out_event": 50000, "selected_by_percent": 10.0}, 10_000_000
        )
        self.assertEqual(falling["direction"], "fall likely")
        steady = my_team.price_pressure({"transfers_in_event": 100, "selected_by_percent": 10.0}, 10_000_000)
        self.assertEqual(steady["direction"], "steady")
        self.assertEqual(my_team.price_pressure({}, None)["direction"], "unknown")

    def test_selling_effect_keeps_half_of_each_rise(self):
        effect = my_team.selling_effect(5.0, 5.3)
        self.assertEqual(effect, {"now": 5.1, "after_rise": 5.2, "after_fall": 5.1})



def squad_rows(players):
    return [{**players[pid], "team_id": pid, "pick_position": pid} for pid in range(1, 16)]


class OutlookTests(unittest.TestCase):
    def test_chip_values_per_gameweek(self):
        players = make_players()
        rows = squad_rows(players)
        points = pd.DataFrame(
            {11: [float(pid) for pid in range(1, 16)], 12: [1.0] * 15}, index=list(range(1, 16))
        )
        chips = [{"name": "3xc", "event": 5}]
        result = my_team.chip_values(
            rows, points, [11, 12], chips_played=chips,
            fixture_counts={15: {11: 2}, 14: {12: 0}},
            free_hit_total=lambda gameweek: {11: 200.0, 12: 5.0}[gameweek],
        )
        week = result["gameweeks"][0]
        # Best XI: GK 12, DEF 5,4,3,13 (or similar), etc. Check against best_lineup.
        records = [{"id": pid, "element_type": players[pid]["element_type"], "expected_points": float(pid)} for pid in range(1, 16)]
        from src.team_rating import best_lineup
        lineup, total = best_lineup(records)
        bench = sum(float(pid) for pid in range(1, 16)) - total
        self.assertEqual(week["bboost"], round(bench, 2))
        self.assertEqual(week["3xc"], 15.0)
        self.assertEqual(week["base"], round(total + 15.0, 2))
        self.assertEqual(week["freehit"], round(200.0 - total - 15.0, 2))
        self.assertEqual((week["doubles"], week["blanks"]), (1, 0))
        self.assertEqual(result["gameweeks"][1]["blanks"], 1)
        self.assertEqual(result["gameweeks"][1]["freehit"], 0.0)  # never below zero
        self.assertFalse(week["available"]["3xc"])  # played in GW5, same half
        self.assertIsNone(result["best"]["3xc"])
        self.assertEqual(result["best"]["bboost"], 11)

    def test_fixture_ticker_reads_each_side_of_a_fixture(self):
        players = make_players()
        rows = squad_rows(players)[:2]
        points = pd.DataFrame({11: [2.0, 3.0], 12: [1.0, 0.0]}, index=[1, 2])
        fixtures = {
            1: {11: [{"opponent_short": "B", "home": True, "difficulty": 2}],
                12: [{"opponent_short": "C", "home": False, "difficulty": 4},
                     {"opponent_short": "D", "home": True, "difficulty": 3}]},
        }
        ticker = my_team.fixture_ticker(rows, points, [11, 12], fixtures)
        first, second = ticker["rows"]
        self.assertEqual(first["cells"][0]["fixtures"], [{"opponent": "B", "home": True, "difficulty": 2}])
        self.assertEqual(len(first["cells"][1]["fixtures"]), 2)
        self.assertEqual(first["total"], 3.0)
        self.assertEqual(first["difficulty"], 3.0)
        self.assertEqual(second["cells"][1]["fixtures"], [])  # a blank
        self.assertIsNone(second["difficulty"])
        self.assertEqual(ticker["gameweeks"], [11, 12])
        self.assertEqual(ticker["totals"][0]["best_xi"], None)  # two players field no XI
        full = my_team.fixture_ticker(squad_rows(players), pd.DataFrame({11: [1.0] * 15}, index=range(1, 16)), [11], {})
        self.assertEqual(full["totals"][0]["best_xi"], 12.0)


def review_payload(captain=6, vice=7, chip=None, subs=(), points=None):
    return {
        "gameweek": 11,
        "active_chip": chip,
        "automatic_subs": [{"element_in": a, "element_out": b} for a, b in subs],
        "entry_history": {"points": points, "points_on_bench": 4, "event_transfers_cost": 4},
        "picks": [
            {"element": pid, "position": pid, "is_captain": pid == captain, "is_vice_captain": pid == vice}
            for pid in range(1, 16)
        ],
    }


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.players = make_players()
        self.actuals = {pid: {"points": 2.0, "minutes": 90.0} for pid in range(1, 16)}
        self.actuals[103] = {"points": 9.0, "minutes": 90.0}
        self.actuals[8] = {"points": 12.0, "minutes": 90.0}
        self.actuals[6] = {"points": 0.0, "minutes": 0.0}

    def test_captain_bench_autosubs_and_hindsight(self):
        review = my_team.gameweek_review(
            review_payload(captain=6, vice=7, subs=[(13, 6)], points=24),
            self.players, self.actuals,
        )
        # P6 missed: P13 came on, P7 wore the armband. 10 starters at 2 + P8 12 + P13 2 + vice 2.
        self.assertEqual(review["captain"]["armband_id"], 7)
        self.assertEqual(review["points"]["computed"], 2 * 9 + 12 + 2 + 2)
        self.assertFalse(review["points"]["matches"])  # the official 24 disagrees
        self.assertEqual(review["captain"]["best_id"], 8)
        self.assertEqual(review["captain"]["cost"], 10.0)
        self.assertEqual(review["bench"]["computed"], 0.0 + 2 + 2 + 2)
        self.assertEqual(review["autosubs"][0]["in_id"], 13)
        self.assertGreaterEqual(review["hindsight"]["left_on_table"], 0)
        self.assertEqual(review["hindsight"]["captain_id"], 8)

    def test_transfers_are_judged_on_the_week_and_since(self):
        transfers = [{"element_in": 103, "element_out": 14, "element_in_cost": 6.0, "element_out_cost": 5.0}]
        review = my_team.gameweek_review(
            review_payload(), self.players, self.actuals, transfers=transfers,
            since={103: 20.0, 14: 30.0},
        )
        item = review["transfers"]["items"][0]
        self.assertEqual((item["in_points"], item["out_points"], item["delta"]), (9.0, 2.0, 7.0))
        self.assertEqual(review["transfers"]["net"], 3.0)  # 7 gained, 4 for the hit
        self.assertEqual(review["transfers"]["since_net"], -14.0)
        self.assertEqual(review["transfers"]["verdict"], "not yet")

    def test_negative_scores_keep_their_sign_in_hindsight(self):
        self.actuals[1] = {"points": -2.0, "minutes": 90.0}
        self.actuals[12] = {"points": -1.0, "minutes": 90.0}
        review = my_team.gameweek_review(review_payload(), self.players, self.actuals)
        hindsight = review["hindsight"]
        self.assertIn(12, hindsight["starting_ids"])
        self.assertNotIn(1, hindsight["starting_ids"])

    def test_forecast_comparison_and_chips(self):
        forecast = {pid: 3.0 for pid in range(1, 16)}
        review = my_team.gameweek_review(
            review_payload(captain=8, vice=7, chip="3xc"), self.players, self.actuals,
            forecast=forecast, finished=False,
        )
        self.assertTrue(review["provisional"])
        self.assertEqual(review["captain"]["multiplier"], 3)
        self.assertEqual(review["points"]["computed"], 2 * 9 + 0 + 12 * 3)
        self.assertEqual(review["forecast"]["xi_expected"], 11 * 3.0 + 2 * 3.0)
        self.assertEqual(review["forecast"]["over"][0]["id"], 8)
        self.assertEqual(review["forecast"]["under"][0]["id"], 6)
        boosted = my_team.gameweek_review(review_payload(captain=8, chip="bboost"), self.players, self.actuals)
        self.assertEqual(boosted["points"]["computed"], 2 * 13 + 0 + 12 * 2)
        self.assertEqual(boosted["bench"]["computed"], 0.0)


if __name__ == "__main__":
    unittest.main()
