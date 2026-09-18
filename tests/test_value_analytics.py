import json
import unittest

from src.observatory import player_value, season_analysis, value_metrics
from test_comparison_diagnostics import player, week


class ValueAnalyticsTests(unittest.TestCase):
    def test_points_divide_by_millions_with_real_zero_and_negative_returns(self):
        self.assertEqual(player_value(player(1, 6, 0, price=5)),
                         {'expected_points_per_million': 1.2, 'actual_points_per_million': 0})
        self.assertEqual(player_value(player(1, 0, -2, price=4)),
                         {'expected_points_per_million': 0, 'actual_points_per_million': -.5})

    def test_missing_or_invalid_price_never_creates_value(self):
        for price in (None, 0, -5, 'bad', float('nan'), float('inf')):
            with self.subTest(price=price):
                row = player(1, 6, 10, price=price)
                self.assertTrue(all(v is None for v in player_value(row).values()))
                result = value_metrics([row])
                self.assertEqual(result['missing_price'], 1)
                self.assertEqual(result['count'], 0)
                self.assertIsNone(result['expected_mean'])
                json.dumps(result, allow_nan=False)

    def test_summary_matches_identical_rows_without_ownership_or_missing_results(self):
        rows = [player(1, 6, 10, price=5), player(2, 2, 0, price=4),
                player(3, None, -2, price=4, selection_score=90),
                player(4, 12, None, price=6), player(5, 10, 100)]
        result = value_metrics(rows)
        self.assertEqual(result, {'rows': 5, 'priced': 4, 'missing_price': 1,
                                 'expected_count': 3, 'actual_count': 3, 'count': 2,
                                 'expected_mean': .85, 'actual_mean': 1})
        self.assertIsNone(player_value(rows[2])['expected_points_per_million'])
        self.assertEqual(player_value(rows[2])['actual_points_per_million'], -.5)

    def test_season_value_averages_each_price_and_respects_final_and_verified_scope(self):
        weeks = [week(1, [player(1, 6, 10, price=5)]),
                 week(2, [player(1, 4, 0, price=8)], eligible=False),
                 week(3, [player(1, 100, 100, price=4)], state='provisional')]
        result = season_analysis(weeks)['players'][0]['value']
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['expected_mean'], .85)
        self.assertEqual(result['actual_mean'], 1)
        verified = season_analysis(weeks, verified=True)['players'][0]['value']
        self.assertEqual(verified['count'], 1)
        self.assertEqual(verified['expected_mean'], 1.2)
        self.assertEqual(verified['actual_mean'], 2)

    def test_empty_summary_is_unknown_not_zero(self):
        result = value_metrics([])
        self.assertEqual(result['count'], 0)
        self.assertIsNone(result['actual_mean'])
        self.assertIsNone(result['expected_mean'])
