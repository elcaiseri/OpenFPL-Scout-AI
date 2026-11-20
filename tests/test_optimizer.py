"""Transfer, Wildcard and Free Hit planning for the owner dashboard."""

import itertools
import unittest
from collections import Counter

import numpy as np
import pandas as pd

from src import optimizer
from src.team_rating import best_lineup


def random_pool(seed, counts=None, clubs=9, gameweeks=(11,)):
    rng = np.random.default_rng(seed)
    counts = counts or {1: 3, 2: 6, 3: 6, 4: 4}
    rows = []
    player_id = 1
    for position, count in counts.items():
        for _ in range(count):
            row = {
                "id": player_id,
                "element_type": position,
                # Spread evenly, so club limits never rule out every squad.
                "team_name": f"Club {(player_id * 7) % clubs}",
                "web_name": f"P{player_id}",
                "price": float(rng.integers(40, 80)) / 10,
                "buyable": True,
            }
            row["cost"] = row["price"]
            for gameweek in gameweeks:
                row[gameweek] = float(np.round(rng.gamma(2.0, 1.5), 3))
            rows.append(row)
            player_id += 1
    return pd.DataFrame(rows)


def objective(squad, gameweeks, bench_weight=0.1):
    """Best XI plus captain per gameweek, plus a little for the bench."""
    total = 0.0
    for gameweek in gameweeks:
        records = [
            {"element_type": int(position), "expected_points": float(points)}
            for position, points in zip(squad["element_type"], squad[gameweek])
        ]
        lineup, starting = best_lineup(records)
        captain = max(player["expected_points"] for player in lineup)
        total += starting + captain + bench_weight * (float(squad[gameweek].sum()) - starting)
    return total


def cheap_squad(pool, max_per_club=3):
    """A legal 15: the cheapest players per position within the club limit."""
    chosen, clubs = [], Counter()
    for position, quota in optimizer.SQUAD_QUOTAS.items():
        taken = 0
        for _, player in pool.loc[pool["element_type"] == position].sort_values("cost").iterrows():
            if taken == quota or clubs[player["team_name"]] >= max_per_club:
                continue
            chosen.append(int(player["id"]))
            clubs[player["team_name"]] += 1
            taken += 1
    return sorted(chosen)


def legal_squads(pool, budget, max_per_club=3):
    groups = {position: pool.index[pool["element_type"] == position] for position in optimizer.SQUAD_QUOTAS}
    for combination in itertools.product(
        *(itertools.combinations(groups[position], quota) for position, quota in optimizer.SQUAD_QUOTAS.items())
    ):
        squad = pool.loc[[index for group in combination for index in group]]
        if squad["cost"].sum() > budget + 1e-9:
            continue
        if squad["team_name"].value_counts().max() > max_per_club:
            continue
        yield squad


class SolverTests(unittest.TestCase):
    def test_wildcard_matches_brute_force_over_every_legal_squad(self):
        for seed, gameweeks in ((1, (11,)), (2, (11, 12)), (3, (11, 12, 13))):
            pool = random_pool(seed, gameweeks=gameweeks)
            budget = 100.0
            result = optimizer.solve_squad(pool, gameweeks, budget=budget)
            best = max(legal_squads(pool, budget), key=lambda squad: objective(squad, gameweeks))
            self.assertEqual(sorted(result["squad_ids"]), sorted(best["id"]), seed)
            self.assertTrue(result["optimal"])

    def test_transfers_mode_matches_brute_force_with_hits(self):
        pool = random_pool(4)
        gameweeks = (11,)
        budget = 100.0
        current = cheap_squad(pool)
        for count, free in ((1, 1), (2, 1), (3, 0)):
            result = optimizer.solve_squad(
                pool, gameweeks, budget=budget, current_ids=current,
                transfers_exact=count, free_transfers=free,
            )
            candidates = [
                squad
                for squad in legal_squads(pool, budget)
                if len(set(squad["id"]) - set(current)) == count
            ]
            best = max(candidates, key=lambda squad: objective(squad, gameweeks))
            self.assertEqual(sorted(result["squad_ids"]), sorted(best["id"]), count)
            self.assertEqual(result["transfers"], count)
            self.assertEqual(result["hits"], max(0, count - free))
            self.assertEqual(result["hit_cost"], 4.0 * max(0, count - free))

    def test_constraints_hold_and_unbuyable_players_are_only_kept(self):
        pool = random_pool(5, counts={1: 4, 2: 9, 3: 9, 4: 6})
        budget = 90.0
        star = int(pool.loc[pool[11].idxmax(), "id"])
        pool.loc[pool["id"] == star, "buyable"] = False
        result = optimizer.solve_squad(pool, (11,), budget=budget)
        squad = pool.loc[pool["id"].isin(result["squad_ids"])]
        self.assertEqual(Counter(squad["element_type"]), Counter({1: 2, 2: 5, 3: 5, 4: 3}))
        self.assertLessEqual(squad["team_name"].value_counts().max(), 3)
        self.assertLessEqual(squad["cost"].sum(), budget + 1e-9)
        self.assertNotIn(star, result["squad_ids"])
        lineup = result["lineups"][0]
        self.assertEqual(len(lineup["starting_ids"]), 11)
        self.assertIn(lineup["captain_id"], lineup["starting_ids"])

        current = result["squad_ids"][:-1] + [star]
        position = int(pool.loc[pool["id"] == result["squad_ids"][-1], "element_type"].iloc[0])
        if int(pool.loc[pool["id"] == star, "element_type"].iloc[0]) == position:
            kept = optimizer.solve_squad(
                pool, (11,), budget=200.0, current_ids=current, transfers_exact=0
            )
            self.assertIn(star, kept["squad_ids"])

    def test_locks_and_bans_match_brute_force_under_the_same_rules(self):
        pool = random_pool(8)
        gameweeks = (11,)
        budget = 100.0
        free_pick = optimizer.solve_squad(pool, gameweeks, budget=budget)
        banned = sorted(free_pick["squad_ids"], key=lambda pid: -float(pool.loc[pool["id"] == pid, 11].iloc[0]))[:2]
        outside = [int(pid) for pid in pool["id"] if int(pid) not in free_pick["squad_ids"]]
        locked = [outside[0]]
        result = optimizer.solve_squad(
            pool, gameweeks, budget=budget, locked_ids=locked, banned_ids=banned
        )
        candidates = [
            squad
            for squad in legal_squads(pool, budget)
            if set(locked) <= set(squad["id"]) and not set(banned) & set(squad["id"])
        ]
        best = max(candidates, key=lambda squad: objective(squad, gameweeks))
        self.assertEqual(sorted(result["squad_ids"]), sorted(best["id"]))
        self.assertIn(locked[0], result["squad_ids"])
        self.assertFalse(set(banned) & set(result["squad_ids"]))

    def test_a_lock_that_cannot_be_bought_is_infeasible(self):
        pool = random_pool(9)
        target = int(pool["id"].iloc[0])
        pool.loc[pool["id"] == target, "buyable"] = False
        self.assertIsNone(optimizer.solve_squad(pool, (11,), budget=100.0, locked_ids=[target]))
        self.assertIsNone(optimizer.solve_squad(pool, (11,), budget=100.0, locked_ids=[999]))

    def test_no_legal_squad_returns_none(self):
        pool = random_pool(6, clubs=3)  # three clubs cannot hold 15 players at 3 each
        self.assertIsNone(optimizer.solve_squad(pool, (11,), budget=200.0))


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.gameweeks = (11, 12)
        self.pool = random_pool(7, counts={1: 4, 2: 10, 3: 10, 4: 6}, gameweeks=self.gameweeks)
        self.current = cheap_squad(self.pool)

    def test_transfers_mode_lists_every_option_and_marks_the_best_net(self):
        result = optimizer.plan(
            self.pool, self.gameweeks, mode="transfers", current_ids=self.current,
            bank=15.0, free_transfers=1, max_transfers=3,
        )
        plans = result["plans"]
        self.assertEqual([plan["transfers"] for plan in plans], [0, 1, 2, 3])
        self.assertEqual(sorted(plans[0]["squad_ids"]), self.current)
        self.assertEqual(plans[0]["net_gain"], 0.0)
        self.assertEqual([plan["hits"] for plan in plans], [0, 0, 1, 2])
        best = plans[result["best_index"]]
        self.assertEqual(best["net_total"], max(plan["net_total"] for plan in plans))
        for plan in plans[1:]:
            self.assertEqual(len(plan["moves"]), plan["transfers"])
            for move in plan["moves"]:
                self.assertEqual(move["out"]["position"], move["in"]["position"])
                self.assertIn(move["out"]["id"], self.current)
                self.assertNotIn(move["in"]["id"], self.current)

    def test_chips_replace_the_squad_within_the_budget(self):
        result = optimizer.plan(
            self.pool, self.gameweeks, mode="wildcard", current_ids=self.current, bank=2.0
        )
        plan = result["plans"][0]
        self.assertEqual(len(result["plans"]), 1)
        self.assertEqual(plan["hits"], 0)
        self.assertGreaterEqual(plan["projected_total"], result["baseline"]["projected_total"])
        self.assertLessEqual(plan["spent"], result["budget"] + 1e-9)
        self.assertAlmostEqual(result["budget"], 2.0 + self.pool.loc[self.pool["id"].isin(self.current), "cost"].sum(), places=6)
        self.assertEqual(len(plan["lineups"]), 2)
        self.assertEqual(sum(1 for player in plan["squad"] if player["new"]), plan["transfers"])

    def test_locks_and_bans_bind_every_plan_but_the_roll(self):
        sold = self.current[5]
        outsiders = [int(pid) for pid in self.pool["id"] if int(pid) not in self.current]
        wanted = outsiders[-1]
        result = optimizer.plan(
            self.pool, self.gameweeks, mode="transfers", current_ids=self.current,
            bank=15.0, free_transfers=1, max_transfers=3,
            locked_ids=[wanted], banned_ids=[sold],
        )
        self.assertEqual(result["locked_ids"], [wanted])
        self.assertEqual(sorted(result["plans"][0]["squad_ids"]), self.current)
        self.assertFalse(result["plans"][1]["feasible"])  # one transfer cannot both sell and buy
        self.assertIn("locks and bans", result["plans"][1]["reason"])
        for plan in result["plans"][2:]:
            if plan["feasible"]:
                self.assertIn(wanted, plan["squad_ids"])
                self.assertNotIn(sold, plan["squad_ids"])
        wildcard = optimizer.plan(
            self.pool, self.gameweeks, mode="wildcard", current_ids=self.current,
            bank=15.0, locked_ids=[wanted], banned_ids=[sold],
        )
        self.assertIn(wanted, wildcard["plans"][0]["squad_ids"])
        with self.assertRaises(ValueError):
            optimizer.plan(
                self.pool, self.gameweeks, mode="wildcard", current_ids=self.current,
                bank=15.0, locked_ids=[wanted], banned_ids=[wanted],
            )

    def test_unknown_mode_and_incomplete_squad_are_rejected(self):
        with self.assertRaises(ValueError):
            optimizer.plan(self.pool, self.gameweeks, mode="bench", current_ids=self.current, bank=0)
        with self.assertRaises(ValueError):
            optimizer.plan(self.pool, self.gameweeks, mode="freehit", current_ids=self.current[:14], bank=0)


class EstimateTests(unittest.TestCase):
    def history(self, *rows):
        return [
            {"event": event, "event_transfers": made, "event_transfers_cost": cost}
            for event, made, cost in rows
        ]

    def test_free_transfers_roll_up_to_five(self):
        rows = self.history(*((event, 0, 0) for event in range(1, 9)))
        estimate = optimizer.estimate_free_transfers(rows, [], 1, 9)
        self.assertEqual(estimate["free_transfers"], 5)
        self.assertEqual([step["available_next"] for step in estimate["trail"]], [1, 2, 3, 4, 5, 5, 5, 5])

    def test_hits_do_not_use_free_transfers(self):
        rows = self.history((1, 0, 0), (2, 0, 0), (3, 3, 4), (4, 1, 0))
        estimate = optimizer.estimate_free_transfers(rows, [], 1, 5)
        # GW2: 2 banked; GW3: 3 transfers, 1 paid -> 2 free used -> 0 + 1;
        # GW4: 1 used -> 0 + 1.
        self.assertEqual(estimate["free_transfers"], 1)
        self.assertEqual(estimate["trail"][2]["hits"], 1)

    def test_wildcard_and_free_hit_keep_banked_transfers(self):
        rows = self.history((1, 0, 0), (2, 0, 0), (3, 11, 0), (4, 0, 0), (5, 9, 0))
        chips = [{"name": "wildcard", "event": 3}, {"name": "freehit", "event": 5}]
        estimate = optimizer.estimate_free_transfers(rows, chips, 1, 6)
        self.assertEqual(estimate["free_transfers"], 5)

    def test_late_starters_and_the_first_deadline(self):
        self.assertTrue(optimizer.estimate_free_transfers([], [], 4, 4)["unlimited"])
        estimate = optimizer.estimate_free_transfers(self.history((4, 0, 0), (5, 1, 0)), [], 4, 6)
        self.assertEqual(estimate["free_transfers"], 1)

    def test_selling_price_keeps_half_the_rise(self):
        self.assertEqual(optimizer.selling_price(5.0, 5.3), 5.1)
        self.assertEqual(optimizer.selling_price(5.0, 5.4), 5.2)
        self.assertEqual(optimizer.selling_price(5.0, 5.1), 5.0)
        self.assertEqual(optimizer.selling_price(5.0, 4.7), 4.7)
        self.assertEqual(optimizer.selling_price(None, 6.0), 6.0)

    def test_purchase_prices_skip_free_hit_weeks(self):
        transfers = [
            {"element_in": 10, "element_in_cost": 6.0, "event": 3, "time": "a"},
            {"element_in": 10, "element_in_cost": 7.0, "event": 6, "time": "b"},
            {"element_in": 11, "element_in_cost": 5.5, "event": 4, "time": "c"},
        ]
        chips = [{"name": "freehit", "event": 6}]
        prices = optimizer.purchase_prices([10, 11, 12, 13], transfers, chips, 1, {12: 4.5})
        self.assertEqual(prices[10]["price"], 6.0)
        self.assertEqual(prices[11]["source"], "bought for GW4")
        self.assertEqual(prices[12]["price"], 4.5)
        self.assertIsNone(prices[13]["price"])

    def test_chip_status_is_per_half_season(self):
        status = optimizer.chip_status([{"name": "wildcard", "event": 3}], 11)["chips"]
        self.assertFalse(status["wildcard"]["available_this_half"])
        self.assertTrue(status["freehit"]["available_this_half"])
        later = optimizer.chip_status([{"name": "wildcard", "event": 3}], 24)["chips"]
        self.assertTrue(later["wildcard"]["available_this_half"])


class HorizonTests(unittest.TestCase):
    def test_doubtful_players_count_as_fit_after_the_first_gameweek(self):
        def predict(gameweek):
            if gameweek == 13:
                raise ValueError("No fixtures are published for gameweek 13")
            return pd.DataFrame(
                {
                    "id": [1, 2, 3],
                    "web_name": ["A", "B", "C"],
                    "team_name": ["X", "Y", "Z"],
                    "element_type": [2, 3, 4],
                    "expected_points": [2.0, 1.5, 0.0],
                    "availability_factor": [1.0, 0.5, 0.0],
                }
            )

        points, identity, notes = optimizer.horizon_points(predict, [11, 12, 13])
        self.assertEqual(points.loc[2, 11], 1.5)  # this gameweek keeps the discount
        self.assertEqual(points.loc[2, 12], 3.0)  # later ones count him as fit
        self.assertEqual(points.loc[3, 12], 0.0)  # ruled out stays out
        self.assertEqual(points.loc[1, 13], 0.0)
        self.assertTrue(any("GW13" in note for note in notes))
        self.assertEqual(identity.loc[1, "web_name"], "A")
        factors = points.attrs["factors"]
        self.assertEqual(factors.loc[2, 11], 0.5)  # the discount the points include
        self.assertEqual(factors.loc[2, 12], 1.0)
        self.assertEqual(factors.loc[3, 12], 0.0)


if __name__ == "__main__":
    unittest.main()
