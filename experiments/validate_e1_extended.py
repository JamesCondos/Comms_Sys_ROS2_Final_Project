"""Independent checks for actual-source E1 runs; overload is a valid outcome."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

from e1_extended import read_csv, digest, queue_occupancy
from metrics import cpu_percent, deadline_stats


def validate_records(config, generated, gateway, server, topics, graph):
    source_ids = {r["id"] for r in generated}
    gateway_ids = {r["id"] for r in gateway}
    server_ids = {r["id"] for r in server}
    assert len(source_ids) == len(generated), "Duplicate source ID"
    assert len(gateway_ids) == len(gateway), "Duplicate gateway ID"
    assert len(server_ids) == len(server), "Duplicate server processing"
    assert gateway_ids <= source_ids, "Gateway received unknown messages"
    assert server_ids <= gateway_ids, "Server received unknown messages"
    assert config["robots"] == config["actual_sources"] == config["max_ros_robots"], "Source-count scaling enabled"
    assert config["source_rate_multiplier"] == 1, "Per-source rates were scaled"
    assert len(graph["actual_nodes"]) == config["actual_sources"], "Wrong actual ROS node count"
    assert len(topics) == config["actual_sources"] * len(config["traffic"]), "Wrong topic count"
    assert all(n == 1 for n in graph["topic_publisher_counts"].values()), "Topic has other than one publisher"
    expected = {(int(t["robot_id"]), t["message_type"]): t for t in topics}
    assert {robot for robot, kind in expected} == set(range(config["actual_sources"])), "Missing configured source"
    for topic in topics:
        traffic = config["traffic"][topic["message_type"]]
        assert topic["rate_hz"] == traffic["rate_hz"], "Configured source rate changed"
        assert topic["payload_bytes"] == traffic["payload_bytes"], "Configured payload changed"
    schedules = defaultdict(list)
    for r in generated:
        key = int(r["robot_id"]), r["message_type"]
        assert key in expected, "Unknown source stream"
        assert int(r["payload_bytes"]) == expected[key]["payload_bytes"], "Wrong generated payload"
        assert int(r["source_ns"]) >= int(r["scheduled_ns"]), "Source precedes its schedule"
        schedules[key].append(r)
    assert set(schedules) == set(expected), "A configured stream never published"
    for key, rows in schedules.items():
        rows.sort(key=lambda r: int(r["sequence_number"]))
        assert [int(r["sequence_number"]) for r in rows] == list(range(len(rows))), "Sequence gap or repetition"
        interval = 1e9 / expected[key]["rate_hz"]
        for before, after in zip(rows, rows[1:]):
            assert abs((int(after["scheduled_ns"]) - int(before["scheduled_ns"])) - interval) < 2000, "Wrong scheduled source rate"
    by_id = {r["id"]: r for r in gateway}
    server_by_id = {r["id"]: r for r in server}
    for row in gateway:
        key = int(row["robot_id"]), row["message_type"]
        assert row["ros_topic"] == expected[key]["topic"], "Wrong ROS topic"
        assert int(row["retry_count"]) == 0, "Unexpected application retry"
        assert int(row["source_ns"]) <= int(row["enqueue_ns"]), "Gateway precedes source"
        assert row["outcome"] in {"ok", "queue_drop", "request_error", "http_error", "pending"}, "Unknown outcome"
        if row["outcome"] == "queue_drop":
            assert row["id"] not in server_ids and not row["dequeue_ns"], "Queue drop was sent"
        if row["outcome"] == "ok":
            assert row["id"] in server_by_id, "False successful response"
            fields = ["source_ns", "enqueue_ns", "dequeue_ns", "request_start_ns", "server_receive_ns",
                      "server_processing_start_ns", "server_processed_ns", "response_ns"]
            values = [int(row[field]) for field in fields]
            assert values == sorted(values), "Incorrect timestamp order"
            assert int(row["http_status"]) == 200 and int(row["http_version"]) == 11, "Wrong HTTP response"
            assert int(row["request_start_ns"]) - int(row["dequeue_ns"]) >= config["gateway_dispatch_delay_ms"] * 1e6, "Gateway delay missing"
            assert int(row["server_processed_ns"]) - int(row["server_processing_start_ns"]) >= config["server_processing_delay_ms"] * 1e6, "Server delay missing"
    for row in server:
        request = by_id[row["id"]]
        assert row["request_body_bytes"] == request["request_body_bytes"], "Body length mismatch"
        assert int(row["payload_bytes"]) == config["traffic"][request["message_type"]]["payload_bytes"], "Server payload mismatch"
        if request["outcome"] == "ok":
            for field in ("server_receive_ns", "server_processing_start_ns", "server_processed_ns", "connection_id"):
                assert row[field] == request[field], "Acknowledgement disagrees with server log"
        assert int(row["handler_start_ns"]) <= int(row["server_receive_ns"]) <= int(row["server_processing_start_ns"]) <= int(row["server_processed_ns"]) <= int(row["handler_end_ns"]), "Invalid server stages"
    assert {r["protocol"] for r in server} == {"HTTP/1.1"}, "Unexpected HTTP protocol"
    sent = [r for r in gateway if r["dequeue_ns"]]
    assert [r["id"] for r in sent] == [r["id"] for r in sorted(sent, key=lambda r: int(r["dequeue_ns"]))], "FIFO order violated"
    return {"generated_all": len(generated), "gateway_received_all": len(gateway), "processed_all": len(server),
            "ros_missing_all": len(source_ids - gateway_ids),
            "connections": len({r["connection_id"] for r in server})}


def validate_run(folder):
    config = json.loads((folder / "config.json").read_text())
    summary = json.loads((folder / "summary.json").read_text())
    canonical = {k: v for k, v in config.items() if k != "config_sha256"}
    assert digest(canonical) == config["config_sha256"], "Configuration hash mismatch"
    for filename, key in [("runner_snapshot.py", "code_sha256"), ("metrics_snapshot.py", "metrics_sha256"),
                          ("traffic_generator_snapshot.py", "ros_traffic_sha256"), ("baseline_helpers_snapshot.py", "baseline_helpers_sha256")]:
        snapshot = folder.parent / filename
        if snapshot.exists():
            assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == config[key], f"Code snapshot mismatch: {filename}"
    generated, gateway, server, events, cpu = [read_csv(folder / name) for name in
        ("generated.csv", "messages.csv", "server.csv", "queue.csv", "cpu.csv")]
    result = validate_records(config, generated, gateway, server,
        json.loads((folder / "topics.json").read_text()), json.loads((folder / "ros_graph.json").read_text()))
    begin, end = config["measurement_start_ns"], config["measurement_end_ns"]
    offered = [r for r in generated if begin <= int(r["source_ns"]) < end]
    cohort = [r for r in gateway if begin <= int(r["source_ns"]) < end]
    outcomes = Counter(r["outcome"] for r in cohort)
    missing = len({r["id"] for r in offered} - {r["id"] for r in cohort})
    assert len(offered) == summary["generated"] and len(cohort) == summary["gateway_received"], "Measurement cohort mismatch"
    assert len(offered) == missing + sum(outcomes.values()), "Message conservation failed"
    assert summary["successful"] == outcomes["ok"], "Success count mismatch"
    assert summary["queue_drops"] == outcomes["queue_drop"], "Drop count mismatch"
    assert summary["request_errors"] == outcomes["request_error"] + outcomes["http_error"], "Failure count mismatch"
    assert summary["pending_after_drain"] == outcomes["pending"], "Unresolved count mismatch"
    assert summary["ros_missing"] == missing, "ROS missing count mismatch"
    assert max((int(r["queue_length"]) for r in events), default=0) <= config["queue_capacity"], "Queue capacity exceeded"
    assert all(int(r["in_flight"]) in (0, 1) for r in events), "Concurrent request limit exceeded"
    mean, maximum, last = queue_occupancy(events, begin, end)
    assert abs(mean - summary["mean_queue_length"]) < 1e-8, "Queue time integral mismatch"
    assert maximum == summary["max_queue_length"] and last == summary["queue_at_measurement_end"]
    for kind, target in config["deadlines_ms"].items():
        assert deadline_stats([r for r in offered if r["message_type"] == kind], cohort, target) == summary["deadlines"][kind], "Deadline denominator mismatch"
    for process in ("server", "gateway", "source"):
        assert abs(cpu_percent(cpu, process + "_cpu_s", begin, end) - summary[process + "_cpu_percent"]) < 1e-8, "CPU calculation mismatch"
    for name in ("server_handler", "server_processing", "sender", "gateway_dispatch", "http"):
        assert 0 <= summary[name + "_busy_percent"] <= 100.001, "Invalid single-worker occupancy"
    assert config["host_metadata"]["clock_domain_id"] == config["server_host_metadata"]["clock_domain_id"], "Unverified clock domain"
    warnings = []
    if missing or summary["request_errors"] or summary["pending_after_drain"]:
        warnings.append("ROS missing, request errors or bounded-drain unresolved work observed; retained as explicit outcomes")
    if result["connections"] != 1:
        warnings.append("More than one TCP connection observed; examine request errors and server logs")
    rate_counts = Counter((int(r["robot_id"]), r["message_type"]) for r in offered)
    for robot in range(config["actual_sources"]):
        for kind, traffic in config["traffic"].items():
            nominal = traffic["rate_hz"] * config["duration_s"]
            if abs(rate_counts[robot, kind] - nominal) > 1:
                warnings.append(f"Source {robot}/{kind} realized count differs from nominal by >1; inspect scheduling lag")
    result.update(run=folder.name, measured_generated=len(offered), successful=outcomes["ok"],
                  fifo_drops=outcomes["queue_drop"], missing=missing,
                  unresolved=outcomes["pending"], passed=True, warnings=warnings)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    folders = sorted(p.parent for p in args.results.glob("*/summary.json") if not (p.parent / "invalid.json").exists())
    assert folders, "No completed runs"
    results = [validate_run(folder) for folder in folders]
    actual = []
    for folder in folders:
        config = json.loads((folder / "config.json").read_text())
        actual.append((config["condition_id"], config["actual_sources"], config["repeat"]))
    planned = [tuple(row) for row in json.loads((args.results / "run_order.json").read_text())]
    assert sorted(actual) == sorted(planned), "Completed runs do not match planned cells"
    report = {"runs_checked": len(results), "all_passed": True, "runs": results,
              "infrastructure_invalid_runs": [str(p.parent.name) for p in args.results.glob("*/invalid.json")]}
    (args.results / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"runs_checked": len(results), "all_passed": True,
                      "measured_generated": sum(r["measured_generated"] for r in results)}))


if __name__ == "__main__":
    main()
