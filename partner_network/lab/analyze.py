"""Independently reconcile source, gateway, central-worker and queue records."""
from collections import Counter, deque
import csv
import json
from pathlib import Path
import struct


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def records(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def pcap_record_count(path):
    """Check capture framing only; this does not reconstruct TCP or HTTP."""
    with Path(path).open("rb") as stream:
        header = stream.read(24)
        endians = {b"\xd4\xc3\xb2\xa1": "<", b"\x4d\x3c\xb2\xa1": "<",
                   b"\xa1\xb2\xc3\xd4": ">", b"\xa1\xb2\x3c\x4d": ">"}
        if len(header) != 24 or header[:4] not in endians:
            raise ValueError("Missing or invalid PCAP header")
        endian = endians[header[:4]]
        snaplen = struct.unpack(endian + "I", header[16:20])[0]
        count = 0
        while True:
            record = stream.read(16)
            if not record:
                return count
            if len(record) != 16:
                raise ValueError("Truncated PCAP record header")
            _, _, included, original = struct.unpack(endian + "IIII", record)
            if included > snaplen or included > original or len(stream.read(included)) != included:
                raise ValueError("Truncated or invalid PCAP packet")
            count += 1


def stats(values):
    if not values:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    values = sorted(values)
    def percentile(p):
        index = (len(values) - 1) * p
        lo = int(index)
        return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (index - lo)
    return {"n": len(values), "mean": sum(values) / len(values), "p50": percentile(.5),
            "p95": percentile(.95), "p99": percentile(.99), "max": values[-1]}


def unique(rows, label):
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate {label} identities")
    return dict(zip(ids, rows))


def replay_queue(events, begin, end, capacity, concurrency):
    waiting, active = deque(), set()
    previous = begin
    area_q = area_busy = max_q = 0
    last_stamp = -1
    intervals = {}
    for event in events:
        stamp = event["time_ns"]
        if stamp < last_stamp:
            raise ValueError("Queue events are not chronological")
        last_stamp = stamp
        edge = min(end, max(begin, stamp))
        if edge > previous:
            max_q = max(max_q, len(waiting))
        area_q += len(waiting) * (edge - previous)
        area_busy += len(active) * (edge - previous)
        previous = edge
        action, identity = event["event"], event["id"]
        if action == "initial":
            if waiting or active:
                raise ValueError("Unexpected queue reset")
        elif action == "admit":
            if identity in intervals or len(waiting) >= capacity:
                raise ValueError("Duplicate admission or overflow")
            intervals[identity] = {"admission_ns": stamp}
            waiting.append(identity)
        elif action == "service_start":
            if not waiting or waiting.popleft() != identity or len(active) >= concurrency:
                raise ValueError("FIFO order or concurrency violation")
            active.add(identity)
            intervals[identity]["dequeue_ns"] = stamp
        elif action == "service_end":
            if identity not in active:
                raise ValueError("Completion without in-service request")
            active.remove(identity)
            intervals[identity]["terminal_ns"] = stamp
        elif action == "drain_timeout":
            if not waiting or waiting.popleft() != identity:
                raise ValueError("Drained waiting identity differs")
            intervals[identity]["terminal_ns"] = stamp
        elif action != "queue_drop":
            raise ValueError("Unknown queue action: " + action)
        if event["queue_len"] != len(waiting) or event["in_service"] != len(active):
            raise ValueError("Queue state differs from independent replay")
        if begin <= stamp < end:
            max_q = max(max_q, len(waiting))
    area_q += len(waiting) * (end - previous)
    area_busy += len(active) * (end - previous)
    if waiting or active:
        raise ValueError("Unresolved queue population after shutdown")
    system_area = sum(max(0, min(r["terminal_ns"], end) - max(r["admission_ns"], begin)) for r in intervals.values())
    queue_area = sum(max(0, min(r.get("dequeue_ns", r["terminal_ns"]), end) - max(r["admission_ns"], begin)) for r in intervals.values())
    if system_area != area_q + area_busy or queue_area != area_q:
        raise ValueError("Item residence and event integrals differ")
    duration = end - begin
    return {"mean_Lq": area_q / duration, "mean_L": (area_q + area_busy) / duration,
            "mean_in_service": area_busy / duration, "max_waiting": max_q,
            "exact_queue_residual_ns": queue_area - area_q,
            "exact_system_residual_ns": system_area - area_q - area_busy,
            "admitted_all": len(intervals)}, intervals


def outcome_metrics(source, terminals, begin, end, deadline_ms=None):
    cohort = [r for r in source if begin <= r["source_ns"] < end]
    successful = [terminals[r["id"]] for r in cohort if r["id"] in terminals and terminals[r["id"]]["outcome"] == "success"]
    latency = [(r["response_ns"] - r["source_ns"]) / 1e6 for r in successful]
    counts = Counter(terminals[r["id"]]["outcome"] if r["id"] in terminals else "ros_missing" for r in cohort)
    result = {"generated": len(cohort), "successful": len(successful), "outcomes": dict(counts),
              "source_to_response_ms": stats(latency),
              "source_scheduling_lag_ms": stats([(r["source_ns"] - r["scheduled_ns"]) / 1e6 for r in cohort]),
              "offered_rps": len(cohort) * 1e9 / (end - begin),
              "successful_cohort_payload_bytes": sum(r["payload_bytes"] for r in successful)}
    if deadline_ms is not None:
        timely = sum(value <= deadline_ms for value in latency)
        result.update(deadline_ms=deadline_ms, timely=timely,
                      deadline_miss_fraction=(len(cohort) - timely) / len(cohort) if cohort else None)
    return result


def analyze(folder):
    folder = Path(folder).resolve()
    config, start = read_json(folder / "resolved.json"), read_json(folder / "start.json")
    begin, end = start["measurement_start_ns"], start["measurement_end_ns"]
    duration = (end - begin) / 1e9
    checks, errors = [], []
    def check(condition, message):
        (checks if condition else errors).append(message)
    worker_rows = []
    for index in range(config["workers"]):
        worker_rows.extend(records(folder / "logs" / f"worker{index+1}" / "worker.jsonl"))
    processed = [r for r in worker_rows if r["outcome"] == "processed"]
    processed_counts = Counter(r["id"] for r in processed)
    processed_by_id = {r["id"]: r for r in processed}
    check(all(count == 1 for count in processed_counts.values()), "Each original ID has at most one central processing record")
    for name in config["active_components"]:
        log = folder / "logs" / name
        check(not (log / "failure.json").exists(), name + " has no component failure record")
        check((log / "final.json").exists(), name + " completed normal finalization")
        if (log / "final.json").exists():
            final = read_json(log / "final.json")
            check(final.get("pending", 0) == 0, name + " has no unresolved internal jobs")
    workers = {}
    for index in range(config["workers"]):
        name = f"worker{index+1}"
        rows = [r for r in processed if r["worker_id"] == name]
        measured = [r for r in rows if begin <= r["processed_ns"] < end]
        workers[name] = {"processed_all": len(rows), "completed_rps": len(measured) / duration,
                         "service_ms": stats([(r["processed_ns"] - r["service_start_ns"]) / 1e6 for r in measured])}
        try:
            queue, intervals = replay_queue(records(folder / "logs" / name / "queue.jsonl"), begin, end,
                                           config["worker_queue_capacity"], config["worker_threads"])
            workers[name]["queue"] = queue
            for row in rows:
                recorded = intervals[row["id"]]
                check(recorded["dequeue_ns"] == row["service_start_ns"] and recorded["terminal_ns"] == row["processed_ns"], f"{name} exact processing boundaries: {row['id']}")
        except (ValueError, KeyError) as exc:
            errors.append(f"{name} queue validation: {exc}")
    all_sources, all_terminals, robot_results, class_table = [], [], {}, []
    for robot in config["robots"]:
        name, site = robot["id"], robot["site"]
        log = folder / "logs" / name
        source, terminal = records(log / "source.jsonl"), records(log / "gateway.jsonl")
        generated, terminals = unique(source, name + " source"), unique(terminal, name + " gateway")
        check(bool(source), name + " generated traffic")
        check(not (terminals.keys() - generated.keys()), name + " has no unknown terminal IDs")
        ready, final = read_json(log / "ready.json"), read_json(log / "final.json")
        model = read_json(log / "arrival_model.json")
        expected_schedule = []
        for stream in model["streams"]:
            sequence = 0
            while (offset := round((stream["phase_s"] + sequence / stream["rate_hz"]) * 1e9)) < model["horizon_ns"]:
                expected_schedule.append((start["start_ns"] + offset, stream["message_type"], sequence))
                sequence += 1
        observed_schedule = [(r["scheduled_ns"], r["message_type"], r["sequence_number"]) for r in source]
        check(observed_schedule == sorted(expected_schedule), name + " generated the complete configured independent source schedule")
        check(all(r["source_ns"] >= r["scheduled_ns"] and r["payload_bytes"] == config["traffic"][r["message_type"]]["payload_bytes"] for r in source), name + " schedule and useful payload sizes")
        check(ready["actual_source_node_count"] == 1 and ready["actual_gateway_node_count"] == 1,
              name + " contains its own source and gateway ROS nodes")
        check(ready["clock_domain_id"] == start["clock_domain_id"], name + " common clock")
        check(set(final["ros_missing_ids"]) == generated.keys() - terminals.keys(), name + " ROS missing identities reconcile")
        check(final["pending"] == 0, name + " queue drained or explicitly resolved")
        try:
            queue, intervals = replay_queue(records(log / "queue.jsonl"), begin, end, config["queue_capacity"], 1)
            for identity, interval in intervals.items():
                row = terminals[identity]
                check(all(row.get(field) == stamp for field, stamp in interval.items()), name + " message/event timestamps: " + identity)
        except (ValueError, KeyError) as exc:
            errors.append(f"{name} queue validation: {exc}")
            queue = None
        for row in terminal:
            original = generated[row["id"]]
            check(all(row[key] == original[key] for key in ("robot_id", "message_type", "source_ns", "scheduled_ns", "payload_bytes")), "Message identity preserved: " + row["id"])
            check(row["http_body_bytes"] == original["http_body_bytes"], "Gateway HTTP body size: " + row["id"])
            if row["outcome"] == "success":
                check(row["id"] in processed_counts, "Acknowledged message was centrally processed: " + row["id"])
                check(row["worker_clock_domain_id"] == start["clock_domain_id"], "Acknowledgement clock: " + row["id"])
                worker = processed_by_id.get(row["id"], {})
                check(all(row.get(ack_field) == worker.get(log_field) for ack_field, log_field in
                          (("worker_id", "worker_id"), ("worker_admission_ns", "admission_ns"),
                           ("worker_service_start_ns", "service_start_ns"), ("worker_processed_ns", "processed_ns"))),
                      "ACK matches the exact worker processing record: " + row["id"])
                ordered = [row[key] for key in ("source_ns", "admission_ns", "dequeue_ns", "request_ns", "worker_admission_ns", "worker_service_start_ns", "worker_processed_ns", "response_ns", "terminal_ns")]
                check(ordered == sorted(ordered), "Stage ordering: " + row["id"])
        metrics = outcome_metrics(source, terminals, begin, end)
        metrics.update(site=site, queue=queue,
                       completed_rps=sum(r["outcome"] == "success" and begin <= r["response_ns"] < end for r in terminal) / duration,
                       payload_goodput_mbit_s=sum(r["payload_bytes"] * 8 for r in terminal if r["outcome"] == "success" and begin <= r["response_ns"] < end) / duration / 1e6)
        robot_results[name] = metrics
        for kind in config["traffic"]:
            row = outcome_metrics([r for r in source if r["message_type"] == kind], terminals, begin, end, config["deadlines_ms"][kind])
            class_table.append({"robot": name, "site": site, "class": kind, **row})
        all_sources.extend(source)
        all_terminals.extend(terminal)
    source_map, terminal_map = unique(all_sources, "global source"), unique(all_terminals, "global gateway")
    check(set(processed_counts).issubset(source_map), "Central processing IDs all originate at a source")
    for row in processed:
        original = source_map.get(row["id"], {})
        check(all(row.get(key) == original.get(key) for key in
                  ("robot_id", "message_type", "sequence_number", "source_ns", "scheduled_ns", "payload_bytes", "http_body_bytes")),
              "Central source identity and exact body bytes: " + row["id"])
    cleanup = read_json(folder / "network" / "cleanup.json")
    check(cleanup["success"], "Owned network resources cleaned up")
    check(not any(r["forced_kill"] or r["exit_code"] != 0 for r in read_json(folder / "process_cleanup.json")), "All components exited normally without forced termination")
    captures = read_json(folder / "network" / "capture_metadata.json") if config["capture"] else None
    if config["capture"]:
        check(set(captures.get("sites", {})) == {"A", "B"}, "Both site capture records exist")
        check(not captures.get("errors"), "No capture lifecycle errors")
        active_sites = {r["site"] for r in config["robots"]}
        for site, capture in captures.get("sites", {}).items():
            check(capture.get("statistics_complete") and capture.get("capture_complete") and capture.get("packets_dropped_by_kernel") == 0,
                  "Site " + site + " capture completed with zero kernel drops")
            try:
                count = pcap_record_count(folder / "network" / ("site" + site + "_wan.pcap"))
                check(count == capture.get("packets_captured") and (count > 0 or site not in active_sites),
                      "Site " + site + " PCAP packet count and framing match recorder metadata")
            except (OSError, ValueError) as exc:
                errors.append("Site " + site + " PCAP integrity: " + str(exc))
    summary = outcome_metrics(all_sources, terminal_map, begin, end)
    summary.update(schema_version=1, run_id=config["run_id"], topology=config["topology"], duration_s=duration,
                   actual_robot_clients=len(config["robots"]), actual_source_nodes=len(config["robots"]),
                   actual_worker_processes=config["workers"], per_robot=robot_results, workers=workers,
                   completed_rps=sum(r["completed_rps"] for r in robot_results.values()),
                   central_processed_all=len(processed), central_processed_unacknowledged_all=sum(identity not in terminal_map or terminal_map[identity]["outcome"] != "success" for identity in processed_counts),
                   duplicate_central_effects=sum(max(0, count-1) for count in processed_counts.values()),
                   accounting_passed=not errors, validation_errors=errors,
                   successful_checks=len(checks), capture_metadata=captures,
                   measurement_note="Source-time cohort; successful source-to-response latency includes drain. Gateway queue ends at terminal_ns after response handling. Completion/goodput rates count responses inside the measurement window. No pooled repetition statistics.")
    # Per-robot rates remain visible; zero service is retained in fairness.
    rates = [r["completed_rps"] for r in robot_results.values()]
    summary["jain_completion_fairness"] = sum(rates)**2 / (len(rates)*sum(r*r for r in rates)) if any(rates) else None
    (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (folder / "validation.json").write_text(json.dumps({"passed": not errors, "successful_checks": len(checks), "errors": errors,
       "note": "Application/queue accounting and capture metadata checks; this is not full TCP/HTTP capture reconstruction."}, indent=2) + "\n", encoding="utf-8")
    flat_rows = []
    for row in class_table:
        flat_rows.append({k: v for k, v in row.items() if not isinstance(v, dict)} | {
            "latency_mean_ms": row["source_to_response_ms"]["mean"], "latency_p95_ms": row["source_to_response_ms"]["p95"],
            "latency_p99_ms": row["source_to_response_ms"]["p99"], "outcomes": json.dumps(row["outcomes"], sort_keys=True)})
    with (folder / "per_robot_class.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(flat_rows[0]))
        writer.writeheader()
        writer.writerows(flat_rows)
    if errors:
        raise ValueError("Independent validation failed: " + "; ".join(errors[:10]))
    return summary
