import unittest

from src.transfers import clubs_over_limit, lineup_value, suggest_transfers

POSITIONS = [1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 4, 4, 4]


def squad(**changes):
    """A 15-player squad from 15 clubs; each player 5.0m and 4 xPts."""
    players = [
        {
            "id": index,
            "web_name": f"Player {index}",
            "team_name": f"Club {index}",
            "element_type": position,
            "price": 5.0,
            "expected_points": 4.0,
            "availability_factor": 1.0,
        }
        for index, position in enumerate(POSITIONS, start=1)
    ]
    for player_id, values in changes.items():
        players[int(player_id.lstrip("p")) - 1].update(values)
    return players


def candidate(player_id, position, xpts, price, club="Club 99", **extra):
    return {
        "id": player_id,
        "web_name": f"Candidate {player_id}",
        "team_name": club,
        "element_type": position,
        "price": price,
        "expected_points": xpts,
        "availability_factor": 1.0,
        **extra,
    }


class TransferSuggestionTests(unittest.TestCase):
    def test_the_best_affordable_upgrade_is_suggested_with_its_gain(self):
        players = squad(p3={"expected_points": 2.0})
        pool = [
            candidate(100, 2, 9.0, 9.0),  # better, but 4.0m over budget
            candidate(101, 2, 6.0, 5.5),
        ]

        [suggestion] = suggest_transfers(players, pool, bank=0.5)

        self.assertEqual(suggestion["out"]["id"], 3)
        self.assertEqual(suggestion["in"]["id"], 101)
        self.assertEqual(suggestion["cost"], 0.5)
        changed = [pool[1] if player["id"] == 3 else player for player in players]
        self.assertAlmostEqual(
            suggestion["gain"], round(lineup_value(changed) - lineup_value(players), 2)
        )
        # +2 in the XI, and +2 more as the new captain.
        self.assertAlmostEqual(suggestion["gain"], 4.0)

    def test_a_fourth_player_from_one_club_is_never_suggested(self):
        players = squad(
            p3={"team_name": "Arsenal"},
            p4={"team_name": "Arsenal"},
            p5={"team_name": "Arsenal", "expected_points": 1.0},
            p8={"expected_points": 1.0},
        )
        pool = [
            candidate(100, 3, 8.0, 5.0, club="Arsenal"),
            candidate(101, 2, 8.0, 5.0, club="Arsenal"),
        ]

        suggestions = suggest_transfers(players, pool, bank=0.0)

        # The defender may replace an Arsenal defender, and replaces the one
        # projected lowest; the midfielder may not join a squad that already
        # has three Arsenal players.
        self.assertEqual(
            [(item["out"]["id"], item["in"]["id"]) for item in suggestions], [(5, 101)]
        )

    def test_unavailable_or_unpriced_players_are_not_suggested(self):
        players = squad(p13={"expected_points": 1.0})
        pool = [
            candidate(100, 4, 9.0, 5.0, availability_factor=0.0),
            candidate(101, 4, 9.0, 5.0, status="i", availability_factor=None),
            candidate(102, 4, 9.0, None),
        ]

        self.assertEqual(suggest_transfers(players, pool, bank=5.0), [])

    def test_nothing_is_suggested_without_a_bank(self):
        players = squad(p13={"expected_points": 1.0})

        self.assertEqual(
            suggest_transfers(players, [candidate(100, 4, 9.0, 5.0)], bank=None), []
        )

    def test_a_shared_best_replacement_falls_back_to_the_next_option(self):
        players = squad(p3={"expected_points": 1.0}, p4={"expected_points": 2.0})
        pool = [candidate(100, 2, 9.0, 5.0), candidate(101, 2, 7.0, 5.0)]

        suggestions = suggest_transfers(players, pool, bank=0.0)

        self.assertEqual(
            [(item["out"]["id"], item["in"]["id"]) for item in suggestions],
            [(3, 100), (4, 101)],
        )
        self.assertGreater(suggestions[0]["gain"], suggestions[1]["gain"])

    def test_suggestions_are_limited_and_ordered_by_gain(self):
        players = squad(
            p8={"expected_points": 1.0},
            p9={"expected_points": 2.0},
            p10={"expected_points": 3.0},
            p11={"expected_points": 3.5},
        )
        pool = [candidate(100 + index, 3, 8.0, 5.0) for index in range(5)]

        suggestions = suggest_transfers(players, pool, bank=0.0)

        self.assertEqual([item["out"]["id"] for item in suggestions], [8, 9, 10])
        self.assertEqual(len({item["in"]["id"] for item in suggestions}), 3)

    def test_a_swap_that_does_not_reach_the_best_xi_is_not_suggested(self):
        # A third goalkeeper upgrade cannot help: only one goalkeeper starts,
        # and the replacement would still be the weaker of the two.
        players = squad(p1={"expected_points": 6.0}, p2={"expected_points": 1.0})
        pool = [candidate(100, 1, 5.0, 5.0)]

        self.assertEqual(suggest_transfers(players, pool, bank=0.0), [])

    def test_current_official_data_overrides_the_forecasts_availability(self):
        players = squad(p13={"expected_points": 1.0})
        # Each forecast says available (factor 1.0); official data has moved on.
        pool = [
            candidate(100, 4, 9.0, 5.0, can_select=False),
            candidate(101, 4, 9.0, 5.0, status="i"),
            candidate(102, 4, 9.0, 5.0, status="u"),
            candidate(103, 4, 9.0, 5.0, chance_of_playing_next_round=0),
            candidate(104, 4, 8.0, 5.0, status="d", chance_of_playing_next_round=75),
        ]

        suggestions = suggest_transfers(players, pool, bank=0.0)

        # Only the doubtful player is still buyable.
        self.assertEqual([item["in"]["id"] for item in suggestions], [104])

    def test_an_over_limit_squad_is_only_offered_sales_from_that_club(self):
        # The review's case: four Arsenal players after a real-life transfer.
        players = squad(
            p3={"team_name": "Arsenal"},
            p4={"team_name": "Arsenal"},
            p5={"team_name": "Arsenal"},
            p8={"team_name": "Arsenal"},
            p13={"expected_points": 1.0},
        )
        pool = [
            candidate(100, 4, 9.0, 5.0, club="Everton"),
            candidate(101, 2, 9.0, 5.0, club="Everton"),
        ]

        suggestions = suggest_transfers(players, pool, bank=0.0)

        self.assertEqual(clubs_over_limit(players), {"Arsenal": 4})
        self.assertEqual(
            [(item["out"]["id"], item["in"]["id"]) for item in suggestions], [(3, 101)]
        )
        for item in suggestions:
            after = [
                player["team_name"]
                for player in players
                if player["id"] != item["out"]["id"]
            ] + [item["in"]["team_name"]]
            self.assertLessEqual(max(after.count(club) for club in set(after)), 3)

    def test_a_swap_may_not_push_another_club_over_the_limit(self):
        players = squad(
            p13={"expected_points": 1.0},
            p1={"team_name": "Everton"},
            p3={"team_name": "Everton"},
            p8={"team_name": "Everton"},
        )

        self.assertEqual(
            suggest_transfers(
                players, [candidate(100, 4, 9.0, 5.0, club="Everton")], bank=0.0
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
