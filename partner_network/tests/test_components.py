"""Lightweight component tests: no ROS processes or live HTTP sockets."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from lab.common import (AccountingQueue, TCPHTTPConnection, configure_tcp,
                        decode_envelope, validate_ack)
from lab.components import Robot, Service, json_bytes, periodic_schedule, request_once


def envelope(identifier="r:robot1:odometry:0"):
    return {"id": identifier, "run_id": "r", "robot_id": "robot1", "message_type": "odometry",
            "source_ns": 100, "scheduled_ns": 90, "sequence_number": 0,
            "payload_bytes": 3, "payload": "xxx"}


def acknowledgement(identifier="r:robot1:odometry:0"):
    return {"id": identifier, "outcome": "processed", "worker_id": "worker1",
            "worker_admission_ns": 101, "worker_service_start_ns": 102,
            "worker_processed_ns": 103, "worker_clock_domain_id": "clock1"}


class ListLog:
    def __init__(self):
        self.rows = []

    def write(self, row):
        self.rows.append(dict(row))


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        self.now += 10
        return self.now


class QueueTests(unittest.TestCase):
    def test_fifo_waiting_capacity_and_exact_timestamps(self):
        log = ListLog()
        queue = AccountingQueue(1, log, Clock())
        first = {"message": envelope("first")}
        second = {"message": envelope("second")}
        rejected = {"message": envelope("rejected")}
        self.assertTrue(queue.put(first))
        self.assertIs(queue.get(), first)
        self.assertTrue(queue.put(second))  # In-service job does not use waiting capacity.
        self.assertFalse(queue.put(rejected))
        terminal = queue.complete(first)
        self.assertIs(queue.get(), second)
        queue.complete(second)
        self.assertEqual(queue.state(), {"queue_len": 0, "in_service": 0, "admitted": 2,
                                        "completed": 2, "queue_dropped": 1, "aborted": 0, "pending": 0})
        first_events = {row["event"]: row for row in log.rows if row["id"] == "first"}
        self.assertEqual(first["admission_ns"], first_events["admit"]["time_ns"])
        self.assertEqual(first["dequeue_ns"], first_events["service_start"]["time_ns"])
        self.assertEqual(terminal, first_events["service_end"]["time_ns"])
        self.assertNotIn("admission_ns", rejected)
        with self.assertRaises(RuntimeError):
            queue.complete(first)

    def test_abort_retains_active_and_reconciles_admitted(self):
        queue = AccountingQueue(3, ListLog(), Clock())
        jobs = [{"message": envelope(str(index))} for index in range(3)]
        for job in jobs:
            queue.put(job)
        self.assertIs(queue.get(), jobs[0])
        self.assertEqual(queue.close(abort_waiting=True), jobs[1:])
        self.assertEqual(queue.state()["pending"], 1)
        queue.complete(jobs[0])
        state = queue.state()
        self.assertEqual(state["admitted"], state["completed"] + state["aborted"] + state["pending"])
        self.assertFalse(queue.put({"message": envelope("late")}))


class ProtocolTests(unittest.TestCase):
    def test_payload_and_ack_identity(self):
        message = envelope()
        self.assertEqual(decode_envelope(json_bytes(message)), message)
        message["payload_bytes"] = 4
        with self.assertRaises(ValueError):
            decode_envelope(json_bytes(message))
        ack = acknowledgement()
        self.assertEqual(validate_ack(ack, ack["id"]), ack)
        with self.assertRaises(ValueError):
            validate_ack(ack, "another-id")
        with self.assertRaises(ValueError):
            validate_ack({**ack, "outcome": "accepted"}, ack["id"])
        with self.assertRaises(ValueError):
            validate_ack({**ack, "worker_processed_ns": 0}, ack["id"])

    def test_no_retry_after_ambiguous_failure(self):
        connection = Mock(request_path="/", connection_id="c1")
        connection.request.side_effect = TimeoutError("response not known")
        result = request_once(connection, b"{}", "id")
        self.assertEqual(result["outcome"], "timeout")
        self.assertTrue(result["possibly_completed"])
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called_once()

    def test_response_must_be_matching_central_ack(self):
        connection = Mock(request_path="/", connection_id="c1")
        response = Mock(status=200)
        response.read.return_value = json_bytes(acknowledgement("wrong-id"))
        connection.getresponse.return_value = response
        result = request_once(connection, b"{}", "correct-id")
        self.assertEqual(result["outcome"], "ack_mismatch")
        self.assertEqual(connection.request.call_count, 1)
        response.read.return_value = json_bytes(acknowledgement("correct-id"))
        result = request_once(connection, b"{}", "correct-id")
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["body"], response.read.return_value)

    def test_congestion_uses_socket_and_fails_if_unsupported(self):
        sock = Mock()
        sock.getsockopt.return_value = b"cubic\0"
        with patch("lab.common.socket.TCP_CONGESTION", 13, create=True):
            self.assertEqual(configure_tcp(sock, "cubic"), "cubic")
            self.assertTrue(any(call.args[-1] == b"cubic" for call in sock.setsockopt.call_args_list))
            sock.setsockopt.side_effect = lambda level, option, value: (
                (_ for _ in ()).throw(OSError("unavailable")) if option == 13 else None)
            with self.assertRaises(RuntimeError):
                configure_tcp(sock, "reno")
        with self.assertRaises(ValueError):
            configure_tcp(Mock(), "cubic;sysctl")

    def test_schedule_has_fixed_rates_reproducible_independent_phases(self):
        config = {"warmup_s": 2, "duration_s": 8, "seed": 61061,
                  "traffic": {"odometry": {"rate_hz": 20, "payload_bytes": 250},
                              "sensor": {"rate_hz": 5, "payload_bytes": 20000}}}
        schedule, model = periodic_schedule(config, "robot1")
        self.assertEqual(schedule, periodic_schedule(config, "robot1")[0])
        self.assertNotEqual(schedule, periodic_schedule(config, "robot2")[0])
        self.assertEqual(sum(kind == "odometry" for _, kind, _ in schedule), 200)
        self.assertEqual(sum(kind == "sensor" for _, kind, _ in schedule), 50)
        self.assertTrue(all(0 <= stamp < 10e9 for stamp, _, _ in schedule))
        self.assertTrue(model["open_loop"])


def service_config(role):
    return {"run_id": "test", "worker_queue_capacity": 1, "worker_threads": 1,
            "worker_service_ms": 0, "request_timeout_s": 1,
            "tcp_congestion": {role: "system"},
            "nodes": {role + "1": {"role": role, "ip": "127.0.0.1",
                                     "backends": ["http://192.0.2.1:8080", "http://192.0.2.2:8080"]}}}


class ServiceTests(unittest.TestCase):
    def test_late_valid_ack_is_a_drain_timeout_with_worker_evidence_preserved(self):
        with tempfile.TemporaryDirectory() as temporary, patch("lab.components.probe_tcp", return_value="cubic"):
            config = {"run_id": "test", "queue_capacity": 1, "request_timeout_s": 1,
                      "tcp_congestion": {"robot": "system"},
                      "nodes": {"robot1": {"role": "robot", "target_url": "http://192.0.2.1:8080"}}}
            robot = Robot(config, "robot1", temporary)
            robot.deadline_ns = 1000
            robot.queue.put({"message": envelope(), "body": json_bytes(envelope())})
            def late_response(*_args):
                robot.stop.set()
                return {"outcome": "success", "response_ns": 1001, "request_ns": 900,
                        "ack": acknowledgement(), "possibly_completed": False}
            with patch("lab.components.time.monotonic_ns", return_value=900), patch(
                    "lab.components.request_once", side_effect=late_response):
                robot.sender()
            robot.finish(**robot.queue.state())
            rows = [json.loads(line) for line in (Path(temporary) / "gateway.jsonl").read_text().splitlines()]
            self.assertEqual(rows[0]["outcome"], "drain_timeout")
            self.assertEqual(rows[0]["original_outcome"], "success")
            self.assertTrue(rows[0]["possibly_completed"])
            self.assertEqual(rows[0]["worker_id"], "worker1")

    def test_proxy_round_robin_preserves_central_ack_and_never_acks_failure(self):
        with tempfile.TemporaryDirectory() as temporary, patch("lab.components.probe_tcp", return_value="cubic"):
            component = Service(service_config("proxy"), "proxy1", "proxy", temporary)
            handler = Mock(upstream={}, connection_id="downstream-1")
            message = envelope()
            body = json_bytes(message)
            ack_body = json_bytes(acknowledgement())
            success = {"outcome": "success", "http_status": 200, "request_ns": 110,
                       "response_ns": 120, "ack": acknowledgement(), "body": ack_body,
                       "connection_id": "upstream-1"}
            with patch("lab.components.request_once", return_value=success) as request:
                for _ in range(3):
                    self.assertEqual(component.forward(handler, message, body, 100, 105), (200, ack_body))
                urls = [call.args[0].url for call in request.call_args_list]
                self.assertEqual(urls, ["http://192.0.2.1:8080", "http://192.0.2.2:8080", "http://192.0.2.1:8080"])
                self.assertTrue(all(call.args[1] is body for call in request.call_args_list))
            with patch("lab.components.request_once", return_value={"outcome": "timeout", "possibly_completed": True}):
                status, response = component.forward(handler, message, body, 100, 105)
                self.assertEqual(status, 502)
                self.assertEqual(json.loads(response)["error"], "upstream_failure")
            component.finish(pending=0)

    def test_worker_ack_is_only_returned_after_service_completion(self):
        with tempfile.TemporaryDirectory() as temporary, patch("lab.components.probe_tcp", return_value="cubic"):
            config = service_config("worker")
            config["worker_service_ms"] = 1
            component = Service(config, "worker1", "worker", temporary)
            entered, release = threading.Event(), threading.Event()
            output = []
            def controlled_service(_delay):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("Test failed to release simulated service")
            def caller():
                output.append(component.process(envelope(), json_bytes(envelope()), 100, 101, "c1"))
            with patch("lab.components.time.sleep", side_effect=controlled_service):
                worker = threading.Thread(target=component.service_worker, args=(0,))
                request = threading.Thread(target=caller)
                worker.start()
                request.start()
                self.assertTrue(entered.wait(2))
                self.assertEqual(output, [])
                self.assertEqual(component.queue.state()["in_service"], 1)
                release.set()
                request.join(2)
                component.queue.close()
                component.stop.set()
                worker.join(2)
            self.assertFalse(request.is_alive())
            self.assertFalse(worker.is_alive())
            status, body = output[0]
            self.assertEqual(status, 200)
            ack = validate_ack(json.loads(body), envelope()["id"])
            self.assertEqual(ack["worker_id"], "worker1")
            component.finish(**component.queue.state())
            rows = [json.loads(line) for line in (Path(temporary) / "worker.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["processed_ns"], ack["worker_processed_ns"])
            self.assertEqual(rows[0]["outcome"], "processed")

    def test_worker_rejects_full_fifo_without_false_processed_ack(self):
        with tempfile.TemporaryDirectory() as temporary, patch("lab.components.probe_tcp", return_value="cubic"):
            component = Service(service_config("worker"), "worker1", "worker", temporary)
            component.queue.put({"message": envelope("already-waiting")})
            status, body = component.process(envelope(), json_bytes(envelope()), 100, 101, "c1")
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)["error"], "worker_queue_full")
            self.assertEqual(component.counts["processed"], 0)
            component.finish(**component.queue.state())


if __name__ == "__main__":
    unittest.main()
