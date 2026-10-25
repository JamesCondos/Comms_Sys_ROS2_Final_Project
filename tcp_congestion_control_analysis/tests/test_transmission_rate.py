"""Check rate calculations using known byte counts and timestamps."""

from pathlib import Path
import sys
import unittest

# Import the graph script from the parent TCP folder.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plot_transmission_rate import transmission_rates


class RateTests(unittest.TestCase):
    def test_counter_increase_is_converted_to_megabits_per_second(self):
        samples = [dict(event="tcp_sample", connection_id="edgeA:1", time_ns=stamp, bytes_sent=count)
                   for stamp, count in [(0, 0), (2_000_000_000, 250_000)]]
        rows = transmission_rates(samples, 0, 2_000_000_000)
        self.assertEqual([row["transmission_mbit_s"] for row in rows], [1, 1])
        self.assertEqual([row["sampled_seconds"] for row in rows], [1, 1])

    def test_new_connection_does_not_create_a_false_counter_increase(self):
        samples = [dict(event="tcp_sample", connection_id=name, time_ns=stamp, bytes_sent=count)
                   for name, stamp, count in [("first", 0, 100), ("second", 1_000_000_000, 99999)]]
        rows = transmission_rates(samples, 0, 1_000_000_000)
        self.assertIsNone(rows[0]["transmission_mbit_s"])


if __name__ == "__main__":
    unittest.main()
