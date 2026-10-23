"""Additive E1: actual ROS sources and controlled gateway/server/network limits.

Run with the ROS2 environment sourced. The original baseline and results are
unchanged. Every dedicated-network condition uses the same namespace/veth path.
"""
import argparse
from collections import Counter, deque
import csv
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import http.client
import json
import multiprocessing as mp
import os
from pathlib import Path
import platform
import random
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid

from baseline import save_csv, save_json, percentile, generator_process
from metrics import clipped_duration_ns, cpu_percent, deadline_stats


def read_csv(path):
    with Path(path).open(newline="") as stream:
        return list(csv.DictReader(stream))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def host_metadata():
    def read(path):
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None
    def link(path):
        try:
            return os.readlink(path)
        except OSError:
            return None
    boot = read("/proc/sys/kernel/random/boot_id")
    time_ns = link("/proc/self/ns/time")
    return {
        "platform": platform.platform(), "hostname": socket.gethostname(),
        "python": platform.python_version(), "logical_cpus": os.cpu_count(),
        "affinity_cpus": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "boot_id": boot, "time_namespace": time_ns,
        "time_namespace_offsets": read("/proc/self/timens_offsets"),
        "network_namespace": link("/proc/self/ns/net"),
        "clock": vars(time.get_clock_info("monotonic")),
        "clock_domain_id": f"{boot}:{time_ns}:CLOCK_MONOTONIC",
        "wall_time_ns": time.time_ns(), "monotonic_reference_ns": time.monotonic_ns(),
        "ros_distro": os.environ.get("ROS_DISTRO"), "rmw_implementation_env": os.environ.get("RMW_IMPLEMENTATION"),
        "ros_domain_id": os.environ.get("ROS_DOMAIN_ID", "0"),
        "ros_localhost_only": os.environ.get("ROS_LOCALHOST_ONLY", "0"),
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "cpu_normalization": "100 percent equals one logical CPU; host capacity normalized separately",
    }


def validate_config(config):
    counts = config.get("actual_source_counts", [1, 2, 4, 8])
    if not counts or any(not isinstance(n, int) or n < 1 for n in counts):
        raise ValueError("actual_source_counts must contain positive integers")
    if len(counts) != len(set(counts)):
        raise ValueError("Duplicate actual source counts")
    if config["duration_s"] <= 0 or config["warmup_s"] < 0 or config["drain_s"] < 0:
        raise ValueError("Invalid run duration")
    if config["queue_capacity"] < 1 or config["request_timeout_s"] <= 0 or config["repeats"] < 1:
        raise ValueError("Invalid queue/timeout/repetition count")
    conditions = config["conditions"]
    ids = [c["id"] for c in conditions]
    if len(ids) != len(set(ids)) or not ids:
        raise ValueError("Conditions must have unique IDs")
    for condition in conditions:
        if not condition["id"].replace("_", "").isalnum():
            raise ValueError("Condition ID must be alphanumeric/underscore")
        for field in ("gateway_dispatch_delay_ms", "server_processing_delay_ms"):
            if condition.get(field, 0) < 0:
                raise ValueError(f"Negative {field}")
        if condition.get("network_rate_mbit") is not None and condition["network_rate_mbit"] <= 0:
            raise ValueError("Nonpositive network rate")
        selected = condition.get("source_counts", counts)
        if not selected or len(selected) != len(set(selected)) or any(n not in counts for n in selected):
            raise ValueError("Condition source_counts must be a nonempty unique subset of actual_source_counts")
    for kind, traffic in config["traffic"].items():
        if traffic["rate_hz"] <= 0 or traffic["payload_bytes"] < 0 or kind not in config["deadlines_ms"]:
            raise ValueError("Invalid traffic/deadline configuration")


def resolved_run_config(sweep, condition, count, repeat, folder):
    """Force source scale=1: node count and traffic multiplier cannot be confused."""
    config = dict(sweep)
    config.update(condition)
    config.update(
        experiment="E1", subexperiment="actual_sources_controlled_limits", topology="T0_SHARED",
        experiment_id=folder.name, condition_id=condition["id"], repeat=repeat,
        seed=sweep["seed"] + repeat, robots=count, actual_sources=count,
        actual_ros_robots=count, max_ros_robots=count, source_rate_multiplier=1,
        gateway_dispatch_delay_ms=condition.get("gateway_dispatch_delay_ms", 0),
        server_processing_delay_ms=condition.get("server_processing_delay_ms", 0),
        network_rate_mbit=condition.get("network_rate_mbit"),
        protocol="HTTP/1.1", queue_policy="FIFO", application_arq=False,
        max_in_flight=1, gateway_count=1, server_count=1, overflow_policy="drop_tail",
        ros_qos="reliable_keep_last_1000", schema_version=3,
        utc_created=datetime.now(timezone.utc).isoformat(),
        nominal_offered_rps=count * sum(t["rate_hz"] for t in sweep["traffic"].values()),
        nominal_payload_mbps=count * sum(t["rate_hz"] * t["payload_bytes"] for t in sweep["traffic"].values()) * 8 / 1e6,
        host_metadata=host_metadata(),
    )
    return config


def serve(config, folder):
    """Separate process, possibly inside a network namespace; no ROS dependency."""
    rows = []
    connections = 0
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *unused: stopping.set())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            nonlocal connections
            super().setup()
            connections += 1
            self.connection_id = connections
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.connection.settimeout(config["request_timeout_s"] + 2)

        def log_message(self, *args):
            pass

        def do_POST(self):
            handler_start = time.monotonic_ns()
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            message = json.loads(raw)
            received = time.monotonic_ns()
            if self.path != f"/robots/{message['robot_id']}/{message['message_type']}":
                raise ValueError("Request path does not match message identity")
            processing_start = time.monotonic_ns()
            if config["server_processing_delay_ms"]:
                time.sleep(config["server_processing_delay_ms"] / 1000)
            processed = time.monotonic_ns()
            reply = {
                "id": message["id"], "server_receive_ns": received,
                "server_processing_start_ns": processing_start,
                "server_processed_ns": processed, "connection_id": self.connection_id,
            }
            body = json.dumps(reply, separators=(",", ":")).encode()
            row = dict(reply, protocol=self.request_version, client_port=self.client_address[1],
                       handler_start_ns=handler_start, handler_end_ns=None,
                       request_body_bytes=len(raw), payload_bytes=len(message["payload"]), error="")
            rows.append(row)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError) as exc:
                row["error"] = repr(exc)
            finally:
                row["handler_end_ns"] = time.monotonic_ns()

    httpd = HTTPServer((config["server_bind"], config["server_port"]), Handler)
    httpd.timeout = 0.1
    save_json(folder / "server_ready.json", {
        "pid": os.getpid(), "port": httpd.server_port,
        "host_metadata": host_metadata(), "ready_ns": time.monotonic_ns(),
    })
    try:
        while not stopping.is_set() and not (folder / "server.stop").exists():
            httpd.handle_request()
    finally:
        httpd.server_close()
        save_csv(folder / "server.csv", rows)


def wait_json(path, process, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Server exited before ready: {process.returncode}")
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.05)
    raise TimeoutError("Server readiness timed out")


def queue_occupancy(events, begin, end):
    area, maximum, previous, length = 0, 0, begin, 0
    for event in events:
        timestamp, next_length = int(event["timestamp_ns"]), int(event["queue_length"])
        if timestamp < begin:
            length = next_length
            continue
        if timestamp >= end:
            break
        area += length * (timestamp - previous)
        maximum = max(maximum, length, next_length)
        previous, length = timestamp, next_length
    area += length * (end - previous)
    return area / (end - begin), max(maximum, length), length


def summarize(folder):
    config = json.loads((folder / "config.json").read_text())
    generated, messages, events, server, cpu = [read_csv(folder / n) for n in
        ("generated.csv", "messages.csv", "queue.csv", "server.csv", "cpu.csv")]
    begin, end = config["measurement_start_ns"], config["measurement_end_ns"]
    duration = (end - begin) / 1e9
    cohort = [r for r in messages if begin <= int(r["source_ns"]) < end]
    offered = [r for r in generated if begin <= int(r["source_ns"]) < end]
    completed = [r for r in messages if r["outcome"] == "ok" and begin <= int(r["response_ns"]) < end]
    outcomes = Counter(r["outcome"] for r in cohort)
    received_ids = {r["id"] for r in cohort}
    mean_queue, max_queue, queue_end = queue_occupancy(events, begin, end)
    source_lateness = [(int(r["source_ns"]) - int(r["scheduled_ns"])) / 1e6 for r in offered]
    result = {
        "run": folder.name, "schema_version": 3, "condition_id": config["condition_id"],
        "actual_sources": config["actual_sources"], "actual_ros_robots": config["actual_sources"],
        "repeat": config["repeat"], "duration_s": duration,
        "generated": len(offered), "gateway_received": len(cohort),
        "ros_missing": len({r["id"] for r in offered} - received_ids),
        "nominal_offered_rps": config["nominal_offered_rps"], "offered_rps": len(offered) / duration,
        "admitted_rps": (len(cohort) - outcomes["queue_drop"]) / duration,
        "completed_rps": len(completed) / duration,
        "payload_goodput_mbps": sum(int(r["payload_bytes"]) for r in completed) * 8 / duration / 1e6,
        "mean_queue_length": mean_queue, "max_queue_length": max_queue,
        "queue_at_measurement_end": queue_end, "queue_drops": outcomes["queue_drop"],
        "request_errors": outcomes["request_error"] + outcomes["http_error"],
        "pending_after_drain": outcomes["pending"], "successful": outcomes["ok"],
        "server_connections": len({r["connection_id"] for r in server}),
        "http_versions": sorted({r["protocol"] for r in server}),
        "source_lateness_p95_ms": percentile(source_lateness, 95),
        "source_lateness_max_ms": max(source_lateness) if source_lateness else None,
        "classes": {}, "deadlines": {}, "source_rates": [],
    }
    for name, rows, start, finish in [
        ("server_handler", server, "handler_start_ns", "handler_end_ns"),
        ("server_processing", server, "server_processing_start_ns", "server_processed_ns"),
        ("sender", messages, "dequeue_ns", "response_ns"),
        ("gateway_dispatch", messages, "dequeue_ns", "request_start_ns"),
        ("http", messages, "request_start_ns", "response_ns"),
    ]:
        busy = sum(clipped_duration_ns(int(r[start]), int(r[finish]) if r.get(finish) else end, begin, end)
                   for r in rows if r.get(start))
        result[f"{name}_busy_percent"] = 100 * busy / (end - begin)
    for process in ("server", "gateway", "source"):
        percent = cpu_percent(cpu, f"{process}_cpu_s", begin, end)
        result[f"{process}_cpu_percent"] = percent
        result[f"{process}_host_cpu_percent"] = percent / config["host_metadata"]["logical_cpus"]
        memory = [int(r[f"{process}_rss_bytes"]) for r in cpu if begin <= int(r["timestamp_ns"]) < end]
        result[f"{process}_max_rss_bytes"] = max(memory, default=0)
    for kind, target in config["deadlines_ms"].items():
        result["deadlines"][kind] = deadline_stats([r for r in offered if r["message_type"] == kind], cohort, target)
    intervals = [
        ("end_to_end", "source_ns", "response_ns"), ("queue_wait", "enqueue_ns", "dequeue_ns"),
        ("gateway_dispatch", "dequeue_ns", "request_start_ns"), ("http", "request_start_ns", "response_ns"),
        ("ros_to_gateway", "source_ns", "enqueue_ns"),
        ("request_to_server", "request_start_ns", "server_receive_ns"),
        ("server_processing", "server_receive_ns", "server_processed_ns"),
        ("server_injected_service", "server_processing_start_ns", "server_processed_ns"),
        ("response_return", "server_processed_ns", "response_ns"),
    ]
    for kind in ["all"] + list(config["traffic"]):
        rows = [r for r in cohort if (kind == "all" or r["message_type"] == kind) and r["outcome"] == "ok"]
        stats = {"successful": len(rows)}
        for name, start, finish in intervals:
            values = [(int(r[finish]) - int(r[start])) / 1e6 for r in rows]
            stats[f"{name}_mean_ms"] = sum(values) / len(values) if values else None
            for p in (50, 95, 99):
                stats[f"{name}_p{p}_ms"] = percentile(values, p)
        result["classes"][kind] = stats
    for robot in range(config["actual_sources"]):
        for kind, traffic in config["traffic"].items():
            rows = [r for r in offered if int(r["robot_id"]) == robot and r["message_type"] == kind]
            result["source_rates"].append({"robot_id": robot, "message_type": kind,
                "configured_hz": traffic["rate_hz"], "measured_hz": len(rows) / duration, "count": len(rows)})
    save_json(folder / "summary.json", result)
    return result


def run(config, folder):
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
    from std_msgs.msg import String
    from rest_traffic import generator

    folder.mkdir(parents=True, exist_ok=False)
    records, events, cpu_rows = [], [], []
    fifo, condition = deque(), threading.Condition()
    stopping, stop_cpu = threading.Event(), threading.Event()
    busy = 0
    network = server = source = node = executor = None
    spin = sender = cpu_thread = None
    server_log = None
    failure = None
    capture = bool(config.get("capture", False))
    initialized = False

    def event():
        events.append({"timestamp_ns": time.monotonic_ns(), "queue_length": len(fifo), "in_flight": busy})

    def receive(msg, ros_topic):
        message = json.loads(msg.data)
        row = {k: v for k, v in message.items() if k != "payload"}
        row.update(enqueue_ns=time.monotonic_ns(), dequeue_ns=None, request_start_ns=None,
                   response_ns=None, server_receive_ns=None, server_processing_start_ns=None,
                   server_processed_ns=None, http_status=None, connection_id=None, http_version=None,
                   outcome="pending", error="", request_body_bytes=0, retry_count=0, ros_topic=ros_topic)
        with condition:
            records.append(row)
            if len(fifo) >= config["queue_capacity"]:
                row["outcome"] = "queue_drop"
            else:
                fifo.append((message, row))
            event()
            condition.notify()

    def send():
        nonlocal busy
        connection = http.client.HTTPConnection(config["server_host"], config["server_port"], timeout=config["request_timeout_s"])
        try:
            while True:
                with condition:
                    condition.wait_for(lambda: fifo or stopping.is_set())
                    if stopping.is_set():
                        break
                    message, row = fifo.popleft()
                    row["dequeue_ns"] = time.monotonic_ns()
                    busy = 1
                    event()
                try:
                    if config["gateway_dispatch_delay_ms"]:
                        time.sleep(config["gateway_dispatch_delay_ms"] / 1000)
                    body = json.dumps(message, separators=(",", ":")).encode()
                    row["request_body_bytes"] = len(body)
                    row["request_start_ns"] = time.monotonic_ns()
                    connection.request("POST", f"/robots/{message['robot_id']}/{message['message_type']}",
                                       body=body, headers={"Content-Type": "application/json"})
                    response = connection.getresponse()
                    ack = json.loads(response.read())
                    row["response_ns"] = time.monotonic_ns()
                    row.update(http_status=response.status, http_version=response.version)
                    for field in ("server_receive_ns", "server_processing_start_ns", "server_processed_ns", "connection_id"):
                        row[field] = ack[field]
                    row["outcome"] = "ok" if response.status == 200 and ack["id"] == message["id"] else "http_error"
                    if row["response_ns"] > config.get("drain_deadline_ns", float("inf")):
                        row["outcome"] = "pending"
                        row["error"] = "Response arrived after bounded drain; timestamps retained but not counted as completed"
                except Exception as exc:
                    row.update(outcome="request_error", error=repr(exc), response_ns=time.monotonic_ns())
                    connection.close()
                finally:
                    with condition:
                        busy = 0
                        event()
        finally:
            connection.close()

    try:
        if config.get("network_mode", "dedicated_veth") == "dedicated_veth":
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
            from e1_network import NetworkHarness
            network = NetworkHarness(folder, rate_mbit=config.get("network_rate_mbit"))
            network.setup()
            verification = network.verify()
            config.update(server_host=network.server_ip, server_bind=network.server_ip,
                          server_command_prefix=network.server_prefix, network_verification=verification)
            network.snapshot("before")
        else:
            if config.get("network_rate_mbit") is not None:
                raise ValueError("Network shaping requires dedicated_veth")
            config.setdefault("server_host", "127.0.0.1")
            config.setdefault("server_bind", "127.0.0.1")
            config.setdefault("server_command_prefix", [])
        config["code_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        config["metrics_sha256"] = hashlib.sha256(Path(__file__).with_name("metrics.py").read_bytes()).hexdigest()
        config["ros_traffic_sha256"] = hashlib.sha256(Path(generator.__file__).read_bytes()).hexdigest()
        config["baseline_helpers_sha256"] = hashlib.sha256(Path(__file__).with_name("baseline.py").read_bytes()).hexdigest()
        save_json(folder / "config.json", config)
        server_log = (folder / "server_process.log").open("w")
        server_command = config["server_command_prefix"] + [sys.executable, str(Path(__file__).resolve()),
            "--server", "--config", str(folder / "config.json"), "--output", str(folder)]
        server = subprocess.Popen(server_command, stdout=server_log, stderr=subprocess.STDOUT)
        server_ready = wait_json(folder / "server_ready.json", server)
        if server_ready["host_metadata"]["clock_domain_id"] != config["host_metadata"]["clock_domain_id"]:
            raise RuntimeError("Source/gateway and server do not share a verified clock domain")
        config["server_port"] = server_ready["port"]
        config["server_pid"] = server_ready["pid"]
        config["server_host_metadata"] = server_ready["host_metadata"]
        if capture and network:
            network.start_capture(config["server_port"])

        ctx = mp.get_context("spawn")
        start, ready = ctx.Value("d", 0), ctx.Queue()
        prefix = "/e1_extended/" + folder.name.replace("-", "_")
        topics = generator.topic_specs(config, prefix)
        save_json(folder / "topics.json", topics)
        source = ctx.Process(target=generator_process, args=(config, str(folder), prefix, start, ready))
        rclpy.init()
        initialized = True
        node = rclpy.create_node("e1_extended_gateway")
        qos = QoSProfile(depth=1000, reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST)
        for spec in topics:
            node.create_subscription(String, spec["topic"], lambda msg, topic=spec["topic"]: receive(msg, topic), qos)
        executor = SingleThreadedExecutor()
        executor.add_node(node)
        spin, sender = threading.Thread(target=executor.spin), threading.Thread(target=send)
        spin.start()
        sender.start()
        source.start()
        config["source_pid"], config["gateway_pid"] = source.pid, os.getpid()

        def sample_cpu():
            ticks = os.sysconf("SC_CLK_TCK")
            page_size = os.sysconf("SC_PAGE_SIZE")
            previous = {name: (0.0, 0) for name in ("server", "gateway", "source")}
            while True:
                sample = {}
                for name in previous:
                    try:
                        fields = Path(f"/proc/{config[name + '_pid']}/stat").read_text().rsplit(")", 1)[1].split()
                        previous[name] = ((int(fields[11]) + int(fields[12])) / ticks, int(fields[21]) * page_size)
                    except (FileNotFoundError, ProcessLookupError):
                        pass
                    sample[f"{name}_cpu_s"], sample[f"{name}_rss_bytes"] = previous[name]
                sample["timestamp_ns"] = time.monotonic_ns()
                cpu_rows.append(sample)
                if stop_cpu.wait(config["cpu_sample_interval_s"]):
                    break
        cpu_thread = threading.Thread(target=sample_cpu)
        cpu_thread.start()
        ready.get(timeout=30)
        graph_deadline = time.monotonic() + 10
        while True:
            actual_nodes = sorted([{"name": name, "namespace": ns} for name, ns in node.get_node_names_and_namespaces()
                                   if name == "traffic_source" and ns.startswith(prefix + "/")], key=lambda x: x["namespace"])
            publisher_counts = {spec["topic"]: len(node.get_publishers_info_by_topic(spec["topic"])) for spec in topics}
            if len(actual_nodes) == config["actual_sources"] and all(n == 1 for n in publisher_counts.values()):
                break
            if time.monotonic() > graph_deadline:
                raise RuntimeError("Actual ROS source nodes/topics do not match configured counts")
            time.sleep(0.1)
        save_json(folder / "ros_graph.json", {"actual_nodes": actual_nodes, "topic_publisher_counts": publisher_counts,
                   "configured_topics": topics, "verified_ns": time.monotonic_ns()})
        config["actual_launched_source_nodes"] = len(actual_nodes)
        config["actual_publisher_topics"] = len(publisher_counts)
        config["rmw_implementation"] = rclpy.utilities.get_rmw_implementation_identifier()
        start.value = time.monotonic() + 0.5
        begin = int((start.value + config["warmup_s"]) * 1e9)
        end = begin + int(config["duration_s"] * 1e9)
        config.update(source_start_ns=int(start.value * 1e9), measurement_start_ns=begin,
                      measurement_end_ns=end, drain_deadline_ns=end + int(config["drain_s"] * 1e9))
        config["config_sha256"] = digest(config)
        save_json(folder / "config.json", config)
        source.join(timeout=config["warmup_s"] + config["duration_s"] + 10)
        if source.is_alive() or source.exitcode != 0:
            raise RuntimeError("Traffic generator did not finish successfully")
        generated_count = len(read_csv(folder / "generated.csv"))
        while time.monotonic_ns() < config["drain_deadline_ns"]:
            if server.poll() is not None:
                raise RuntimeError("Server exited during run")
            with condition:
                if not fifo and not busy and len(records) == generated_count:
                    break
            time.sleep(0.05)
        save_json(folder / "drain.json", {"observed_end_ns": time.monotonic_ns(), "deadline_ns": config["drain_deadline_ns"],
                   "waiting": len(fifo), "in_flight": busy, "generated_all": generated_count,
                   "gateway_received_all": len(records)})
    except BaseException as exc:
        failure = exc
        save_json(folder / "invalid.json", {"reason": repr(exc), "traceback": traceback.format_exc(),
                   "infrastructure_invalid": True})
    finally:
        stopping.set()
        with condition:
            condition.notify_all()
        if executor is not None:
            executor.shutdown(timeout_sec=3)
        if spin is not None and spin.ident:
            spin.join(timeout=3)
        if sender is not None and sender.ident:
            sender.join(timeout=config["request_timeout_s"] + config["gateway_dispatch_delay_ms"] / 1000 + 3)
        if source is not None and source.is_alive():
            source.terminate()
            source.join(timeout=5)
        stop_cpu.set()
        if cpu_thread is not None and cpu_thread.ident:
            cpu_thread.join(timeout=3)
        if server is not None:
            (folder / "server.stop").touch()
            try:
                server.wait(timeout=config["request_timeout_s"] + 5)
            except subprocess.TimeoutExpired:
                # Target the server process in its owned namespace, not an unrelated host process.
                if config.get("server_pid"):
                    subprocess.run(config.get("server_command_prefix", []) + ["kill", "-TERM", str(config["server_pid"])], check=False)
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
                failure = failure or RuntimeError("Server required forced shutdown")
        if server_log is not None:
            server_log.close()
        if node is not None:
            node.destroy_node()
        if initialized:
            rclpy.shutdown()
        save_csv(folder / "messages.csv", records)
        save_csv(folder / "queue.csv", events)
        save_csv(folder / "cpu.csv", cpu_rows)
        if network is not None:
            try:
                if capture:
                    network.stop_capture()
                network.snapshot("after")
            finally:
                network.cleanup()
    if failure is not None:
        raise failure
    if server.returncode != 0 or sender.is_alive():
        raise RuntimeError("Unclean server/sender shutdown")
    try:
        result = summarize(folder)
        from validate_e1_extended import validate_run
        validity = validate_run(folder)
        save_json(folder / "validation.json", validity)
        return result
    except Exception as exc:
        save_json(folder / "invalid.json", {"reason": repr(exc), "traceback": traceback.format_exc(),
                   "infrastructure_invalid": True, "stage": "offline_summary_or_validation"})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--server", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Skip existing validated cells; retain invalid/incomplete runs")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    folder = args.output.resolve()
    if args.server:
        serve(config, folder)
        return
    validate_config(config)
    folder.mkdir(parents=True, exist_ok=args.resume)
    if args.resume and (folder / "sweep_config.json").exists():
        if json.loads((folder / "sweep_config.json").read_text()) != config:
            raise ValueError("Resume configuration differs from saved sweep")
    else:
        save_json(folder / "sweep_config.json", config)
    from rest_traffic import generator
    snapshots = {"runner_snapshot.py": Path(__file__), "baseline_helpers_snapshot.py": Path(__file__).with_name("baseline.py"),
                 "metrics_snapshot.py": Path(__file__).with_name("metrics.py"), "traffic_generator_snapshot.py": Path(generator.__file__),
                 "validator_snapshot.py": Path(__file__).with_name("validate_e1_extended.py")}
    network_path = Path(__file__).resolve().parents[1] / "scripts" / "e1_network.py"
    if network_path.exists():
        snapshots["network_harness_snapshot.py"] = network_path
    for name, path in snapshots.items():
        destination = folder / name
        if args.resume and destination.exists() and destination.read_bytes() != path.read_bytes():
            raise ValueError(f"Resume code differs: {name}")
        destination.write_bytes(path.read_bytes())
    plan = [(c["id"], count, repeat) for repeat in range(1, config["repeats"] + 1)
            for c in config["conditions"] for count in c.get("source_counts", config.get("actual_source_counts", [1, 2, 4, 8]))]
    random.Random(config["seed"]).shuffle(plan)
    save_json(folder / "run_order.json", plan)
    completed = set()
    for path in folder.glob("*/validation.json"):
        if json.loads(path.read_text()).get("passed"):
            old = json.loads((path.parent / "config.json").read_text())
            completed.add((old["condition_id"], old["actual_sources"], old["repeat"]))
    by_condition = {c["id"]: c for c in config["conditions"]}
    for condition_id, count, repeat in plan:
        if (condition_id, count, repeat) in completed:
            continue
        run_folder = folder / f"{condition_id}_n{count:03d}_r{repeat:02d}_{uuid.uuid4().hex[:8]}"
        print(f"START condition={condition_id} actual_sources={count} repeat={repeat} {run_folder}", flush=True)
        result = run(resolved_run_config(config, by_condition[condition_id], count, repeat, run_folder), run_folder)
        print(f"DONE condition={condition_id} actual_sources={count} completed={result['completed_rps']:.2f}/s "
              f"queue={result['mean_queue_length']:.2f} drops={result['queue_drops']} "
              f"p95_ms={result['classes']['all']['end_to_end_p95_ms']:.3f}", flush=True)
        time.sleep(config.get("inter_run_pause_s", 1))


if __name__ == "__main__":
    main()
