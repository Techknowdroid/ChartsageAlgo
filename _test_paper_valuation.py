import unittest
from paper_signal_replay import PositionSizer, replay, xirr


class ValuationTests(unittest.TestCase):
    def test_annual_return(self):
        self.assertAlmostEqual(xirr([('2025-01-01', 100)], '2026-01-01', 110), 10)
        self.assertAlmostEqual(xirr([('2025-01-01', 100)], '2026-01-01', 80), -20)

    def test_contributions(self):
        terminal = 100*1.1 + 50*1.1**(184/365)
        self.assertAlmostEqual(xirr([('2025-01-01',100), ('2025-07-01',50)], '2026-01-01', terminal),10)

    def test_no_duration(self):
        self.assertIsNone(xirr([('2025-01-01',100)], '2025-01-01',110))

    def test_open_valuation_and_costs(self):
        rows = {'2025-01-01':[dict(Symbol='ABC', Signal='BUY', Close=100)]}
        result = replay(rows, '2025-01-01', '2026-01-01', 100000, 1,
                        {'ABC':dict(price=120, date='2025-12-31', source='test')})
        sized, = result['Summary']
        self.assertEqual(sized['Contributions'],101000)
        self.assertEqual(sized['FinalPortfolioValue'],120000)
        self.assertEqual(sized['TotalPnL'],19000)
        self.assertAlmostEqual(sized['XIRRPercent'], (120000/101000-1)*100)
        self.assertEqual(result['OpenHoldings'][0]['PriceDate'],'2025-12-31')

    def test_missing_future_and_old_prices(self):
        rows = {'2025-07-01':[dict(Symbol='ABC', Signal='BUY', Close=100)]}
        for mark in ({}, {'price':120,'date':'2026-01-02'}, {'price':120,'date':'2025-01-01'}):
            result = replay(rows,'2025-01-01','2026-01-01', valuations={'ABC':mark})
            self.assertIsNone(result['Summary'][0]['TotalPnL'])
            self.assertIsNone(result['Summary'][0]['XIRRPercent'])
            self.assertEqual(result['Summary'][0]['MissingValuations'],1)

    def test_closed_repeated_and_no_short(self):
        rows = {'2025-01-01':[dict(Symbol='ABC',Signal='BUY',Close=100)],
                '2025-01-02':[dict(Symbol='ABC',Signal='BUY',Close=100)],
                '2025-12-31':[dict(Symbol='ABC',Signal='EXIT',Close=120)],
                '2026-01-01':[dict(Symbol='ABC',Signal='EXIT',Close=120)]}
        result = replay(rows,'2025-01-01','2026-01-01', reference_capital=100)
        self.assertEqual(result['Summary'][0]['TotalPnL'],40)
        self.assertEqual(result['Summary'][0]['FinalPortfolioValue'],240)
        self.assertIsNotNone(result['Summary'][0]['XIRRPercent'])
        self.assertFalse(result['OpenHoldings'])
        self.assertEqual(len(result['Exceptions']),1)

    def test_holding_exit_does_not_require_later_step1_match(self):
        buy_results = {
            '2025-01-02': [dict(
                Symbol='ABC', Signal='BUY', Close=100,
                **{'Matched Members': 'BUY RULE', 'Evidence': 'buy evidence'}
            )]
        }
        holding_exits = {
            '2025-02-03': [dict(
                Symbol='ABC', Signal='EXIT / WEAKNESS', Close=90,
                **{'Matched Members': 'EXIT RULE', 'Evidence': 'exit evidence'}
            )],
            # XYZ was never bought; this must never create a short position.
            '2025-02-04': [dict(
                Symbol='XYZ', Signal='EXIT / WEAKNESS', Close=50,
                **{'Matched Members': 'EXIT RULE', 'Evidence': 'exit evidence'}
            )],
        }
        result = replay(
            buy_results,
            '2025-01-01',
            '2025-12-31',
            holding_exit_rows_by_date=holding_exits,
        )
        sales = [
            row for row in result['Transactions']
            if row['Mode'] == 'Reference capital' and row['Action'] == 'SELL'
        ]
        self.assertEqual(len(sales), 1)
        self.assertEqual(sales[0]['Symbol'], 'ABC')
        self.assertEqual(sales[0]['Quantity'], 1000)
        self.assertEqual(
            sales[0]['SignalSource'],
            'Paper holding EXIT rule match',
        )
        self.assertFalse(result['OpenHoldings'])
        self.assertEqual(len(result['EXIT Evaluation Audit']), 2)
        abc_audit = next(
            row for row in result['EXIT Evaluation Audit']
            if row['Symbol'] == 'ABC'
        )
        xyz_audit = next(
            row for row in result['EXIT Evaluation Audit']
            if row['Symbol'] == 'XYZ'
        )
        self.assertEqual(abc_audit['Executed'], 'Yes')
        self.assertAlmostEqual(abc_audit['ExitPnLPercent'], -10.0)
        self.assertEqual(xyz_audit['Executed'], 'No')
        self.assertEqual(xyz_audit['ExitPnLPercent'], '')
        self.assertEqual(
            sum('No BUY was executed' in row['Reason'] for row in result['Exceptions']),
            1,
        )

    def test_unmatched_exits_record_specific_reasons(self):
        rows = {
            '2024-12-31': [dict(Symbol='PRE', Signal='BUY', Close=80)],
            '2025-01-01': [
                dict(Symbol='ABC', Signal='BUY', Close=100),
                dict(Symbol='NEVER', Signal='EXIT', Close=50),
                dict(Symbol='PRE', Signal='EXIT', Close=90),
                dict(Symbol='SAME', Signal='BUY', Close=100),
                dict(Symbol='SAME', Signal='EXIT', Close=100),
            ],
            '2025-01-02': [dict(Symbol='ABC', Signal='EXIT', Close=110)],
            '2025-01-03': [dict(Symbol='ABC', Signal='EXIT', Close=105)],
        }
        result = replay(rows, '2025-01-01', '2025-12-31', reference_capital=100)
        reasons = {}
        for row in result['Exceptions']:
            reasons.setdefault(row['Symbol'], []).append(row['Reason'])
        self.assertTrue(any('No BUY was executed' in reason for reason in reasons['NEVER']))
        self.assertTrue(any('before the selected start date' in reason for reason in reasons['PRE']))
        self.assertTrue(any('same-day BUY is processed after EXIT' in reason for reason in reasons['SAME']))
        self.assertTrue(any('Already fully sold on 2025-01-02' in reason for reason in reasons['ABC']))

    def test_reference_capital_integer_sizing_and_tolerance(self):
        sizer = PositionSizer(130000)
        expected = {
            130000: 1,
            48000: 3,
            43000: 3,
            4000: 33,
            700: 186,
            20: 6500,
        }
        for price, quantity in expected.items():
            sized = sizer.size(price)
            self.assertEqual(sized.quantity, quantity)
            self.assertIsInstance(sized.quantity, int)
        tolerance_case = sizer.size(100000)
        self.assertEqual(tolerance_case.quantity, 2)
        self.assertTrue(tolerance_case.tolerance_increased_quantity)
        self.assertEqual(sizer.reference_capital, 130000)

    def test_highest_reference_stock_buys_one_share(self):
        sized = PositionSizer(130000).size(130000)
        self.assertEqual(sized.quantity, 1)
        self.assertEqual(sized.actual_investment, 130000)

    def test_profit_rule_uses_average_cost_exits_and_allows_repurchase(self):
        profit_rule = {
            'name': '[EXIT] Take Profit 10%',
            'expression': {
                'type': 'group', 'enabled': True, 'op': 'AND',
                'children': [{
                    'type': 'rule', 'enabled': True,
                    'left': {'kind': 'field', 'name': 'Profit %'},
                    'operator': '>=',
                    'right': {'kind': 'value', 'value': 10},
                }],
            },
        }
        rows = {
            '2025-01-01': [dict(Symbol='ABC', Signal='BUY', Close=100)],
            '2025-01-02': [dict(Symbol='ABC', Signal='BUY', Close=105)],
            '2025-01-04': [dict(Symbol='ABC', Signal='BUY', Close=80)],
        }
        daily_prices = {'ABC': {
            '2025-01-01': 100,
            '2025-01-02': 105,
            '2025-01-03': 112.75,
            '2025-01-04': 80,
            '2025-01-05': 88,
        }}
        result = replay(
            rows, '2025-01-01', '2025-01-05',
            reference_capital=100,
            position_exit_rule=profit_rule,
            daily_prices=daily_prices,
        )
        transactions = result['Transactions']
        self.assertEqual(
            [row['Action'] for row in transactions],
            ['BUY', 'BUY', 'SELL', 'BUY', 'SELL'],
        )
        self.assertEqual(
            [row['Quantity'] for row in transactions if row['Action'] == 'SELL'],
            [2, 2],
        )
        self.assertTrue(all(
            row['SignalSource'] == 'Paper holding profit/stop EXIT rule match'
            for row in transactions if row['Action'] == 'SELL'
        ))
        self.assertFalse(result['OpenHoldings'])
        self.assertEqual(len(result['EXIT Evaluation Audit']), 2)


if __name__ == '__main__':
    unittest.main()
