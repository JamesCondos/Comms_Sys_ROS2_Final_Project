"""Independent raw-log validation for E2B queue populations and arrivals.

The queue replay and finite-window calculations below do not import the
runner's summary functions. Invalid or lossy runs cannot pass the stable gate.
"""
import argparse
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path

from e2_source import make_schedule


def read_rows(path):
    with Path(path).open(newline="") as stream:
        return list(csv.DictReader(stream))


def same_number(actual, expected, name):
    assert math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-9), f"{name}: {actual} != {expected}"


def percent(values, p):
    ordered = sorted(values)
    index = (len(ordered) - 1) * p / 100
    lo = int(index)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def check_stats(saved, values, label):
    assert saved["n"] == len(values), f"{label} sample count mismatch"
    if not values:
        return
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    for key, expected in {"mean": mean, "sd": math.sqrt(variance), "max": max(values),
                          "p50": percent(values, 50), "p95": percent(values, 95), "p99": percent(values, 99)}.items():
        same_number(saved[key], expected, f"{label}.{key}")
    if mean:
        same_number(saved["cv2"], variance / mean ** 2, f"{label}.cv2")


def replay_queue(rows, events, begin, end, capacity):
    """Reconstruct every queue transition from message timestamps and identities."""
    expected = sorted([(int(r[field]), action, r["id"]) for r in rows
                       for field, action in (("enqueue_ns", "admit"), ("dequeue_ns", "dequeue"), ("response_ns", "complete"))])
    actual = [(int(e["timestamp_ns"]), e["action"], e["id"]) for e in events]
    assert actual == expected, "Event timestamps/actions differ from message transitions"
    waiting, active = [], None
    area_q = area_busy = 0
    previous = begin
    max_q = max_system = 0
    initial = final = None
    for event in events:
        timestamp = int(event["timestamp_ns"])
        if timestamp >= begin and initial is None:
            initial = len(waiting) + int(active is not None)
        if timestamp >= end and final is None:
            final = len(waiting) + int(active is not None)
        if begin <= timestamp < end:
            area_q += len(waiting) * (timestamp - previous)
            area_busy += int(active is not None) * (timestamp - previous)
            previous = timestamp
            max_q = max(max_q, len(waiting))
            max_system = max(max_system, len(waiting) + int(active is not None))
        elif timestamp >= end and previous < end:
            area_q += len(waiting) * (end - previous)
            area_busy += int(active is not None) * (end - previous)
            previous = end
        if event["action"] == "admit":
            waiting.append(event["id"])
            assert len(waiting) <= capacity, "Queue exceeds configured capacity"
        elif event["action"] == "dequeue":
            assert active is None, "Two simultaneous workers"
            assert waiting and waiting.pop(0) == event["id"], "FIFO order violation"
            active = event["id"]
        else:
            assert active == event["id"], "Completion without active request"
            active = None
        assert int(event["queue_length"]) == len(waiting), "Wrong queue trace state"
        assert int(event["in_flight"]) == int(active is not None), "Wrong busy trace state"
        assert int(event["system_length"]) == len(waiting) + int(active is not None), "Wrong population trace"
        if begin <= timestamp < end:
            max_q = max(max_q, len(waiting))
            max_system = max(max_system, len(waiting) + int(active is not None))
    if previous < end:
        area_q += len(waiting) * (end - previous)
        area_busy += int(active is not None) * (end - previous)
    assert not waiting and active is None, "Incomplete drain"
    return {"queue_area_ns": area_q, "busy_area_ns": area_busy, "system_area_ns": area_q + area_busy,
            "max_Lq": max_q, "max_L": max_system, "initial_population": initial or 0,
            "final_population": final or 0}


def verify_little(rows, begin, end, finish, saved, event_area):
    cohort = [r for r in rows if begin <= int(r["enqueue_ns"]) < end]
    residence_sum = sum(int(r[finish]) - int(r["enqueue_ns"]) for r in cohort)
    carry_in = sum(max(0, min(int(r[finish]), end) - begin) for r in rows if int(r["enqueue_ns"]) < begin)
    tail = sum(max(0, int(r[finish]) - end) for r in cohort)
    overlap = sum(max(0, min(int(r[finish]), end) - max(int(r["enqueue_ns"]), begin)) for r in rows)
    assert overlap == event_area, "Time integral differs from item residence overlap"
    assert overlap == residence_sum + carry_in - tail, "Finite-window Little identity failed"
    expected = {"arrival_count": len(cohort), "residence_sum_ns": residence_sum,
                "overlap_area_ns": overlap, "carry_in_area_ns": carry_in,
                "carry_out_area_ns": tail, "identity_residual_ns": 0}
    for key, value in expected.items():
        assert saved[key] == value, f"Little field {key} incorrect"
    for key, value in {"uncorrected_lambda_W": residence_sum / (end - begin),
                       "boundary_correction": (carry_in - tail) / (end - begin),
                       "corrected_lambda_W": overlap / (end - begin)}.items():
        same_number(saved[key], value, key)


def validate_run(folder):
    config = json.loads((folder / "config.json").read_text())
    summary = json.loads((folder / "summary.json").read_text())
    canonical = {k: v for k, v in config.items() if k != "config_sha256"}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert digest == config["config_sha256"], "Configuration hash mismatch"
    for filename, key in (("runner_snapshot.py", "code_sha256"), ("metrics_snapshot.py", "metrics_sha256"),
                          ("traffic_generator_snapshot.py", "ros_traffic_sha256"),
                          ("baseline_helpers_snapshot.py", "baseline_helpers_sha256"), ("e1_helpers_snapshot.py", "e1_helpers_sha256")):
        assert hashlib.sha256((folder.parent / filename).read_bytes()).hexdigest() == config[key], f"Snapshot differs: {filename}"
    generated, rows, server, events = [read_rows(folder / name) for name in ("generated.csv", "messages.csv", "server.csv", "queue.csv")]
    assert generated and rows and server and events, "Empty run"
    for records in (generated, rows, server):
        assert len({r["id"] for r in records}) == len(records), "Duplicate message identity"
    assert {r["id"] for r in generated} == {r["id"] for r in rows} == {r["id"] for r in server}, "Message conservation failure"
    assert all(r["outcome"] == "ok" for r in rows), "E2B stable comparison requires no drops/errors/unresolved work"
    assert config["actual_sources"] == config["robots"] == config["max_ros_robots"] == 4 and config["source_rate_multiplier"] == 1
    assert config["gateway_count"] == config["server_count"] == config["max_in_flight"] == 1
    assert config["network_mode"] == "dedicated_veth" and config["network_rate_mbit"] is None
    assert config["host_metadata"]["clock_domain_id"] == config["server_host_metadata"]["clock_domain_id"]
    assert config["host_metadata"]["network_namespace"] != config["server_host_metadata"]["network_namespace"]
    graph = json.loads((folder / "ros_graph.json").read_text())
    topics = json.loads((folder / "topics.json").read_text())
    assert len(graph["actual_nodes"]) == 4 and len(topics) == 4 * len(config["traffic"])
    assert all(n == 1 for n in graph["topic_publisher_counts"].values())
    expected_topics = {(int(t["robot_id"]), t["message_type"]): t for t in topics}
    for topic in topics:
        assert topic["rate_hz"] == config["traffic"][topic["message_type"]]["rate_hz"]
        assert topic["payload_bytes"] == config["traffic"][topic["message_type"]]["payload_bytes"]
    schedule, model = make_schedule(config)
    assert model == json.loads((folder / "arrival_model.json").read_text()), "Saved arrival model changed"
    expected = [(config["source_start_ns"] + offset, robot, kind, seq) for offset, robot, kind, seq in schedule]
    observed = [(int(r["scheduled_ns"]), int(r["robot_id"]), r["message_type"], int(r["sequence_number"])) for r in generated]
    assert observed == expected, "Generated open-loop schedule differs from frozen model"
    source_by_id = {r["id"]: r for r in generated}
    server_by_id = {r["id"]: r for r in server}
    for row in rows:
        identity = row["id"]
        source = source_by_id[identity]
        service = server_by_id[identity]
        for key in ("robot_id", "message_type", "sequence_number", "scheduled_ns", "source_ns", "payload_bytes"):
            assert row[key] == source[key], f"Source identity changed: {key}"
        assert row["ros_topic"] == expected_topics[int(row["robot_id"]), row["message_type"]]["topic"]
        times = [int(row[key]) for key in ("scheduled_ns", "source_ns", "callback_ns", "enqueue_ns", "dequeue_ns",
                 "request_start_ns", "server_receive_ns", "server_processing_start_ns", "server_processed_ns", "response_body_received_ns", "response_ns")]
        assert times == sorted(times), "Timestamp inversion"
        assert int(source["source_ns"]) <= int(source["publish_start_ns"]) <= int(source["publish_end_ns"]), "Invalid publication timing"
        assert int(row["server_processed_ns"]) - int(row["server_processing_start_ns"]) >= config["server_processing_delay_ms"] * 1e6
        for key in ("server_receive_ns", "server_processing_start_ns", "server_processed_ns", "connection_id", "request_body_bytes"):
            assert row[key] == service[key], "Server and acknowledgement differ"
        assert int(row["retry_count"]) == 0 and int(row["http_status"]) == 200 and int(row["http_version"]) == 11
        assert int(row["payload_bytes"]) == int(service["payload_bytes"]) == config["traffic"][row["message_type"]]["payload_bytes"]
    assert {r["protocol"] for r in server} == {"HTTP/1.1"}
    assert len({r["connection_id"] for r in server}) == 1, "Persistent connection unexpectedly replaced"
    assert [r["id"] for r in rows] == [r["id"] for r in server], "Server order differs from FIFO admission order"
    begin, end = config["measurement_start_ns"], config["measurement_end_ns"]
    duration = (end - begin) / 1e9
    q = summary["queue_system"]
    replay = replay_queue(rows, events, begin, end, config["queue_capacity"])
    for key, expected in replay.items():
        assert q[key] == expected, f"Incorrect summary {key}"
    admitted = [r for r in rows if begin <= int(r["enqueue_ns"]) < end]
    departures = sum(begin <= int(r["response_ns"]) < end for r in rows)
    assert q["arrivals"] == len(admitted) and q["departures"] == departures
    assert q["initial_population"] + q["arrivals"] == departures + q["final_population"], "Window population conservation failed"
    assert q["end_L"] == q["final_population"] and q["end_Lq"] + q["end_busy"] == q["end_L"]
    for key, expected in {"mean_L": replay["system_area_ns"] / (end - begin),
                          "mean_Lq": replay["queue_area_ns"] / (end - begin),
                          "busy_fraction": replay["busy_area_ns"] / (end - begin),
                          "arrival_rate_rps": len(admitted) / duration, "completed_rps": departures / duration}.items():
        same_number(q[key], expected, key)
    for name, finish, area in (("little_system", "response_ns", replay["system_area_ns"]),
                               ("little_queue", "dequeue_ns", replay["queue_area_ns"])):
        verify_little(rows, begin, end, finish, q[name], area)
    for name, first, last in (("W_ms", "enqueue_ns", "response_ns"), ("Wq_ms", "enqueue_ns", "dequeue_ns"),
                              ("S_ms", "dequeue_ns", "response_ns")):
        check_stats(q[name], [(int(r[last]) - int(r[first])) / 1e6 for r in admitted], name)
    for name in ("event_vs_residence_system_residual_ns", "event_vs_residence_queue_residual_ns", "population_identity_residual_ns"):
        assert q[name] == 0, "Nonzero exact accounting residual"
    assert 0 < q["busy_fraction"] < 0.98, "Saturated run is unsuitable for this stable comparison"
    offered = [r for r in generated if begin <= int(r["source_ns"]) < end]
    assert summary["generated"] == summary["successful"] == len(offered)
    assert summary["ros_missing"] == summary["queue_drops"] == summary["request_errors"] == summary["pending_after_drain"] == 0
    same_number(summary["offered_rps"], len(offered) / duration, "offered_rps")
    for label, data, field in (("scheduled", generated, "scheduled_ns"), ("source", generated, "source_ns"), ("gateway", rows, "enqueue_ns")):
        timestamps = sorted(int(r[field]) for r in data if begin <= int(r[field]) < end)
        check_stats(summary["arrival_variability"][label]["interarrival_ms"], [(b-a)/1e6 for a,b in zip(timestamps,timestamps[1:])], label)
        for prefix, width in (("one_second", 1_000_000_000), ("hundred_ms", 100_000_000)):
            bins = [0] * math.ceil((end - begin) / width)
            for timestamp in timestamps:
                bins[(timestamp - begin) // width] += 1
            assert summary["arrival_variability"][label][prefix + "_bin_counts"] == bins
            mean = sum(bins) / len(bins)
            fano = sum((x - mean) ** 2 for x in bins) / len(bins) / mean
            same_number(summary["arrival_variability"][label][prefix + "_count_fano"], fano, label + prefix)
    for name, first, last in (("schedule_to_source_ms", "scheduled_ns", "source_ns"), ("schedule_to_publish_ms", "scheduled_ns", "publish_start_ns"), ("publish_call_ms", "publish_start_ns", "publish_end_ns")):
        cohort = [r for r in generated if begin <= int(r["scheduled_ns"]) < end]
        check_stats(summary["source_jitter"][name], [(int(r[last])-int(r[first]))/1e6 for r in cohort], name)
    return {"run": folder.name, "passed": True, "condition_id": config["condition_id"], "repeat": config["repeat"],
            "generated_all": len(generated), "measured_generated": len(offered), "connections": 1,
            "exact_system_residual_ns": 0, "exact_queue_residual_ns": 0, "population_residual": 0,
            "busy_fraction": q["busy_fraction"], "source_lateness_p95_ms": summary["source_jitter"]["schedule_to_source_ms"]["p95"]}


def validate_sweep(folder):
    sweep = json.loads((folder / "sweep_config.json").read_text())
    folders = sorted(p.parent for p in folder.glob("*/summary.json") if not (p.parent / "invalid.json").exists())
    assert folders, "No completed runs"
    fixed_fields = ("warmup_s", "duration_s", "drain_s", "queue_capacity", "request_timeout_s", "traffic",
                    "deadlines_ms", "server_processing_delay_ms", "network_mode", "capture", "bursty_model")
    for path in folders:
        config = json.loads((path / "config.json").read_text())
        for key in fixed_fields:
            assert config[key] == sweep[key], f"Run setting {key} differs from frozen sweep"
        assert config["seed"] == sweep["seed"] + config["repeat"], "Wrong per-repeat seed"
        assert config["actual_sources"] == 4 and config["actual_source_counts"] == [4]
        assert config["arrival_pattern"] == config["condition_id"]
    results = [validate_run(path) for path in folders]
    actual = sorted((r["condition_id"], 4, r["repeat"]) for r in results)
    planned = sorted(tuple(row) for row in json.loads((folder / "run_order.json").read_text()))
    assert actual == planned, "Completed runs differ from frozen plan"
    report = {"all_passed": True, "runs_checked": len(results), "runs": results,
              "infrastructure_invalid_runs": [p.parent.name for p in folder.glob("*/invalid.json")]}
    (folder / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    result = validate_sweep(args.results)
    print(json.dumps({"all_passed": result["all_passed"], "runs_checked": result["runs_checked"]}))


if __name__ == "__main__":
    main()
