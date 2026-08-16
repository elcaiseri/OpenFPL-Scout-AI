import unittest

from src.team_rating import rate_manager_team


def squad(expected_points=5.0):
    positions = [1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 1, 2, 3, 4]
    return [
        {
            "id": index,
            "web_name": f"Player {index}",
            "element_type": position,
            "expected_points": expected_points,
            "pick_position": index,
            "multiplier": 2 if index == 1 else 1 if index <= 11 else 0,
            "is_captain": index == 1,
            "is_vice_captain": index == 2,
            "availability_factor": 1.0,
            "selected_by_percent": 20.0,
        }
        for index, position in enumerate(positions, start=1)
    ]


class TeamRatingTests(unittest.TestCase):
    def test_perfect_team_scores_one_hundred(self):
        manager = squad()
        benchmark = squad()

        result = rate_manager_team(manager, benchmark)

        self.assertEqual(result["rating"], 100)
        self.assertEqual(result["grade"], "A+")
        self.assertEqual(result["projected_points"], 60.0)
        self.assertEqual(result["ai_projected_points"], 60.0)
        self.assertEqual(
            result["components"],
            {"starting_xi": 80.0, "captaincy": 10.0, "availability": 10.0},
        )

    def test_captain_and_availability_risks_reduce_score(self):
        manager = squad()
        manager[0]["expected_points"] = 1.0
        manager[0]["availability_factor"] = 0.0

        result = rate_manager_team(manager, squad())

        self.assertLess(result["rating"], 90)
        self.assertTrue(any("captain" in risk.lower() for risk in result["risks"]))
        self.assertTrue(
            any("Player 1" in risk for risk in result["risks"])
        )

    def test_rejects_an_incomplete_published_squad(self):
        with self.assertRaisesRegex(ValueError, "must contain 15 players"):
            rate_manager_team(squad()[:-1], squad())


if __name__ == "__main__":
    unittest.main()
