"""Audit TCP socket selection and plot application performance from raw logs."""
import argparse
from collections import Counter, defaultdict
import json
import math
import os
from pathlib import Path
import statistics
import tempfile

COLORS = {"cubic": "#2878b5", "reno": "#e1812c", "bbr": "#30975c"}


def read_json(path):
    return json.loads(Path(path).read_text())


def read_log(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (index - lower)


def audit_run(folder):
    """Use local relative log paths; recorded historical absolute paths stay untouched."""
    config, summary, timing = [read_json(folder / name) for name in ("resolved.json", "summary.json", "start.json")]
    begin, end = timing["measurement_start_ns"], timing["measurement_end_ns"]
    duration = (end - begin) / 1e9
    trial = config["tcp_trial"]
    generated, terminal, queue_events = [], [], []
    for robot in config["robots"]:
        log = folder / "logs" / robot["id"]
        generated.extend(read_log(log / "source.jsonl"))
        terminal.extend(read_log(log / "gateway.jsonl"))
        queue_events.extend((event, robot["id"]) for event in read_log(log / "queue.jsonl"))
    source_map = {row["id"]: row for row in generated}
    terminal_map = {row["id"]: row for row in terminal}
    offered = [row for row in generated if begin <= row["source_ns"] < end]
    cohort = [terminal_map[row["id"]] for row in offered if row["id"] in terminal_map]
    successful = [row for row in cohort if row["outcome"] == "success"]
    completed = [row for row in terminal if row["outcome"] == "success" and begin <= row.get("response_ns", 0) < end]
    latency = [(row["response_ns"] - row["source_ns"]) / 1e9 for row in successful]
    waiting = [(row["dequeue_ns"] - row["admission_ns"]) / 1e9 for row in successful]
    exchange = [(row["response_ns"] - row["request_ns"]) / 1e9 for row in successful]
    connections = []
    processed = []
    for name in config["active_components"]:
        role = config["nodes"][name]["role"]
        if role == trial["wan_sender_role"]:
            connections.extend(row for row in read_log(folder / "logs" / name / "connections.jsonl")
                               if row["event"] == "outgoing_connection")
        if role == "worker":
            processed.extend(row for row in read_log(folder / "logs" / name / "worker.jsonl") if row["outcome"] == "processed")
    processed_counts = Counter(row["id"] for row in processed)
    actual = sorted({row["tcp_congestion_actual"] for row in connections})
    expected_peers = {node["ip"] for node in config["nodes"].values()
                      if node["role"] == ("proxy" if config["topology"] in ("T2_CENTRAL_POOL", "T3_EDGE_PASS") else "worker")}
    checks = {
        "saved_application_validation_passed": read_json(folder / "validation.json")["passed"] is True,
        "summary_accounting_passed": summary["accounting_passed"] is True,
        "unique_source_and_terminal_ids": len(source_map) == len(generated) and len(terminal_map) == len(terminal),
        "terminal_ids_originate_at_source": set(terminal_map).issubset(source_map),
        "successful_cohort_was_processed_once": all(processed_counts[row["id"]] == 1 for row in successful),
        "offered_count_matches_summary": len(offered) == summary["generated"],
        "successful_count_matches_summary": len(successful) == summary["successful"],
        "completion_rate_matches_summary": math.isclose(len(completed) / duration, summary["completed_rps"]),
        "p95_matches_raw_log": (not latency and summary["source_to_response_ms"]["p95"] is None)
            or (bool(latency) and math.isclose(percentile(latency, .95) * 1000, summary["source_to_response_ms"]["p95"])),
        "wan_socket_algorithm_verified": bool(connections) and all(
            row["tcp_congestion_requested"] == row["tcp_congestion_actual"] == trial["algorithm"] for row in connections),
        "wan_socket_endpoints_verified": bool(connections) and all(row["peer"][0] in expected_peers for row in connections),
        "network_cleanup_succeeded": read_json(folder / "network/cleanup.json")["success"] is True,
        "processes_exited_cleanly": all(not row["forced_kill"] and row["exit_code"] == 0 for row in read_json(folder / "process_cleanup.json")),
    }
    capture_drops = None
    if config["capture"]:
        captures = read_json(folder / "network/capture_metadata.json")
        checks["capture_metadata_complete_without_drops"] = (
            not captures.get("errors") and set(captures["sites"]) == {"A", "B"}
            and all(row.get("capture_complete") and row.get("statistics_complete")
                    and row.get("packets_dropped_by_kernel") == 0 for row in captures["sites"].values()))
        capture_drops = sum(row.get("packets_dropped_by_kernel", 0) for row in captures["sites"].values())
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(f"{folder.name}: failed evidence checks: {failed}")
    queue_state = defaultdict(int)
    trace = []
    for event, robot in sorted(queue_events, key=lambda pair: pair[0]["time_ns"]):
        queue_state[robot] = event["queue_len"]
        if event["time_ns"] >= timing["start_ns"]:
            trace.append(((event["time_ns"] - begin) / 1e9, sum(queue_state.values())))
    trace.insert(0, ((timing["start_ns"] - begin) / 1e9, 0))
    mean_latency = statistics.mean(latency) if latency else None
    mean_waiting = statistics.mean(waiting) if waiting else None
    mean_exchange = statistics.mean(exchange) if exchange else None
    mean_other = max(0, mean_latency - mean_waiting - mean_exchange) if latency else None
    return {
        **trial, "run": folder.name, "duration_s": duration,
        "generated": len(offered), "successful_including_drain": len(successful),
        "completed_in_window": len(completed), "offered_rps": len(offered) / duration,
        "completed_rps": len(completed) / duration,
        "payload_goodput_mbit_s": sum(row["payload_bytes"] for row in completed) * 8 / duration / 1e6,
        "latency_p95_s": percentile(latency, .95), "latency_mean_s": mean_latency,
        "queue_wait_mean_s": mean_waiting, "http_exchange_mean_s": mean_exchange,
        "other_mean_s": mean_other,
        "gateway_queue_fraction_of_mean_latency": mean_waiting / mean_latency if mean_latency else None,
        "mean_total_waiting_queue": sum(row["queue"]["mean_Lq"] for row in summary["per_robot"].values()),
        "outcomes": dict(Counter(row["outcome"] for row in cohort)),
        "ros_missing": len(offered) - len(cohort), "wan_connections": len(connections),
        "verified_algorithms": actual, "capture_kernel_drops": capture_drops,
        "checks": checks, "queue_trace": trace,
    }


def review_sweep(sweep):
    plan = read_json(sweep / "plan.json")
    status = read_json(sweep / "sweep_status.json")
    if not status.get("passed") or status["completed_runs"] != len(plan["runs"]):
        raise ValueError("A complete successful sweep is required; partial/failed runs are not silently omitted")
    results = []
    for planned in plan["runs"]:
        folder = sweep / planned["run"]
        expected_config = read_json(sweep / planned["config"])
        actual_config = read_json(folder / "resolved.json")
        if any(actual_config.get(key) != value for key, value in expected_config.items()):
            raise ValueError(f"{folder.name}: runtime settings differ from the planned configuration")
        row = audit_run(folder)
        if any(row[key] != planned[key] for key in ("algorithm", "load", "loss_percent", "repeat", "wan_sender_role")):
            raise ValueError("Saved run conditions differ from sweep plan")
        results.append(row)
    return results


def generate_report(sweep):
    sweep = Path(sweep).resolve()
    rows = review_sweep(sweep)
    output = sweep / "analysis"
    output.mkdir(exist_ok=True)
    evidence = [{key: value for key, value in row.items() if key != "queue_trace"} for row in rows]
    (output / "evidence.json").write_text(json.dumps({"all_checks_passed": True, "runs": evidence,
        "limitations": ["Application performance and verified socket settings; no automatic retransmission classification.",
                        "Capture drop metadata is not full TCP stream reconstruction.",
                        "Single repetitions do not establish statistical superiority."]}, indent=2) + "\n")
    os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / f"comms-tcp-matplotlib-{os.getuid()}"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    groups = defaultdict(list)
    for row in rows:
        groups[row["load"], row["loss_percent"]].append(row)
    figures = []
    for (load, loss), selected in sorted(groups.items()):
        algorithms = sorted({row["algorithm"] for row in selected})
        by_algorithm = {algorithm: [row for row in selected if row["algorithm"] == algorithm] for algorithm in algorithms}
        fig, axes = plt.subplots(2, 2, figsize=(12, 8.5), constrained_layout=True)
        fig.suptitle(f"TCP comparison — load ×{load:g}, forward loss {loss:g}%", fontsize=16, weight="bold")
        x = list(range(len(algorithms)))
        def values(field):
            samples = [[row[field] for row in by_algorithm[algorithm] if row[field] is not None] for algorithm in algorithms]
            return [statistics.mean(sample) if sample else 0 for sample in samples]
        def spread(field):
            means = values(field)
            samples = [[row[field] for row in by_algorithm[algorithm] if row[field] is not None] or [0] for algorithm in algorithms]
            return [[mean - min(sample) for mean, sample in zip(means, samples)],
                    [max(sample) - mean for mean, sample in zip(means, samples)]]
        labels = [f"{algorithm.upper()}\nn={len(by_algorithm[algorithm])} run(s)" for algorithm in algorithms]
        colors = [COLORS.get(algorithm, "#777777") for algorithm in algorithms]
        repeated = any(len(by_algorithm[algorithm]) > 1 for algorithm in algorithms)
        ax = axes[0, 0]
        ax.bar([i-.18 for i in x], values("offered_rps"), width=.34, color="#d1d7dc", label="Offered")
        bars = ax.bar([i+.18 for i in x], values("completed_rps"), width=.34, color=colors,
                      yerr=spread("completed_rps") if repeated else None, capsize=4, label="Completed inside window")
        ax.bar_label(bars, fmt="%.1f", padding=4)
        ax.set(title="Offered and completed traffic", ylabel="Messages per second", xticks=x, xticklabels=labels)
        ax.set_ylim(0, max(values("offered_rps") + values("completed_rps")) * 1.25)
        ax.legend(fontsize=9)
        ax = axes[0, 1]
        bars = ax.bar(x, values("latency_p95_s"), color=colors, width=.5,
                      yerr=spread("latency_p95_s") if repeated else None, capsize=4)
        ax.bar_label(bars, labels=[f"{value:.3f} s" if any(row["latency_p95_s"] is not None for row in by_algorithm[algorithm])
                                  else "Undefined\n(no successes)" for algorithm, value in zip(algorithms, values("latency_p95_s"))], padding=5)
        ax.set(title="Successful-message p95 latency", ylabel="Source to response (seconds)", xticks=x, xticklabels=labels)
        ax.set_ylim(0, max(max(values("latency_p95_s")) * 1.25, .1))
        ax.text(.02, .97, "Includes drain; undefined for runs without successes", transform=ax.transAxes, va="top", fontsize=8)
        ax = axes[1, 0]
        for row in selected:
            times, queue = zip(*row["queue_trace"])
            ax.step(times, queue, where="post", color=COLORS.get(row["algorithm"], "#777777"),
                    linestyle="--" if row["algorithm"] == "reno" else "-",
                    label=f"{row['algorithm'].upper()} r{row['repeat']}", alpha=.85)
        duration = selected[0]["duration_s"]
        ax.axvspan(0, duration, color="#dce9ee", alpha=.45, label="Measurement window")
        ax.axvline(duration, color="#555555", linestyle="--", linewidth=1)
        ax.set(title="Gateway queue over time", xlabel="Seconds from measurement start", ylabel="Waiting messages across all robots")
        ax.legend(fontsize=8)
        ax = axes[1, 1]
        waiting, exchange, other = values("queue_wait_mean_s"), values("http_exchange_mean_s"), values("other_mean_s")
        ax.bar(x, waiting, color="#b84a48", label="Gateway queue wait")
        ax.bar(x, exchange, bottom=waiting, color="#58a6a6", label="HTTP exchange")
        ax.bar(x, other, bottom=[a+b for a,b in zip(waiting, exchange)], color="#c4c4c4", label="Other source/gateway overhead")
        ax.set(title="Mean successful-message latency breakdown", ylabel="Seconds per successful message", xticks=x, xticklabels=labels)
        for i, fraction in enumerate(values("gateway_queue_fraction_of_mean_latency")):
            if any(row["latency_mean_s"] is not None for row in by_algorithm[algorithms[i]]):
                ax.text(i, waiting[i] / 2, f"{fraction:.1%}\nqueue wait", ha="center", va="center", color="white", weight="bold")
            else:
                ax.text(i, .01, "Undefined\n(no successes)", ha="center", va="bottom", fontsize=9)
        ax.set_ylim(0, max(max(values("latency_mean_s"))*1.35, .1))
        ax.legend(fontsize=8, loc="upper left")
        for ax in axes.flat:
            ax.spines[["top", "right"]].set_visible(False)
            ax.grid(axis="y", alpha=.18)
            ax.set_axisbelow(True)
        fig.supxlabel("Run-level means; bars show observed min–max when repeated. Short smoke runs do not establish an algorithm winner.", fontsize=9)
        name = f"comparison_load{load:g}_loss{loss:g}".replace(".", "p")
        for extension in ("png", "pdf"):
            path = output / f"{name}.{extension}"
            fig.savefig(path, dpi=180)
            figures.append(path.name)
        plt.close(fig)
    lines = ["# TCP result review", "", "All raw-log and socket-selection checks passed.", "",
             "| Algorithm | Load | Loss % | Repeat | Generated | Successful incl. drain | Completed/s | p95 (s) | Mean queue wait (s) | Verified WAN sockets |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in rows:
        p95 = f"{row['latency_p95_s']:.3f}" if row['latency_p95_s'] is not None else "undefined"
        wait = f"{row['queue_wait_mean_s']:.3f}" if row['queue_wait_mean_s'] is not None else "undefined"
        lines.append(f"| {row['algorithm']} | {row['load']:g} | {row['loss_percent']:g} | {row['repeat']} | {row['generated']} | {row['successful_including_drain']} | {row['completed_rps']:.2f} | {p95} | {wait} | {row['wan_connections']} |")
    lines += ["", "Successful cohort counts include drain; completion rates count responses inside the measurement window.",
              "Socket settings confirm that the requested algorithms were applied. Similar application performance does not establish equivalent TCP behavior.",
              "Retransmissions and congestion-window dynamics have not been inferred from capture-drop counters.", "",
              "## Figures", ""] + [f"- [{name}]({name})" for name in figures]
    (output / "review.md").write_text("\n".join(lines) + "\n")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sweep", type=Path, help="Completed TCP sweep directory")
    args = parser.parse_args()
    print(generate_report(args.sweep))
