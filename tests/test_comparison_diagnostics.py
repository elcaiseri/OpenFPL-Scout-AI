import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.model_lab import ModelLab
from src.observatory import (
    baseline_comparison, baseline_predictions, coverage, error_distribution,
    gameweek_analysis, model_comparison, point_metrics, position_heatmap,
    season_analysis, selection_metrics,
)


def player(pid, expected, actual, position='MID', **extra):
    return {'id': pid, 'name': f'Player {pid}', 'team': 'Club', 'position': position,
            'expected_points': expected, 'selection_score': expected,
            'actual_points': actual, 'error': expected - actual if expected is not None and actual is not None else None,
            **extra}


def week(gw, players, eligible=True, state='final'):
    result = {'gameweek': gw, 'players': players, 'eligible': eligible, 'result_state': state,
              'forecast_state': 'pre-deadline' if eligible else 'post-deadline',
              'prediction_count': len(players), 'is_points_forecast': True,
              'metrics': point_metrics(players), 'squad': {'matches_forecast': False, 'eligible': False, 'count': 0}}
    result['analysis'] = gameweek_analysis(result, {}, [], {})
    return result


class BaselineTests(unittest.TestCase):
    def test_prior_gameweeks_only_and_double_fixtures_sum_before_averaging(self):
        history = pd.DataFrame([
            {'id': 1, 'gameweek': 0, 'total_points': 900, 'official_fixture': 0},
            {'id': 1, 'gameweek': 1, 'total_points': 2, 'official_fixture': 1},
            {'id': 1, 'gameweek': 2, 'total_points': 3, 'official_fixture': 2},
            {'id': 1, 'gameweek': 2, 'total_points': 4, 'official_fixture': 3},
            {'id': 1, 'gameweek': 2, 'total_points': 4, 'official_fixture': 3},
            {'id': 1, 'gameweek': 3, 'total_points': 100, 'official_fixture': 4},
        ])
        result = baseline_predictions(history, 3)
        self.assertEqual(result['last_gameweek'], {'1': 7})
        self.assertEqual(result['recent_three_gameweeks'], {'1': 4.5})

    def test_missing_history_is_not_zero_and_partial_fixture_totals_are_unknown(self):
        history = pd.DataFrame([
            {'id': 1, 'gameweek': 1, 'total_points': 0},
            {'id': 2, 'gameweek': 2, 'total_points': 4},
            {'id': 2, 'gameweek': 2, 'total_points': None},
        ])
        result = baseline_predictions(history, 3)
        self.assertEqual(result['last_gameweek'], {})
        self.assertEqual(result['recent_three_gameweeks'], {'1': 0})
        self.assertEqual(baseline_predictions(pd.DataFrame(), 1)['last_gameweek'], {})

    def test_recent_average_uses_only_last_three_recorded_gameweeks(self):
        history = pd.DataFrame([{'id': 1, 'gameweek': gw, 'total_points': points}
                                for gw, points in [(1, 100), (2, 0), (4, -2), (5, 8)]])
        self.assertEqual(baseline_predictions(history, 6)['recent_three_gameweeks']['1'], 2)

    def test_baseline_lift_pairs_identical_rows_and_handles_zero_error_baseline(self):
        rows = [player(1, 4, 6, baseline_predictions={'last_gameweek': 0, 'recent_three_gameweeks': 6}),
                player(2, 100, 0), player(3, 5, None, baseline_predictions={'last_gameweek': 8})]
        previous, recent = baseline_comparison(rows)
        self.assertEqual(previous['count'], 1)
        self.assertEqual(previous['candidate_count'], 2)
        self.assertEqual(previous['ensemble_mae'], 2)
        self.assertEqual(previous['baseline_mae'], 6)
        self.assertEqual(previous['improvement_points'], 4)
        self.assertAlmostEqual(previous['improvement_pct'], 100 * 4 / 6)
        self.assertEqual(recent['improvement_points'], -2)
        self.assertIsNone(recent['improvement_pct'])


class DiagnosticTests(unittest.TestCase):
    def test_histogram_boundaries_are_exhaustive_and_non_overlapping(self):
        errors = [-9, -8, -4, -2, -0.5, 0, 0.5, 2, 4, 8, 9]
        result = error_distribution([player(i, error, 0) for i, error in enumerate(errors)] + [player(99, None, 4), player(100, 8, None)])
        self.assertEqual(result['count'], 11)
        self.assertEqual([b['count'] for b in result['bins']], [1, 1, 1, 2, 1, 2, 1, 1, 1])
        self.assertEqual(sum(b['count'] for b in result['bins']), result['count'])

    def test_shared_model_population_preserves_individual_coverage(self):
        rows = [player(1, 6, 5, model_predictions={'ridge': 4}), player(2, 100, 0), player(3, 5, None, model_predictions={'ridge': 10})]
        models = {m['name']: m for m in model_comparison(rows)}
        self.assertEqual(models['ensemble']['count'], 2)
        self.assertEqual(models['ridge']['count'], 1)
        for model in models.values():
            self.assertEqual(model['comparison']['count'], 1)
            self.assertEqual(model['comparison']['actual_mean'], 5)
            self.assertEqual(model['comparison']['mae'], 1)

    def test_disjoint_model_predictions_do_not_invent_shared_comparison(self):
        models = model_comparison([player(1, 2, 3, model_predictions={'ridge': 2}), player(2, 2, 3, model_predictions={'catboost': 2})])
        self.assertTrue(all(m['comparison']['count'] == 0 for m in models))
        self.assertTrue(all(m['comparison']['mae'] is None for m in models))

    def test_coverage_separates_missing_results_from_ranking_only(self):
        result = coverage([player(1, 3, 0), player(2, 3, None), player(3, None, 5, selection_score=99)])
        self.assertEqual(result, {'forecasted': 2, 'matched': 1, 'matched_pct': 50, 'missing_results': 1, 'ranking_only': 1})

    def test_heatmap_and_season_scope_distinguish_zero_missing_and_excluded(self):
        weeks = [week(1, [player(1, 2, 2)]), week(2, [player(1, 10, 0)], eligible=False),
                 week(3, [player(1, 2, 1)], state='provisional')]
        cells = position_heatmap(weeks, verified=True)
        self.assertEqual(cells[0]['positions'][2]['mae'], 0)
        self.assertEqual(cells[0]['positions'][0]['count'], 0)
        self.assertIsNone(cells[0]['positions'][0]['mae'])
        self.assertFalse(cells[1]['included'])
        self.assertFalse(cells[2]['included'])
        verified, all_runs = season_analysis(weeks, verified=True), season_analysis(weeks)
        self.assertEqual(verified['error_distribution']['count'], 1)
        self.assertEqual(all_runs['error_distribution']['count'], 2)
        self.assertEqual(verified['coverage']['verified_gameweeks'], 1)
        self.assertEqual(all_runs['coverage']['evaluated_gameweeks'], 2)
        self.assertEqual(verified['coverage']['pending_gameweeks'], 1)
        self.assertEqual(len(verified['selection_windows']['5']), 1)
        json.dumps(verified, allow_nan=False)

    def test_ranking_windows_handle_short_pools_missing_results_and_ties(self):
        rows = [player(pid, 3, 3) for pid in range(7, 0, -1)] + [player(8, 100, None)]
        for size in (5, 10, 20):
            result = selection_metrics(rows, size)
            self.assertEqual(result['count'], min(size, 7))
            self.assertEqual(result['missing_results'], 1)
            self.assertEqual(result['overlap_pct'], 100)
            self.assertEqual(result['our_top'][0]['id'], 1)

    def test_training_shared_metrics_use_same_rows_despite_different_availability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pd.DataFrame([{'actual': 6, 'weighted_ensemble': 4, 'ridge': 5, 'baseline_last': 0},
                          {'actual': 0, 'weighted_ensemble': 100, 'ridge': None, 'baseline_last': 0}]).to_csv(root / 'holdout_predictions.csv', index=False)
            report = ModelLab({'models': {'ridge': {'path': str(root / 'ridge.pkl')}}}).report()
            models = {m['name']: m for m in report['models']}
            self.assertEqual(report['comparison_count'], 1)
            self.assertEqual(models['weighted_ensemble']['count'], 2)
            self.assertEqual(models['weighted_ensemble']['comparison']['mae'], 2)
            self.assertEqual(models['baseline_last']['comparison']['mae'], 6)
            self.assertEqual(models['ridge']['error_distribution']['count'], 1)
