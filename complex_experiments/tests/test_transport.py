"""Real TLS/TCP and QUIC loopback checks; run with the study venv on Linux."""
import asyncio
import pathlib
import socket
import subprocess
import tempfile
import time
import unittest
from h11 import RemoteProtocolError

from complex_experiments.transport import create_client, serve


class TransportTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.cert = str(pathlib.Path(cls.temp.name) / "certificate.pem")
        cls.key = str(pathlib.Path(cls.temp.name) / "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", cls.key, "-out", cls.cert, "-days", "1", "-subj", "/CN=complex.local",
            "-addext", "subjectAltName=DNS:complex.local"], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def port(self, protocol):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if protocol == "h3" else socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    async def exchange(self, protocol):
        events, requests = [], []
        async def app(body, headers):
            requests.append((body, headers))
            await asyncio.sleep(0.002)
            return (422 if headers["x-message-id"] == "invalid" else 200), body[::-1]
        port = self.port(protocol)
        async with await serve(protocol, "127.0.0.1", port, self.cert, self.key, app, events.append):
            async with await create_client(protocol, "127.0.0.1", port, self.cert,
                                           max_concurrency=4, event_callback=events.append) as client:
                bodies = [bytes([i]) * size for i, size in enumerate([0, 64, 90000, 600000] * 4)]
                responses = await asyncio.wait_for(asyncio.gather(*[
                    client.post(body, f"id-{i}") for i, body in enumerate(bodies)]), 20)
                for response, body in zip(responses, bodies):
                    self.assertEqual((response.status, response.body), (200, body[::-1]))
                    self.assertEqual(response.protocol, {"h1": "http/1.1", "h2": "h2", "h3": "h3"}[protocol])
                rejected = await client.post(b"invalid", "invalid")
                self.assertEqual(rejected.status, 422)
                self.assertEqual(len({r.connection_id for r in responses}), 4 if protocol == "h1" else 1)
                self.assertEqual(len({(r.connection_id, r.stream_id) for r in responses}), 16)
        self.assertEqual(len(requests), 17)
        connections = [e for e in events if e["event"] == "connection" and e["role"] == "client"]
        self.assertEqual(len(connections), 4 if protocol == "h1" else 1)
        self.assertTrue(all(e["tls"] == "TLSv1.3" for e in connections))
        self.assertTrue(all(e["alpn"] == {"h1": "http/1.1", "h2": "h2", "h3": "h3"}[protocol] for e in connections))
        self.assertTrue(all(h["x-transport-protocol"] == {"h1": "http/1.1", "h2": "h2", "h3": "h3"}[protocol] for _, h in requests))

    async def test_h1_pool_real_tls_body_integrity_and_reuse(self):
        await self.exchange("h1")

    async def test_h2_multiplex_real_tls_body_integrity_and_flow_control(self):
        await self.exchange("h2")

    async def test_h3_multiplex_real_quic_body_integrity_and_flow_control(self):
        await self.exchange("h3")

    async def test_certificate_verification_on_all_protocols(self):
        async def app(body, headers):
            return 200, body
        for protocol in ("h1", "h2", "h3"):
            with self.subTest(protocol=protocol):
                port = self.port(protocol)
                async with await serve(protocol, "127.0.0.1", port, self.cert, self.key, app):
                    with self.assertRaises(Exception):
                        await create_client(protocol, "127.0.0.1", port, self.cert,
                                            server_name="wrong.invalid", max_concurrency=1, connect_timeout=3)

    async def test_no_h2_to_h1_fallback(self):
        async def app(body, headers):
            return 200, body
        port = self.port("h1")
        async with await serve("h1", "127.0.0.1", port, self.cert, self.key, app):
            with self.assertRaises(ConnectionError):
                await create_client("h2", "127.0.0.1", port, self.cert, max_concurrency=1)

    async def test_cancelled_multiplex_stream_does_not_retry_or_break_peer(self):
        for protocol in ("h2", "h3"):
            with self.subTest(protocol=protocol):
                calls = []
                async def app(body, headers):
                    calls.append(headers["x-message-id"])
                    if body == b"slow":
                        await asyncio.sleep(0.2)
                    return 200, body
                port = self.port(protocol)
                async with await serve(protocol, "127.0.0.1", port, self.cert, self.key, app):
                    async with await create_client(protocol, "127.0.0.1", port, self.cert, max_concurrency=2) as client:
                        task = asyncio.create_task(client.post(b"slow", "cancelled"))
                        await asyncio.sleep(0.03)
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        response = await asyncio.wait_for(client.post(b"other", "healthy"), 3)
                        self.assertEqual(response.body, b"other")
                        await asyncio.sleep(0.25)
                self.assertEqual(calls.count("cancelled"), 1)
                self.assertEqual(calls.count("healthy"), 1)

    async def test_cancelled_h1_request_is_terminal_and_next_request_gets_new_connection(self):
        calls, events = [], []
        started = asyncio.Event()
        async def app(body, headers):
            calls.append(headers["x-message-id"])
            if body == b"slow":
                started.set()
                await asyncio.sleep(.2)
            return 200, body
        port = self.port("h1")
        async with await serve("h1", "127.0.0.1", port, self.cert, self.key, app):
            async with await create_client("h1", "127.0.0.1", port, self.cert,
                                            max_concurrency=1, event_callback=events.append) as client:
                old_id = client.connections[0].cid
                task = asyncio.create_task(client.post(b"slow", "cancelled-once"))
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                response = await asyncio.wait_for(client.post(b"next", "new-operation"), 3)
                self.assertEqual((response.status, response.body), (200, b"next"))
                self.assertNotEqual(response.connection_id, old_id)
                self.assertEqual(len(client.connections), 2)
                await asyncio.sleep(.25)
        self.assertEqual(calls, ["cancelled-once", "new-operation"])
        starts = [e for e in events if e["event"] == "request_start"]
        self.assertEqual([e["message_id"] for e in starts], calls)
        replacements = [e for e in events if e["event"] == "connection_replacement"]
        self.assertEqual(len(replacements), 1)
        self.assertEqual(replacements[0]["triggering_message_id"], "new-operation")
        self.assertFalse(replacements[0]["failed_operation_resent"])

    async def test_whole_connection_failure_reconnects_only_for_new_operations(self):
        for protocol in ("h1", "h2", "h3"):
            with self.subTest(protocol=protocol):
                calls, events = [], []
                async def app(body, headers):
                    calls.append(headers["x-message-id"])
                    if body == b"cut":
                        for connection in list(server.connections):
                            if protocol == "h3":
                                connection.close(error_code=0x100, reason_phrase="test connection failure")
                                connection.transmit()
                            else:
                                connection.writer.close()
                        await asyncio.sleep(.05)
                    return 200, body
                port = self.port(protocol)
                async with await serve(protocol, "127.0.0.1", port, self.cert, self.key, app) as server:
                    async with await create_client(protocol, "127.0.0.1", port, self.cert,
                                                    max_concurrency=1, event_callback=events.append) as client:
                        old_id = client.connections[0].cid
                        with self.assertRaises((ConnectionError, RemoteProtocolError)):
                            await asyncio.wait_for(client.post(b"cut", "failed-once"), 3)
                        responses = await asyncio.wait_for(asyncio.gather(*[
                            client.post(b"new", f"new-{i}") for i in range(3)]), 5)
                        self.assertTrue(all(r.body == b"new" and r.connection_id != old_id for r in responses))
                        self.assertEqual(len(client.connections), 2)
                self.assertEqual(calls.count("failed-once"), 1)
                self.assertEqual(len(calls), 4)
                replacements = [e for e in events if e["event"] == "connection_replacement"]
                self.assertEqual(len(replacements), 1)

    async def test_h1_pool_shutdown_waits_in_parallel_and_records_forced_cleanup(self):
        events = []
        async def app(body, headers):
            return 200, body
        async def stalled_wait_closed():
            await asyncio.Future()
        port = self.port("h1")
        async with await serve("h1", "127.0.0.1", port, self.cert, self.key, app):
            client = await create_client("h1", "127.0.0.1", port, self.cert,
                                         max_concurrency=8, event_callback=events.append)
            original_waiters = []
            for connection in client.connections:
                original_waiters.append(connection.writer.wait_closed)
                connection.writer.wait_closed = stalled_wait_closed
            before = time.monotonic()
            report = await client.close()
            elapsed = time.monotonic()-before
            self.assertLess(elapsed, 3.5, "Eight 2-second waits should run together")
            self.assertTrue(report["forced_cleanup"])
            self.assertEqual(len(report["forced_connection_ids"]), 8)
            self.assertFalse(report["overall_timeout"])
            self.assertTrue(all(c.writer.is_closing() for c in client.connections))
            await asyncio.gather(*(waiter() for waiter in original_waiters), return_exceptions=True)
        self.assertEqual([e["event"] for e in events].count("client_shutdown"), 1)


if __name__ == "__main__":
    unittest.main()
