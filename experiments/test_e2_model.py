"""E2A tests: queue boundaries, capacity, drain and independent raw validation."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from e2_model import event_log, fifo_schedule, run, seed_for, simulate, summarize, validate_config
from validate_e2_model import validate, verify_run


def small_config():
    return {"experiment": "E2A", "model": "discrete_event_mm1", "mu_per_s": 100,
            "repeats": 1, "queue_capacity": 10000, "sample_interval_s": 1,
            "seed": 61062026, "cases": [{"rho": .5, "warmup_s": 2, "duration_s": 10}]}


class ModelTests(unittest.TestCase):
    def test_fifo_queue_capacity_excludes_in_service_job(self):
        start, departure, accepted = fifo_schedule([0, .1, .2, 2, 3], [2, 1, 1, 1, 1], 1)
        self.assertEqual(accepted.tolist(), [True, True, False, True, True])
        np.testing.assert_array_equal(start[accepted], [0, 2, 3, 4])
        np.testing.assert_array_equal(departure[accepted], [2, 3, 4, 5])
        self.assertTrue(np.isnan(start[2]) and np.isnan(departure[2]))

    def test_exact_finite_window_identity_with_both_boundary_terms(self):
        arrivals = np.array([.5, 1, 2.5, 3])
        service = np.array([2.5, 1, 2, 1])
        start, departure, accepted = fifo_schedule(arrivals, service, 100)
        jobs = {"job_id": np.arange(4), "arrival_s": arrivals,
                "interarrival_s": np.array([.5, .5, 1.5, .5]), "service_draw_s": service,
                "service_start_s": start, "departure_s": departure, "admitted": accepted}
        events = event_log(jobs)
        config = {**small_config(), "warmup_s": 1, "duration_s": 3}
        summary = summarize(jobs, events, config, .5)
        self.assertEqual(summary["initial_population"], 1)
        self.assertEqual(summary["final_population"], 3)
        self.assertEqual(summary["completed_in_window"], 1)
        self.assertEqual(summary["completed_admission_cohort_including_drain"], 3)
        self.assertEqual(summary["conservation_residual"], 0)
        self.assertEqual(summary["L"], 2.5)
        self.assertEqual(summary["Lq"], 1.5)
        self.assertEqual(summary["busy_fraction"], 1)
        self.assertEqual(summary["little"]["cohort_residence_sum_s"], 10.5)
        self.assertEqual(summary["little"]["system_carry_in_area_job_s"], 2)
        self.assertEqual(summary["little"]["system_carry_out_area_job_s"], 5)
        self.assertEqual(summary["little"]["queue_carry_out_area_job_s"], 2)
        self.assertEqual(summary["little"]["system_identity_residual"], 0)
        self.assertEqual(summary["little"]["queue_identity_residual"], 0)
        # The uncorrected finite-window lambda*W is deliberately NOT equal to L.
        self.assertEqual(summary["little"]["lambda_W"], 3.5)
        at_three = events["kind"][events["timestamp_s"] == 3]
        self.assertEqual(at_three.tolist(), [0, 1])

    def test_idle_system_and_draining_exact_schedule(self):
        start, departure, accepted = fifo_schedule([1, 5, 9], [.5, 1, 2], 1)
        np.testing.assert_array_equal(start, [1, 5, 9])
        np.testing.assert_array_equal(departure, [1.5, 6, 11])
        self.assertTrue(accepted.all())

    def test_independent_seeds_and_deterministic_replay(self):
        config = small_config()
        run_config = {**config, **config["cases"][0]}
        jobs1, events1, summary1 = simulate(run_config, .5, 1)
        jobs2, events2, summary2 = simulate(run_config, .5, 1)
        for field in jobs1:
            np.testing.assert_array_equal(jobs1[field], jobs2[field])
        for field in events1:
            np.testing.assert_array_equal(events1[field], events2[field])
        self.assertEqual(summary1, summary2)
        seeds = {seed_for(config["seed"], rho, repeat, stream)
                 for rho in [.2, .5, .8, .9] for repeat in range(1, 6)
                 for stream in ["arrival", "service"]}
        self.assertEqual(len(seeds), 40)

    def test_configuration_rejects_unstable_mm1(self):
        config = small_config()
        config["cases"][0]["rho"] = 1
        with self.assertRaises(AssertionError):
            validate_config(config)

    def test_independent_validator_detects_raw_and_summary_tampering(self):
        config = small_config()
        with tempfile.TemporaryDirectory(prefix="e2a_test_") as directory:
            base = Path(directory)
            config_path = base / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = base / "results"
            with contextlib.redirect_stdout(io.StringIO()):
                run(config_path, output)
                result = validate(output)
            self.assertTrue(result["all_passed"])
            run_path = output / "rho0.50_r01"
            summary_path = run_path / "summary.json"
            summary = json.loads(summary_path.read_text())
            original_l = summary["L"]
            summary["L"] += 1
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "L:"):
                verify_run(run_path, config, config["cases"][0], 1)
            summary["L"] = original_l
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            with np.load(run_path / "jobs.npz") as archive:
                jobs = {key: archive[key] for key in archive.files}
            jobs["service_start_s"][1] += .01
            np.savez_compressed(run_path / "jobs.npz", **jobs)
            with self.assertRaisesRegex(AssertionError, "FIFO or one-worker start"):
                verify_run(run_path, config, config["cases"][0], 1)


if __name__ == "__main__":
    unittest.main()
