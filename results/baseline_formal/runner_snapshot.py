"""Actual ROS2 -> bounded FIFO -> persistent HTTP/1.1 baseline.

Run on Linux after sourcing ROS2 Humble. Standard library except rclpy/std_msgs.
All timestamps use the same host's monotonic clock. No application retries.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import http.client
import json
import multiprocessing as mp
import os
from pathlib import Path
import platform
import random
import socket
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def save_csv(path, rows, fields=None):
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields or list(rows[0]) if rows else fields or [])
        writer.writeheader()
        writer.writerows(rows)


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    x = (len(values) - 1) * p / 100
    a = int(x)
    return values[a] + (values[min(a + 1, len(values) - 1)] - values[a]) * (x - a)


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
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            message = json.loads(raw)
            received = time.monotonic_ns()
            expected = "/robots/{}/{}".format(message["robot_id"], message["message_type"])
            assert self.path == expected
            body = json.dumps({"id": message["id"], "server_receive_ns": received,
                               "connection_id": self.connection_id}).encode()
            rows.append({"id": message["id"], "server_receive_ns": received,
                         "connection_id": self.connection_id, "protocol": self.request_version,
                         "request_body_bytes": len(raw), "payload_bytes": len(message["payload"])})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

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
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
    from std_msgs.msg import String
    rclpy.init()
    node = rclpy.create_node("baseline_source")
    qos = QoSProfile(depth=1000, reliability=ReliabilityPolicy.RELIABLE,
                     history=HistoryPolicy.KEEP_LAST)
    publisher = node.create_publisher(String, topic, qos)
    deadline = time.monotonic() + 20
    while publisher.get_subscription_count() == 0:
        if time.monotonic() > deadline:
            raise RuntimeError("ROS2 subscriber discovery timed out")
        time.sleep(0.05)
    ready.put(True)
    while start.value == 0:
        time.sleep(0.01)
    rng = random.Random(config["seed"])
    schedule = []
    for robot in range(config["robots"]):
        for kind, spec in config["traffic"].items():
            period = 1 / spec["rate_hz"]
            heapq.heappush(schedule, (start.value + rng.random() * period, robot, kind, 0))
    end = start.value + config["warmup_s"] + config["duration_s"]
    rows = []
    while schedule:
        due, robot, kind, sequence = heapq.heappop(schedule)
        if due >= end:
            continue
        remaining = due - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        now = time.monotonic_ns()
        spec = config["traffic"][kind]
        message = {"experiment_id": config["experiment_id"], "robot_id": robot,
                   "message_type": kind, "sequence_number": sequence,
                   "source_ns": now, "scheduled_ns": int(due * 1e9),
                   "payload_bytes": spec["payload_bytes"], "payload": "x" * spec["payload_bytes"],
                   "id": f"{config['experiment_id']}:{robot}:{kind}:{sequence}"}
        publisher.publish(String(data=json.dumps(message, separators=(",", ":"))))
        rows.append({k: v for k, v in message.items() if k != "payload"})
        heapq.heappush(schedule, (due + 1 / spec["rate_hz"], robot, kind, sequence + 1))
    # Allow reliable DDS delivery before destroying the publisher.
    time.sleep(0.5)
    save_csv(Path(folder) / "generated.csv", rows)
    node.destroy_node()
    rclpy.shutdown()


def run(config, folder):
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
    from std_msgs.msg import String

    folder.mkdir(parents=True, exist_ok=False)
    config = dict(config, experiment_id=folder.name, protocol="HTTP/1.1", queue_policy="FIFO",
                  network="loopback_clean", application_arq=False, max_in_flight=1,
                  overflow_policy="drop_tail", ros_qos="reliable_keep_last_1000",
                  host=platform.platform(), python=platform.python_version(),
                  utc_created=datetime.now(timezone.utc).isoformat(),
                  code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    save_json(folder / "config.json", config)
    ctx = mp.get_context("spawn")
    server_ready, source_ready = ctx.Queue(), ctx.Queue()
    stop_server, start = ctx.Event(), ctx.Value("d", 0)
    server = ctx.Process(target=server_process, args=(str(folder), server_ready, stop_server, config.get("server_port", 18061)))
    server.start()
    port = server_ready.get(timeout=15)
    topic = "/baseline/" + folder.name.replace("-", "_")
    source = ctx.Process(target=generator_process, args=(config, str(folder), topic, start, source_ready))
    fifo, records, events = deque(), [], []
    condition = threading.Condition()
    finish = False
    busy = 0

    def event():
        events.append({"timestamp_ns": time.monotonic_ns(), "queue_length": len(fifo), "in_flight": busy})

    def receive(msg):
        message = json.loads(msg.data)
        row = {k: v for k, v in message.items() if k != "payload"}
        row.update(enqueue_ns=time.monotonic_ns(), dequeue_ns=None, response_ns=None,
                   server_receive_ns=None, http_status=None, connection_id=None,
                   http_version=None, outcome="pending", error="", request_body_bytes=0)
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
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=config["request_timeout_s"])
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
                    connection.request("POST", f"/robots/{message['robot_id']}/{message['message_type']}",
                                       body=body, headers={"Content-Type": "application/json"})
                    response = connection.getresponse()
                    ack = json.loads(response.read())
                    row["response_ns"] = time.monotonic_ns()
                    row.update(http_status=response.status, http_version=response.version,
                               server_receive_ns=ack["server_receive_ns"], connection_id=ack["connection_id"])
                    row["outcome"] = "ok" if response.status == 200 and ack["id"] == message["id"] else "http_error"
                except Exception as exc:
                    row.update(outcome="request_error", error=repr(exc), response_ns=time.monotonic_ns())
                    connection.close()  # No replay/retry of this message.
                with condition:
                    busy = 0
                    event()
        finally:
            connection.close()

    rclpy.init()
    node = rclpy.create_node("baseline_gateway")
    qos = QoSProfile(depth=1000, reliability=ReliabilityPolicy.RELIABLE,
                     history=HistoryPolicy.KEEP_LAST)
    node.create_subscription(String, topic, receive, qos)
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    spin = threading.Thread(target=executor.spin)
    sender = threading.Thread(target=send)
    try:
        spin.start()
        sender.start()
        source.start()
        source_ready.get(timeout=25)
        start.value = time.monotonic() + 0.5
        begin = start.value + config["warmup_s"]
        end = begin + config["duration_s"]
        config.update(measurement_start_ns=int(begin * 1e9), measurement_end_ns=int(end * 1e9), server_port=port)
        save_json(folder / "config.json", config)
        source.join(timeout=config["warmup_s"] + config["duration_s"] + 10)
        if source.is_alive() or source.exitcode != 0:
            raise RuntimeError("Traffic generator did not finish successfully")
        drain_end = end + config["drain_s"]
        while time.monotonic() < drain_end:
            with condition:
                if not fifo and not busy:
                    break
            time.sleep(0.05)
    finally:
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
    if server.exitcode != 0 or sender.is_alive():
        raise RuntimeError("Unclean server/sender shutdown")
    return summarize(folder)


def summarize(folder):
    config = json.loads((folder / "config.json").read_text())
    def read(name):
        with (folder / name).open() as f:
            return list(csv.DictReader(f))
    generated, messages, events, server = [read(n) for n in ("generated.csv", "messages.csv", "queue.csv", "server.csv")]
    begin, end = config["measurement_start_ns"], config["measurement_end_ns"]
    duration = (end - begin) / 1e9
    cohort = [r for r in messages if begin <= int(r["source_ns"]) < end]
    offered = [r for r in generated if begin <= int(r["source_ns"]) < end]
    completed = [r for r in messages if r["outcome"] == "ok" and begin <= int(r["response_ns"]) < end]
    area, max_q, previous, length = 0, 0, begin, 0
    for e in events:
        t = int(e["timestamp_ns"])
        if t < begin:
            length = int(e["queue_length"])
            continue
        if t >= end:
            break
        area += length * (t - previous)
        max_q = max(max_q, length, int(e["queue_length"]))
        previous, length = t, int(e["queue_length"])
    area += length * (end - previous)
    result = {"run": folder.name, "robots": config["robots"], "repeat": config["repeat"],
              "duration_s": duration, "generated": len(offered), "gateway_received": len(cohort),
              "ros_missing": len(set(r["id"] for r in offered) - set(r["id"] for r in cohort)),
              "offered_rps": len(offered) / duration, "completed_rps": len(completed) / duration,
              "payload_goodput_mbps": sum(int(r["payload_bytes"]) for r in completed) * 8 / duration / 1e6,
              "mean_queue_length": area / (end - begin), "max_queue_length": max_q,
              "queue_at_measurement_end": length,
              "queue_drops": sum(r["outcome"] == "queue_drop" for r in cohort),
              "request_errors": sum(r["outcome"] in ("request_error", "http_error") for r in cohort),
              "pending_after_drain": sum(r["outcome"] == "pending" for r in cohort),
              "server_connections": len(set(r["connection_id"] for r in server)),
              "http_versions": sorted(set(r["protocol"] for r in server)),
              "source_lateness_p95_ms": percentile([(int(r["source_ns"]) - int(r["scheduled_ns"])) / 1e6 for r in offered], 95),
              "classes": {}}
    for kind in ["all"] + list(config["traffic"]):
        rows = [r for r in cohort if (kind == "all" or r["message_type"] == kind) and r["outcome"] == "ok"]
        stats = {"successful": len(rows)}
        for label, a, b in [("end_to_end", "source_ns", "response_ns"),
                            ("queue_wait", "enqueue_ns", "dequeue_ns"),
                            ("http", "dequeue_ns", "response_ns"),
                            ("ros_to_gateway", "source_ns", "enqueue_ns")]:
            values = [(int(r[b]) - int(r[a])) / 1e6 for r in rows]
            stats[label + "_mean_ms"] = sum(values) / len(values) if values else None
            for p in (50, 95, 99):
                stats[f"{label}_p{p}_ms"] = percentile(values, p)
        result["classes"][kind] = stats
    all_generated = {r["id"] for r in generated}
    all_received = {r["id"] for r in messages}
    successes = {r["id"] for r in messages if r["outcome"] == "ok"}
    server_ids = {r["id"] for r in server}
    result["validation"] = {
        "unique_generated_ids": len(all_generated) == len(generated),
        "unique_gateway_ids": len(all_received) == len(messages),
        "unique_server_ids": len(server_ids) == len(server),
        "all_successes_at_server": successes <= server_ids,
        "all_gateway_ids_generated": all_received <= all_generated,
        "timestamp_order": all(int(r["source_ns"]) <= int(r["enqueue_ns"]) <= int(r["dequeue_ns"]) <= int(r["server_receive_ns"]) <= int(r["response_ns"]) for r in messages if r["outcome"] == "ok"),
        "http11": result["http_versions"] == ["HTTP/1.1"],
        "one_persistent_connection": result["server_connections"] == 1,
    }
    save_json(folder / "summary.json", result)
    if not all(result["validation"].values()):
        raise RuntimeError(f"Validation failed: {result['validation']}")
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
    plan = [(robot, repeat) for repeat in range(1, config["repeats"] + 1) for robot in config["robot_counts"]]
    random.Random(config["seed"]).shuffle(plan)
    save_json(args.output / "run_order.json", plan)
    for robot, repeat in plan:
        folder = args.output / f"n{robot:03d}_r{repeat:02d}_{uuid.uuid4().hex[:8]}"
        print(f"START robots={robot} repeat={repeat} {folder}", flush=True)
        result = run(dict(config, robots=robot, repeat=repeat, seed=config["seed"] + repeat), folder)
        print("DONE " + json.dumps({k: result[k] for k in ("robots", "completed_rps", "mean_queue_length", "queue_drops", "ros_missing")}) + f" p95_ms={result['classes']['all']['end_to_end_p95_ms']:.3f}", flush=True)


if __name__ == "__main__":
    main()
