"""Workloads and link settings for the live network."""

# Added delay (ms per direction), loss (% per direction), upload cap (Mbit/s/site).
PROFILES = {
    "clean": (0, 0, None),
    "delay": (30, 0, None),
    "loss": (0, 1, None),
    "rate": (0, 0, 2),
    "combined": (30, 1, 2),
}


def make_config(study, protocol="h2", profile="clean", load=None, seconds=30, random=False):
    traffic = study == "traffic"
    delay, loss, rate = PROFILES[profile]

    return dict(
        protocol=protocol,
        runtime="simple_https_v1",
        topology="T3_EDGE_PASS",
        warmup_s=5,
        capture=True,
        video_enabled=not random,
        robots=[{"id": f"robot{i}", "site": "A" if i <= 2 else "B"} for i in range(1, 5)],
        tcp_congestion={role: "cubic" for role in ("robot", "edge", "proxy", "worker")},
        impairment_schedule=[],
        workload="random" if random else "mixed",
        load=load if load is not None else (74 if traffic else 148),
        seconds=seconds,
        finish_seconds=30 if traffic else 15,
        request_timeout_s=30 if traffic else 8,
        # These are project deadline targets, not robot safety requirements.
        deadlines_ms={"command":100, "status":200, "odometry":100, "sensor":1000, "video":1000, "probe":1000},
        queue_capacity=2000,
        gateway_concurrency=512 if traffic else 8,
        edge_concurrency=1024 if traffic else 32,
        mean_service_s=0.020 if traffic else 0.004,
        service_mode="exponential" if traffic else "fixed",
        seed=610613001,
        video_fps=5,
        separate_topics=traffic,
        sites={site: {
            "forward": dict(delay_ms=delay, loss_pct=loss, rate_mbit=rate),
            "reverse": dict(delay_ms=delay, loss_pct=loss, rate_mbit=None),
        } for site in ("A", "B")},
    )


def load_config(path):
    # Small preset files override the shared defaults; nested dictionaries are merged.
    import copy
    import json
    from pathlib import Path

    def merge(base, changes):
        result = copy.deepcopy(base)
        for key, value in changes.items():
            result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
        return result

    def read(current, seen):
        current = Path(current).resolve()
        if current in seen:
            raise ValueError("Preset inheritance contains a cycle")
        value = json.loads(current.read_text())
        parent = value.pop("extends", None)
        return merge(read(current.parent / parent, seen | {current}), value) if parent else value

    return merge(make_config("http", "h1", "rate"), read(path, set()))
