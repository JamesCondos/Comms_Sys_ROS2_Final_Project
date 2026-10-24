"""Strict, persistent HTTP transports for the controlled complex-network study.

H1 uses a fixed pool of serial connections; H2 and H3 multiplex the same maximum
number of requests on one connection. All use TLS 1.3, verified certificates,
and the exact requested ALPN. There are no application retries or fallbacks.
Failed connections are replaced only when a later, distinct operation needs one;
the operation that failed is never resent.
Only complete request bodies are passed to the application.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import socket
import ssl
import time
import uuid
from typing import Awaitable, Callable

import h11
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import (ConnectionTerminated as H2Terminated, DataReceived as H2Data,
                       RequestReceived, ResponseReceived, StreamEnded, StreamReset,
                       WindowUpdated)
from h2.settings import SettingCodes
from aioquic.asyncio import QuicConnectionProtocol
from aioquic.asyncio.client import connect as quic_connect
from aioquic.asyncio.server import serve as quic_serve
from aioquic.h3.connection import H3Connection
from aioquic.h3.events import DataReceived as H3Data, HeadersReceived as H3Headers
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import (ConnectionTerminated as QuicTerminated,
                                 ProtocolNegotiated, StreamReset as QuicReset)
from aioquic.quic.packet import QuicProtocolVersion

App = Callable[[bytes, dict[str, str]], Awaitable[tuple[int, bytes]]]
ALPNS = {"h1": "http/1.1", "h2": "h2", "h3": "h3"}


@dataclass
class Response:
    status: int
    body: bytes
    protocol: str
    connection_id: str
    stream_id: int


def _emit(callback, kind, **fields):
    if callback:
        callback({"event": kind, "time_ns": time.monotonic_ns(), **fields})


def _context(protocol, *, certfile=None, keyfile=None, cafile=None):
    if certfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
    else:
        ctx = ssl.create_default_context(cafile=cafile)
    ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    ctx.set_alpn_protocols([ALPNS[protocol]])
    return ctx


def _tcp_evidence(writer, protocol, cid, callback, role):
    tls = writer.get_extra_info("ssl_object")
    if tls.selected_alpn_protocol() != ALPNS[protocol] or tls.version() != "TLSv1.3":
        raise ConnectionError("Required HTTP ALPN / TLS 1.3 was not negotiated")
    sock = writer.get_extra_info("socket")
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if hasattr(socket, "TCP_CONGESTION"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, b"cubic")
        congestion = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, 64).split(b"\0")[0].decode()
    else:
        congestion = "platform_default"
    _emit(callback, "connection", protocol=protocol, connection_id=cid, role=role,
          alpn=tls.selected_alpn_protocol(), tls=tls.version(), cipher=tls.cipher()[0],
          local=writer.get_extra_info("sockname"), peer=writer.get_extra_info("peername"),
          congestion=congestion)


async def _call(app, body, headers):
    try:
        status, output = await app(body, headers)
        return int(status), bytes(output)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Observable HTTP 500, never silently acknowledge a failed application.
        return 500, b'{"error":"application_failure"}'


class _H1Connection:
    def __init__(self, reader, writer, callback=None, role="client"):
        self.reader, self.writer = reader, writer
        self.conn = h11.Connection(h11.CLIENT if role == "client" else h11.SERVER,
                                  max_incomplete_event_size=131072)
        self.cid = uuid.uuid4().hex
        self.callback = callback
        self.counter = 0
        self.usable = True
        _tcp_evidence(writer, "h1", self.cid, callback, role)

    async def next(self):
        while True:
            event = self.conn.next_event()
            if event is not h11.NEED_DATA:
                return event
            data = await self.reader.read(65536)
            self.conn.receive_data(data)

    def send(self, event):
        data = self.conn.send(event)
        if data:
            self.writer.write(data)

    async def post(self, body, message_id, authority):
        self.counter += 1
        sid = self.counter
        _emit(self.callback, "request_start", protocol="h1", connection_id=self.cid,
              stream_id=sid, message_id=message_id, body_bytes=len(body))
        try:
            self.send(h11.Request(method=b"POST", target=b"/message", headers=[
                (b"host", authority.encode()), (b"content-length", str(len(body)).encode()),
                (b"content-type", b"application/octet-stream"),
                (b"x-message-id", message_id.encode())]))
            self.send(h11.Data(data=body))
            self.send(h11.EndOfMessage())
            await self.writer.drain()
            status, parts = 0, []
            while True:
                event = await self.next()
                if isinstance(event, h11.Response):
                    status = event.status_code
                elif isinstance(event, h11.Data):
                    parts.append(event.data)
                elif isinstance(event, h11.EndOfMessage):
                    self.conn.start_next_cycle()
                    _emit(self.callback, "request_end", protocol="h1", connection_id=self.cid,
                          stream_id=sid, message_id=message_id, status=status)
                    return Response(status, b"".join(parts), "http/1.1", self.cid, sid)
                elif isinstance(event, h11.ConnectionClosed):
                    raise ConnectionError("H1 connection closed before response")
        except BaseException:
            # A cancelled partial exchange cannot safely be reused. The failed
            # operation is terminal; only a later operation may get a new socket.
            self.usable = False
            self.writer.close()
            raise

    async def server_loop(self, app):
        while True:
            headers, parts = {}, []
            while True:
                event = await self.next()
                if isinstance(event, h11.ConnectionClosed):
                    return
                if isinstance(event, h11.Request):
                    headers = {k.decode().lower(): v.decode() for k, v in event.headers}
                    headers[":method"] = event.method.decode()
                    headers[":path"] = event.target.decode()
                elif isinstance(event, h11.Data):
                    parts.append(event.data)
                elif isinstance(event, h11.EndOfMessage):
                    break
            self.counter += 1
            headers.update({"x-transport-protocol": "http/1.1", "x-transport-connection": self.cid,
                            "x-transport-stream": str(self.counter)})
            status, output = await _call(app, b"".join(parts), headers)
            self.send(h11.Response(status_code=status, headers=[
                (b"content-length", str(len(output)).encode()),
                (b"content-type", b"application/octet-stream")]))
            self.send(h11.Data(data=output))
            self.send(h11.EndOfMessage())
            await self.writer.drain()
            self.conn.start_next_cycle()


class _H2Connection:
    def __init__(self, reader, writer, callback=None, app=None):
        self.reader, self.writer, self.callback, self.app = reader, writer, callback, app
        self.cid = uuid.uuid4().hex
        self.conn = H2Connection(config=H2Configuration(client_side=app is None, header_encoding="utf-8"))
        self.pending, self.headers, self.bodies = {}, {}, {}
        self.flow = asyncio.Event()
        self.tasks = set()
        self.error = None
        _tcp_evidence(writer, "h2", self.cid, callback, "server" if app else "client")
        self.conn.initiate_connection()
        self.conn.update_settings({SettingCodes.INITIAL_WINDOW_SIZE: 16 * 1024 * 1024,
                                   SettingCodes.MAX_CONCURRENT_STREAMS: 1024})
        self.conn.increment_flow_control_window(16 * 1024 * 1024)
        self.flush()
        self.reader_task = asyncio.create_task(self.read_loop())

    def flush(self):
        data = self.conn.data_to_send()
        if data:
            self.writer.write(data)

    async def send_body(self, sid, body):
        if not body:
            self.conn.end_stream(sid)
            self.flush()
            return
        offset = 0
        while offset < len(body):
            self.flow.clear()
            if self.error:
                raise ConnectionError(str(self.error))
            size = min(self.conn.local_flow_control_window(sid),
                       self.conn.max_outbound_frame_size, len(body) - offset)
            if size <= 0:
                await self.flow.wait()
                continue
            self.conn.send_data(sid, body[offset:offset + size], end_stream=offset + size == len(body))
            offset += size
            self.flush()
            await self.writer.drain()

    async def post(self, body, message_id, authority):
        if self.error:
            raise ConnectionError(str(self.error))
        sid = self.conn.get_next_available_stream_id()
        future = asyncio.get_running_loop().create_future()
        self.pending[sid] = future
        self.headers[sid], self.bodies[sid] = {}, []
        self.conn.send_headers(sid, [(":method", "POST"), (":scheme", "https"),
            (":authority", authority), (":path", "/message"),
            ("content-length", str(len(body))), ("content-type", "application/octet-stream"),
            ("x-message-id", message_id)])
        _emit(self.callback, "request_start", protocol="h2", connection_id=self.cid,
              stream_id=sid, message_id=message_id, body_bytes=len(body))
        self.flush()
        try:
            await self.send_body(sid, body)
            response = await future
            _emit(self.callback, "request_end", protocol="h2", connection_id=self.cid,
                  stream_id=sid, message_id=message_id, status=response.status)
            return response
        except BaseException:
            self.pending.pop(sid, None)
            self.headers.pop(sid, None)
            self.bodies.pop(sid, None)
            try:
                self.conn.reset_stream(sid)
                self.flush()
            except Exception:
                pass
            raise

    async def respond(self, sid, body, headers):
        headers.update({"x-transport-protocol": "h2", "x-transport-connection": self.cid,
                        "x-transport-stream": str(sid)})
        status, output = await _call(self.app, body, headers)
        try:
            self.conn.send_headers(sid, [(":status", str(status)),
                ("content-length", str(len(output))), ("content-type", "application/octet-stream")])
            self.flush()
            await self.send_body(sid, output)
        except Exception as exc:
            _emit(self.callback, "response_send_error", protocol="h2", connection_id=self.cid,
                  stream_id=sid, error=repr(exc))

    async def read_loop(self):
        try:
            while True:
                data = await self.reader.read(65536)
                if not data:
                    raise ConnectionError("H2 connection closed")
                for event in self.conn.receive_data(data):
                    sid = getattr(event, "stream_id", None)
                    if isinstance(event, (RequestReceived, ResponseReceived)):
                        self.headers[sid] = dict(event.headers)
                        self.bodies[sid] = []
                    elif isinstance(event, H2Data):
                        if sid in self.bodies:
                            self.bodies[sid].append(event.data)
                        self.conn.acknowledge_received_data(event.flow_controlled_length, sid)
                    elif isinstance(event, StreamEnded):
                        headers = self.headers.pop(sid, {})
                        body = b"".join(self.bodies.pop(sid, []))
                        if self.app:
                            task = asyncio.create_task(self.respond(sid, body, headers))
                            self.tasks.add(task)
                            task.add_done_callback(self.tasks.discard)
                        else:
                            future = self.pending.pop(sid, None)
                            if future is not None and not future.done():
                                future.set_result(Response(int(headers[":status"]), body, "h2", self.cid, sid))
                    elif isinstance(event, StreamReset):
                        future = self.pending.pop(sid, None)
                        self.headers.pop(sid, None)
                        self.bodies.pop(sid, None)
                        if future is not None and not future.done():
                            future.set_exception(ConnectionError(f"H2 stream reset: {event.error_code}"))
                    elif isinstance(event, WindowUpdated):
                        self.flow.set()
                    elif isinstance(event, H2Terminated):
                        raise ConnectionError(f"H2 terminated: {event.error_code}")
                self.flush()
        except BaseException as exc:
            self.error = exc
            self.flow.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError(str(exc)))
            self.pending.clear()
            self.writer.close()

    async def close(self):
        self.reader_task.cancel()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(self.reader_task, *list(self.tasks), return_exceptions=True)
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), 2)
            return {"connection_id": self.cid, "forced": False}
        except asyncio.TimeoutError:
            self.writer.transport.abort()
            return {"connection_id": self.cid, "forced": True, "reason": "tls_shutdown_timeout"}
        except Exception as exc:
            # A connection that already failed may report its original error.
            return {"connection_id": self.cid, "forced": False, "closed_with_error": repr(exc)}


class _H3Protocol(QuicConnectionProtocol):
    def __init__(self, *args, app=None, event_callback=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.app, self.callback = app, event_callback
        self.cid = uuid.uuid4().hex
        self.http = H3Connection(self._quic)
        self.pending, self.headers, self.bodies = {}, {}, {}
        self.tasks = set()
        self.negotiated = None
        self.error = None

    def quic_event_received(self, event):
        if isinstance(event, ProtocolNegotiated):
            self.negotiated = event.alpn_protocol
            if self.negotiated != "h3":
                self.error = ConnectionError("H3 ALPN required")
                self.close()
            _emit(self.callback, "connection", protocol="h3", connection_id=self.cid,
                  role="server" if self.app else "client", alpn=self.negotiated,
                  tls="TLSv1.3", quic_version=int(self._quic._version), congestion="cubic")
        if isinstance(event, QuicTerminated):
            self.error = ConnectionError(f"QUIC terminated: {event.error_code} {event.reason_phrase}")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(self.error)
            self.pending.clear()
            return
        if isinstance(event, QuicReset):
            future = self.pending.pop(event.stream_id, None)
            self.headers.pop(event.stream_id, None)
            self.bodies.pop(event.stream_id, None)
            if future is not None and not future.done():
                future.set_exception(ConnectionError(f"H3 stream reset: {event.error_code}"))
        for item in self.http.handle_event(event):
            if not isinstance(item, (H3Headers, H3Data)):
                continue
            sid = item.stream_id
            if isinstance(item, H3Headers):
                self.headers[sid] = {k.decode(): v.decode() for k, v in item.headers}
                self.bodies.setdefault(sid, [])
            else:
                self.bodies.setdefault(sid, []).append(item.data)
            if item.stream_ended:
                headers = self.headers.pop(sid, {})
                body = b"".join(self.bodies.pop(sid, []))
                if self.app:
                    task = asyncio.create_task(self.respond(sid, body, headers))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                else:
                    future = self.pending.pop(sid, None)
                    if future is not None and not future.done():
                        future.set_result(Response(int(headers[":status"]), body, "h3", self.cid, sid))

    async def respond(self, sid, body, headers):
        headers.update({"x-transport-protocol": "h3", "x-transport-connection": self.cid,
                        "x-transport-stream": str(sid)})
        status, output = await _call(self.app, body, headers)
        try:
            self.http.send_headers(sid, [(b":status", str(status).encode()),
                (b"content-length", str(len(output)).encode()), (b"content-type", b"application/octet-stream")])
            self.http.send_data(sid, output, end_stream=True)
            self.transmit()
        except Exception as exc:
            _emit(self.callback, "response_send_error", protocol="h3", connection_id=self.cid,
                  stream_id=sid, error=repr(exc))

    async def post(self, body, message_id, authority):
        if self.error or self.negotiated != "h3":
            raise ConnectionError(str(self.error or "H3 not negotiated"))
        sid = self._quic.get_next_available_stream_id()
        future = asyncio.get_running_loop().create_future()
        self.pending[sid] = future
        self.headers[sid], self.bodies[sid] = {}, []
        self.http.send_headers(sid, [(b":method", b"POST"), (b":scheme", b"https"),
            (b":authority", authority.encode()), (b":path", b"/message"),
            (b"content-length", str(len(body)).encode()), (b"content-type", b"application/octet-stream"),
            (b"x-message-id", message_id.encode())])
        self.http.send_data(sid, body, end_stream=True)
        _emit(self.callback, "request_start", protocol="h3", connection_id=self.cid,
              stream_id=sid, message_id=message_id, body_bytes=len(body))
        self.transmit()
        try:
            response = await future
            _emit(self.callback, "request_end", protocol="h3", connection_id=self.cid,
                  stream_id=sid, message_id=message_id, status=response.status)
            return response
        except BaseException:
            self.pending.pop(sid, None)
            self.headers.pop(sid, None)
            self.bodies.pop(sid, None)
            self._quic.reset_stream(sid, 0x10C)
            self.transmit()
            raise


def _quic_config(*, client, certfile=None, keyfile=None, cafile=None, server_name=None):
    config = QuicConfiguration(is_client=client, alpn_protocols=["h3"],
        congestion_control_algorithm="cubic", idle_timeout=120,
        max_data=64 * 1024 * 1024, max_stream_data=16 * 1024 * 1024,
        supported_versions=[QuicProtocolVersion.VERSION_1])
    if client:
        config.verify_mode = ssl.CERT_REQUIRED
        config.load_verify_locations(cafile=cafile)
        config.server_name = server_name
    else:
        config.load_cert_chain(certfile, keyfile)
    return config


class TransportClient:
    def __init__(self, protocol, host, port, max_concurrency):
        self.protocol, self.host, self.port = protocol, host, port
        self.authority = f"{host}:{port}"
        self.limit = asyncio.Semaphore(max_concurrency)
        self.pool = asyncio.Queue()
        self.connections = []
        self.quic_context = None
        self.quic_contexts = []
        self.active_connection = None
        self.reconnect_lock = asyncio.Lock()
        self.factory = None
        self.callback = None
        self.closed = False

    def healthy(self, connection):
        if isinstance(connection, _H1Connection):
            return connection.usable and not connection.writer.is_closing() and not connection.reader.at_eof()
        if isinstance(connection, _H2Connection):
            return connection.error is None and not connection.writer.is_closing()
        return connection.error is None

    async def replace(self, previous, message_id):
        # Opening this socket is part of the new operation's elapsed time. No
        # failed request body is retained or submitted by this recovery path.
        connection, context = await self.factory()
        self.connections.append(connection)
        if context is not None:
            self.quic_contexts.append(context)
            self.quic_context = context
        _emit(self.callback, "connection_replacement", protocol=self.protocol,
              previous_connection_id=previous.cid, connection_id=connection.cid,
              triggering_message_id=message_id, reason="prior_connection_unusable",
              failed_operation_resent=False)
        return connection

    async def post(self, body: bytes, message_id: str) -> Response:
        if self.closed:
            raise ConnectionError("Client closed")
        async with self.limit:
            if self.protocol == "h1":
                connection = await self.pool.get()
                try:
                    if not self.healthy(connection):
                        connection = await self.replace(connection, message_id)
                    return await connection.post(body, message_id, self.authority)
                finally:
                    self.pool.put_nowait(connection)
            if not self.healthy(self.active_connection):
                async with self.reconnect_lock:
                    if not self.healthy(self.active_connection):
                        self.active_connection = await self.replace(self.active_connection, message_id)
            return await self.active_connection.post(body, message_id, self.authority)

    async def close(self):
        if self.closed:
            return getattr(self, "shutdown_report", None)
        self.closed = True
        # Start all TLS closes together: one congested pool must never consume
        # pool_size * timeout. No application operation is issued during close.
        for connection in self.connections:
            if isinstance(connection, _H1Connection):
                connection.writer.close()

        async def close_h1(connection):
            try:
                await asyncio.wait_for(connection.writer.wait_closed(), 2)
                return {"connection_id": connection.cid, "forced": False}
            except asyncio.TimeoutError:
                connection.writer.transport.abort()
                return {"connection_id": connection.cid, "forced": True, "reason": "tls_shutdown_timeout"}
            except Exception as exc:
                return {"connection_id": connection.cid, "forced": False, "closed_with_error": repr(exc)}

        async def close_quic(context, connection):
            try:
                await asyncio.wait_for(context.__aexit__(None, None, None), 8)
                return {"connection_id": connection.cid, "forced": False}
            except BaseException as exc:
                # aioquic's context closes UDP after wait_closed; a timed-out
                # close needs an explicit local socket/timer cleanup instead.
                force_quic(connection)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return {"connection_id": connection.cid, "forced": True, "reason": repr(exc)}

        def force_quic(connection):
            for name in ("_timer", "_transmit_task"):
                handle = getattr(connection, name, None)
                if handle is not None:
                    handle.cancel()
            if connection._transport is not None:
                connection._transport.close()
            connection.error = ConnectionError("Forced local QUIC cleanup")

        jobs = []
        for connection in self.connections:
            if isinstance(connection, _H1Connection):
                jobs.append(close_h1(connection))
            elif isinstance(connection, _H2Connection):
                jobs.append(connection.close())
        quic_connections = [c for c in self.connections if isinstance(c, _H3Protocol)]
        for context, connection in zip(self.quic_contexts, quic_connections):
            jobs.append(close_quic(context, connection))
        overall_forced = False
        try:
            details = await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), 10)
        except asyncio.TimeoutError:
            overall_forced = True
            details = []
            for connection in self.connections:
                if isinstance(connection, _H3Protocol):
                    force_quic(connection)
                else:
                    connection.writer.transport.abort()
        self.shutdown_report = dict(protocol=self.protocol, connections_total=len(self.connections),
            overall_timeout=overall_forced,
            forced_connection_ids=[row["connection_id"] for row in details if isinstance(row, dict) and row.get("forced")],
            cleanup_errors=[repr(row) for row in details if isinstance(row, BaseException)],
            details=[row for row in details if isinstance(row, dict)])
        self.shutdown_report["forced_cleanup"] = overall_forced or bool(self.shutdown_report["forced_connection_ids"])
        _emit(self.callback, "client_shutdown", **self.shutdown_report)
        return self.shutdown_report

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


async def create_client(protocol: str, host: str, port: int, cafile: str,
                        server_name: str = "complex.local", max_concurrency: int = 16,
                        event_callback=None, connect_timeout: float = 15) -> TransportClient:
    """Connect and verify every persistent connection before returning."""
    if protocol not in ALPNS or max_concurrency < 1:
        raise ValueError("Require protocol h1/h2/h3 and positive concurrency")
    result = TransportClient(protocol, host, port, max_concurrency)
    result.callback = event_callback
    ctx = _context(protocol, cafile=cafile) if protocol != "h3" else None

    async def open_connection():
        if protocol == "h3":
            context = quic_connect(host, port,
                configuration=_quic_config(client=True, cafile=cafile, server_name=server_name),
                create_protocol=lambda *a, **kw: _H3Protocol(*a, event_callback=event_callback, **kw),
                wait_connected=True)
            connection = await asyncio.wait_for(context.__aenter__(), connect_timeout)
            if connection.negotiated != "h3":
                await context.__aexit__(None, None, None)
                raise ConnectionError("H3 ALPN not negotiated")
            return connection, context
        reader, writer = await asyncio.wait_for(asyncio.open_connection(
            host, port, ssl=ctx, server_hostname=server_name), connect_timeout)
        try:
            connection = (_H1Connection(reader, writer, event_callback) if protocol == "h1"
                          else _H2Connection(reader, writer, event_callback))
        except BaseException:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except Exception:
                pass
            raise
        return connection, None

    result.factory = open_connection
    try:
        for _ in range(max_concurrency if protocol == "h1" else 1):
            connection, context = await open_connection()
            result.connections.append(connection)
            result.active_connection = connection
            if context is not None:
                result.quic_context = context
                result.quic_contexts.append(context)
            result.pool.put_nowait(connection)
        return result
    except BaseException:
        await result.close()
        raise


class TransportServer:
    def __init__(self):
        self.listener = None
        self.connections = set()
        self.tasks = set()
        self.quic = False

    async def close(self):
        if self.quic:
            self.listener.close()
            for connection in list(self.connections):
                for task in list(connection.tasks):
                    task.cancel()
                await asyncio.gather(*list(connection.tasks), return_exceptions=True)
            return
        self.listener.close()
        await self.listener.wait_closed()
        for connection in list(self.connections):
            if isinstance(connection, _H2Connection):
                await connection.close()
            else:
                connection.writer.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


async def serve(protocol: str, host: str, port: int, certfile: str, keyfile: str,
                app: App, event_callback=None) -> TransportServer:
    if protocol not in ALPNS:
        raise ValueError("Require protocol h1/h2/h3")
    result = TransportServer()
    if protocol == "h3":
        def factory(*a, **kw):
            connection = _H3Protocol(*a, app=app, event_callback=event_callback, **kw)
            result.connections.add(connection)
            return connection
        result.listener = await quic_serve(host, port,
            configuration=_quic_config(client=False, certfile=certfile, keyfile=keyfile),
            create_protocol=factory)
        result.quic = True
    else:
        async def accept(reader, writer):
            task = asyncio.current_task()
            result.tasks.add(task)
            connection = None
            try:
                connection = (_H1Connection(reader, writer, event_callback, "server") if protocol == "h1"
                              else _H2Connection(reader, writer, event_callback, app))
                result.connections.add(connection)
                if protocol == "h1":
                    await connection.server_loop(app)
                else:
                    await connection.reader_task
            except (ConnectionError, asyncio.CancelledError, ssl.SSLError, h11.RemoteProtocolError):
                pass
            finally:
                if connection is not None:
                    result.connections.discard(connection)
                    if isinstance(connection, _H2Connection):
                        await connection.close()
                writer.close()
                result.tasks.discard(task)
        result.listener = await asyncio.start_server(accept, host, port,
            ssl=_context(protocol, certfile=certfile, keyfile=keyfile))
    return result
