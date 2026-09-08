import json
import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.model_lab import ModelLab
from src.observatory import best_xi, calibration, model_comparison, point_metrics, selection_metrics, squad_analysis, scout_analysis, scout_season_analysis


def squad():
    positions = ['GK'] * 2 + ['DEF'] * 5 + ['MID'] * 5 + ['FWD'] * 3
    return [{
        'id': i + 1, 'name': f'Player {i+1}', 'position': position,
        'in_squad': True, 'selection_score': 16 - i,
        'expected_points': 16 - i, 'actual_points': i - 1,
    } for i, position in enumerate(positions)]


class ComparisonTests(unittest.TestCase):
    def test_metric_denominators_exclude_missing_nonfinite_but_keep_zero_and_negative(self):
        values = [
            {'expected_points': 1, 'actual_points': -1},
            {'expected_points': 0, 'actual_points': 0},
            {'expected_points': None, 'actual_points': 10},
            {'expected_points': math.inf, 'actual_points': 8},
        ]
        result = point_metrics(values)
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['mae'], 1)
        self.assertEqual(result['predicted_total'], 1)
        self.assertEqual(result['actual_total'], -1)
        self.assertAlmostEqual(result['rank_correlation'], -1)
        json.dumps(result, allow_nan=False)

    def test_degenerate_metrics_do_not_invent_correlation(self):
        result = point_metrics([{'expected_points': 2, 'actual_points': 2}] * 4)
        self.assertEqual(result['mae'], 0)
        self.assertIsNone(result['r2'])
        self.assertIsNone(result['rank_correlation'])
        self.assertIsNone(point_metrics([])['actual_total'])

    def test_calibration_uses_identical_matched_population(self):
        bins = calibration([{'expected_points': 1, 'actual_points': 3}, {'expected_points': 0, 'actual_points': None}, {'expected_points': 2, 'actual_points': -1}])
        self.assertEqual(bins[0]['count'], 1)
        self.assertEqual(bins[0]['predicted_mean'], 1)
        self.assertEqual(bins[0]['actual_mean'], 3)
        self.assertEqual(bins[1]['actual_mean'], -1)
        self.assertIsNone(bins[-1]['mae'])

    def test_component_models_have_their_own_explicit_coverage(self):
        values = [
            {'expected_points': 4, 'actual_points': 5, 'model_predictions': {'ridge': 6}},
            {'expected_points': 2, 'actual_points': 0},
            {'expected_points': 8, 'actual_points': None, 'model_predictions': {'ridge': 10}},
        ]
        models = {m['name']: m for m in model_comparison(values)}
        self.assertEqual(models['ensemble']['count'], 2)
        self.assertEqual(models['ridge']['count'], 1)
        self.assertEqual(models['ridge']['actual_mean'], 5)

    def test_ownership_scores_evaluate_rankings_without_becoming_points(self):
        values = [{'id': i, 'selection_score': i, 'expected_points': None, 'actual_points': i * 2} for i in range(1, 16)]
        ranking = selection_metrics(values)
        self.assertEqual(ranking['top10_overlap_pct'], 100)
        self.assertEqual(ranking['ndcg'], 1)
        self.assertEqual(ranking['our_top'][0]['id'], 15)
        self.assertEqual(point_metrics(values)['count'], 0)

    def test_xi_is_legal_and_fixed_captain_actual_is_doubled_once(self):
        players = squad()
        result = squad_analysis(players)
        xi = result['xi']
        self.assertEqual(len(xi), 11)
        self.assertEqual(len(result['bench']), 4)
        self.assertEqual(sum(p['position'] == 'GK' for p in xi), 1)
        self.assertGreaterEqual(sum(p['position'] == 'DEF' for p in xi), 3)
        self.assertGreaterEqual(sum(p['position'] == 'FWD' for p in xi), 1)
        captain = next(p for p in xi if p['is_captain'])
        self.assertEqual(captain['id'], 1)
        self.assertEqual(result['actual_points'], sum(p['actual_points'] for p in xi) - 1)
        best = best_xi(players, 'actual_points')
        self.assertEqual(result['hindsight_points'], sum(p['actual_points'] for p in best) + 13)
        self.assertGreaterEqual(result['selection_gap'], 0)

    def test_missing_bench_score_blocks_hindsight_but_not_complete_xi(self):
        players = squad()
        bench_id = squad_analysis(players)['bench'][0]['id']
        next(p for p in players if p['id'] == bench_id)['actual_points'] = None
        result = squad_analysis(players)
        self.assertIsNotNone(result['actual_points'])
        self.assertIsNone(result['hindsight_points'])
        self.assertIsNone(result['selection_gap'])

    def test_missing_starter_score_or_incomplete_squad_stays_unknown(self):
        players = squad()
        players[0]['actual_points'] = None
        self.assertIsNone(squad_analysis(players)['actual_points'])
        self.assertEqual(squad_analysis(players[:-1])['xi'], [])
        self.assertIsNone(squad_analysis([])['predicted_points'])
        for p in players:
            p['expected_points'] = None
        self.assertIsNone(squad_analysis(players)['predicted_points'])
        self.assertEqual(len(squad_analysis(players)['xi']), 11)


class ScoutComparisonTests(unittest.TestCase):
    def week(self, gw=1):
        players = squad()
        players[0]['role'] = 'captain'
        players[1]['role'] = 'vice'
        players.append({'id': 99, 'name': 'Not selected', 'position': 'MID', 'in_squad': False, 'expected_points': 50, 'actual_points': 60})
        return {
            'gameweek': gw, 'players': players, 'eligible': True,
            'forecast_state': 'pre-deadline', 'result_state': 'final',
            'is_points_forecast': True,
            'squad': {'count': 15, 'matches_forecast': True, 'eligible': True},
        }

    def test_xpts_vs_points_uses_saved_picks_and_counts_captains_once(self):
        report = scout_analysis(self.week())
        self.assertEqual(report['count'], 15)
        self.assertEqual(report['matched_actuals'], 15)
        self.assertEqual(report['expected_points'], 135)
        self.assertEqual(report['actual_points'], 90)
        self.assertEqual(report['error'], 45)
        self.assertEqual(report['metrics']['predicted_total'], 135)
        self.assertEqual(report['metrics']['actual_total'], 90)
        self.assertEqual(report['captain']['id'], 1)
        self.assertEqual(report['vice']['actual_points'], 0)
        self.assertEqual(sum(p['count'] for p in report['positions']), 15)

    def test_missing_actual_keeps_full_total_unknown_and_pairs_metrics(self):
        week = self.week()
        week['players'][0]['actual_points'] = None
        report = scout_analysis(week)
        self.assertEqual(report['expected_points'], 135)
        self.assertIsNone(report['actual_points'])
        self.assertIsNone(report['error'])
        self.assertEqual(report['metrics']['count'], 14)
        self.assertEqual(report['metrics']['predicted_total'], 119)
        self.assertEqual(report['metrics']['actual_total'], 91)

    def test_cold_start_keeps_actual_returns_without_inventing_xpts(self):
        week = self.week()
        week['is_points_forecast'] = False
        for player in week['players']:
            player['expected_points'] = None
        report = scout_analysis(week)
        self.assertEqual(report['actual_points'], 90)
        self.assertIsNone(report['expected_points'])
        self.assertIsNone(report['error'])
        self.assertEqual(report['metrics']['count'], 0)
        season = scout_season_analysis([week])
        self.assertIsNone(season['metrics']['actual_total'])
        self.assertFalse(season['timeline'][0]['is_points_forecast'])

    def test_missing_or_mismatched_shortlist_does_not_reconstruct_picks(self):
        week = self.week()
        week['squad']['matches_forecast'] = False
        report = scout_analysis(week)
        self.assertEqual(report['archive_state'], 'mismatched')
        self.assertEqual(report['players'], [])
        self.assertIsNone(report['captain'])
        self.assertIsNone(report['expected_points'])
        self.assertEqual(scout_season_analysis([week])['timeline'], [])
        week['squad']['count'] = 0
        self.assertEqual(scout_analysis(week)['archive_state'], 'missing')

    def test_season_weights_picks_and_requires_final_results_and_squad_timing(self):
        first, late, upcoming = self.week(1), self.week(2), self.week(3)
        late['players'] = [{'id': 30, 'name': 'Late selection', 'position': 'FWD', 'in_squad': True, 'expected_points': 10, 'actual_points': 0}]
        late['squad'].update(count=1, eligible=False)
        upcoming['result_state'] = 'provisional'
        all_runs = scout_season_analysis([first, late, upcoming])
        self.assertEqual(all_runs['metrics']['count'], 16)
        self.assertEqual(all_runs['metrics']['predicted_total'], 145)
        self.assertEqual(all_runs['metrics']['actual_total'], 90)
        self.assertAlmostEqual(all_runs['metrics']['mae'], (scout_analysis(first)['metrics']['mae'] * 15 + 10) / 16)
        self.assertEqual([w['gameweek'] for w in all_runs['timeline']], [1, 2])
        verified = scout_season_analysis([first, late, upcoming], verified=True)
        self.assertEqual(verified['metrics']['count'], 15)
        self.assertEqual([w['gameweek'] for w in verified['timeline']], [1])


class ModelLabTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.lab = ModelLab({'models': {'ridge': {'path': str(self.root / 'ridge.pkl')}}})

    def test_missing_artifacts_are_explicit_and_dataset_is_whitelisted(self):
        self.assertFalse(self.lab.report()['available'])
        with self.assertRaises(ValueError):
            self.lab.report('../private')

    def test_recorded_models_and_baselines_compare_with_actual_labels(self):
        pd.DataFrame([
            {'season': 2025, 'gameweek': 1, 'actual': 4, 'ridge': 6, 'baseline_last': 0},
            {'season': 2025, 'gameweek': 2, 'actual': 0, 'ridge': 1, 'baseline_last': 3},
            {'season': 2025, 'gameweek': 2, 'actual': -2, 'ridge': float('nan'), 'baseline_last': -2},
        ]).to_csv(self.root / 'holdout_predictions.csv', index=False)
        report = self.lab.report()
        self.assertTrue(report['available'])
        self.assertTrue(report['warnings'])  # Missing metadata is disclosed.
        models = {m['name']: m for m in report['models']}
        self.assertEqual(models['ridge']['count'], 2)
        self.assertEqual(models['ridge']['mae'], 1.5)
        self.assertEqual(models['baseline_last']['count'], 3)
        self.assertAlmostEqual(models['baseline_last']['mae'], 7/3)
        self.assertEqual(len(models['ridge']['timeline']), 2)
        self.assertEqual(models['ridge']['outliers'][0]['actual_points'], 4)
        self.assertFalse(self.lab.report('cross-validation')['available'])
        json.dumps(report, allow_nan=False)

    def test_absent_actual_label_is_not_an_evaluation(self):
        (self.root / 'holdout_predictions.csv').write_text('ridge\n2\n')
        self.assertFalse(self.lab.report()['available'])


if __name__ == '__main__':
    unittest.main()
