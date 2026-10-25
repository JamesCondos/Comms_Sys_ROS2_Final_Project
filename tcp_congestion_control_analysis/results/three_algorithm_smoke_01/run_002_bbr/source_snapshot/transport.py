"""HTTPX handles HTTP/1.1 and HTTP/2; aioquic handles HTTP/3."""

import asyncio
import socket
import ssl
import struct
import time

import httpx
from hypercorn.app_wrappers import ASGIWrapper
from hypercorn.asyncio.run import worker_serve
from hypercorn.config import Config
from aioquic.asyncio import QuicConnectionProtocol, connect, serve as quic_serve
from aioquic.h3.connection import H3Connection
from aioquic.h3.events import HeadersReceived, DataReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import ConnectionTerminated, StreamReset, ProtocolNegotiated
from aioquic.quic.packet import QuicProtocolVersion


class TLSConfig(Config):
    def create_ssl_context(self):
        context = super().create_ssl_context()
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
        return context


def quic_config(client, cafile=None, certfile=None, keyfile=None):
    config = QuicConfiguration(
        is_client=client, alpn_protocols=["h3"], congestion_control_algorithm="cubic",
        max_data=64 * 1024**2, max_stream_data=16 * 1024**2, idle_timeout=120,
        supported_versions=[QuicProtocolVersion.VERSION_1], server_name="complex.local",
    )
    if client:
        config.load_verify_locations(cafile=cafile)
    else:
        config.load_cert_chain(certfile, keyfile)
    return config


class HTTP3(QuicConnectionProtocol):
    """Collect complete QUIC messages and match replies to their streams."""

    def __init__(self, *args, app=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.http, self.app = H3Connection(self._quic), app
        self.pending, self.messages, self.tasks = {}, {}, set()
        self.error = None
        self.negotiated = None

    def quic_event_received(self, event):
        # Check the negotiated protocol instead of assuming the peer used HTTP/3.
        if isinstance(event, ProtocolNegotiated):
            self.negotiated = event.alpn_protocol
            if self.negotiated != "h3":
                self.error = ConnectionError("HTTP/3 ALPN was not negotiated")
                self.close()

        if isinstance(event, ConnectionTerminated):
            self.error = ConnectionError("QUIC connection closed")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(self.error)
            self.pending.clear()
            self.messages.clear()

            return

        
        if isinstance(event, StreamReset):
            future = self.pending.pop(event.stream_id, None)
            self.messages.pop(event.stream_id, None)
            if future is not None and not future.done():
                future.set_exception(ConnectionError("QUIC stream reset"))


        for item in self.http.handle_event(event):
            if not isinstance(item, (HeadersReceived, DataReceived)):
                continue
            headers, body = self.messages.setdefault(item.stream_id, ({}, bytearray()))


            if isinstance(item, HeadersReceived):
                headers.update((name.decode(), value.decode()) for name, value in item.headers)
            else:
                body.extend(item.data)


            if item.stream_ended:
                self.messages.pop(item.stream_id)

                if self.app:
                    task = asyncio.create_task(self.respond(item.stream_id, bytes(body), headers))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                else:

                    future = self.pending.pop(item.stream_id, None)
                    if future is not None and not future.done():
                        future.set_result((int(headers[":status"]), bytes(body)))



    async def respond(self, stream, body, headers):

        status, reply = await self.app(body, headers)


        self.http.send_headers(stream, [(b":status", str(status).encode())])
        self.http.send_data(stream, reply, end_stream=True)
        self.transmit()

    async def post(self, body, message_id, authority):
        stream = self._quic.get_next_available_stream_id()
        future = asyncio.get_running_loop().create_future()
        self.pending[stream] = future

        self.http.send_headers(stream, [
            (b":method", b"POST"), (b":scheme", b"https"),
            (b":authority", authority.encode()), (b":path", b"/message"),
            (b"x-message-id", message_id.encode()),
        ])
        self.http.send_data(stream, body, end_stream=True)
        self.transmit()

        try:
            return await future
        except BaseException:
            # Removing a cancelled stream prevents a failed request being reused.
            self.pending.pop(stream, None)
            self._quic.reset_stream(stream, 0x10C)
            self.transmit()
            raise



class Client:
    async def post(self, body, message_id):
        async with self.limit:
            if self.http:
                reply = await self.http.post("/message", content=body, headers={"x-message-id": message_id})
                stream = reply.extensions["network_stream"]
                tls = stream.get_extra_info("ssl_object")
                expected = "HTTP/1.1" if self.protocol == "h1" else "HTTP/2"
                if reply.http_version != expected or tls.version() != "TLSv1.3":
                    raise ValueError("The requested HTTP version / TLS 1.3 was not used")

                # Reading the actual socket confirms Linux applied the requested algorithm.
                sock = stream.get_extra_info("socket")
                actual = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, 64)
                actual = actual.split(b"\0")[0].decode()
                if actual != self.algorithm:
                    raise ValueError("TCP algorithm mismatch: " + actual)
                key = (tuple(sock.getsockname()), tuple(sock.getpeername()))
                if key not in self.sockets:
                    identity = f"{self.owner}:{len(self.sockets) + 1}"
                    self.sockets[key] = (sock, identity)
                    self.record(dict(event="outgoing_connection", connection_id=identity,
                        local=list(key[0]), peer=list(key[1]), tcp_congestion_requested=self.algorithm,
                        tcp_congestion_actual=actual, protocol=reply.http_version, tls=tls.version()))
                self.sample_sockets()
                return reply.status_code, reply.content
            
            if self.quic.error:
                async with self.reconnect:
                    if self.quic.error:
                        await self.close()
                        self.context = connect(self.host, self.port, configuration=self.config, create_protocol=HTTP3)
                        self.quic = await asyncio.wait_for(self.context.__aenter__(), 15)

            return await self.quic.post(body, message_id, f"{self.host}:{self.port}")

    def record(self, row):
        # The component owns the log file, so the transport only supplies measurements.
        if self.callback:
            self.callback(dict(row, time_ns=time.monotonic_ns()))

    def sample_sockets(self):
        for sock, identity in self.sockets.values():
            try:
                info = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 104)
                # These offsets are from Linux's struct tcp_info in linux/tcp.h.
                self.record(dict(event="tcp_sample", connection_id=identity,
                    rtt_us=struct.unpack_from("=I", info, 68)[0],
                    congestion_window_segments=struct.unpack_from("=I", info, 80)[0],
                    total_retransmissions=struct.unpack_from("=I", info, 100)[0]))
            except OSError:
                # A closed socket cannot be sampled; do not invent a zero counter.
                continue

    async def monitor(self):
        while True:
            self.sample_sockets()
            await asyncio.sleep(0.2)

    async def close(self):
        if self.http:
            self.sample_sockets()
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
            await self.http.aclose()
        else:
            try:
                await asyncio.wait_for(self.context.__aexit__(None, None, None), 5)
            except asyncio.TimeoutError:
                pass
            finally:
                # Stop local QUIC timers even when an unreachable peer cannot finish closing.
                for name in ("_timer", "_transmit_task"):
                    handle = getattr(self.quic, name, None)
                    if handle:
                        handle.cancel()
                self.quic._transport.close()

class Server:
    async def close(self):
        if self.quic:
            self.listener.close()
            tasks = [task for connection in self.connections for task in connection.tasks]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
            self.stop.set()
            await self.task


async def serve(protocol, host, port, certfile, keyfile, app):
    server = Server()
    server.quic = protocol == "h3"

    if server.quic:
        server.connections = set()
        def factory(*args, **kwargs):
            connection = HTTP3(*args, app=app, **kwargs)
            server.connections.add(connection)
            return connection
        
        server.listener = await quic_serve(host, port, configuration = quic_config(
            False, certfile=certfile, keyfile=keyfile), create_protocol = factory)
        
        return server

    async def asgi(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                await send({"type": event["type"] + ".complete"})
                if event["type"] == "lifespan.shutdown":
                    return
        body = bytearray()


        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body.extend(event.get("body", b""))

            if not event.get("more_body", False):
                break

        headers = {name.decode(): value.decode() for name, value in scope["headers"]}

        if scope["http_version"] != ("1.1" if protocol == "h1" else "2"):
            status, reply = 505, b"HTTP version mismatch"
        else:
            status, reply = await app(bytes(body), headers)

        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": reply})

    config = TLSConfig()
    config.bind, config.certfile, config.keyfile = [f"{host}:{port}"], certfile, keyfile
    config.alpn_protocols = ["http/1.1" if protocol == "h1" else "h2"]
    
    config.accesslog = config.errorlog = None
    config.h2_max_concurrent_streams, config.graceful_timeout = 1024, 2
    sockets = config.create_sockets()


    sock = sockets.secure_sockets[0]
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, b"cubic")
    server.stop = asyncio.Event()

    server.task = asyncio.create_task(worker_serve(ASGIWrapper(asgi), config,
        sockets=sockets, shutdown_trigger=server.stop.wait))

    
    while not sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):

        if server.task.done():
            await server.task

        await asyncio.sleep(0.01)

    return server


async def create_client(protocol, host, port, cafile, max_concurrency=8,
                        algorithm="cubic", callback=None, owner="client", timeout=8):
    # Each hop gets its own client and a fixed application concurrency limit.
    client = Client()
    client.protocol, client.algorithm = protocol, algorithm
    client.owner, client.callback = owner, callback
    client.limit = asyncio.Semaphore(max_concurrency)
    client.sockets = {}
    client.http = None
    if protocol in ("h1", "h2"):
        context = ssl.create_default_context(cafile=cafile)
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
        count = max_concurrency if protocol == "h1" else 1
        limits = httpx.Limits(max_connections=count, max_keepalive_connections=count)
        # Setting TCP_CONGESTION before connecting also controls the initial window.
        options = [(socket.IPPROTO_TCP, socket.TCP_CONGESTION, algorithm.encode()),
                   (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]
        transport = httpx.AsyncHTTPTransport(verify=context, http1=protocol == "h1",
            http2=protocol == "h2", limits=limits, retries=0, socket_options=options)
        client.http = httpx.AsyncClient(base_url=f"https://{host}:{port}",
            transport=transport, timeout=timeout, trust_env=False)
        client.monitor_task = asyncio.create_task(client.monitor())
    elif protocol == "h3":
        # QUIC uses its own congestion control, not the kernel TCP algorithm.
        if algorithm != "cubic":
            raise ValueError("This HTTP/3 implementation supports QUIC CUBIC only")
        client.host, client.port = host, port
        client.config = quic_config(True, cafile=cafile)
        client.reconnect = asyncio.Lock()
        client.context = connect(host, port, configuration=client.config, create_protocol=HTTP3)
        client.quic = await asyncio.wait_for(client.context.__aenter__(), timeout)
        if client.quic.negotiated != "h3":
            await client.close()
            raise ConnectionError("HTTP/3 ALPN was not negotiated")
        client.record(dict(event="quic_connection", protocol="h3", alpn=client.quic.negotiated,
                           congestion="cubic", quic_version=int(client.quic._quic._version)))
    else:
        raise ValueError("Choose h1, h2 or h3")
    return client
