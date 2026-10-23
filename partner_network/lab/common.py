"""Standard-library helpers shared by the portable partner network.

All interval timestamps use the host monotonic clock. JSONL records deliberately
omit payload strings; the useful payload size and complete HTTP body size are
separate fields. Nothing in this module imports ROS.
"""
from collections import deque
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlsplit


SCHEMA_VERSION = 1


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


class JsonlLog:
    """One lock and buffered file per log; close always flushes it."""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.file = Path(path).open("x", encoding="utf-8", buffering=65536)
        self.lock = threading.Lock()
        self.count = 0

    def write(self, row):
        if "payload" in row:
            raise ValueError("Payload contents must not enter the measurement logs")
        data = json.dumps(row, separators=(",", ":"), sort_keys=True)
        with self.lock:
            self.file.write(data + "\n")
            self.count += 1

    def flush(self):
        with self.lock:
            self.file.flush()

    def close(self):
        with self.lock:
            if not self.file.closed:
                self.file.close()


def clock_identity():
    boot_file = Path("/proc/sys/kernel/random/boot_id")
    boot_id = boot_file.read_text().strip() if boot_file.exists() else "unknown-boot"
    try:
        namespace = os.readlink("/proc/self/ns/time")
    except OSError:
        namespace = "unknown-time-namespace"
    return {
        "clock_domain_id": f"{boot_id}:{namespace}:CLOCK_MONOTONIC",
        "boot_id": boot_id,
        "time_namespace": namespace,
        "clock": "time.monotonic_ns",
        "wall_time_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
    }


def stream_seed(seed, *parts):
    text = ":".join([str(seed)] + [str(part) for part in parts])
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")


def visible_message(message):
    return {key: value for key, value in message.items() if key != "payload"}


def decode_envelope(body):
    """Validate the identity and useful payload length without altering the body."""
    message = json.loads(body)
    if not isinstance(message, dict):
        raise ValueError("HTTP body must be a JSON object")
    for key in ("id", "robot_id", "message_type"):
        if not isinstance(message.get(key), str) or not message[key]:
            raise ValueError(f"Missing or invalid {key}")
    for key in ("source_ns", "scheduled_ns", "payload_bytes", "sequence_number"):
        if isinstance(message.get(key), bool) or not isinstance(message.get(key), int) or message[key] < 0:
            raise ValueError(f"Missing or invalid {key}")
    if not isinstance(message.get("payload"), str):
        raise ValueError("Missing payload")
    if len(message["payload"].encode("utf-8")) != message["payload_bytes"]:
        raise ValueError("Useful payload length does not match payload_bytes")
    return message


def validate_ack(ack, message_id):
    if not isinstance(ack, dict) or ack.get("id") != message_id:
        raise ValueError("Response acknowledgement ID does not match the request")
    if ack.get("outcome") != "processed" or not isinstance(ack.get("worker_id"), str):
        raise ValueError("Response does not contain a central worker completion")
    for key in ("worker_admission_ns", "worker_service_start_ns", "worker_processed_ns"):
        if not isinstance(ack.get(key), int):
            raise ValueError(f"Response lacks worker timing {key}")
    if not ack["worker_admission_ns"] <= ack["worker_service_start_ns"] <= ack["worker_processed_ns"]:
        raise ValueError("Worker timestamps are not ordered")
    if not isinstance(ack.get("worker_clock_domain_id"), str):
        raise ValueError("Response lacks worker clock identity")
    return ack


def endpoint_parts(url):
    parsed = urlsplit(url)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Endpoints must be plain http://host:port URLs without credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Endpoint query strings and fragments are unsupported")
    return parsed.hostname, parsed.port or 80, parsed.path or "/"


def configure_tcp(sock, requested):
    """Set a per-socket Linux TCP algorithm, without changing host sysctls."""
    requested = None if requested in (None, "", "system") else str(requested).lower()
    if requested is not None and (not requested.isascii() or not requested.replace("_", "").isalnum()):
        raise ValueError("Invalid TCP congestion-control algorithm name")
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    option = getattr(socket, "TCP_CONGESTION", None)
    if option is None:
        if requested is not None:
            raise RuntimeError("Selecting TCP congestion control requires Linux TCP_CONGESTION")
        return "unavailable"
    if requested is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, option, requested.encode("ascii"))
        except OSError as exc:
            raise RuntimeError(f"TCP congestion algorithm {requested!r} is unavailable in this kernel: {exc}") from exc
    actual = sock.getsockopt(socket.IPPROTO_TCP, option, 256).split(b"\0", 1)[0].decode("ascii")
    if requested is not None and actual != requested:
        raise RuntimeError(f"Requested TCP algorithm {requested!r}, socket reports {actual!r}")
    return actual


def probe_tcp(requested):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return configure_tcp(sock, requested)


class TCPHTTPConnection(http.client.HTTPConnection):
    """Persistent HTTP/1.1 connection; callers never retry a failed request."""

    def __init__(self, url, timeout, congestion=None, logger=None, owner=""):
        host, port, self.request_path = endpoint_parts(url)
        super().__init__(host, port, timeout=timeout)
        self.url = url
        self.congestion = congestion
        self.logger = logger
        self.owner = owner
        self.connection_id = None
        self.generation = 0
        self.actual_congestion = None

    def connect(self):
        # Configure before connect so TCP's initial congestion window uses the
        # requested algorithm too. The kit deliberately supports HTTP only.
        family, kind, protocol, _canonical, address = socket.getaddrinfo(
            self.host, self.port, type=socket.SOCK_STREAM)[0]
        self.sock = socket.socket(family, kind, protocol)
        try:
            self.sock.settimeout(self.timeout)
            self.actual_congestion = configure_tcp(self.sock, self.congestion)
            if self.source_address:
                self.sock.bind(self.source_address)
            self.sock.connect(address)
        except Exception:
            self.sock.close()
            self.sock = None
            raise
        self.generation += 1
        self.connection_id = f"{self.owner}:{self.host}:{self.port}:{self.generation}:{time.monotonic_ns()}"
        if self.logger is not None:
            self.logger.write({"event": "outgoing_connection", "time_ns": time.monotonic_ns(),
                               "connection_id": self.connection_id, "owner": self.owner,
                               "local": list(self.sock.getsockname()), "peer": list(self.sock.getpeername()),
                               "tcp_congestion_requested": self.congestion,
                               "tcp_congestion_actual": self.actual_congestion})

    def set_timeout(self, timeout):
        self.timeout = timeout
        if self.sock is not None:
            self.sock.settimeout(timeout)


class AccountingQueue:
    """Bounded waiting FIFO with exact state transition timestamps.

    Capacity excludes items in service. Both each job timestamp and its queue
    event use the same timestamp while holding the condition lock.
    """

    def __init__(self, capacity, logger, clock=time.monotonic_ns):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("Waiting queue capacity must be a positive integer")
        self.capacity, self.logger, self.clock = capacity, logger, clock
        self.condition = threading.Condition()
        self.waiting = deque()
        self.active = {}
        self.closed = False
        self.admitted = self.completed = self.dropped = self.aborted = 0
        self._event("initial", None, self.clock())

    def _event(self, event, job, stamp):
        self.logger.write({"event": event, "time_ns": stamp,
                           "id": job["message"]["id"] if job is not None else None,
                           "queue_len": len(self.waiting), "in_service": len(self.active)})

    def put(self, job):
        with self.condition:
            stamp = self.clock()
            job["callback_ns"] = stamp
            if self.closed or len(self.waiting) >= self.capacity:
                job["terminal_ns"] = stamp
                self.dropped += 1
                self._event("queue_drop", job, stamp)
                return False
            job["admission_ns"] = stamp
            self.waiting.append(job)
            self.admitted += 1
            self._event("admit", job, stamp)
            self.condition.notify()
            return True

    def get(self, timeout=0.1):
        with self.condition:
            if not self.waiting and not self.closed:
                self.condition.wait(timeout)
            if not self.waiting:
                return None
            job = self.waiting.popleft()
            job["dequeue_ns"] = self.clock()
            key = job["message"]["id"]
            if key in self.active:
                raise RuntimeError("Duplicate in-service request ID")
            self.active[key] = job
            self._event("service_start", job, job["dequeue_ns"])
            return job

    def complete(self, job):
        with self.condition:
            key = job["message"]["id"]
            if self.active.pop(key, None) is not job:
                raise RuntimeError("Completing a request that is not in service")
            job["terminal_ns"] = self.clock()
            self.completed += 1
            self._event("service_end", job, job["terminal_ns"])
            self.condition.notify_all()
            return job["terminal_ns"]

    def close(self, abort_waiting=False):
        with self.condition:
            self.closed = True
            aborted = []
            if abort_waiting:
                while self.waiting:
                    job = self.waiting.popleft()
                    job["terminal_ns"] = self.clock()
                    self.aborted += 1
                    self._event("drain_timeout", job, job["terminal_ns"])
                    aborted.append(job)
            self.condition.notify_all()
            return aborted

    def state(self):
        with self.condition:
            return {"queue_len": len(self.waiting), "in_service": len(self.active),
                    "admitted": self.admitted, "completed": self.completed,
                    "queue_dropped": self.dropped, "aborted": self.aborted,
                    "pending": len(self.waiting) + len(self.active)}
