"""Checks for preserving failed attempts and requiring validated suite resumes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from complex_experiments.runner import digest, matrix_runs, suite, validate


BASE = dict(protocol='h2', experiment='queue', warmup_s=1, duration_s=2, drain_s=3,
            request_timeout_s=4, mean_service_s=.02, service_mode='exponential',
            queue_capacity=20, gateway_concurrency=4, edge_concurrency=8,
            offered_rps_total=30, video_enabled=False)


class RunnerTests(unittest.TestCase):
    def test_fixed_topology_and_invalid_profiles(self):
        config = validate(BASE)
        self.assertEqual([r['site'] for r in config['robots']], ['A', 'A', 'B', 'B'])
        self.assertEqual(config['workers'], 2)
        for override in (dict(protocol='auto'), dict(workers=1), dict(edge_concurrency=0),
                         dict(sites={'A': {'forward': {'loss_pct': 101}}})):
            with self.assertRaises(ValueError):
                validate(dict(BASE, **override))

    def test_matrix_rejects_duplicate_or_unsafe_names(self):
        for rows in ([dict(name='case'), dict(name='case')], [dict(name='../elsewhere')]):
            with self.assertRaises(ValueError):
                matrix_runs(dict(base=BASE, runs=rows))

    def test_invalid_attempt_is_preserved_and_not_resumed(self):
        matrix = dict(base=BASE, runs=[dict(name='case')])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            prior = output/'case'
            prior.mkdir()
            sentinel = prior/'invalid.json'
            sentinel.write_text('{"reason":"prior failure"}')
            with patch('complex_experiments.runner.run_experiment') as run:
                suite(matrix, output)
                self.assertEqual(run.call_args.args[1], output/'case__attempt02')
            self.assertEqual(sentinel.read_text(), '{"reason":"prior failure"}')

    def test_valid_resume_reruns_independent_analysis(self):
        matrix = dict(base=BASE, runs=[dict(name='case')])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            prior = output/'case'
            prior.mkdir()
            (prior/'COMPLETE.json').write_text(json.dumps(dict(passed=True, requested_config_sha256=digest(BASE))))
            (prior/'validation.json').write_text('{"passed":true}')
            with patch('complex_experiments.runner.run_experiment') as run, patch('complex_experiments.analyze.analyze') as analyze:
                results = suite(matrix, output)
                self.assertFalse(run.called)
                analyze.assert_called_once_with(prior)
            self.assertTrue(results[0]['resumed'])

    def test_existing_suite_definition_cannot_be_silently_changed(self):
        matrix = dict(base=BASE, runs=[dict(name='case')])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output/'matrix.json').write_text(json.dumps(dict(base=BASE, runs=[])))
            with self.assertRaises(ValueError):
                suite(matrix, output)


if __name__ == '__main__':
    unittest.main()
