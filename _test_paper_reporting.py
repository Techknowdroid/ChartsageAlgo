import math
import unittest

from paper_signal_replay import replay


class PaperReportingTests(unittest.TestCase):
    def test_campaign_twr_drawdown_and_benchmark_statistics(self):
        rows = {
            '2025-01-02': [
                {'Signal': 'BUY', 'Symbol': 'AAA', 'Close': 100},
                {'Signal': 'BUY', 'Symbol': 'BBB', 'Close': 100},
            ],
            '2025-01-03': [
                {'Signal': 'EXIT', 'Symbol': 'AAA', 'Close': 110},
            ],
            '2025-01-06': [
                {'Signal': 'EXIT', 'Symbol': 'BBB', 'Close': 90},
            ],
        }
        result = replay(
            rows, '2025-01-02', '2025-01-06', reference_capital=100,
            daily_prices={
                'AAA': {'2025-01-02': 100, '2025-01-03': 110, '2025-01-06': 110},
                'BBB': {'2025-01-02': 100, '2025-01-03': 100, '2025-01-06': 90},
            },
            benchmark_prices={
                '2025-01-02': 100, '2025-01-03': 102, '2025-01-06': 101,
            },
            benchmark_name='NIFTY PHARMA TRI',
        )
        summary = result['Summary'][0]
        self.assertEqual(summary['ClosedCampaigns'], 2)
        self.assertEqual(summary['OpenCampaigns'], 0)
        self.assertEqual(summary['WinRatePercent'], 50)
        self.assertAlmostEqual(summary['EqualWeightedAverageCampaignReturnPercent'], 0)
        self.assertAlmostEqual(summary['PayoffRatio'], 1)
        self.assertAlmostEqual(summary['SimpleReturnOnContributedCapitalPercent'], 0)
        self.assertAlmostEqual(summary['TimeWeightedReturnPercent'], 0, places=9)
        self.assertLess(summary['MaximumDrawdownPercent'], 0)
        self.assertAlmostEqual(summary['BenchmarkPeriodReturnPercent'], 1)
        self.assertEqual(summary['BenchmarkName'], 'NIFTY PHARMA TRI')
        self.assertAlmostEqual(summary['ExcessTWRPercent'], -1)
        self.assertEqual(summary['HistoricalOutcome'], 'BREAK-EVEN AFTER COSTS')
        self.assertEqual(len(result['Campaigns']), 2)
        self.assertEqual(len(result['Equity Curve']), 3)
        self.assertEqual(len(result['Benchmark']), 3)

    def test_open_campaign_is_separate_from_closed_win_rate(self):
        result = replay(
            {'2025-01-02': [{'Signal': 'BUY', 'Symbol': 'AAA', 'Close': 100}]},
            '2025-01-02', '2025-01-03', reference_capital=100,
            daily_prices={'AAA': {'2025-01-02': 100, '2025-01-03': 120}},
            valuations={'AAA': {'price': 120, 'date': '2025-01-03', 'source': 'test'}},
        )
        summary = result['Summary'][0]
        self.assertEqual(summary['ClosedCampaigns'], 0)
        self.assertEqual(summary['OpenCampaigns'], 1)
        self.assertIsNone(summary['WinRatePercent'])
        self.assertEqual(result['Campaigns'][0]['Status'], 'OPEN')
        self.assertTrue(math.isclose(result['Campaigns'][0]['ReturnPercent'], 20))


if __name__ == '__main__':
    unittest.main()
