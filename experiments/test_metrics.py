"""Checks for deadline denominators, window boundaries and CPU normalisation."""
import unittest
from metrics import clipped_duration_ns, cpu_percent, deadline_stats


class MeasurementTests(unittest.TestCase):
    def test_deadlines_include_drops_and_missing_messages(self):
        offered = [{"id": str(n), "source_ns": "1000000000"} for n in range(5)]
        received = [{"id": "0", "outcome": "ok", "response_ns": "1100000000"},
                    {"id": "1", "outcome": "ok", "response_ns": "1100000001"},
                    {"id": "2", "outcome": "queue_drop"},
                    {"id": "3", "outcome": "request_error"}]
        result = deadline_stats(offered, received, 100)
        self.assertEqual(result["on_time"], 1)
        self.assertEqual(result["late_successful"], 1)
        self.assertEqual(result["not_completed"], 3)
        self.assertEqual(result["miss_ratio"], 0.8)

    def test_busy_intervals_are_clipped_not_counted_whole(self):
        intervals = [(0, 15), (17, 25), (40, 50)]
        self.assertEqual(sum(clipped_duration_ns(a, b, 10, 20) for a, b in intervals), 8)

    def test_cpu_uses_one_core_and_interpolated_boundaries(self):
        samples = [{"timestamp_ns": n * 1_000_000_000, "cpu": n * 0.25} for n in range(4)]
        self.assertAlmostEqual(cpu_percent(samples, "cpu", 500_000_000, 2_500_000_000), 25.0)
        with self.assertRaises(ValueError):
            cpu_percent(samples, "cpu", -1, 2_000_000_000)


if __name__ == "__main__":
    unittest.main()
