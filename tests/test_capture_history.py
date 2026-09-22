import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from src.admin_dashboard import AdminDashboard, CaptureDeadlineError
from src.capture_history import CaptureHistory
from src.data_archive import DataArchive


class CaptureHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_records_survive_restart_without_claiming_unfinished_work_succeeded(self):
        history = CaptureHistory(self.root)
        running = history.start(5)
        finished = history.start(6)
        history.save({**finished, 'status': 'saved', 'duration_seconds': 1.5})
        snapshot = CaptureHistory(self.root).snapshot()
        self.assertEqual([r['status'] for r in snapshot['attempts']], ['saved', 'running'])
        self.assertEqual(snapshot['attempts'][1]['id'], running['id'])

    def test_distinct_workers_and_bounded_retention(self):
        first, second = CaptureHistory(self.root, limit=2), CaptureHistory(self.root, limit=2)
        one = first.start(1)
        two = second.start(2)
        first.save({**one, 'status': 'saved'})
        second.save({**two, 'status': 'failed'})
        self.assertEqual(len(CaptureHistory(self.root).snapshot()['attempts']), 2)
        third = first.start(3)
        first.save({**third, 'status': 'saved'})
        self.assertEqual([r['gameweek'] for r in CaptureHistory(self.root, limit=2).snapshot()['attempts']], [3, 2])
        self.assertEqual(len(list(first.root.glob('*.json'))), 2)

    def test_write_failure_keeps_memory_record_and_exposes_warning(self):
        history = CaptureHistory(self.root)
        with patch('src.capture_history.os.replace', side_effect=OSError('read only')):
            record = history.start(4)
        snapshot = history.snapshot()
        self.assertEqual(snapshot['attempts'][0]['id'], record['id'])
        self.assertIn('could not be saved', snapshot['warning'])
        self.assertEqual(list(history.root.glob('*.tmp')), [])

    def test_corrupt_history_is_disclosed_and_does_not_hide_other_attempts(self):
        history = CaptureHistory(self.root)
        record = history.start(2)
        (history.root / 'broken.json').write_text('invalid JSON')
        snapshot = history.snapshot()
        self.assertEqual(len(snapshot['attempts']), 1)
        self.assertEqual(snapshot['attempts'][0]['id'], record['id'])
        self.assertIn('could not be read', snapshot['warning'])


class OwnerCaptureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.predictions = pd.DataFrame([{'id': 1, 'expected_points': 4}])
        self.predictions.attrs = {'archive': {'status': 'saved'}, 'inference': {
            'successful_models': ['ridge'], 'failed_models': {'catboost': 'model error'},
        }}
        self.scout = SimpleNamespace(
            config={}, data_archive=DataArchive(self.root),
            official_client=SimpleNamespace(bootstrap=Mock(return_value={'events': [
                {'id': 1, 'deadline_time': '2099-08-15T10:00:00Z'},
            ]})),
            get_official_predictions=Mock(return_value=self.predictions),
            select_optimal_team=Mock(return_value=self.predictions),
        )
        self.scout.data_archive.capture_squad = Mock(return_value={'status': 'saved'})
        self.dashboard = AdminDashboard(self.scout)

    def test_success_captures_models_duration_and_both_archive_outcomes(self):
        self.dashboard.cache['old'] = (0, {})
        result = self.dashboard.capture_forecast(1)
        record = CaptureHistory(self.root).snapshot()['attempts'][0]
        self.assertEqual(result['capture_id'], record['id'])
        self.assertEqual(record['status'], 'saved')
        self.assertEqual(record['season'], '2099-2100')
        self.assertEqual(record['successful_models'], ['ridge'])
        self.assertEqual(record['failed_models'], ['catboost'])
        self.assertGreaterEqual(record['duration_seconds'], 0)
        self.assertIsNotNone(record['finished_at_utc'])
        self.assertEqual(self.dashboard.cache, {})

    def test_partial_save_is_not_reported_as_success(self):
        self.scout.data_archive.capture_squad.return_value = {'status': 'failed', 'error': 'disk full'}
        self.dashboard.capture_forecast(1)
        record = self.dashboard.capture_history.snapshot()['attempts'][0]
        self.assertEqual(record['status'], 'incomplete')
        self.assertEqual(record['archive_status'], 'saved')
        self.assertEqual(record['squad_status'], 'failed')

    def test_failure_preserves_stage_and_does_not_persist_arbitrary_exception_text(self):
        self.scout.select_optimal_team.side_effect = ValueError('secret-token-in-external-response')
        with self.assertRaises(ValueError):
            self.dashboard.capture_forecast(1)
        record = self.dashboard.capture_history.snapshot()['attempts'][0]
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['archive_status'], 'saved')
        self.assertEqual(record['phase'], 'shortlist selection and archive')
        self.assertIn('ValueError', record['error'])
        self.assertNotIn('secret-token', json.dumps(record))

    def test_expired_or_missing_deadline_is_logged_without_running_inference(self):
        for events in [[{'id': 1, 'deadline_time': '2020-08-15T10:00:00Z'}], []]:
            self.scout.official_client.bootstrap.return_value = {'events': events}
            with self.assertRaises(CaptureDeadlineError):
                self.dashboard.capture_forecast(1)
            self.assertEqual(self.dashboard.capture_history.snapshot()['attempts'][0]['status'], 'rejected')
        self.scout.get_official_predictions.assert_not_called()

    def test_inference_failure_is_recorded_without_claiming_model_success(self):
        self.scout.get_official_predictions.side_effect = RuntimeError('unavailable')
        with self.assertRaises(RuntimeError):
            self.dashboard.capture_forecast(1)
        record = self.dashboard.capture_history.snapshot()['attempts'][0]
        self.assertEqual(record['successful_models'], [])
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['phase'], 'inference and forecast archive')
