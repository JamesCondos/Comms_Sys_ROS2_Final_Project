"""Offline checks across independently recorded source/gateway/server logs."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
from metrics import cpu_percent, deadline_stats


def read(path):
    with path.open() as f:
        return list(csv.DictReader(f))


def validate(folder, snapshot):
    config = json.loads((folder / "config.json").read_text())
    summary = json.loads((folder / "summary.json").read_text())
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == config["code_sha256"], "Runner hash mismatch"
    if config.get("schema_version", 1) >= 2:
        for filename, key in [("metrics_snapshot.py", "metrics_sha256"), ("traffic_generator_snapshot.py", "ros_traffic_sha256")]:
            assert hashlib.sha256((folder.parent / filename).read_bytes()).hexdigest() == config[key], "Auxiliary source hash mismatch"
        canonical = {k: v for k, v in config.items() if k != "config_sha256"}
        assert hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == config["config_sha256"], "Config hash mismatch"
    generated, gateway, server = [read(folder / f) for f in ("generated.csv", "messages.csv", "server.csv")]
    source_ids = {r["id"] for r in generated}
    gateway_ids = {r["id"] for r in gateway}
    server_ids = {r["id"] for r in server}
    assert len(source_ids) == len(generated), "Duplicate source IDs"
    assert len(gateway_ids) == len(gateway), "Duplicate gateway IDs"
    assert len(server_ids) == len(server), "Duplicate server processing"
    assert gateway_ids <= source_ids, "Unknown gateway messages"
    assert server_ids <= gateway_ids, "Unknown server messages"
    assert {r["id"] for r in gateway if r["outcome"] == "ok"} <= server_ids, "False successful response"
    assert not ({r["id"] for r in gateway if r["outcome"] == "queue_drop"} & server_ids), "Dropped message reached server"
    by_id = {r["id"]: r for r in gateway}
    for r in server:
        g = by_id[r["id"]]
        assert int(r["payload_bytes"]) == config["traffic"][g["message_type"]]["payload_bytes"], "Incorrect payload size"
        assert r["request_body_bytes"] == g["request_body_bytes"], "HTTP body length mismatch"
    begin, end = config["measurement_start_ns"], config["measurement_end_ns"]
    cohort = [r for r in gateway if begin <= int(r["source_ns"]) < end]
    offered = [r for r in generated if begin <= int(r["source_ns"]) < end]
    assert len(offered) == summary["generated"], "Wrong measurement-window source count"
    assert len(cohort) == summary["gateway_received"], "Wrong measurement-window gateway count"
    success = sum(r["outcome"] == "ok" for r in cohort)
    assert success == summary["classes"]["all"]["successful"], "Wrong success count"
    assert len(cohort) == success + summary["queue_drops"] + summary["request_errors"] + summary["pending_after_drain"], "Unaccounted outcome"
    assert all(summary["validation"].values()), "Run-level validation failed"
    # Intentional overload may drop messages at the FIFO, but clean ROS and HTTP
    # should still be checked separately, without calling overflow a TCP failure.
    assert summary["ros_missing"] == 0, "Missing source-to-gateway messages"
    assert summary["request_errors"] == 0, "HTTP error"
    assert summary["pending_after_drain"] == 0, "Insufficient drain interval"
    if config.get("schema_version", 1) >= 2:
        specs = json.loads((folder / "topics.json").read_text())
        expected = {(s["robot_id"], s["message_type"]): s for s in specs}
        actual_topics = {(int(r["robot_id"]), r["message_type"]): r["ros_topic"] for r in gateway}
        assert {k: v["topic"] for k, v in expected.items()} == actual_topics, "Namespace/topic mismatch"
        for r in gateway:
            assert r["ros_topic"] == expected[(int(r["robot_id"]), r["message_type"])]["topic"]
            assert int(r["retry_count"]) == 0
        for kind, target in config["deadlines_ms"].items():
            calculated = deadline_stats([r for r in offered if r["message_type"] == kind], cohort, target)
            assert calculated == summary["deadlines"][kind], "Deadline denominator or classification mismatch"
        for kind in ("server", "gateway", "source"):
            expected_cpu = cpu_percent(read(folder / "cpu.csv"), kind + "_cpu_s", begin, end)
            assert abs(expected_cpu - summary[kind + "_cpu_percent"]) < 1e-8
        assert 0 <= summary["server_handler_busy_percent"] <= 100.001
        assert 0 <= summary["sender_busy_percent"] <= 100.001
    return {"run": folder.name, "measured_generated": len(offered), "successful": success,
            "fifo_drops": summary["queue_drops"], "passed": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    folders = sorted(p.parent for p in args.results.glob("*/summary.json"))
    if not folders:
        raise SystemExit("No completed runs")
    planned = sorted(tuple(pair) for pair in json.loads((args.results / "run_order.json").read_text()))
    actual = []
    for folder in folders:
        config = json.loads((folder / "config.json").read_text())
        actual.append((config["robots"], config["repeat"]))
    assert sorted(actual) == planned, "Completed runs do not match the planned sweep"
    results = [validate(p, args.results / "runner_snapshot.py") for p in folders]
    report = {"runs_checked": len(results), "all_passed": True, "runs": results}
    (args.results / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Validated {len(results)} runs; {sum(r['measured_generated'] for r in results)} measured source messages accounted for.")


if __name__ == "__main__":
    main()
