"""Periodic traffic from up to two namespaced ROS2 robots.

Equivalent load N uses min(N,2) ROS nodes and rate multiplier N/min(N,2).
No HTTP state controls this open-loop scheduler. Actual timing is logged.
"""
import argparse
import heapq
import json
from pathlib import Path
import random
import time


def topic_specs(config, prefix):
    count = min(config["robots"], config.get("max_ros_robots", 2))
    scale = config["robots"] / count
    return [{"robot_id": robot, "message_type": kind,
             "topic": f"{prefix}/robot_{robot}/{kind}",
             "rate_hz": spec["rate_hz"] * scale, "payload_bytes": spec["payload_bytes"]}
            for robot in range(count) for kind, spec in config["traffic"].items()]


def generate(config, prefix, start, ready, require_subscriber=True):
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
    from std_msgs.msg import String
    rclpy.init()
    specs = topic_specs(config, prefix)
    nodes = {r: rclpy.create_node("traffic_source", namespace=f"{prefix}/robot_{r}")
             for r in sorted({s["robot_id"] for s in specs})}
    qos = QoSProfile(depth=1000, reliability=ReliabilityPolicy.RELIABLE,
                     history=HistoryPolicy.KEEP_LAST)
    publishers = {(s["robot_id"], s["message_type"]):
                  nodes[s["robot_id"]].create_publisher(String, s["message_type"], qos) for s in specs}
    rows = []
    try:
        deadline = time.monotonic() + 25
        while require_subscriber and any(p.get_subscription_count() == 0 for p in publishers.values()):
            if time.monotonic() > deadline:
                raise RuntimeError("ROS2 topic discovery timed out")
            time.sleep(0.05)
        ready.put(True)
        while start.value == 0:
            time.sleep(0.01)
        rng, schedule = random.Random(config["seed"]), []
        by_key = {(s["robot_id"], s["message_type"]): s for s in specs}
        for s in specs:
            heapq.heappush(schedule, (start.value + rng.random() / s["rate_hz"], s["robot_id"], s["message_type"], 0))
        end = start.value + config["warmup_s"] + config["duration_s"]
        while schedule:
            due, robot, kind, seq = heapq.heappop(schedule)
            if due >= end:
                continue
            if due > time.monotonic():
                time.sleep(max(0, due - time.monotonic()))
            spec = by_key[(robot, kind)]
            message = {"experiment_id": config["experiment_id"], "robot_id": robot,
                       "message_type": kind, "sequence_number": seq,
                       "source_ns": time.monotonic_ns(), "scheduled_ns": int(due * 1e9),
                       "payload_bytes": spec["payload_bytes"], "payload": "x" * spec["payload_bytes"],
                       "id": f"{config['experiment_id']}:{robot}:{kind}:{seq}"}
            publishers[(robot, kind)].publish(String(data=json.dumps(message, separators=(",", ":"))))
            rows.append({k: v for k, v in message.items() if k != "payload"})
            heapq.heappush(schedule, (due + 1 / spec["rate_hz"], robot, kind, seq + 1))
        time.sleep(0.5)
        return rows
    finally:
        for node in nodes.values():
            node.destroy_node()
        rclpy.shutdown()


def main():
    """Standalone two-robot launch for topic inspection with ros2 topic hz."""
    import multiprocessing as mp
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--robots", type=int, default=2)
    parser.add_argument("--duration", type=float, default=300)
    parser.add_argument("--prefix", default="/traffic_demo")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    config.update(robots=args.robots, duration_s=args.duration, warmup_s=0, experiment_id="topic_demo")
    start, ready = mp.Value("d", time.monotonic() + 1), mp.Queue()
    print(json.dumps(topic_specs(config, args.prefix), indent=2), flush=True)
    rows = generate(config, args.prefix, start, ready, require_subscriber=False)
    print(f"Published {len(rows)} messages", flush=True)
