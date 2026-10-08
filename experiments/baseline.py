"""Experiment 1: ROS2 traffic through a FIFO queue to a local HTTP/1.1 server."""

import argparse
import csv
import hashlib
import http.client
import json
import multiprocessing as mp
import os
from pathlib import Path
import platform
import random
import socket
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from metrics import clipped_duration_ns, cpu_percent, deadline_stats


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def save_csv(path, rows):
    fields = list(rows[0]) if rows else []

    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def percentile(values, p):
    if not values:
        return None

    values = sorted(values)
    position = (len(values) - 1) * p / 100
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)

    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def server_process(folder, ready, stop, port):
    rows = []
    connections = 0

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            nonlocal connections

            super().setup()
            connections += 1
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.connection_id = connections

        def log_message(self, *args):
            pass

        def do_POST(self):
            handler_start = time.monotonic_ns()
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            message = json.loads(raw)
            received = time.monotonic_ns()

            expected = f"/robots/{message['robot_id']}/{message['message_type']}"
            assert self.path == expected

            reply = {
                "id": message["id"],
                "server_receive_ns": received,
                "connection_id": self.connection_id,
            }
            body = json.dumps(reply).encode()

            row = {
                "id": message["id"],
                "server_receive_ns": received,
                "connection_id": self.connection_id,
                "protocol": self.request_version,
                "client_port": self.client_address[1],
                "handler_start_ns": handler_start,
                "handler_end_ns": None,
                "request_body_bytes": len(raw),
                "payload_bytes": len(message["payload"]),
            }
            rows.append(row)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            row["handler_end_ns"] = time.monotonic_ns()

    httpd = HTTPServer(("127.0.0.1", port), Handler)
    httpd.timeout = 0.2
    ready.put(httpd.server_port)

    try:
        while not stop.is_set():
            httpd.handle_request()
    finally:
        httpd.server_close()
        save_csv(Path(folder) / "server.csv", rows)


def generator_process(config, folder, topic, start, ready):
    from rest_traffic.generator import generate

    rows = generate(config, topic, start, ready)
    save_csv(Path(folder) / "generated.csv", rows)


def run(config, folder):
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
    from std_msgs.msg import String
    from rest_traffic import generator

    folder.mkdir(parents=True, exist_ok=False)
    config = config.copy()
    config.update(
        experiment_id=folder.name,
        protocol="HTTP/1.1",
        queue_policy="FIFO",
        network="loopback_clean",
        application_arq=False,
        max_in_flight=1,
        overflow_policy="drop_tail",
        ros_qos="reliable_keep_last_1000",
        host=platform.platform(),
        python=platform.python_version(),
        utc_created=datetime.now(timezone.utc).isoformat(),
        schema_version=2,
        actual_ros_robots=min(config["robots"], config.get("max_ros_robots", 2)),
        equivalent_load=config["robots"],
    )

    # Save which source files were used for this run.
    sources = {
        "code_sha256": Path(__file__),
        "metrics_sha256": Path(__file__).with_name("metrics.py"),
        "ros_traffic_sha256": Path(generator.__file__),
    }
    for key, path in sources.items():
        config[key] = hashlib.sha256(path.read_bytes()).hexdigest()

    save_json(folder / "config.json", config)

    ctx = mp.get_context("spawn")
    server_ready = ctx.Queue()
    source_ready = ctx.Queue()
    stop_server = ctx.Event()
    start = ctx.Value("d", 0)

    server = ctx.Process(
        target=server_process,
        args=(str(folder), server_ready, stop_server, config["server_port"]),
    )
    server.start()
    port = server_ready.get(timeout=15)

    topic = "/baseline/" + folder.name.replace("-", "_")
    topics = generator.topic_specs(config, topic)
    save_json(folder / "topics.json", topics)

    source = ctx.Process(
        target=generator_process,
        args=(config, str(folder), topic, start, source_ready),
    )

    fifo = deque()
    records = []
    events = []
    condition = threading.Condition()
    finish = False
    busy = 0

    def event():
        events.append({
            "timestamp_ns": time.monotonic_ns(),
            "queue_length": len(fifo),
            "in_flight": busy,
        })

    def receive(msg, ros_topic):
        message = json.loads(msg.data)
        row = {k: v for k, v in message.items() if k != "payload"}
        row.update(
            enqueue_ns=time.monotonic_ns(),
            dequeue_ns=None,
            response_ns=None,
            server_receive_ns=None,
            http_status=None,
            connection_id=None,
            http_version=None,
            outcome="pending",
            error="",
            request_body_bytes=0,
            retry_count=0,
            ros_topic=ros_topic,
        )

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

        connection = http.client.HTTPConnection(
            "127.0.0.1", port, timeout=config["request_timeout_s"]
        )

        try:
            while True:
                with condition:
                    condition.wait_for(lambda: fifo or finish)
                    if finish:
                        break

                    message, row = fifo.popleft()
                    busy = 1
                    row["dequeue_ns"] = time.monotonic_ns()
                    event()

                try:
                    body = json.dumps(message, separators=(",", ":")).encode()
                    row["request_body_bytes"] = len(body)
                    path = f"/robots/{message['robot_id']}/{message['message_type']}"
                    connection.request(
                        "POST", path, body=body,
                        headers={"Content-Type": "application/json"},
                    )

                    response = connection.getresponse()
                    ack = json.loads(response.read())
                    row["response_ns"] = time.monotonic_ns()
                    row.update(
                        http_status=response.status,
                        http_version=response.version,
                        server_receive_ns=ack["server_receive_ns"],
                        connection_id=ack["connection_id"],
                    )

                    if response.status == 200 and ack["id"] == message["id"]:
                        row["outcome"] = "ok"
                    else:
                        row["outcome"] = "http_error"

                except Exception as exc:
                    row["outcome"] = "request_error"
                    row["error"] = repr(exc)
                    row["response_ns"] = time.monotonic_ns()
                    connection.close()  # Do not retry this message.

                with condition:
                    busy = 0
                    event()
        finally:
            connection.close()

    rclpy.init()
    node = rclpy.create_node("baseline_gateway")
    qos = QoSProfile(
        depth=1000,
        reliability=ReliabilityPolicy.RELIABLE,
        history=HistoryPolicy.KEEP_LAST,
    )

    for spec in topics:
        callback = lambda msg, name=spec["topic"]: receive(msg, name)
        node.create_subscription(String, spec["topic"], callback, qos)

    executor = SingleThreadedExecutor()
    executor.add_node(node)
    spin = threading.Thread(target=executor.spin)
    sender = threading.Thread(target=send)
    cpu_rows = []
    stop_cpu = threading.Event()

    def sample_cpu():
        ticks = os.sysconf("SC_CLK_TCK")
        # Keep the source's final CPU count after it exits.
        previous = {"server": 0.0, "gateway": 0.0, "source": 0.0}
        processes = {"server": server.pid, "gateway": os.getpid(), "source": source.pid}

        while not stop_cpu.is_set():
            for name, pid in processes.items():
                try:
                    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                    previous[name] = (int(fields[11]) + int(fields[12])) / ticks
                except FileNotFoundError:
                    pass
            sample = {"timestamp_ns": time.monotonic_ns()}
            for name, value in previous.items():
                sample[f"{name}_cpu_s"] = value

            cpu_rows.append(sample)
            stop_cpu.wait(config["cpu_sample_interval_s"])

    cpu_thread = threading.Thread(target=sample_cpu)

    try:
        spin.start()
        sender.start()
        source.start()
        cpu_thread.start()
        source_ready.get(timeout=25)

        start.value = time.monotonic() + 0.5
        begin = start.value + config["warmup_s"]
        end = begin + config["duration_s"]
        config.update(
            measurement_start_ns=int(begin * 1e9),
            measurement_end_ns=int(end * 1e9),
            server_port=port,
            wall_time_ns=time.time_ns(),
            monotonic_reference_ns=time.monotonic_ns(),
        )
        canonical = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        config["config_sha256"] = hashlib.sha256(canonical).hexdigest()
        save_json(folder / "config.json", config)

        source.join(timeout=config["warmup_s"] + config["duration_s"] + 10)
        if source.is_alive() or source.exitcode != 0:
            raise RuntimeError("Traffic generator did not finish successfully")

        with (folder / "generated.csv").open() as generated_file:
            generated_count = sum(1 for _ in csv.DictReader(generated_file))

        drain_end = end + config["drain_s"]
        while time.monotonic() < drain_end:
            with condition:
                if not fifo and not busy and len(records) == generated_count:
                    break
            time.sleep(0.05)

    finally:
        stop_cpu.set()
        if cpu_thread.ident:
            cpu_thread.join(timeout=3)

        executor.shutdown(timeout_sec=3)
        if spin.ident:
            spin.join(timeout=3)

        with condition:
            finish = True
            condition.notify_all()
        if sender.ident:
            sender.join(timeout=config["request_timeout_s"] + 3)

        if source.is_alive():
            source.terminate()
            source.join()

        stop_server.set()
        server.join(timeout=5)
        if server.is_alive():
            server.terminate()
            server.join()

        node.destroy_node()
        rclpy.shutdown()
        save_csv(folder / "messages.csv", records)
        save_csv(folder / "queue.csv", events)
        save_csv(folder / "cpu.csv", cpu_rows)

    if server.exitcode != 0 or sender.is_alive():
        raise RuntimeError("Unclean server/sender shutdown")

    return summarize(folder)


def summarize(folder):
    config = json.loads((folder / "config.json").read_text())

    def read(name):
        with (folder / name).open() as f:
            return list(csv.DictReader(f))

    generated = read("generated.csv")
    messages = read("messages.csv")
    events = read("queue.csv")
    server = read("server.csv")
    cpu = read("cpu.csv")

    begin = config["measurement_start_ns"]
    end = config["measurement_end_ns"]
    duration = (end - begin) / 1e9

    # Latency uses messages sent in the window; throughput uses responses in it.
    cohort = [r for r in messages if begin <= int(r["source_ns"]) < end]
    offered = [r for r in generated if begin <= int(r["source_ns"]) < end]
    completed = [
        r for r in messages
        if r["outcome"] == "ok" and begin <= int(r["response_ns"]) < end
    ]

    area = 0
    max_queue = 0
    previous = begin
    length = 0

    for event in events:
        timestamp = int(event["timestamp_ns"])
        next_length = int(event["queue_length"])

        if timestamp < begin:
            length = next_length
            continue

        if timestamp >= end:
            break

        area += length * (timestamp - previous)
        max_queue = max(max_queue, length, next_length)
        previous = timestamp
        length = next_length

    area += length * (end - previous)

    server_busy = 0
    for row in server:
        server_busy += clipped_duration_ns(
            int(row["handler_start_ns"]), int(row["handler_end_ns"]), begin, end
        )

    sender_busy = 0
    for row in messages:
        if row["dequeue_ns"]:
            finish = int(row["response_ns"]) if row["response_ns"] else end
            sender_busy += clipped_duration_ns(int(row["dequeue_ns"]), finish, begin, end)

    offered_ids = {r["id"] for r in offered}
    received_ids = {r["id"] for r in cohort}
    payload_bytes = sum(int(r["payload_bytes"]) for r in completed)
    source_lateness = [
        (int(r["source_ns"]) - int(r["scheduled_ns"])) / 1e6 for r in offered
    ]

    result = {
        "run": folder.name,
        "robots": config["robots"],
        "repeat": config["repeat"],
        "schema_version": 2,
        "actual_ros_robots": config["actual_ros_robots"],
        "duration_s": duration,
        "generated": len(offered),
        "gateway_received": len(cohort),
        "ros_missing": len(offered_ids - received_ids),
        "offered_rps": len(offered) / duration,
        "completed_rps": len(completed) / duration,
        "payload_goodput_mbps": payload_bytes * 8 / duration / 1e6,
        "mean_queue_length": area / (end - begin),
        "max_queue_length": max_queue,
        "queue_at_measurement_end": length,
        "queue_drops": sum(r["outcome"] == "queue_drop" for r in cohort),
        "request_errors": sum(r["outcome"] in ("request_error", "http_error") for r in cohort),
        "pending_after_drain": sum(r["outcome"] == "pending" for r in cohort),
        "server_connections": len({r["connection_id"] for r in server}),
        "http_versions": sorted({r["protocol"] for r in server}),
        "source_lateness_p95_ms": percentile(source_lateness, 95),
        "server_handler_busy_percent": 100 * server_busy / (end - begin),
        "sender_busy_percent": 100 * sender_busy / (end - begin),
        "classes": {},
        "deadlines": {},
    }

    for process in ("server", "gateway", "source"):
        result[f"{process}_cpu_percent"] = cpu_percent(cpu, f"{process}_cpu_s", begin, end)

    for kind, target in config["deadlines_ms"].items():
        sources = [r for r in offered if r["message_type"] == kind]
        result["deadlines"][kind] = deadline_stats(sources, cohort, target)

    intervals = [
        ("end_to_end", "source_ns", "response_ns"),
        ("queue_wait", "enqueue_ns", "dequeue_ns"),
        ("http", "dequeue_ns", "response_ns"),
        ("ros_to_gateway", "source_ns", "enqueue_ns"),
    ]

    for kind in ["all"] + list(config["traffic"]):
        rows = [
            r for r in cohort
            if (kind == "all" or r["message_type"] == kind) and r["outcome"] == "ok"
        ]
        stats = {"successful": len(rows)}

        for label, start_field, end_field in intervals:
            values = [(int(r[end_field]) - int(r[start_field])) / 1e6 for r in rows]
            stats[label + "_mean_ms"] = sum(values) / len(values) if values else None

            for p in (50, 95, 99):
                stats[f"{label}_p{p}_ms"] = percentile(values, p)

        result["classes"][kind] = stats

    save_json(folder / "summary.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("experiments/baseline.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--robots", type=int, nargs="+")
    parser.add_argument("--repeats", type=int)
    args = parser.parse_args()

    config = json.loads(args.config.read_text())
    if args.duration is not None:
        config["duration_s"] = args.duration
    if args.robots:
        config["robot_counts"] = args.robots
    if args.repeats:
        config["repeats"] = args.repeats

    assert config["duration_s"] > 0 and config["queue_capacity"] > 0
    args.output.mkdir(parents=True, exist_ok=False)
    save_json(args.output / "sweep_config.json", config)

    from rest_traffic import generator

    snapshots = {
        "runner_snapshot.py": Path(__file__),
        "metrics_snapshot.py": Path(__file__).with_name("metrics.py"),
        "traffic_generator_snapshot.py": Path(generator.__file__),
    }
    for name, path in snapshots.items():
        (args.output / name).write_bytes(path.read_bytes())

    # E1 needs clean loopback. This only reads the network settings.
    qdisc = subprocess.run(
        ["tc", "-j", "qdisc", "show", "dev", "lo"],
        capture_output=True, text=True, check=True,
    )
    qdisc = json.loads(qdisc.stdout)
    assert all(q["kind"] == "noqueue" for q in qdisc), "Loopback is not clean"

    ping = subprocess.run(
        ["ping", "-c", "5", "-i", "0.2", "127.0.0.1"],
        capture_output=True, text=True, check=True,
    )
    save_json(args.output / "network_verification.json", {
        "profile": "CLEAN",
        "interface": "lo",
        "qdisc": qdisc,
        "ping": ping.stdout,
    })

    plan = []
    for repeat in range(1, config["repeats"] + 1):
        for robot in config["robot_counts"]:
            plan.append((robot, repeat))

    random.Random(config["seed"]).shuffle(plan)
    save_json(args.output / "run_order.json", plan)

    for robot, repeat in plan:
        folder = args.output / f"n{robot:03d}_r{repeat:02d}_{uuid.uuid4().hex[:8]}"
        print(f"START robots={robot} repeat={repeat} {folder}", flush=True)

        run_config = dict(config, robots=robot, repeat=repeat, seed=config["seed"] + repeat)
        result = run(run_config, folder)

        print(
            f"DONE robots={robot} "
            f"completed={result['completed_rps']:.1f}/s "
            f"mean_queue={result['mean_queue_length']:.2f} "
            f"drops={result['queue_drops']} "
            f"missing={result['ros_missing']} "
            f"p95_ms={result['classes']['all']['end_to_end_p95_ms']}",
            flush=True,
        )


if __name__ == "__main__":
    main()
