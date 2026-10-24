"""Check socket evidence and measurement/drain boundaries in TCP reviews."""
import json
from pathlib import Path
import tempfile
import unittest

from plot_results import audit_run


def fixture(root, response_ns=1_300_000_000, actual_algorithm="cubic"):
    def save(name, value):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    def log(name, values):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(value) for value in values) + "\n")
    save("resolved.json", {"tcp_trial": {"algorithm": "cubic", "wan_sender_role": "edge", "load": 1, "loss_percent": 0, "repeat": 1},
         "topology": "T3_EDGE_PASS", "robots": [{"id": "robot1"}], "active_components": ["robot1", "edgeA", "proxy", "worker1"],
         "capture": False, "nodes": {name: {"role": role, "ip": ip} for name, role, ip in
             (("robot1", "robot", "10.0.0.20"), ("edgeA", "edge", "10.0.0.10"),
              ("proxy", "proxy", "10.1.0.10"), ("worker1", "worker", "10.1.0.20"))}})
    save("start.json", {"start_ns": 500_000_000, "measurement_start_ns": 1_000_000_000, "measurement_end_ns": 2_000_000_000})
    save("summary.json", {"accounting_passed": True, "generated": 1, "successful": 1,
         "completed_rps": 1 if response_ns < 2_000_000_000 else 0,
         "source_to_response_ms": {"p95": (response_ns - 1_100_000_000) / 1e6},
         "per_robot": {"robot1": {"queue": {"mean_Lq": .05}}}})
    save("validation.json", {"passed": True})
    save("network/cleanup.json", {"success": True})
    save("process_cleanup.json", [{"forced_kill": False, "exit_code": 0}])
    log("logs/robot1/source.jsonl", [{"id": "x", "source_ns": 1_100_000_000}])
    log("logs/robot1/gateway.jsonl", [{"id": "x", "source_ns": 1_100_000_000,
         "response_ns": response_ns, "outcome": "success", "payload_bytes": 100,
         "admission_ns": 1_150_000_000, "dequeue_ns": 1_200_000_000, "request_ns": 1_210_000_000}])
    log("logs/robot1/queue.jsonl", [{"time_ns": 0, "queue_len": 0},
         {"time_ns": 1_150_000_000, "queue_len": 1}, {"time_ns": 1_200_000_000, "queue_len": 0}])
    log("logs/worker1/worker.jsonl", [{"id": "x", "outcome": "processed"}])
    log("logs/edgeA/connections.jsonl", [{"event": "outgoing_connection", "tcp_congestion_requested": "cubic",
         "tcp_congestion_actual": actual_algorithm, "peer": ["10.1.0.10", 8080]}])


class ReviewTests(unittest.TestCase):
    def test_socket_actual_algorithm_is_required_not_only_requested_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root, actual_algorithm="reno")
            with self.assertRaisesRegex(ValueError, "wan_socket_algorithm_verified"):
                audit_run(root)

    def test_drain_completion_counts_for_latency_but_not_window_throughput(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root, response_ns=2_300_000_000)
            row = audit_run(root)
            self.assertEqual(row["successful_including_drain"], 1)
            self.assertEqual(row["completed_in_window"], 0)
            self.assertEqual(row["completed_rps"], 0)
            self.assertAlmostEqual(row["latency_p95_s"], 1.2)

    def test_review_recalculates_counts_and_latency_from_raw_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            row = audit_run(root)
            self.assertEqual(row["verified_algorithms"], ["cubic"])
            self.assertEqual(row["completed_rps"], 1)
            self.assertAlmostEqual(row["latency_p95_s"], .2)
            summary = json.loads((root / "summary.json").read_text())
            summary["completed_rps"] = 50
            (root / "summary.json").write_text(json.dumps(summary))
            with self.assertRaisesRegex(ValueError, "completion_rate_matches_summary"):
                audit_run(root)


if __name__ == "__main__":
    unittest.main()
