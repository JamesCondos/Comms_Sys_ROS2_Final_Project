"""ROS 2 robot gateways and HTTP/1.1 services for the partner network.

Run from the kit root: python3 -B -m lab.components --help.
The network driver owns namespaces, routing and component lifecycle. This file
never changes network configuration or host-wide TCP settings.
"""
import argparse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import random
import re
import signal
import socket
import threading
import time

from .common import (AccountingQueue, JsonlLog, SCHEMA_VERSION,
                     TCPHTTPConnection, clock_identity, configure_tcp,
                     decode_envelope, endpoint_parts, probe_tcp, stream_seed,
                     validate_ack, visible_message, write_json)


MAX_BODY_BYTES = 16 * 1024 * 1024


def periodic_schedule(config, name):
    """Independent seeded phase per robot/class; no dependency on HTTP replies."""
    horizon_ns = int((config["warmup_s"] + config["duration_s"]) * 1e9)
    schedules, metadata = [], []
    for kind, traffic in sorted(config["traffic"].items()):
        rate = float(traffic["rate_hz"])
        if rate <= 0 or not 0 <= int(traffic["payload_bytes"]) <= MAX_BODY_BYTES // 2:
            raise ValueError("Traffic rates must be positive and payload size must be 0..8 MiB")
        seed = stream_seed(config["seed"], name, kind)
        phase = random.Random(seed).random() / rate
        metadata.append({"message_type": kind, "rate_hz": rate, "seed": seed, "phase_s": phase})
        sequence = 0
        while (offset_ns := int(round((phase + sequence / rate) * 1e9))) < horizon_ns:
            schedules.append((offset_ns, kind, sequence))
            sequence += 1
    schedules.sort()
    return schedules, {"arrival_pattern": "periodic_independent_seeded_phase", "open_loop": True,
                       "horizon_ns": horizon_ns, "streams": metadata,
                       "tie_order": "scheduled offset, message type, sequence number"}


def json_bytes(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def wait_until(stamp_ns, stop=None):
    while (remaining := stamp_ns - time.monotonic_ns()) > 0:
        if stop is not None:
            if stop.wait(min(remaining / 1e9, 0.1)):
                return False
        else:
            time.sleep(remaining / 1e9)
    return True


def request_once(connection, body, message_id):
    """Exactly one attempt. A failure can still mean the worker processed it."""
    request_ns = time.monotonic_ns()
    try:
        connection.request("POST", connection.request_path, body=body,
                           headers={"Content-Type": "application/json", "Content-Length": str(len(body)),
                                    "Connection": "keep-alive", "X-Request-ID": message_id})
        response = connection.getresponse()
        data = response.read(MAX_BODY_BYTES + 1)
        response_ns = time.monotonic_ns()
        if len(data) > MAX_BODY_BYTES:
            raise ValueError("Upstream response exceeded safety limit")
        if response.status != 200:
            return {"outcome": "http_error", "http_status": response.status,
                    "request_ns": request_ns, "response_ns": response_ns,
                    "possibly_completed": True, "body": data,
                    "connection_id": connection.connection_id}
        try:
            ack = validate_ack(json.loads(data), message_id)
        except (ValueError, TypeError) as exc:
            return {"outcome": "ack_mismatch", "http_status": 200,
                    "request_ns": request_ns, "response_ns": response_ns,
                    "possibly_completed": True, "error": str(exc), "body": data,
                    "connection_id": connection.connection_id}
        return {"outcome": "success", "http_status": 200, "request_ns": request_ns,
                "response_ns": response_ns, "possibly_completed": False,
                "ack": ack, "body": data, "connection_id": connection.connection_id}
    except Exception as exc:
        # The broad exception intentionally covers http.client's protocol errors
        # too. Do not resubmit: the upstream may have committed this request.
        connection_id = connection.connection_id
        connection.close()
        return {"outcome": "timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "request_error",
                "request_ns": request_ns, "response_ns": time.monotonic_ns(),
                "possibly_completed": True, "error_type": type(exc).__name__,
                "error": str(exc), "connection_id": connection_id}


class Component:
    def __init__(self, config, name, role, output):
        self.config, self.name, self.role = config, name, role
        self.node = config["nodes"][name]
        if self.node["role"] != role:
            raise ValueError("CLI role and resolved node role disagree")
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        if (self.output / "ready.json").exists() or (self.output / "final.json").exists():
            raise FileExistsError("Refusing to overwrite component evidence")
        self.clock = clock_identity()
        self.algorithm = config.get("tcp_congestion", {}).get(role, "system")
        self.probed_algorithm = probe_tcp(self.algorithm)
        self.logs = {}
        self.log_lock = threading.Lock()
        self.connections = self.log("connections")
        self.stop = threading.Event()

    def log(self, name):
        with self.log_lock:
            if name not in self.logs:
                self.logs[name] = JsonlLog(self.output / f"{name}.jsonl")
            return self.logs[name]

    def ready(self, **extra):
        row = {"schema_version": SCHEMA_VERSION, "pid": os.getpid(), "name": self.name,
               "role": self.role, "run_id": self.config["run_id"], **self.clock,
               "tcp_congestion_requested": self.algorithm,
               "tcp_congestion_probe_actual": self.probed_algorithm, **extra}
        write_json(self.output / "ready.json", row)

    def finish(self, **extra):
        for log in self.logs.values():
            log.flush()
        write_json(self.output / "final.json", {"schema_version": SCHEMA_VERSION,
                   "name": self.name, "role": self.role, "run_id": self.config["run_id"],
                   "clock_domain_id": self.clock["clock_domain_id"],
                   "finished_ns": time.monotonic_ns(), **extra})
        for log in self.logs.values():
            log.close()


class Robot(Component):
    def __init__(self, config, name, output):
        super().__init__(config, name, "robot", output)
        self.source_log, self.terminal_log = self.log("source"), self.log("gateway")
        self.queue = AccountingQueue(config["queue_capacity"], self.log("queue"))
        self.generated, self.seen, self.terminal_ids = set(), set(), set()
        self.count_lock = threading.Lock()
        self.outcomes = Counter()
        self.duplicates = 0
        self.deadline_ns = None
        self.target_url = self.node["target_url"]
        endpoint_parts(self.target_url)
        self.connection = TCPHTTPConnection(self.target_url, config["request_timeout_s"],
                                             self.algorithm, self.connections, name)

    def terminal(self, job, result):
        message = job["message"]
        row = visible_message(message)
        row.update(http_body_bytes=len(job["body"]), target_url=self.target_url,
                   callback_ns=job.get("callback_ns"), admission_ns=job.get("admission_ns"),
                   dequeue_ns=job.get("dequeue_ns"), terminal_ns=job["terminal_ns"])
        row.update({key: value for key, value in result.items() if key not in ("body", "ack")})
        if "ack" in result:
            row.update({key: value for key, value in result["ack"].items() if key != "id" and key != "outcome"})
        with self.count_lock:
            if message["id"] in self.terminal_ids:
                raise RuntimeError("A generated message acquired more than one terminal outcome")
            self.terminal_ids.add(message["id"])
            self.outcomes[row["outcome"]] += 1
        self.terminal_log.write(row)

    def callback(self, ros_message):
        body = ros_message.data.encode("utf-8")
        try:
            message = decode_envelope(body)
        except (ValueError, TypeError) as exc:
            self.log("errors").write({"event": "invalid_ros_message", "time_ns": time.monotonic_ns(),
                                      "error": str(exc)})
            return
        with self.count_lock:
            if message["id"] in self.seen:
                self.duplicates += 1
                self.log("errors").write({"event": "duplicate_ros_callback", "id": message["id"],
                                          "time_ns": time.monotonic_ns()})
                return
            self.seen.add(message["id"])
        job = {"message": message, "body": body}
        if not self.queue.put(job):
            self.terminal(job, {"outcome": "queue_drop", "possibly_completed": False})

    def sender(self):
        while not self.stop.is_set():
            job = self.queue.get()
            if job is None:
                continue
            remaining = (self.deadline_ns - time.monotonic_ns()) / 1e9
            if remaining <= 0:
                self.queue.complete(job)
                self.terminal(job, {"outcome": "drain_timeout", "possibly_completed": False})
                continue
            self.connection.set_timeout(max(0.001, min(self.config["request_timeout_s"], remaining)))
            result = request_once(self.connection, job["body"], job["message"]["id"])
            if result["response_ns"] > self.deadline_ns:
                result.update(original_outcome=result["outcome"], outcome="drain_timeout",
                              possibly_completed=True)
            # Occupancy ends at terminal_ns; response_ns is the socket-read
            # timestamp and may precede it by the local validation overhead.
            self.queue.complete(job)
            self.terminal(job, result)
        self.connection.close()

    def run(self):
        # ROS stays local to this robot network namespace. Each robot is a
        # separate OS process containing two actual ROS nodes.
        os.environ["ROS_LOCALHOST_ONLY"] = "1"
        os.environ.setdefault("ROS_DOMAIN_ID", "174")
        import rclpy
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
        from std_msgs.msg import String

        run_token = re.sub(r"[^A-Za-z0-9_]", "_", self.config["run_id"])
        prefix = f"/lab/run_{run_token}/{self.name}"
        qos = QoSProfile(depth=1000, reliability=ReliabilityPolicy.RELIABLE,
                         history=HistoryPolicy.KEEP_LAST)
        schedule, model = periodic_schedule(self.config, self.name)
        write_json(self.output / "arrival_model.json", model)
        rclpy.init()
        source_node = rclpy.create_node("source", namespace=prefix)
        gateway_node = rclpy.create_node("gateway", namespace=prefix)
        publishers, subscriptions = {}, {}
        for kind in self.config["traffic"]:
            publishers[kind] = source_node.create_publisher(String, kind, qos)
            subscriptions[kind] = gateway_node.create_subscription(String, kind, self.callback, qos)
        executor = SingleThreadedExecutor()
        executor.add_node(source_node)
        executor.add_node(gateway_node)
        ros_thread = threading.Thread(target=executor.spin, name="ros_callbacks", daemon=True)
        ros_thread.start()
        sender_thread = None
        executor_stopped = False
        try:
            discovery_deadline = time.monotonic() + 30
            while True:
                observed = {kind: {"publishers": gateway_node.get_publishers_info_by_topic(f"{prefix}/{kind}"),
                                   "subscribers": source_node.count_subscribers(f"{prefix}/{kind}"),
                                   "matches": publishers[kind].get_subscription_count()}
                            for kind in publishers}
                if any(len(row["publishers"]) > 1 or row["subscribers"] > 1 for row in observed.values()):
                    raise RuntimeError("Unexpected duplicate ROS publisher or subscriber in isolated robot topics")
                if all(len(row["publishers"]) == 1 and row["subscribers"] == 1 and row["matches"] == 1
                       for row in observed.values()):
                    break
                if time.monotonic() > discovery_deadline:
                    raise RuntimeError("Local ROS DDS subscriptions did not match within 30 seconds")
                time.sleep(0.05)
            graph = [{"name": name, "namespace": namespace}
                     for name, namespace in source_node.get_node_names_and_namespaces()]
            expected_nodes = {(name, prefix) for name in ("source", "gateway")}
            if not expected_nodes.issubset({(row["name"], row["namespace"]) for row in graph}):
                raise RuntimeError("ROS graph does not contain both local source and gateway nodes")
            topics = [{"topic": f"{prefix}/{kind}", "type": "std_msgs/msg/String",
                       "publisher_count": len(observed[kind]["publishers"]),
                       "publishers": [{"name": item.node_name, "namespace": item.node_namespace}
                                      for item in observed[kind]["publishers"]],
                       "subscriber_count": observed[kind]["subscribers"],
                       "subscription_matches": observed[kind]["matches"]}
                      for kind in self.config["traffic"]]
            self.ready(actual_ros_node_count=2, actual_source_node_count=1,
                       actual_gateway_node_count=1, ros_nodes=graph, topics=topics,
                       ros_localhost_only=True, ros_domain_id=os.environ["ROS_DOMAIN_ID"],
                       target_url=self.target_url, fifo_waiting_capacity=self.queue.capacity,
                       outstanding_http_limit=1, qos={"reliability": "RELIABLE", "history": "KEEP_LAST", "depth": 1000})
            start_file = Path(self.config["start_file"])
            start_wait_deadline = time.monotonic() + float(self.config.get("start_timeout_s", 180))
            while not start_file.exists():
                if self.stop.wait(0.02):
                    raise RuntimeError("Stopped before start signal")
                if time.monotonic() > start_wait_deadline:
                    raise RuntimeError("Timed out waiting for driver start file")
            start = json.loads(start_file.read_text(encoding="utf-8"))
            start_ns = int(start["start_ns"])
            horizon_ns = start_ns + model["horizon_ns"]
            self.deadline_ns = horizon_ns + int(self.config["drain_s"] * 1e9)
            sender_thread = threading.Thread(target=self.sender, name="http_sender", daemon=True)
            sender_thread.start()
            skipped_at_horizon = 0
            for schedule_index, (offset_ns, kind, sequence) in enumerate(schedule):
                scheduled_ns = start_ns + offset_ns
                if not wait_until(scheduled_ns, self.stop):
                    break
                if time.monotonic_ns() >= horizon_ns:
                    skipped_at_horizon = len(schedule) - schedule_index
                    self.log("errors").write({"event": "schedule_horizon_reached", "time_ns": time.monotonic_ns(),
                                              "scheduled_but_not_published": skipped_at_horizon})
                    break
                traffic = self.config["traffic"][kind]
                message = {"run_id": self.config["run_id"], "robot_id": self.name,
                           "message_type": kind, "sequence_number": sequence,
                           "message_semantics": "uplink_synthetic_critical_event" if kind == "command" else "uplink_telemetry",
                           "id": f"{self.config['run_id']}:{self.name}:{kind}:{sequence}",
                           "scheduled_ns": scheduled_ns, "source_ns": time.monotonic_ns(),
                           "source_clock_domain_id": self.clock["clock_domain_id"],
                           "payload_bytes": int(traffic["payload_bytes"]),
                           "payload": "x" * int(traffic["payload_bytes"])}
                data = json_bytes(message)
                with self.count_lock:
                    self.generated.add(message["id"])
                publish_start = time.monotonic_ns()
                publishers[kind].publish(String(data=data.decode("utf-8")))
                publish_end = time.monotonic_ns()
                self.source_log.write({**visible_message(message), "http_body_bytes": len(data),
                                       "publish_start_ns": publish_start, "publish_end_ns": publish_end,
                                       "scheduler_lag_ns": message["source_ns"] - scheduled_ns})
            wait_until(horizon_ns, self.stop)
            while not self.stop.is_set() and time.monotonic_ns() < self.deadline_ns:
                with self.count_lock:
                    received_all = self.generated.issubset(self.seen)
                if received_all and self.queue.state()["pending"] == 0:
                    break
                time.sleep(0.01)
            self.stop.set()
            # An individual socket timeout applies to each I/O operation. Abort
            # the socket at the total drain deadline too, so a peer that slowly
            # trickles a response cannot extend the experiment indefinitely.
            if time.monotonic_ns() >= self.deadline_ns and self.connection.sock is not None:
                try:
                    self.connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
            for job in self.queue.close(abort_waiting=True):
                self.terminal(job, {"outcome": "drain_timeout", "possibly_completed": False})
            sender_thread.join(timeout=float(self.config["request_timeout_s"]) + 1)
            if sender_thread.is_alive():
                raise RuntimeError("HTTP sender did not finish after bounded request timeout")
            executor.shutdown(timeout_sec=2)
            ros_thread.join(timeout=2)
            executor_stopped = True
            if ros_thread.is_alive():
                raise RuntimeError("ROS callback executor failed to finish during shutdown")
            with self.count_lock:
                missing_ids = sorted(self.generated - self.seen)
                received_unknown = sorted(self.seen - self.generated)
            self.finish(generated_count=len(self.generated), callback_count=len(self.seen),
                        terminal_count=len(self.terminal_ids), outcomes=dict(self.outcomes),
                        ros_missing_ids=missing_ids, unknown_callback_ids=received_unknown,
                        duplicate_callbacks=self.duplicates, **self.queue.state(),
                        scheduled_count=len(schedule), scheduled_but_not_published=skipped_at_horizon,
                        start_ns=start_ns, measurement_start_ns=start_ns + int(self.config["warmup_s"] * 1e9),
                        measurement_end_ns=horizon_ns, drain_deadline_ns=self.deadline_ns)
        finally:
            self.stop.set()
            self.connection.close()
            if sender_thread is not None:
                sender_thread.join(timeout=1)
            if not executor_stopped:
                executor.shutdown(timeout_sec=2)
                ros_thread.join(timeout=2)
            source_node.destroy_node()
            gateway_node.destroy_node()
            rclpy.shutdown()


class NetworkServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, component):
        self.component = component
        self.inflight = 0
        self.inflight_lock = threading.Lock()
        self.accepted_connections = {}
        self.accepted_lock = threading.Lock()
        super().__init__(address, Handler)

    def get_request(self):
        sock, address = super().get_request()
        try:
            actual = configure_tcp(sock, self.component.algorithm)
            sock.settimeout(max(2.0, float(self.component.config["request_timeout_s"]) + 1))
        except Exception:
            sock.close()
            raise
        accepted_ns = time.monotonic_ns()
        connection_id = f"{self.component.name}:{address[0]}:{address[1]}:{accepted_ns}"
        with self.accepted_lock:
            self.accepted_connections[sock] = connection_id
        self.component.connections.write({"event": "accepted_connection", "time_ns": accepted_ns,
                                         "connection_id": connection_id,
                                         "local": list(sock.getsockname()), "peer": list(address),
                                         "tcp_congestion_requested": self.component.algorithm,
                                         "tcp_congestion_actual": actual})
        return sock, address


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PartnerLab/1"

    def setup(self):
        super().setup()
        self.upstream = {}
        with self.server.accepted_lock:
            self.connection_id = self.server.accepted_connections.pop(self.request)

    def finish(self):
        try:
            for connection in self.upstream.values():
                connection.close()
        finally:
            super().finish()

    def log_message(self, *_args):
        pass

    def send_body(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close" if self.close_connection else "keep-alive")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self):
        if self.path != "/health":
            self.send_body(404, json_bytes({"error": "Unknown endpoint"}))
            return
        component = self.server.component
        self.send_body(200, json_bytes({"name": component.name, "role": component.role,
                                      "run_id": component.config["run_id"], "ready": True}))

    def do_POST(self):
        component = self.server.component
        with self.server.inflight_lock:
            self.server.inflight += 1
        try:
            receive_ns = time.monotonic_ns()
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                self.send_body(400, json_bytes({"error": "Content-Length required; chunking is unsupported"}))
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= length <= MAX_BODY_BYTES:
                    raise ValueError("Invalid Content-Length")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("Truncated request body")
                message = decode_envelope(body)
                header_id = self.headers.get("X-Request-ID")
                if header_id is not None and header_id != message["id"]:
                    raise ValueError("Header request ID and body ID disagree")
            except (ValueError, TypeError) as exc:
                self.close_connection = True
                self.send_body(400, json_bytes({"error": str(exc)}))
                return
            body_read_ns = time.monotonic_ns()
            if component.role == "worker":
                status, response_body = component.process(message, body, receive_ns, body_read_ns,
                                                          self.connection_id)
            else:
                status, response_body = component.forward(self, message, body, receive_ns, body_read_ns)
            try:
                self.send_body(status, response_body)
            except (OSError, ValueError) as exc:
                component.log("errors").write({"event": "response_write_error", "id": message["id"],
                                                "time_ns": time.monotonic_ns(), "error": str(exc),
                                                "possibly_completed": True,
                                                "connection_id": self.connection_id})
                self.close_connection = True
        except (OSError, ValueError) as exc:
            self.close_connection = True
            component.log("errors").write({"event": "request_read_error", "time_ns": time.monotonic_ns(),
                                            "error": str(exc), "connection_id": self.connection_id})
        finally:
            with self.server.inflight_lock:
                self.server.inflight -= 1


class Service(Component):
    def __init__(self, config, name, role, output):
        super().__init__(config, name, role, output)
        self.server = None
        self.workers = []
        self.round_robin_counter = 0
        self.round_robin_lock = threading.Lock()
        self.processed_ids = set()
        self.count_lock = threading.Lock()
        self.counts = Counter()
        if role == "worker":
            self.worker_log = self.log("worker")
            self.queue = AccountingQueue(config["worker_queue_capacity"], self.log("queue"))
            worker_count = int(config.get("worker_threads", 1))
            if worker_count < 1:
                raise ValueError("worker_threads must be positive")
            if float(config["worker_service_ms"]) < 0:
                raise ValueError("worker_service_ms must be nonnegative")
            for index in range(worker_count):
                thread = threading.Thread(target=self.service_worker, args=(index,),
                                          name=f"worker_{index}", daemon=True)
                self.workers.append(thread)
        else:
            self.hop_log = self.log("hops")
            self.backends = list(self.node["backends"])
            if not self.backends or (role == "edge" and len(self.backends) != 1):
                raise ValueError("Each edge requires one backend; a proxy requires at least one")
            for backend in self.backends:
                endpoint_parts(backend)

    def service_worker(self, index):
        while not self.stop.is_set() or self.queue.state()["queue_len"]:
            job = self.queue.get()
            if job is None:
                continue
            delay = float(self.config["worker_service_ms"]) / 1000
            if delay:
                time.sleep(delay)
            processed_ns = self.queue.complete(job)
            message = job["message"]
            ack = {"id": message["id"], "outcome": "processed", "worker_id": self.name,
                   "worker_thread_index": index, "worker_clock_domain_id": self.clock["clock_domain_id"],
                   "worker_admission_ns": job["admission_ns"],
                   "worker_service_start_ns": job["dequeue_ns"], "worker_processed_ns": processed_ns}
            row = {**visible_message(message), "outcome": "processed", "worker_id": self.name,
                   "worker_thread_index": index, "clock_domain_id": self.clock["clock_domain_id"],
                   "http_body_bytes": len(job["body"]), "admission_ns": job["admission_ns"],
                   "service_start_ns": job["dequeue_ns"], "processed_ns": processed_ns,
                   "terminal_ns": processed_ns, "request_received_ns": job["receive_ns"],
                   "body_read_ns": job["body_read_ns"], "connection_id": job["connection_id"]}
            with self.count_lock:
                duplicate = message["id"] in self.processed_ids
                self.processed_ids.add(message["id"])
                self.counts["processed"] += 1
                self.counts["duplicate_ids"] += int(duplicate)
            row["duplicate_id"] = duplicate
            self.worker_log.write(row)
            job["response_body"] = json_bytes(ack)
            job["done"].set()

    def process(self, message, body, receive_ns, body_read_ns, connection_id):
        job = {"message": message, "body": body, "receive_ns": receive_ns,
               "body_read_ns": body_read_ns, "connection_id": connection_id, "done": threading.Event()}
        if not self.queue.put(job):
            self.worker_log.write({**visible_message(message), "outcome": "worker_queue_drop",
                                   "http_body_bytes": len(body), "worker_id": self.name,
                                   "terminal_ns": job["terminal_ns"], "request_received_ns": receive_ns,
                                   "body_read_ns": body_read_ns, "connection_id": connection_id})
            with self.count_lock:
                self.counts["queue_dropped"] += 1
            return 503, json_bytes({"id": message["id"], "error": "worker_queue_full", "worker_id": self.name})
        # Deliberately wait for real central processing, even if a caller times
        # out. Such jobs remain visible in worker logs as possibly orphaned work.
        job["done"].wait()
        return job.get("response_status", 200), job["response_body"]

    def forward(self, handler, message, body, receive_ns, body_read_ns):
        with self.round_robin_lock:
            selection_index = self.round_robin_counter
            self.round_robin_counter += 1
            backend = self.backends[selection_index % len(self.backends)]
        if backend not in handler.upstream:
            handler.upstream[backend] = TCPHTTPConnection(
                backend, self.config["request_timeout_s"], self.algorithm,
                self.connections, f"{self.name}:{handler.connection_id}")
        connection = handler.upstream[backend]
        result = request_once(connection, body, message["id"])
        row = {**visible_message(message), "hop_id": self.name, "hop_role": self.role,
               "clock_domain_id": self.clock["clock_domain_id"], "http_body_bytes": len(body),
               "downstream_connection_id": handler.connection_id, "selected_backend": backend,
               "selection_index": selection_index, "request_received_ns": receive_ns,
               "body_read_ns": body_read_ns, "terminal_ns": time.monotonic_ns(),
               **{key: value for key, value in result.items() if key not in ("body", "ack")}}
        if "ack" in result:
            row["worker_id"] = result["ack"]["worker_id"]
        self.hop_log.write(row)
        with self.count_lock:
            self.counts[result["outcome"]] += 1
        if result["outcome"] == "success":
            return 200, result["body"]  # Byte-identical central-worker acknowledgement.
        if result["outcome"] == "http_error":
            return result["http_status"], result["body"]
        return 502, json_bytes({"id": message["id"], "error": "upstream_failure",
                                "hop_id": self.name, "possibly_completed": True})

    def run(self):
        self.server = NetworkServer((self.node["ip"], 8080), self)
        for thread in self.workers:
            thread.start()
        self.ready(endpoint=f"http://{self.node['ip']}:8080", backends=self.node.get("backends", []),
                   worker_threads=len(self.workers),
                   worker_waiting_capacity=self.config["worker_queue_capacity"] if self.role == "worker" else None,
                   acknowledgement="only_after_central_worker_completion", protocol="HTTP/1.1")

        def stop_server(_number, _frame):
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop_server)
        signal.signal(signal.SIGINT, stop_server)
        try:
            self.server.serve_forever(poll_interval=0.1)
        finally:
            if self.role == "worker":
                # A terminating server cannot silently abandon admitted jobs.
                # Waiting jobs receive an explicit non-ACK terminal record and
                # HTTP503; jobs already in service finish normally.
                for job in self.queue.close(abort_waiting=True):
                    message = job["message"]
                    self.worker_log.write({**visible_message(message), "outcome": "shutdown_aborted",
                                           "worker_id": self.name, "http_body_bytes": len(job["body"]),
                                           "admission_ns": job["admission_ns"],
                                           "terminal_ns": job["terminal_ns"],
                                           "request_received_ns": job["receive_ns"],
                                           "body_read_ns": job["body_read_ns"],
                                           "connection_id": job["connection_id"]})
                    job["response_status"] = 503
                    job["response_body"] = json_bytes({"id": message["id"], "error": "worker_shutdown",
                                                        "worker_id": self.name})
                    with self.count_lock:
                        self.counts["shutdown_aborted"] += 1
                    job["done"].set()
                self.stop.set()
                for thread in self.workers:
                    thread.join(timeout=float(self.config["request_timeout_s"]) + 5)
                if any(thread.is_alive() for thread in self.workers):
                    raise RuntimeError("Worker queue did not drain during shutdown")
            else:
                self.stop.set()
            grace = time.monotonic() + float(self.config["request_timeout_s"]) + 2
            while time.monotonic() < grace:
                with self.server.inflight_lock:
                    active = self.server.inflight
                if active == 0:
                    break
                time.sleep(0.01)
            self.server.server_close()
            if active:
                raise RuntimeError("HTTP requests remained active during shutdown")
            self.finish(counts=dict(self.counts),
                        **(self.queue.state() if self.role == "worker" else {"pending": 0}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("robot", "worker", "edge", "proxy"), required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    component = None
    try:
        component = Robot(config, args.name, args.output) if args.role == "robot" else Service(
            config, args.name, args.role, args.output)
        component.run()
    except Exception as exc:
        write_json(args.output / "failure.json", {"name": args.name, "role": args.role,
                   "time_ns": time.monotonic_ns(), "error_type": type(exc).__name__, "error": str(exc)})
        if component is not None:
            for log in component.logs.values():
                log.close()
        raise


if __name__ == "__main__":
    main()
