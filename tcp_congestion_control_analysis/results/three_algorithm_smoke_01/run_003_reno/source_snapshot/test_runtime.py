"""Small checks for the shared transport, message accounting and cleanup rules."""

import asyncio
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from https_protocol_analysis.components import Node, message, schedule
from https_protocol_analysis.metrics import queue_mean
from https_protocol_analysis.network import LabNetwork
from https_protocol_analysis.runner import validate_config
from https_protocol_analysis.settings import make_config
from https_protocol_analysis.transport import create_client, serve


class RuntimeTests(unittest.TestCase):
    def test_schedule_preserves_total_rate_and_is_repeatable(self):
        config = make_config("http", load=148, seconds=2)
        config["warmup_s"] = 1
        events = [event for i in range(1, 5) for event in schedule(config, f"robot{i}")]
        self.assertEqual(len(events), 148 * 3)
        self.assertEqual(schedule(config, "robot1"), schedule(config, "robot1"))
        self.assertEqual({event[1] for event in events}, {"command", "status", "odometry", "sensor", "video"})

    def test_queue_mean_keeps_carry_in_and_clips_boundaries(self):
        events = [{"time_ns": 1, "queue_len": 2}, {"time_ns": 3, "queue_len": 0}]
        self.assertEqual(queue_mean(events, 2, 4), 1)

    def test_invalid_network_and_protocol_settings_are_rejected(self):
        config = make_config("http")
        config["sites"]["A"]["forward"]["loss_pct"] = 101
        with self.assertRaises(ValueError):
            validate_config(config)
        config = make_config("http", protocol="h3")
        config["tcp_congestion"]["edge"] = "reno"
        with self.assertRaisesRegex(ValueError, "QUIC"):
            validate_config(config)

    def test_failed_network_command_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            network = LabNetwork({}, directory)
            result = subprocess.CompletedProcess([], 1, "", "Controlled failure")
            with patch("https_protocol_analysis.network.subprocess.run", return_value=result):
                with self.assertRaisesRegex(RuntimeError, "Controlled failure"):
                    network.run(["ip", "link", "show"])

    def test_profile_readback_converts_kernel_units_and_detects_wrong_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            network = LabNetwork({}, directory)
            network.nodes = {"routerA": {"namespace": "test-router"}}
            rows = [{"kind": "netem", "options": {"delay": {"delay": .03},
                     "loss-random": {"loss": .01}, "rate": {"rate": 250000}}}]
            profile = dict(delay_ms=30, loss_pct=1, rate_mbit=2)
            with patch.object(network, "read_json", return_value=rows):
                network.check_profile("A", "wan0", profile)
                with self.assertRaisesRegex(RuntimeError, "rate_mbit"):
                    network.check_profile("A", "wan0", dict(profile, rate_mbit=4))
                with self.assertRaisesRegex(RuntimeError, "not removed"):
                    network.check_profile("A", "wan0", {})

    def test_cleanup_refuses_a_changed_interface_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            network = LabNetwork({}, directory)
            network.created_links = ["test-link"]
            result = subprocess.CompletedProcess([], 0, '[{"ifalias":"another-run"}]', "")
            with patch.object(network, "run", return_value=result) as run:
                with self.assertRaisesRegex(RuntimeError, "ownership changed"):
                    network.cleanup()
                self.assertEqual(run.call_count, 1)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_failure_resolves_the_queue_instead_of_hanging(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config("http")
            config.update(nodes={"robot1": {"role": "robot"}}, start_file=str(Path(directory) / "start.json"))
            node = Node(config, "robot1", Path(directory) / "robot")
            class FailedClient:
                async def post(self, *args):
                    raise TimeoutError("Controlled request failure")
            reply = node.admit(message("test", "status", b"data"))
            node.tasks.append(asyncio.create_task(node.forward(FailedClient())))
            status, _ = await asyncio.wait_for(reply, 1)
            self.assertEqual(status, 504)
            await asyncio.wait_for(node.finish(), 1)
            node.close_logs()
            rows = (node.output / "gateway.jsonl").read_text().splitlines()
            self.assertEqual(json.loads(rows[0])["outcome"], "timeout")

    async def test_shutdown_accounts_for_a_stalled_request(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config("http")
            config.update(nodes={"robot1": {"role": "robot"}}, finish_seconds=.03,
                          start_file=str(Path(directory) / "start.json"))
            node = Node(config, "robot1", Path(directory) / "robot")
            class StalledClient:
                async def post(self, *args):
                    await asyncio.Future()
            reply = node.admit(message("test", "status", b"data"))
            node.tasks.append(asyncio.create_task(node.forward(StalledClient())))
            await asyncio.wait_for(node.finish(), 1)
            self.assertTrue(reply.done())
            self.assertFalse(node.active)
            node.close_logs()

    async def test_real_http_versions_and_socket_algorithm(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=complex.local",
                "-addext", "subjectAltName=DNS:complex.local,IP:127.0.0.1"], check=True, capture_output=True)
            for protocol in ("h1", "h2", "h3"):
                with self.subTest(protocol=protocol):
                    kind = socket.SOCK_DGRAM if protocol == "h3" else socket.SOCK_STREAM
                    with socket.socket(socket.AF_INET, kind) as sock:
                        sock.bind(("127.0.0.1", 0))
                        port = sock.getsockname()[1]
                    async def app(body, headers):
                        return 200, body[::-1]
                    server = await serve(protocol, "127.0.0.1", port, str(cert), str(key), app)
                    records = []
                    client = await create_client(protocol, "127.0.0.1", port, str(cert),
                        algorithm="reno" if protocol != "h3" else "cubic", callback=records.append,
                        max_concurrency=3)
                    try:
                        bodies = [b"small", b"x" * 90000, b""]
                        replies = await asyncio.wait_for(asyncio.gather(*[
                            client.post(body, str(index)) for index, body in enumerate(bodies)]), 10)
                        self.assertEqual(replies, [(200, body[::-1]) for body in bodies])
                        if protocol != "h3":
                            connections = [row for row in records if row["event"] == "outgoing_connection"]
                            self.assertTrue(connections)
                            self.assertTrue(all(row["tcp_congestion_actual"] == "reno" for row in connections))
                            self.assertTrue(any(row["event"] == "tcp_sample" for row in records))
                    finally:
                        await client.close()
                        await server.close()


if __name__ == "__main__":
    unittest.main()
