"""Explicit retrospective GW1 model scoring; original Scout evidence is immutable."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import joblib
import pandas as pd

from src.data_archive import DataArchive, utc_datetime
from src.features import TEAM_NAME_ALIASES, normalize_fpl_columns
from src.scout import FPLScout


def load_replay(path, snapshot_bytes, season, gameweek):
    replay = json.loads(Path(path).read_text())
    snapshot = json.loads(snapshot_bytes)
    if not isinstance(replay, dict) or not isinstance(snapshot, dict):
        raise ValueError('Invalid retrospective snapshot')
    ids = {str(int(p['id'])) for p in snapshot['predictions']}
    values = replay.get('predictions', {})
    generated = utc_datetime(replay.get('generated_at_utc'))
    deadline = utc_datetime(snapshot.get('deadline_time'))
    original_capture = utc_datetime(snapshot.get('metadata', {}).get('captured_at_utc'))
    if (
        replay.get('kind') != 'retrospective-model'
        or replay.get('season') != season or replay.get('gameweek') != gameweek
        or replay.get('source_snapshot_sha256') != hashlib.sha256(snapshot_bytes).hexdigest()
        or generated is None or deadline is None or generated <= deadline
        or original_capture is None or generated < original_capture
        or not isinstance(replay.get('note'), str)
        or not isinstance(replay.get('model_predictions'), dict)
        or not isinstance(values, dict) or set(values) != ids
        or not all(type(v) in (int, float) and math.isfinite(v) for v in values.values())
        or not all(isinstance(component, dict) and set(component) == ids
                   and all(type(v) in (int, float) and math.isfinite(v) for v in component.values())
                   for component in replay['model_predictions'].values())
    ):
        raise ValueError('Retrospective estimates do not match the saved snapshot')
    return replay


def create_gw1_replay(config, season, *, model_loader=joblib.load):
    if not re.fullmatch(r'\d{4}-\d{4}', season):
        raise ValueError('Invalid season')
    archive = DataArchive.from_config(config)
    root = archive.root_path / season
    snapshot_path = root / 'evaluation/gw_01.json'
    snapshot_bytes = snapshot_path.read_bytes()
    snapshot = json.loads(snapshot_bytes)
    metadata = snapshot['metadata']
    deadline = utc_datetime(snapshot.get('deadline_time'))
    if (
        metadata.get('season') != season or metadata.get('prediction_gameweek') != 1
        or metadata.get('inference', {}).get('strategy') != 'ownership-cold-start'
        or deadline is None or deadline >= datetime.now(timezone.utc)
    ):
        raise ValueError('An archived, completed GW1 ownership run is required')
    roster_path = root / 'enriched/history/before_gw_01.csv'
    roster = pd.read_csv(roster_path, float_precision='round_trip')
    if not pd.to_numeric(roster.gameweek, errors='coerce').eq(0).all():
        raise ValueError('GW1 replay requires a roster-only GW0 input')
    # Whitelist roster context. Never pass current-season outcomes, form,
    # ownership ranking scores, or the target points into the model inputs.
    columns = ['id', 'element_type', 'web_name', 'team_name', 'gameweek', 'now_cost', 'selected_by_percent']
    roster = normalize_fpl_columns(roster[columns])
    expected_ids = {int(p['id']) for p in snapshot['predictions']}
    if set(roster.id) != expected_ids or roster.id.duplicated().any():
        raise ValueError('Archived roster and saved Scout universe differ')
    bootstrap_path = root / 'official/snapshots/gw_01/bootstrap.json'
    fixtures_path = root / 'official/snapshots/gw_01/fixtures.json'
    teams = {t['id']: TEAM_NAME_ALIASES.get(t['name'], t['name']) for t in json.loads(bootstrap_path.read_text())['teams']}
    contexts = {}
    for f in json.loads(fixtures_path.read_text()):
        if f.get('event') != 1:
            continue
        for team, opponent, home in [(f['team_h'], f['team_a'], True), (f['team_a'], f['team_h'], False)]:
            if teams[team] in contexts:
                raise ValueError('Multiple GW1 fixtures need an explicit replay policy')
            contexts[teams[team]] = {'opponent_team_name': teams[opponent], 'was_home': home}
    if not set(roster.team_name).issubset(contexts):
        raise ValueError('Archived GW1 fixtures are incomplete')
    replay_config = copy.deepcopy(config)
    replay_config.setdefault('inference', {}).setdefault('cold_start', {})['enabled'] = False
    replay_config.setdefault('fpl_data_inference', {})['enabled'] = False
    scout = FPLScout(replay_config, fixture_provider=lambda *_: contexts, model_loader=model_loader)
    predictions = scout.predict_players(roster, gameweek=1)
    model_metadata = {}
    for name, info in config.get('models', {}).items():
        path = Path(info['path'])
        model_metadata[name] = {**info, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
    replay = {
        'kind': 'retrospective-model', 'season': season, 'gameweek': 1,
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'source_snapshot_sha256': hashlib.sha256(snapshot_bytes).hexdigest(),
        'inputs': {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in (roster_path, bootstrap_path, fixtures_path)},
        'predictions': {str(int(p.id)): float(p.expected_points) for p in predictions.itertuples()},
        'model_predictions': predictions.attrs['model_predictions'],
        'inference': predictions.attrs['inference'], 'models': model_metadata,
        'note': 'Retrospective model xPts for the original Scout picks, computed after GW1 from the archived roster and fixture schedule. Prior-match statistics are unavailable and use the models’ trained imputation. The roster was observed after the deadline. These are not original pre-deadline forecasts; official results are not model inputs.',
    }
    target = root / 'evaluation/replays/gw_01.json'
    archive._write_bytes(target, archive._json_bytes(replay))
    load_replay(target, snapshot_bytes, season, 1)
    return replay
