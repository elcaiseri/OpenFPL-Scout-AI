import unittest

from src.manager_lab import (
    HIT_COST,
    MAX_FREE_TRANSFERS,
    catalog,
    estimate_free_transfers,
    optimize,
    review,
    squad_from_picks,
    xi_value,
)

SHAPE = ['GK'] * 2 + ['DEF'] * 5 + ['MID'] * 5 + ['FWD'] * 3


def player(pid, position, expected, price=5.0, team=None, status='a'):
    return {
        'id': pid, 'name': f'P{pid}', 'team': team or f'Club {pid}',
        'position': position, 'expected_points': expected, 'price': price,
        'status': status, 'opponent': 'OPP',
    }


def squad(expected=None):
    scores = expected or [10 - i * 0.25 for i in range(15)]
    return [player(i + 1, position, scores[i]) for i, position in enumerate(SHAPE)]


def bootstrap():
    elements = []
    for index, position in enumerate(SHAPE + ['MID', 'FWD']):
        code = {'GK': 1, 'DEF': 2, 'MID': 3, 'FWD': 4}[position]
        elements.append({
            'id': index + 1, 'web_name': f'P{index + 1}', 'team': (index % 5) + 1,
            'element_type': code, 'now_cost': 50, 'status': 'a',
        })
    return {'elements': elements, 'teams': [{'id': i, 'name': f'Club {i}'} for i in range(1, 6)]}


class XiTests(unittest.TestCase):
    def test_xi_doubles_the_captain_and_respects_formation_limits(self):
        score, xi, captain = xi_value(squad())
        self.assertEqual(len(xi), 11)
        self.assertEqual(captain['id'], 1)
        self.assertEqual(sum(p['position'] == 'GK' for p in xi), 1)
        self.assertGreaterEqual(sum(p['position'] == 'DEF' for p in xi), 3)
        self.assertGreaterEqual(sum(p['position'] == 'FWD' for p in xi), 1)
        self.assertAlmostEqual(score, sum(p['expected_points'] for p in xi) + captain['expected_points'])

    def test_players_without_a_forecast_are_never_fielded(self):
        roster = squad()
        for entry in roster[:6]:
            entry['expected_points'] = None
        score, xi, _ = xi_value(roster)
        self.assertIsNone(score)
        self.assertEqual(xi, [])


class FreeTransferTests(unittest.TestCase):
    def test_banked_transfers_accumulate_and_cap(self):
        history = {'current': [{'event': gw, 'event_transfers': 0} for gw in range(1, 10)], 'chips': []}
        self.assertEqual(estimate_free_transfers(history, 10)['free_transfers'], MAX_FREE_TRANSFERS)

    def test_transfers_made_reduce_the_bank_and_never_go_negative(self):
        history = {
            'current': [
                {'event': 1, 'event_transfers': 0},
                {'event': 2, 'event_transfers': 3},
                {'event': 3, 'event_transfers': 0},
            ],
            'chips': [],
        }
        result = estimate_free_transfers(history, 4)
        self.assertEqual(result['free_transfers'], 2)
        self.assertEqual(result['basis'], 'derived-from-public-transfer-history')

    def test_chip_weeks_do_not_spend_banked_transfers(self):
        history = {
            'current': [{'event': 1, 'event_transfers': 0}, {'event': 2, 'event_transfers': 11}],
            'chips': [{'event': 2, 'name': 'wildcard'}],
        }
        result = estimate_free_transfers(history, 3)
        self.assertEqual(result['free_transfers'], 2)
        self.assertEqual(result['chip_gameweeks'], [2])

    def test_an_entry_without_completed_gameweeks_is_labelled_not_derived(self):
        result = estimate_free_transfers({'current': [], 'chips': []}, 1)
        self.assertEqual(result['free_transfers'], 1)
        self.assertEqual(result['basis'], 'no-completed-gameweeks')


class OptimizeTests(unittest.TestCase):
    def test_a_single_affordable_upgrade_is_found_and_priced(self):
        roster = squad()
        pool = [player(100, 'MID', 30.0, price=5.0, team='Club 100')]
        result = optimize(roster, pool, bank=0.0, free_transfers=1)
        plan = result['plans'][0]
        self.assertEqual(plan['transfers'], 1)
        self.assertTrue(plan['exhaustive'])
        self.assertEqual(plan['moves'][0]['in']['id'], 100)
        self.assertEqual(plan['hits'], 0)
        self.assertGreater(plan['net_gain'], 0)
        self.assertEqual(result['recommended']['moves'][0]['in']['id'], 100)

    def test_an_unaffordable_target_is_never_recommended(self):
        roster = squad()
        pool = [player(100, 'MID', 30.0, price=12.0, team='Club 100')]
        result = optimize(roster, pool, bank=0.5, free_transfers=1)
        self.assertIsNone(result['recommended'])
        self.assertEqual(result['plans'], [])

    def test_the_three_player_club_limit_blocks_a_fourth_signing(self):
        roster = squad()
        for entry in roster[7:10]:
            entry['team'] = 'Loaded'
        # The only candidate would be a fourth Loaded player, and no single
        # defensive transfer can free one of their midfield slots.
        pool = [player(100, 'DEF', 40.0, price=5.0, team='Loaded')]
        self.assertEqual(optimize(roster, pool, bank=0.0, free_transfers=1)['plans'], [])

    def test_replacing_a_club_mate_stays_within_the_limit(self):
        roster = squad()
        for entry in roster[2:5]:
            entry['team'] = 'Loaded'
        pool = [player(100, 'DEF', 40.0, price=5.0, team='Loaded')]
        plan = optimize(roster, pool, bank=0.0, free_transfers=1)['plans'][0]
        self.assertEqual(plan['moves'][0]['in']['id'], 100)
        self.assertEqual(plan['moves'][0]['out']['team'], 'Loaded')

    def test_transfers_beyond_the_free_allowance_are_charged_a_hit(self):
        roster = squad()
        pool = [
            player(100, 'MID', 30.0, price=5.0, team='Club 100'),
            player(101, 'FWD', 30.0, price=5.0, team='Club 101'),
        ]
        result = optimize(roster, pool, bank=0.0, free_transfers=1)
        second = next(p for p in result['plans'] if p['transfers'] == 2)
        self.assertEqual(second['hits'], 1)
        self.assertEqual(second['hit_cost'], HIT_COST)
        self.assertAlmostEqual(second['net_gain'], second['gain'] - HIT_COST)
        self.assertAlmostEqual(second['net_predicted_points'], second['predicted_points'] - HIT_COST)

    def test_a_hit_that_does_not_pay_for_itself_is_not_recommended(self):
        roster = squad()
        pool = [
            player(100, 'MID', 10.1, price=5.0, team='Club 100'),
            player(101, 'FWD', 10.1, price=5.0, team='Club 101'),
        ]
        result = optimize(roster, pool, bank=0.0, free_transfers=0)
        self.assertIsNone(result['recommended'])
        self.assertTrue(all(p['net_gain'] <= 0 for p in result['plans']))

    def test_unavailable_players_are_excluded_from_the_pool(self):
        roster = squad()
        pool = [player(100, 'MID', 40.0, price=5.0, team='Club 100', status='i')]
        self.assertEqual(optimize(roster, pool, bank=0.0, free_transfers=1)['plans'], [])

    def test_an_incomplete_squad_is_refused_rather_than_padded(self):
        with self.assertRaises(ValueError):
            optimize(squad()[:14], [], bank=0.0, free_transfers=1)

    def test_selling_price_and_search_limits_are_disclosed(self):
        result = optimize(squad(), [], bank=0.0, free_transfers=1)
        self.assertTrue(any('Selling prices are not public' in n for n in result['notes']))
        self.assertTrue(any('not proven optima' in n for n in result['notes']))


class CatalogTests(unittest.TestCase):
    def test_prices_come_from_the_live_bootstrap_not_the_archived_forecast(self):
        rows = [{'id': 1, 'name': 'P1', 'position': 'GK', 'expected_points': 4.0, 'price': 99.0}]
        players = catalog(rows, bootstrap())
        self.assertEqual(players[1]['price'], 5.0)
        self.assertEqual(players[1]['expected_points'], 4.0)

    def test_rows_without_a_forecast_are_not_candidates(self):
        rows = [{'id': 1, 'web_name': 'P1', 'element_type': 1, 'expected_points': None}]
        self.assertEqual(catalog(rows, bootstrap()), {})

    def test_picks_without_a_forecast_are_reported_and_held(self):
        rows = [{'id': i + 1, 'web_name': f'P{i+1}', 'element_type': {'GK': 1, 'DEF': 2, 'MID': 3, 'FWD': 4}[p], 'expected_points': 3.0} for i, p in enumerate(SHAPE)]
        players = catalog(rows, bootstrap())
        del players[3]
        picks = {'picks': [{'element': i + 1, 'element_type': {'GK': 1, 'DEF': 2, 'MID': 3, 'FWD': 4}[p]} for i, p in enumerate(SHAPE)]}
        roster, unforecast, unresolved = squad_from_picks(picks, players, bootstrap())
        self.assertEqual(len(roster), 15)
        self.assertEqual(unforecast, ['P3'])
        self.assertEqual(unresolved, [])
        self.assertIsNone(next(p for p in roster if p['id'] == 3)['expected_points'])


class ReviewTests(unittest.TestCase):
    def test_official_multipliers_apply_to_both_our_forecast_and_the_result(self):
        week = {'players': [
            {'id': 1, 'name': 'Keeper', 'team': 'A', 'position': 'GK', 'expected_points': 3.0, 'actual_points': 2.0},
            {'id': 2, 'name': 'Star', 'team': 'B', 'position': 'MID', 'expected_points': 7.0, 'actual_points': 12.0},
        ], 'squad': {}, 'analysis': {}}
        picks = {
            'gameweek': 5,
            'picks': [
                {'element': 1, 'element_type': 1, 'multiplier': 1},
                {'element': 2, 'element_type': 3, 'multiplier': 2, 'is_captain': True},
            ],
            'entry_history': {'points': 26, 'points_on_bench': 4, 'bank': 12, 'value': 1005, 'event_transfers': 1, 'event_transfers_cost': 0},
        }
        result = review(picks, week)
        self.assertEqual(result['predicted_points'], 3.0 + 7.0 * 2)
        self.assertEqual(result['actual_points'], 2.0 + 12.0 * 2)
        self.assertEqual(result['official_points'], 26)
        self.assertEqual(result['bank'], 1.2)
        self.assertEqual(result['squad_value'], 100.5)
        self.assertEqual(result['captain']['id'], 2)

    def test_a_missing_result_leaves_the_total_unknown_rather_than_low(self):
        week = {'players': [
            {'id': 1, 'name': 'A', 'position': 'GK', 'expected_points': 3.0, 'actual_points': 2.0},
            {'id': 2, 'name': 'B', 'position': 'MID', 'expected_points': 7.0, 'actual_points': None},
        ], 'squad': {}, 'analysis': {}}
        picks = {'gameweek': 5, 'picks': [
            {'element': 1, 'element_type': 1, 'multiplier': 1},
            {'element': 2, 'element_type': 3, 'multiplier': 1},
        ], 'entry_history': {}}
        result = review(picks, week)
        self.assertIsNone(result['actual_points'])
        self.assertEqual(result['matched_actuals'], 1)

    def test_picks_without_an_archived_forecast_stay_unscored(self):
        picks = {'gameweek': 5, 'picks': [{'element': 9, 'element_type': 3, 'multiplier': 1}], 'entry_history': {}}
        result = review(picks, None)
        self.assertIsNone(result['predicted_points'])
        self.assertEqual(result['matched_forecasts'], 0)
        self.assertEqual(result['squad'][0]['position'], 'MID')

    def test_benched_players_are_separated_from_the_starting_eleven(self):
        picks = {'gameweek': 5, 'picks': [
            {'element': 1, 'element_type': 1, 'multiplier': 1},
            {'element': 2, 'element_type': 3, 'multiplier': 0},
        ], 'entry_history': {}}
        result = review(picks, None)
        self.assertEqual([p['id'] for p in result['starting']], [1])
        self.assertEqual([p['id'] for p in result['bench']], [2])


if __name__ == '__main__':
    unittest.main()
