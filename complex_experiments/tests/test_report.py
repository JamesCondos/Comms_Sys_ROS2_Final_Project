import json
from pathlib import Path
import tempfile
import unittest

from complex_experiments.report import aggregate_rows, distribution, read_runs


class ReportAccountingTests(unittest.TestCase):
    def test_replicate_ranges_do_not_pool_percentiles(self):
        rows = [dict(experiment='protocol', protocol='h2', profile='clean',
                     configured_offered_rps=None, repetition=i,
                     p95_latency_ms=value, calibrated_mmc_Wq_s=None)
                for i,value in enumerate((10,20,90),1)]
        cell = aggregate_rows(rows)[0]
        self.assertEqual(cell['p95_latency_ms_mean'],40)
        self.assertEqual(cell['p95_latency_ms_minimum'],10)
        self.assertEqual(cell['p95_latency_ms_maximum'],90)
        self.assertIsNone(cell['calibrated_mmc_Wq_s_mean'])
        self.assertEqual(cell['calibrated_mmc_Wq_s_n'],0)

    def test_invalid_runs_are_not_silently_omitted(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)/'run'
            folder.mkdir()
            (folder/'summary.json').write_text(json.dumps(dict(experiment='queue',protocol='h2',accounting_passed=False)))
            (folder/'resolved.json').write_text('{}')
            (folder/'validation.json').write_text('{"passed":false}')
            with self.assertRaisesRegex(ValueError,'invalid run'):
                read_runs(temporary,allow_partial=True)

    def test_incomplete_formal_collection_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError,'Expected 60'):
                read_runs(temporary)

    def test_no_stationary_prediction_is_not_zero(self):
        self.assertEqual(distribution([None,None]),dict(n=0,mean=None,minimum=None,maximum=None))
        self.assertEqual(distribution([None,12])['mean'],12)


if __name__=='__main__':
    unittest.main()
