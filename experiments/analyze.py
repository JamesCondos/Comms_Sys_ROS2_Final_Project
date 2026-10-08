"""Create run-level tables, plots and a report from immutable raw results."""
import argparse
import csv
import json
from pathlib import Path
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    summaries = [json.loads(p.read_text()) for p in sorted(args.results.glob("*/summary.json"))]
    if not summaries:
        raise SystemExit("No completed runs found")
    rows = []
    for s in summaries:
        row = {k: v for k, v in s.items() if not isinstance(v, (dict, list))}
        row.update(s["classes"]["all"])
        rows.append(row)
    with (args.results / "runs.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    counts = sorted(set(s["robots"] for s in summaries))
    groups = [[s for s in summaries if s["robots"] == n] for n in counts]
    x = [statistics.mean(s["offered_rps"] for s in group) for group in groups]
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.5))
    for kind in ("command", "status", "odometry", "sensor"):
        values = [[s["classes"][kind]["end_to_end_p95_ms"] for s in g] for g in groups]
        means = [statistics.mean(v) for v in values]
        axes[0].errorbar(x, means, yerr=[[m-min(v) for m,v in zip(means,values)],
                                       [max(v)-m for m,v in zip(means,values)]], marker="o", capsize=3, label=kind)
    axes[0].set_ylabel("Source-to-response p95 (ms)")
    axes[0].legend()
    for ax, field, label in [(axes[1], "mean_queue_length", "Time-averaged FIFO length (messages)"),
                              (axes[2], "payload_goodput_mbps", "Payload goodput (Mbit/s)")]:
        values = [[s[field] for s in g] for g in groups]
        means = [statistics.mean(v) for v in values]
        ax.errorbar(x, means, yerr=[[m-min(v) for m,v in zip(means,values)],
                                   [max(v)-m for m,v in zip(means,values)]], marker="o", capsize=3)
        ax.set_ylabel(label)
    for ax in axes:
        ax.set_xlabel("Observed offered load (messages/s)")
        ax.set_xscale("log", base=2)
        ax.set_xticks(x, labels=[str(round(v)) for v in x])
        ax.grid(alpha=0.25)
    axes[0].set_yscale("log")
    axes[1].set_yscale("log")
    fig.suptitle("ROS2 / FIFO / persistent HTTP/1.1 / clean loopback\nPoints: mean of run statistics; error bars: run minimum–maximum")
    fig.tight_layout()
    fig.savefig(args.results / "baseline.png", dpi=180)
    fig.savefig(args.results / "baseline.svg")
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(10, 4))
    for group in groups:
        s = min(group, key=lambda s: s["repeat"])
        folder = args.results / s["run"]
        config = json.loads((folder / "config.json").read_text())
        begin, end = config["measurement_start_ns"], config["measurement_end_ns"]
        sample_ns, length, times, lengths = begin, 0, [], []
        with (folder / "queue.csv").open() as f:
            for event in csv.DictReader(f):
                timestamp = int(event["timestamp_ns"])
                while sample_ns < min(timestamp, end):
                    times.append((sample_ns - begin) / 1e9)
                    lengths.append(length)
                    sample_ns += 100_000_000
                if timestamp >= end:
                    break
                length = int(event["queue_length"])
        ax.plot(times, lengths, label=f"{s['robots']} robots")
    ax.set(xlabel="Measurement time (s)", ylabel="Waiting messages",
           title="FIFO occupancy: repeat 1, sampled every 100 ms")
    ax.legend(ncol=4)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(args.results / "queue_occupancy.png", dpi=180)
    plt.close(fig)
    lines = ["# Baseline experiment results", "",
             *(["[Interpretation and main findings](findings.md)", ""] if (args.results / "findings.md").exists() else []),
             "Actual ROS2 Humble publishers and subscriber in Ubuntu 22.04 WSL2, with a separate local HTTP server. "
             "One shared bounded FIFO, one sender, one outstanding HTTP request, no application retries or injected network faults.", "",
             "Each condition has a 5-second warm-up, 60-second measurement window (unless overridden in saved config), "
             "and up to 15 seconds to drain. Load order is seeded and shuffled. Publishers use seeded independent periodic phases. "
             "All clocks are the same Linux host's monotonic clock.", "",
             "| Robots | Runs | Offered msg/s | Completed msg/s | p95 ms, mean [min–max] | Mean FIFO length | Goodput Mbit/s | Drops | ROS missing |",
             "|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for n, group in zip(counts, groups):
        mean = lambda k: statistics.mean(s[k] for s in group)
        p = [s["classes"]["all"]["end_to_end_p95_ms"] for s in group]
        lines.append(f"| {n} | {len(group)} | {mean('offered_rps'):.1f} | {mean('completed_rps'):.1f} | "
                     f"{statistics.mean(p):.2f} [{min(p):.2f}–{max(p):.2f}] | {mean('mean_queue_length'):.3f} | "
                     f"{mean('payload_goodput_mbps'):.3f} | {sum(s['queue_drops'] for s in group)} | {sum(s['ros_missing'] for s in group)} |")
    lines += ["", "## Per-class latency", "",
              "Mean of each run's source-to-response p95, in milliseconds. Successful messages only.", "",
              "| Robots | Command | Status | Odometry | Sensor |",
              "|---:|---:|---:|---:|---:|"]
    for n, group in zip(counts, groups):
        values = [statistics.mean(s["classes"][kind]["end_to_end_p95_ms"] for s in group)
                  for kind in ("command", "status", "odometry", "sensor")]
        lines.append(f"| {n} | " + " | ".join(f"{v:.2f}" for v in values) + " |")
    lines += ["", "## Delay components and delivery", "",
              "Mean delays across runs, in milliseconds. Delivery includes the drain period; drops are summed across repeats.", "",
              "| Robots | ROS to gateway | FIFO wait | HTTP exchange | Successful/generated | FIFO drop fraction |",
              "|---:|---:|---:|---:|---:|---:|"]
    for n, group in zip(counts, groups):
        delays = [statistics.mean(s["classes"]["all"][key + "_mean_ms"] for s in group)
                  for key in ("ros_to_gateway", "queue_wait", "http")]
        generated = sum(s["generated"] for s in group)
        successful = sum(s["classes"]["all"]["successful"] for s in group)
        dropped = sum(s["queue_drops"] for s in group)
        lines.append(f"| {n} | " + " | ".join(f"{v:.3f}" for v in delays) +
                     f" | {successful}/{generated} | {100*dropped/generated:.2f}% |")
    lines += ["", "![Baseline plots](baseline.png)", "", "![Queue occupancy](queue_occupancy.png)",
              "", "## Definitions and limitations", "",
              "- Latency: source publication timestamp to complete HTTP response receipt. Queue wait: gateway enqueue to dequeue. "
              "HTTP time includes JSON serialization and the exchange. Source-to-gateway delay includes ROS transport and callback scheduling.",
              "- Latency statistics use successfully completed messages generated during the measurement window, including those completed during drain. "
              "Throughput counts completions inside the measurement window, so the populations intentionally differ.",
              "- FIFO length is integrated over enqueue/dequeue events, excludes the request in service, and is bounded at 1,000 messages. "
              "Overflow drops the arriving message. ROS additionally uses reliable KEEP_LAST depth 1,000.",
              "- Payload goodput counts only configured payload bytes, excluding JSON metadata, HTTP, TCP/IP and ROS overhead. "
              "The payload is repeated ASCII bytes; no compression is enabled.",
              "- Each robot contributes 32 messages/s and nominally 0.8456 Mbit/s of application payload. "
              "These are synthetic robot identities/topics carried through one source process; they are not separate physical computers.",
              "- This is a local WSL2 architecture baseline, not physical-link capacity or a real robot benchmark. "
              "Publisher, gateway and server share host CPU. There is no artificial server processing delay.",
              "- HTTP is cleartext, with no TLS or compression. The server enables TCP_NODELAY. "
              "The client uses Python's standard-library HTTPConnection, with a two-second request timeout and no replay on failure.",
              "- The plots report means of per-run statistics with min–max ranges; these are not confidence intervals or pooled percentiles. "
              "Three repeats and approximately 120 command samples per robot per run limit p99 precision.",
              "- No M/M/1 fit is claimed. Periodic mixed-size arrivals and measured HTTP service times do not establish its assumptions.",
              "- Missing ROS messages, overflow drops, failed requests and pending messages must be considered alongside successful-message latency.", "",
              "## Validation", ""]
    if all("validation" in s for s in summaries):
        lines.append(f"All {len(summaries)} completed runs passed identity uniqueness, source/gateway/server matching checks, timestamp ordering, "
                     "HTTP/1.1 verification and exactly one accepted HTTP connection per run." if all(all(s['validation'].values()) for s in summaries) else "Some validation checks failed; inspect summaries.")
    else:
        lines.append("Run `validate_results.py` on this results folder to check message identities, timestamp ordering, "
                     "HTTP/1.1 and connection reuse from the saved logs.")

    lines.append(f"Total request errors: {sum(s['request_errors'] for s in summaries)}. "
                 f"Pending after drain: {sum(s['pending_after_drain'] for s in summaries)}.")
    (args.results / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(args.results / "report.md")


if __name__ == "__main__":
    main()
