import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.scout_replay import create_gw1_replay, load_replay


class RecordingModel:
    def predict(self, frame):
        self.inputs = frame.copy()
        return np.full(len(frame), 2.5)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / '2020-2021'
        environment = patch.dict(os.environ, {'OPENFPL_DATA_ARCHIVE_ROOT': self.directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        for folder in ('evaluation', 'enriched/history', 'official/snapshots/gw_01'):
            (self.root / folder).mkdir(parents=True)
        self.snapshot = self.root / 'evaluation/gw_01.json'
        self.snapshot.write_text(json.dumps({
            'metadata': {'season': '2020-2021', 'prediction_gameweek': 1, 'captured_at_utc': '2020-08-02T10:00:00Z', 'inference': {'strategy': 'ownership-cold-start'}},
            'deadline_time': '2020-08-01T10:00:00Z',
            'predictions': [{'id': 1, 'expected_points': 4}, {'id': 2, 'expected_points': 3}],
            'actuals_snapshot': {'payload': {'total_points': 9999}},
        }))
        pd.DataFrame([
            {'id': i, 'element_type': 3, 'web_name': f'Player {i}', 'team_name': team, 'gameweek': 0, 'now_cost': 5, 'selected_by_percent': 10, 'minutes': 9999, 'total_points': 9999, 'expected_points': 9999}
            for i, team in [(1, 'Arsenal'), (2, 'Manchester City')]
        ]).to_csv(self.root / 'enriched/history/before_gw_01.csv', index=False)
        (self.root / 'official/snapshots/gw_01/bootstrap.json').write_text(json.dumps({'teams': [{'id': 1, 'name': 'Arsenal'}, {'id': 2, 'name': 'Man City'}]}))
        (self.root / 'official/snapshots/gw_01/fixtures.json').write_text(json.dumps([{'event': 1, 'team_h': 1, 'team_a': 2, 'team_h_score': 99, 'team_a_score': 99}]))
        self.config = {'models': {'ridge': {'path': 'not-used.pkl'}}, 'inference': {'minimum_successful_models': 1, 'cold_start': {'enabled': True}}, 'data_archive': {'enabled': True, 'root_path': self.directory.name}}
        self.model = RecordingModel()

    def test_replay_computes_model_points_without_results_or_mutating_snapshot(self):
        before = self.snapshot.read_bytes()
        config = copy.deepcopy(self.config)
        replay = create_gw1_replay(self.config, '2020-2021', model_loader=lambda _: self.model)
        self.assertEqual(replay['predictions'], {'1': 2.5, '2': 2.5})
        self.assertEqual(replay['model_predictions']['ridge'], {'1': 2.5, '2': 2.5})
        self.assertEqual(replay['kind'], 'retrospective-model')
        self.assertEqual(replay['source_snapshot_sha256'], hashlib.sha256(before).hexdigest())
        self.assertTrue(self.model.inputs.minutes.isna().all())
        self.assertTrue(self.model.inputs.expected_points.isna().all())
        self.assertNotIn('total_points', self.model.inputs)
        self.assertEqual(set(self.model.inputs.opponent_team_name), {'Arsenal', 'Manchester City'})
        self.assertEqual(self.snapshot.read_bytes(), before)
        self.assertEqual(self.config, config)

    def test_loader_rejects_changed_snapshot_wrong_identity_and_invalid_points(self):
        create_gw1_replay(self.config, '2020-2021', model_loader=lambda _: self.model)
        path = self.root / 'evaluation/replays/gw_01.json'
        good = json.loads(path.read_text())
        self.assertEqual(load_replay(path, self.snapshot.read_bytes(), '2020-2021', 1)['predictions']['1'], 2.5)
        with self.assertRaises(ValueError):
            load_replay(path, self.snapshot.read_bytes() + b' ', '2020-2021', 1)
        for change in [{'season': '2021-2022'}, {'gameweek': 2}, {'generated_at_utc': '2020-07-01T00:00:00Z'}, {'predictions': {'1': 2}}, {'predictions': {'1': None, '2': 2}}, {'predictions': {'1': True, '2': 2}}, {'predictions': []}, {'model_predictions': {'ridge': []}}]:
            path.write_text(json.dumps({**good, **change}))
            with self.assertRaises(ValueError):
                load_replay(path, self.snapshot.read_bytes(), '2020-2021', 1)

    def test_replay_rejects_post_gameweek_rows(self):
        path = self.root / 'enriched/history/before_gw_01.csv'
        frame = pd.read_csv(path)
        frame.loc[0, 'gameweek'] = 1
        frame.to_csv(path, index=False)
        with self.assertRaisesRegex(ValueError, 'roster-only'):
            create_gw1_replay(self.config, '2020-2021', model_loader=lambda _: self.model)
        self.assertFalse((self.root / 'evaluation/replays/gw_01.json').exists())
