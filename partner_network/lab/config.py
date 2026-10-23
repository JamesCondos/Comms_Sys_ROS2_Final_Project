"""Configuration loading and constraints, independent of ROS or root access."""
import copy
import json
import math
from pathlib import Path
import re

TOPOLOGIES = ("T1_DISTRIBUTED", "T2_CENTRAL_POOL", "T3_EDGE_PASS")


def merge(base, update):
    result = copy.deepcopy(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(path, seen=None):
    path = Path(path).resolve()
    seen = set() if seen is None else set(seen)
    if path in seen:
        raise ValueError("Configuration inheritance cycle")
    seen.add(path)
    config = json.loads(path.read_text(encoding="utf-8"))
    parent = config.pop("extends", None)
    if parent:
        config = merge(load_config(path.parent / parent, seen), config)
    return config


def number(value, name, low=0, high=math.inf):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"Invalid {name}: {value!r}")


def validate_profile(profile):
    if set(profile) - {"delay_ms", "loss_pct", "rate_mbit", "limit_packets"}:
        raise ValueError("Unknown network profile field")
    number(profile.get("delay_ms", 0), "delay_ms", 0, 10000)
    number(profile.get("loss_pct", 0), "loss_pct", 0, 100)
    if profile.get("rate_mbit") is not None:
        number(profile["rate_mbit"], "rate_mbit", .001, 100000)
    limit = profile.get("limit_packets", 1000)
    if not isinstance(limit, int) or not 1 <= limit <= 100000:
        raise ValueError("limit_packets must be a positive bounded integer")


def validate_config(config):
    if config["topology"] not in TOPOLOGIES:
        raise ValueError("Supported topologies: " + ", ".join(TOPOLOGIES))
    robots = config["robots"]
    if not 1 <= len(robots) <= 16 or len({r["id"] for r in robots}) != len(robots):
        raise ValueError("Use 1--16 uniquely named robot clients")
    for robot in robots:
        if not re.fullmatch(r"robot[1-9][0-9]?", robot["id"]) or robot["site"] not in ("A", "B"):
            raise ValueError("Robot IDs must be robot1..robot99; sites A or B")
    if not isinstance(config["workers"], int) or not 1 <= config["workers"] <= 8:
        raise ValueError("Use 1--8 central worker processes")
    if config["topology"] == "T1_DISTRIBUTED" and config["workers"] != 1:
        raise ValueError("T1 has exactly one central worker; use T2 for a pool")
    for field in ("queue_capacity", "worker_queue_capacity", "worker_threads"):
        if not isinstance(config[field], int) or config[field] < 1:
            raise ValueError(f"{field} must be a positive integer")
    for field in ("warmup_s", "worker_service_ms"):
        number(config[field], field)
    for field in ("duration_s", "drain_s", "request_timeout_s"):
        number(config[field], field, .01)
    if config["drain_s"] < config["request_timeout_s"]:
        raise ValueError("Drain must allow at least one request timeout")
    if config.get("application_retries", False):
        raise ValueError("Application retries are not implemented in this kit")
    if config.get("protocol", "HTTP/1.1") != "HTTP/1.1":
        raise ValueError("This starter kit implements HTTP/1.1 only")
    if config.get("operator_enabled", False):
        raise ValueError("Operator/downlink traffic is not implemented")
    if config.get("edge_policy", "PASS_THROUGH") != "PASS_THROUGH":
        raise ValueError("Only pass-through edges are implemented")
    if config.get("balancing", "round_robin") != "round_robin":
        raise ValueError("Only request-level round robin is implemented")
    for role in ("robot", "edge", "proxy", "worker"):
        algorithm = config["tcp_congestion"][role]
        if algorithm is not None and not re.fullmatch(r"[a-z0-9_]{1,24}", algorithm):
            raise ValueError("Invalid TCP congestion algorithm name")
    if set(config["traffic"]) != {"command", "status", "odometry", "sensor"}:
        raise ValueError("Keep the four defined traffic classes")
    for kind, traffic in config["traffic"].items():
        number(traffic["rate_hz"], kind + " rate", .001, 100000)
        if not isinstance(traffic["payload_bytes"], int) or not 0 <= traffic["payload_bytes"] <= 1_000_000:
            raise ValueError("Invalid useful payload size")
        number(config["deadlines_ms"][kind], kind + " deadline", .001)
    for site in ("A", "B"):
        for direction in ("forward", "reverse"):
            validate_profile(config["sites"][site][direction])
    for event in config.get("impairment_schedule", []):
        number(event["at_s"], "impairment at_s", 0, config["duration_s"])
        if event["site"] not in ("A", "B") or event["direction"] not in ("forward", "reverse", "both"):
            raise ValueError("Invalid scheduled impairment site/direction")
        validate_profile(event["profile"])
    return config
