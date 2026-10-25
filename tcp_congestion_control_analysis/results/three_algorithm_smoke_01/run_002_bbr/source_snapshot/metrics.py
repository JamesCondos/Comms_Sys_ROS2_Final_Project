"""Save readable logs and calculate measurements from the saved messages."""

import csv
import json
import math
from pathlib import Path


def save_json(path, value):
    # Replacing a temporary file prevents other processes reading half a config.
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def read_rows(path):
    # Each line is one complete message or event, so logs remain easy to inspect.
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def percentile(values, percent):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * percent / 100
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def queue_mean(events, begin, end):
    # Weight each queue length by the time it was actually present.
    previous, length, area = begin, 0, 0
    for event in events:
        stamp = event["time_ns"]
        if stamp < begin:
            length = event["queue_len"]
        elif stamp < end:
            area += length * (stamp - previous)
            previous, length = stamp, event["queue_len"]
        else:
            break
    area += length * (end - previous)
    return area / (end - begin)


def summarize(folder):
    folder = Path(folder)
    config = json.loads((folder / "resolved.json").read_text())
    timing = json.loads((folder / "start.json").read_text())
    begin, end = timing["measurement_start_ns"], timing["measurement_end_ns"]
    seconds = (end - begin) / 1e9
    sources, replies, workers, samples = [], [], [], []
    per_robot, errors = {}, []

    # Check every source against its own gateway; unsuccessful messages stay counted.
    for robot in config["robots"]:
        name = robot["id"]
        log = folder / "logs" / name
        generated = read_rows(log / "source.jsonl")
        terminal = read_rows(log / "gateway.jsonl")
        sources.extend(generated)
        replies.extend(terminal)
        events = read_rows(log / "queue.jsonl")
        per_robot[name] = {"site": robot["site"],
                           "queue": {"mean_Lq": queue_mean(events, begin, end)}}
        if not generated:
            errors.append(name + " published no messages")

    for name in config["active_components"]:
        log = folder / "logs" / name
        final_path = log / "final.json"
        if not final_path.exists() or json.loads(final_path.read_text()).get("pending", 1):
            errors.append(name + " did not finish with an empty queue")
        if (log / "failure.json").exists():
            errors.append(name + " recorded a component failure")
        if name.startswith("worker"):
            workers.extend(read_rows(log / "worker.jsonl"))
        if name.startswith("edge"):
            samples.extend(read_rows(log / "connections.jsonl"))

    # A successful reply must correspond to one real worker completion.
    source_ids = {row["id"] for row in sources}
    reply_ids = {row["id"] for row in replies}
    processed = {row["id"]: row for row in workers}
    if len(source_ids) != len(sources) or len(reply_ids) != len(replies):
        errors.append("Duplicate source or terminal message IDs")
    if source_ids - reply_ids:
        errors.append("Some ROS publications have no gateway outcome")
    if len(processed) != len(workers) or not reply_ids.issubset(source_ids):
        errors.append("Worker duplication or unknown gateway IDs")
    for row in replies:
        if row["outcome"] == "success":
            worker = processed.get(row["id"])
            if not worker or worker["processed_ns"] != row.get("worker_processed_ns"):
                errors.append("Acknowledgement does not match worker: " + row["id"])
            stamps = [row[key] for key in ("source_ns", "admission_ns", "dequeue_ns", "request_ns", "response_ns")]
            if stamps != sorted(stamps):
                errors.append("Message timestamps are not ordered: " + row["id"])

    # Network and process failures must not be mistaken for successful experiments.
    if not json.loads((folder / "network/cleanup.json").read_text())["success"]:
        errors.append("Network cleanup failed")
    stopped = json.loads((folder / "process_cleanup.json").read_text())
    if any(row["forced_kill"] or row["exit_code"] != 0 for row in stopped):
        errors.append("A component did not exit normally")
    if config["capture"]:
        capture = json.loads((folder / "network/capture_metadata.json").read_text())
        if capture["errors"] or set(capture["sites"]) != {"A", "B"} or any(
                not row["capture_complete"] or row["packets_dropped_by_kernel"] != 0
                for row in capture["sites"].values()):
            errors.append("Capture incomplete or recorder drops observed")

    offered = [row for row in sources if begin <= row["source_ns"] < end]
    by_id = {row["id"]: row for row in replies}
    successful = [by_id[row["id"]] for row in offered
                  if row["id"] in by_id and by_id[row["id"]]["outcome"] == "success"]
    completed = [row for row in replies if row["outcome"] == "success"
                 and begin <= row["response_ns"] < end]
    outcomes = {}
    for row in offered:
        outcome = by_id.get(row["id"], {}).get("outcome", "ros_missing")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    latency = [(row["response_ns"] - row["source_ns"]) / 1e6 for row in successful]
    for name in per_robot:
        measured = [row for row in completed if row["robot_id"] == name]
        per_robot[name].update(completed_rps=len(measured) / seconds,
            payload_goodput_mbit_s=sum(row["payload_bytes"] for row in measured) * 8 / seconds / 1e6)

    # TCP counters are cumulative per connection. Taking the maximum avoids double counting.
    retransmissions = {}
    for row in samples:
        if row["event"] == "tcp_sample":
            key = row["connection_id"]
            retransmissions[key] = max(retransmissions.get(key, 0), row["total_retransmissions"])
    summary = dict(runtime="simple_https_v1", duration_s=seconds, generated=len(offered),
        successful=len(successful), outcomes=outcomes, offered_rps=len(offered) / seconds,
        completed_rps=len(completed) / seconds, per_robot=per_robot,
        source_to_response_ms={"mean": sum(latency) / len(latency) if latency else None,
                               "p95": percentile(latency, 95), "p99": percentile(latency, 99)},
        observed_tcp_retransmissions=sum(retransmissions.values()),
        tcp_counter_scope="Observed edge sockets across warm-up, measurement and drain; closed unsampled sockets may be missed",
        accounting_passed=not errors)
    # Deadline failures include dropped, failed and missing source messages.
    summary["classes"] = {}
    for topic, deadline in config.get("deadlines_ms", {}).items():
        sent = [row for row in offered if row["topic"] == topic]
        received = [row for row in successful if row["topic"] == topic]
        delays = [(row["response_ns"] - row["source_ns"]) / 1e6 for row in received]
        timely = sum(delay <= deadline for delay in delays)
        summary["classes"][topic] = dict(generated=len(sent), successful=len(received),
            latency_p95_ms=percentile(delays, 95), deadline_ms=deadline,
            deadline_miss_ratio=(len(sent) - timely) / len(sent) if sent else None)
    save_json(folder / "summary.json", summary)
    save_json(folder / "validation.json", {"passed": not errors, "errors": errors})

    # Save a one-second application throughput series, including the drain period.
    horizon = max([end] + [(row.get("response_ns") or end) + 1 for row in replies])
    bins = max(1, math.ceil((horizon - begin) / 1e9))
    with (folder / "timeseries.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["second", "phase", "generated", "completed", "payload_goodput_mbit_s"])
        for index in range(bins):
            left, right = begin + index * 10**9, begin + (index + 1) * 10**9
            width = (right - left) / 1e9
            received = [row for row in replies if row["outcome"] == "success"
                        and left <= row["response_ns"] < right]
            writer.writerow([index, "measurement" if left < end else "drain",
                sum(left <= row["source_ns"] < right for row in sources), len(received),
                sum(row["payload_bytes"] for row in received) * 8 / max(width, 1e-9) / 1e6])
    if errors:
        raise ValueError("Measurement checks failed: " + "; ".join(errors[:5]))
    return summary
