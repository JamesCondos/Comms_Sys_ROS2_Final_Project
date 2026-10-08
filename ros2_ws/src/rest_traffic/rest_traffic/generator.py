"""Publish scheduled ROS2 traffic independently of HTTP responses."""

import argparse
import heapq
import json
from pathlib import Path
import random
import time


def topic_specs(config, prefix):
    count = min(config["robots"], config.get("max_ros_robots", 2))
    scale = config["robots"] / count
    specs = []

    for robot in range(count):
        for kind, traffic in config["traffic"].items():
            specs.append({
                "robot_id": robot,
                "message_type": kind,
                "topic": f"{prefix}/robot_{robot}/{kind}",
                "rate_hz": traffic["rate_hz"] * scale,
                "payload_bytes": traffic["payload_bytes"],
            })

    return specs


def generate(config, prefix, start, ready, require_subscriber=True):
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
    from std_msgs.msg import String

    rclpy.init()
    nodes = {}
    publishers = {}
    by_key = {}
    rows = []

    try:
        specs = topic_specs(config, prefix)
        qos = QoSProfile(
            depth=1000,
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
        )

        for spec in specs:
            robot = spec["robot_id"]
            kind = spec["message_type"]

            if robot not in nodes:
                nodes[robot] = rclpy.create_node(
                    "traffic_source", namespace=f"{prefix}/robot_{robot}"
                )

            publishers[(robot, kind)] = nodes[robot].create_publisher(String, kind, qos)
            by_key[(robot, kind)] = spec

        if require_subscriber:
            deadline = time.monotonic() + 25
            while any(p.get_subscription_count() == 0 for p in publishers.values()):
                if time.monotonic() > deadline:
                    raise RuntimeError("ROS2 topic discovery timed out")
                time.sleep(0.05)

        ready.put(True)
        while start.value == 0:
            time.sleep(0.01)

        rng = random.Random(config["seed"])
        schedule = []

        for spec in specs:
            due = start.value + rng.random() / spec["rate_hz"]
            heapq.heappush(schedule, (due, spec["robot_id"], spec["message_type"], 0))

        end = start.value + config["warmup_s"] + config["duration_s"]

        while schedule:
            due, robot, kind, seq = heapq.heappop(schedule)
            if due >= end:
                break

            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)

            spec = by_key[(robot, kind)]
            message = {
                "experiment_id": config["experiment_id"],
                "robot_id": robot,
                "message_type": kind,
                "sequence_number": seq,
                "source_ns": time.monotonic_ns(),
                "scheduled_ns": int(due * 1e9),
                "payload_bytes": spec["payload_bytes"],
                "payload": "x" * spec["payload_bytes"],
                "id": f"{config['experiment_id']}:{robot}:{kind}:{seq}",
            }
            data = json.dumps(message, separators=(",", ":"))
            publishers[(robot, kind)].publish(String(data=data))

            row = message.copy()
            del row["payload"]
            rows.append(row)

            heapq.heappush(schedule, (due + 1 / spec["rate_hz"], robot, kind, seq + 1))

        time.sleep(0.5)
        return rows
    finally:
        for node in nodes.values():
            node.destroy_node()
        rclpy.shutdown()


def main():
    """Publish traffic for inspection with ros2 topic hz."""
    import multiprocessing as mp

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--robots", type=int, default=2)
    parser.add_argument("--duration", type=float, default=300)
    parser.add_argument("--prefix", default="/traffic_demo")
    args = parser.parse_args()

    config = json.loads(args.config.read_text())
    config.update(
        robots=args.robots,
        duration_s=args.duration,
        warmup_s=0,
        experiment_id="topic_demo",
    )
    start = mp.Value("d", time.monotonic() + 1)
    ready = mp.Queue()

    print(json.dumps(topic_specs(config, args.prefix), indent=2), flush=True)
    rows = generate(config, args.prefix, start, ready, require_subscriber=False)
    print(f"Published {len(rows)} messages", flush=True)
