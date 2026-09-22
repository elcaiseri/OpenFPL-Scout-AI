"""Run with: python -m scripts.replay_gw1 --season 2026-2027."""
import argparse
import json

from src.scout_replay import create_gw1_replay
from src.utils import load_config


def main():
    parser = argparse.ArgumentParser(description='Score an archived GW1 roster with the current models; retain the original Scout picks.')
    parser.add_argument('--season', required=True)
    parser.add_argument('--config', default='config/config.yaml')
    args = parser.parse_args()
    replay = create_gw1_replay(load_config(args.config), args.season)
    print(json.dumps({'gameweek': 1, 'kind': replay['kind'], 'players': len(replay['predictions']), 'generated_at_utc': replay['generated_at_utc']}))


if __name__ == '__main__':
    main()
