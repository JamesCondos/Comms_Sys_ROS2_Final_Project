import math
import unittest

from complex_experiments.theory import (
    audit_little, coefficient_of_variation, event_area_ns, interval_area_ns, mmc,
)


class TheoryTest(unittest.TestCase):
    def test_mm1_known_means(self):
        result = mmc(8, 10, 1)
        self.assertAlmostEqual(result['W_s'], 0.5)
        self.assertAlmostEqual(result['Wq_s'], 0.4)
        self.assertAlmostEqual(result['L'], 4)
        self.assertAlmostEqual(result['Lq'], 3.2)

    def test_mmc_independent_closed_form(self):
        # c=2, lambda=50, mu=50: p0=1/3, Pwait=1/3.
        result = mmc(50, 50, 2)
        self.assertAlmostEqual(result['p_wait'], 1/3)
        self.assertAlmostEqual(result['Wq_s'], 1/150)
        self.assertAlmostEqual(result['W_s'], 2/75)
        self.assertAlmostEqual(result['Lq'], 1/3)

    def test_no_stationary_prediction_overload(self):
        for arrival in (100, 110):
            result = mmc(arrival, 50, 2)
            self.assertFalse(result['stable'])
            self.assertIsNone(result['W_s'])
            self.assertIsNone(result['Lq'])
        self.assertEqual(mmc(110, 50, 2)['fluid_backlog_growth_per_s'], 10)

    def test_zero_arrivals_and_large_server_count(self):
        self.assertEqual(mmc(0, 50, 2)['Lq'], 0)
        self.assertEqual(mmc(0, 50, 2)['W_s'], .02)
        self.assertTrue(math.isfinite(mmc(900, 1, 1000)['L']))

    def test_reject_invalid_models(self):
        for args in ((-1, 10, 2), (1, 0, 2), (1, 10, 0), (1, 10, 1.5), (1, 10, True)):
            with self.assertRaises(ValueError):
                mmc(*args)

    def test_exact_area_with_carry_in_out(self):
        # Window [10,20): jobs [5,12), [11,18), [15,25), [0,infinite).
        intervals = [(5, 12), (11, 18), (15, 25), (0, None)]
        events = [dict(time_ns=t, queue_len=n) for t, n in
                  [(0, 1), (5, 2), (11, 3), (12, 2), (15, 3), (18, 2), (25, 1)]]
        result = audit_little(intervals, events, 10, 20)
        self.assertTrue(result['passed'])
        self.assertEqual(result['interval_area_ns'], 24)
        self.assertEqual(result['mean_occupancy'], 2.4)

    def test_equal_timestamps_use_log_order(self):
        events = [dict(time_ns=t, queue_len=n) for t, n in [(0,0),(10,1),(10,2),(20,0)]]
        self.assertEqual(event_area_ns(events, 10, 20), 20)

    def test_tamper_is_detected(self):
        events = [dict(time_ns=0, queue_len=1)]
        result = audit_little([(0, 5)], events, 0, 10)
        self.assertFalse(result['passed'])
        self.assertEqual(result['residual_ns'], 5)

    def test_interval_bounds_and_missing_initial_state(self):
        self.assertEqual(interval_area_ns([(0, 5),(30,40)], 10,20), 0)
        with self.assertRaises(ValueError):
            interval_area_ns([(15,12)], 10,20)
        with self.assertRaises(ValueError):
            event_area_ns([dict(time_ns=11,queue_len=0)],10,20)

    def test_cv_diagnostics(self):
        self.assertEqual(coefficient_of_variation([1,1,1]), 0)
        self.assertIsNone(coefficient_of_variation([1]))


if __name__ == '__main__':
    unittest.main()
