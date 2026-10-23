"""Open-loop E2B ROS2 publishers with reproducible arrival schedules.

Every schedule is generated before execution and is independent of HTTP
completions. Four actual nodes each publish their own four typed topics.
"""
import hashlib
import json
from pathlib import Path
import random
import time

from baseline import save_csv, save_json


PATTERNS = ("periodic_staggered", "periodic_synchronized", "poisson", "bursty_onoff_poisson")


def stream_seed(seed, *keys):
    return int.from_bytes(hashlib.sha256((str(seed) + ":" + ":".join(map(str, keys))).encode()).digest()[:8], "big")


def topic_specs(config, prefix):
    return [{"robot_id": robot, "message_type": kind, "topic": f"{prefix}/robot_{robot}/{kind}",
             "rate_hz": traffic["rate_hz"], "payload_bytes": traffic["payload_bytes"]}
            for robot in range(config["actual_sources"]) for kind, traffic in config["traffic"].items()]


def wait_until_ns(deadline_ns, clock=time.monotonic_ns, sleeper=time.sleep):
    """Keep the configured observation horizon even when the source is quiet."""
    while (remaining_ns := deadline_ns - clock()) > 0:
        sleeper(remaining_ns / 1e9)


def make_schedule(config):
    """Return (offset_ns, robot_id, class, sequence) plus explicit model metadata."""
    pattern, seed = config["arrival_pattern"], config["seed"]
    if pattern not in PATTERNS:
        raise ValueError("Unknown arrival pattern")
    horizon = config["warmup_s"] + config["duration_s"]
    specs = topic_specs(config, "")
    intervals = []
    metadata = {"pattern": pattern, "seed": seed, "horizon_s": horizon,
                "open_loop": True, "scheduled_before_execution": True,
                "simultaneous_tie_order": "scheduled offset, source ID, lexicographic class name, sequence number",
                "staggered_phase_rule": "stream index divided by number of streams and that stream's rate",
                "stream_seeds": [], "state_intervals": []}
    if pattern == "bursty_onoff_poisson":
        model = config["bursty_model"]
        cycle, on_duration = model["cycle_s"], model["on_s"]
        probability_on = on_duration / cycle
        multiplier = 1 / probability_on
        now = 0.0
        while now < horizon:
            intervals.append((now, min(horizon, now + on_duration), 1))
            if now + on_duration < horizon:
                intervals.append((now + on_duration, min(horizon, now + cycle), 0))
            now += cycle
        metadata.update(on_fraction=probability_on, on_rate_multiplier=multiplier,
                        off_rate_multiplier=0, cycle_s=cycle, on_s=on_duration,
                        deterministic_modulator=True, initial_on_phase_s=0,
                        global_modulator=True,
                        state_intervals=[{"start_s": a, "end_s": b, "on": bool(s)} for a, b, s in intervals])
    schedule = []
    for index, spec in enumerate(specs):
        robot, kind, rate = spec["robot_id"], spec["message_type"], spec["rate_hz"]
        seed_value = stream_seed(seed, robot, kind)
        metadata["stream_seeds"].append({"robot_id": robot, "message_type": kind, "seed": seed_value})
        rng, due_times = random.Random(seed_value), []
        if pattern.startswith("periodic"):
            # Same-class sources are evenly spaced; class offsets are also distinct.
            phase = index / len(specs) / rate if pattern == "periodic_staggered" else 0.0
            sequence = 0
            while (due := phase + sequence / rate) < horizon:
                due_times.append(due)
                sequence += 1
        elif pattern == "poisson":
            due = rng.expovariate(rate)
            while due < horizon:
                due_times.append(due)
                due += rng.expovariate(rate)
        else:
            # Conditional on the common state path, streams are independent Poisson.
            for begin, end, state in intervals:
                if state:
                    due = begin + rng.expovariate(rate * multiplier)
                    while due < end:
                        due_times.append(due)
                        due += rng.expovariate(rate * multiplier)
        schedule.extend((int(round(due * 1e9)), robot, kind, seq) for seq, due in enumerate(due_times))
    schedule.sort()
    metadata["scheduled_messages"] = len(schedule)
    return schedule, metadata


def generate(config, folder, prefix, start, ready):
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
    from std_msgs.msg import String

    folder = Path(folder)
    schedule, model = make_schedule(config)
    save_json(folder / "arrival_model.json", model)
    rclpy.init()
    nodes, publishers, rows = {}, {}, []
    try:
        specs = topic_specs(config, prefix)
        qos = QoSProfile(depth=1000, reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST)
        by_key = {(s["robot_id"], s["message_type"]): s for s in specs}
        for spec in specs:
            robot, kind = spec["robot_id"], spec["message_type"]
            if robot not in nodes:
                nodes[robot] = rclpy.create_node("traffic_source", namespace=f"{prefix}/robot_{robot}")
            publishers[robot, kind] = nodes[robot].create_publisher(String, kind, qos)
        deadline = time.monotonic() + 25
        while any(p.get_subscription_count() == 0 for p in publishers.values()):
            if time.monotonic() > deadline:
                raise RuntimeError("ROS2 topic discovery timed out")
            time.sleep(0.05)
        ready.put(True)
        while start.value == 0:
            time.sleep(0.01)
        start_ns = int(start.value * 1e9)
        for offset_ns, robot, kind, sequence in schedule:
            scheduled_ns = start_ns + offset_ns
            delay = (scheduled_ns - time.monotonic_ns()) / 1e9
            if delay > 0:
                time.sleep(delay)
            spec = by_key[robot, kind]
            message = {"experiment_id": config["experiment_id"], "robot_id": robot,
                       "message_type": kind, "sequence_number": sequence,
                       "source_ns": time.monotonic_ns(), "scheduled_ns": scheduled_ns,
                       "payload_bytes": spec["payload_bytes"], "payload": "x" * spec["payload_bytes"],
                       "id": f"{config['experiment_id']}:{robot}:{kind}:{sequence}"}
            data = json.dumps(message, separators=(",", ":"))
            publish_start = time.monotonic_ns()
            publishers[robot, kind].publish(String(data=data))
            publish_end = time.monotonic_ns()
            row = {key: value for key, value in message.items() if key != "payload"}
            row.update(publish_start_ns=publish_start, publish_end_ns=publish_end)
            rows.append(row)
        # The last ON interval ends 0.8 s before the horizon in the burst case.
        # Silence is part of the experiment, not a signal to finish observing.
        wait_until_ns(start_ns + int((config["warmup_s"] + config["duration_s"]) * 1e9))
        time.sleep(0.5)
        save_csv(folder / "generated.csv", rows)
    finally:
        for node in nodes.values():
            node.destroy_node()
        rclpy.shutdown()


def generator_process(config, folder, prefix, start, ready):
    generate(config, folder, prefix, start, ready)
