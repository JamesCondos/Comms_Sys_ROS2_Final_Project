"""Meaningful timing, conservation, actual-source and HTTP service checks."""
import copy
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from e1_extended import queue_occupancy, resolved_run_config, validate_config, wait_json, read_csv
from validate_e1_extended import validate_records


def base_config():
    return {"actual_source_counts": [1, 2, 4, 8], "conditions": [{"id": "reference"},
        {"id": "gateway", "source_counts": [8], "gateway_dispatch_delay_ms": 10}],
        "duration_s": 60, "warmup_s": 10, "drain_s": 30, "repeats": 5,
        "queue_capacity": 1000, "request_timeout_s": 2, "seed": 61061,
        "traffic": {"command": {"rate_hz": 2, "payload_bytes": 100}}, "deadlines_ms": {"command": 100}}


def record_fixture():
    config = resolved_run_config(base_config(), {"id": "reference"}, 1, 1, Path("fixture"))
    generated = [{"id": "x", "robot_id": "0", "message_type": "command", "sequence_number": "0",
                  "scheduled_ns": "100000000", "source_ns": "100000000", "payload_bytes": "100"}]
    gateway = [dict(generated[0], enqueue_ns="110000000", dequeue_ns="120000000", request_start_ns="130000000",
        server_receive_ns="140000000", server_processing_start_ns="140000100", server_processed_ns="150000000",
        response_ns="160000000", http_status="200", http_version="11", ros_topic="/fixture/robot_0/command",
        retry_count="0", outcome="ok", connection_id="1", request_body_bytes="222")]
    server = [{k: gateway[0][k] for k in ("id", "server_receive_ns", "server_processing_start_ns", "server_processed_ns",
                "connection_id", "request_body_bytes", "payload_bytes")}]
    server[0].update(protocol="HTTP/1.1", handler_start_ns="130000000", handler_end_ns="155000000")
    topics = [{"robot_id": 0, "message_type": "command", "rate_hz": 2, "payload_bytes": 100, "topic": "/fixture/robot_0/command"}]
    graph = {"actual_nodes": [{"name": "traffic_source", "namespace": "/fixture/robot_0"}],
             "topic_publisher_counts": {topics[0]["topic"]: 1}}
    return config, generated, gateway, server, topics, graph


class E1ExtendedTests(unittest.TestCase):
    def test_actual_source_configuration_never_uses_rate_scaling(self):
        sweep = base_config()
        for count in (1, 2, 4, 8):
            config = resolved_run_config(sweep, sweep["conditions"][0], count, 1, Path("test"))
            self.assertEqual(config["robots"], count)
            self.assertEqual(config["max_ros_robots"], count)
            self.assertEqual(config["source_rate_multiplier"], 1)
            self.assertEqual(config["traffic"]["command"]["rate_hz"], 2)
            self.assertEqual(config["nominal_offered_rps"], 2 * count)

    def test_subset_conditions_and_invalid_parameters(self):
        config = base_config()
        validate_config(config)
        for change in ({"source_counts": [16]}, {"gateway_dispatch_delay_ms": -1}, {"network_rate_mbit": 0}):
            altered = copy.deepcopy(config)
            altered["conditions"][1].update(change)
            with self.assertRaises(ValueError):
                validate_config(altered)

    def test_queue_integral_uses_time_not_event_average_and_keeps_initial_state(self):
        events = [{"timestamp_ns": 1, "queue_length": 5}, {"timestamp_ns": 11, "queue_length": 0},
                  {"timestamp_ns": 19, "queue_length": 2}, {"timestamp_ns": 20, "queue_length": 9}]
        mean, maximum, end = queue_occupancy(events, 10, 20)
        self.assertAlmostEqual(mean, 0.7)
        self.assertEqual((maximum, end), (5, 2))

    def test_conservation_allows_real_queue_drop_but_rejects_false_success(self):
        values = record_fixture()
        validate_records(*values)
        config, generated, gateway, server, topics, graph = values
        dropped = dict(generated[0], id="y", sequence_number="1", scheduled_ns="600000000", source_ns="600000000")
        generated.append(dropped)
        gateway.append(dict(dropped, enqueue_ns="610000000", dequeue_ns="", request_start_ns="", outcome="queue_drop",
                            ros_topic=topics[0]["topic"], retry_count="0"))
        self.assertEqual(validate_records(*values)["generated_all"], 2)
        gateway[1]["outcome"] = "ok"
        with self.assertRaisesRegex(AssertionError, "False successful"):
            validate_records(*values)

    def test_timestamp_inversion_and_scaled_rate_are_detected(self):
        values = record_fixture()
        values[2][0]["server_processed_ns"] = "170000000"
        with self.assertRaisesRegex(AssertionError, "timestamp order"):
            validate_records(*values)
        values = record_fixture()
        values[4][0]["rate_hz"] = 64
        with self.assertRaisesRegex(AssertionError, "rate changed"):
            validate_records(*values)

    def test_controlled_http_service_records_processing_and_reuses_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            config = {"server_bind": "127.0.0.1", "server_port": 0,
                      "server_processing_delay_ms": 10, "request_timeout_s": 2}
            (folder / "config.json").write_text(json.dumps(config))
            command = [sys.executable, str(Path(__file__).with_name("e1_extended.py")), "--server",
                       "--config", str(folder / "config.json"), "--output", str(folder)]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            connection = None
            try:
                ready = wait_json(folder / "server_ready.json", process)
                connection = http.client.HTTPConnection("127.0.0.1", ready["port"], timeout=2)
                acknowledgements = []
                for sequence in range(2):
                    message = {"id": str(sequence), "robot_id": 0, "message_type": "command", "payload": "x" * 100}
                    connection.request("POST", "/robots/0/command", json.dumps(message), {"Content-Type": "application/json"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    ack = json.loads(response.read())
                    self.assertEqual(ack["id"], str(sequence))
                    self.assertGreaterEqual(ack["server_processed_ns"] - ack["server_processing_start_ns"], 10_000_000)
                    self.assertLessEqual(ack["server_receive_ns"], ack["server_processing_start_ns"])
                    acknowledgements.append(ack)
                self.assertEqual({r["connection_id"] for r in acknowledgements}, {1})
            finally:
                if connection is not None:
                    connection.close()
                (folder / "server.stop").touch()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
            self.assertEqual(process.returncode, 0, stderr)
            rows = read_csv(folder / "server.csv")
            self.assertEqual(len(rows), 2)
            self.assertEqual({r["protocol"] for r in rows}, {"HTTP/1.1"})


if __name__ == "__main__":
    unittest.main()
