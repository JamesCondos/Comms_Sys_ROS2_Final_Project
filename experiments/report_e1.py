"""Produce the final E1 report text, tables and publication-size figures."""
import argparse
import csv
import json
from pathlib import Path
import statistics as st

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

KINDS = ("command", "status", "odometry", "sensor")
COLORS = ("#0072B2", "#E69F00", "#009E73", "#CC79A7")
MARKERS = ("o", "s", "^", "D")


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    root = args.results
    summaries = [json.loads(p.read_text()) for p in sorted(root.glob("*/summary.json"))]
    assert summaries and all(s.get("schema_version") == 2 for s in summaries)
    loads = sorted({s["robots"] for s in summaries})
    groups = [[s for s in summaries if s["robots"] == n] for n in loads]
    assert all(len(g) == 3 for g in groups), "Final report requires three repeats per load"
    x = [st.mean(s["offered_rps"] for s in g) for g in groups]
    figures = root / "figures"
    figures.mkdir(exist_ok=True)
    plt.rcParams.update({"font.size": 9, "axes.spines.top": False, "axes.spines.right": False,
                         "savefig.dpi": 300, "pdf.fonttype": 42, "svg.fonttype": "none"})

    def errorbar(ax, getter, label, color=None, marker="o", style="-", x_values=None):
        values = [[getter(s) for s in g] for g in groups]
        means = [st.mean(v) for v in values]
        ax.errorbar(x if x_values is None else x_values, means, yerr=[[m-min(v) for m,v in zip(means,values)],
                                   [max(v)-m for m,v in zip(means,values)]],
                    label=label, color=color, marker=marker, linestyle=style,
                    markersize=4, linewidth=1.3, capsize=2)

    def axes_format(axes, x_values=None, xlabel="Total mixed-traffic load (messages/s)"):
        ticks = x if x_values is None else x_values
        for ax in axes:
            ax.set_xscale("log", base=2)
            ax.set_xticks(ticks, labels=[str(round(v)) for v in ticks])
            ax.set_xlabel(xlabel)
            ax.grid(alpha=0.22)

    def save(fig, name, layout_rect=None):
        fig.tight_layout(rect=layout_rect)
        for suffix in ("png", "pdf", "svg"):
            fig.savefig(figures / f"{name}.{suffix}", bbox_inches="tight")
        plt.close(fig)

    def class_load_format(fig, ax, title):
        axes_format([ax], x_values=loads, xlabel="Robot-equivalent load (N)")
        ax.set_title(title, pad=48)
        total_axis = ax.secondary_xaxis("top", functions=(lambda n: 32*n, lambda rate: rate/32))
        total_axis.set_xticks([32*n for n in loads], labels=[str(32*n) for n in loads])
        total_axis.set_xlabel("Nominal total mixed traffic (messages/s)", fontsize=8)
        total_axis.tick_params(labelsize=8)
        total_axis.minorticks_off()
        fig.text(0.5, 0.085, "Each curve measures one class within the same mixed workload.",
                 ha="center", fontsize=8)
        fig.text(0.5, 0.044, "N = 1: command 2/s + status 5/s + odometry 20/s + sensor 5/s.",
                 ha="center", fontsize=7.5)
        fig.text(0.5, 0.011, "Total = 32/s; all four rates scale by N.", ha="center", fontsize=7.5)

    fig, ax = plt.subplots(figsize=(5, 4.5))
    for kind, color, marker in zip(KINDS, COLORS, MARKERS):
        errorbar(ax, lambda s, k=kind: s["classes"][k]["end_to_end_p95_ms"], kind, color, marker, x_values=loads)
    ax.set(ylabel="Source-to-response p95 (ms)", yscale="log")
    ax.legend(fontsize=8)
    class_load_format(fig, ax, "Successful-message tail latency")
    save(fig, "fig1_latency", layout_rect=(0, 0.13, 1, 1))

    fig, ax = plt.subplots(figsize=(5, 4.5))
    for kind, color, marker in zip(KINDS, COLORS, MARKERS):
        target = summaries[0]["deadlines"][kind]["deadline_ms"]
        errorbar(ax, lambda s, k=kind: 100*s["deadlines"][k]["miss_ratio"], f"{kind}: {target} ms", color, marker, x_values=loads)
    ax.set(ylabel="Generated messages missing target (%)", ylim=(-3, 105))
    ax.legend(fontsize=8, loc="upper left")
    class_load_format(fig, ax, "Deadline failures, including drops")
    save(fig, "fig2_deadlines", layout_rect=(0, 0.13, 1, 1))

    fig, ax = plt.subplots(figsize=(5, 3.6))
    errorbar(ax, lambda s: s["mean_queue_length"], "Mean waiting queue", COLORS[0])
    ax.axhline(1000, color="0.4", linestyle="--", linewidth=1, label="Capacity: 1,000")
    ax.set(title="FIFO occupancy", ylabel="Time-averaged waiting messages", yscale="log")
    ax.legend(fontsize=8)
    axes_format([ax])
    save(fig, "fig3_queue")

    fig, ax = plt.subplots(figsize=(5, 3.6))
    errorbar(ax, lambda s: s["completed_rps"], "Completed requests", COLORS[0])
    ax.plot(x, x, "--", color="0.4", label="Completed = offered")
    ax.set(title="Throughput", ylabel="Completed requests/s")
    ax.legend(fontsize=8)
    axes_format([ax])
    save(fig, "fig4_throughput")

    fig, ax = plt.subplots(figsize=(5, 3.6))
    for name, color, marker in zip(("gateway", "server", "source"), COLORS, MARKERS):
        errorbar(ax, lambda s, n=name: s[n+"_cpu_percent"], name, color, marker)
    ax.axhline(100, color="0.6", linestyle=":", linewidth=1)
    ax.set(title="Process CPU consumption", ylabel="CPU use (% of one logical CPU)")
    ax.legend(fontsize=8)
    axes_format([ax])
    save(fig, "fig5_cpu")

    fig, ax = plt.subplots(figsize=(5, 3.6))
    errorbar(ax, lambda s: s["sender_busy_percent"], "Gateway sender", COLORS[0])
    errorbar(ax, lambda s: s["server_handler_busy_percent"], "Server POST handler", COLORS[1], "s")
    ax.set(title="Measured activity fractions", ylabel="Measurement window occupied (%)", ylim=(0, 105))
    ax.legend(fontsize=8)
    axes_format([ax])
    save(fig, "fig6_busy_time")

    fig, ax = plt.subplots(figsize=(5, 3.6))
    errorbar(ax, lambda s: s["payload_goodput_mbps"], "Delivered application payload", COLORS[0])
    ax.plot(x, [n*0.8456 for n in loads], "--", color="0.4", label="Nominal offered payload")
    ax.set(title="Payload goodput", ylabel="Application payload (Mbit/s)")
    ax.legend(fontsize=8)
    axes_format([ax])
    save(fig, "appendix_goodput")

    fig, ax = plt.subplots(figsize=(5, 3.6))
    errorbar(ax, lambda s: 100*s["queue_drops"]/s["generated"], "FIFO admission drops", COLORS[1])
    ax.set(title="Overflow loss", ylabel="Generated messages dropped (%)", ylim=(-3, 100))
    axes_format([ax])
    save(fig, "appendix_drops")

    fig, ax = plt.subplots(figsize=(7.4, 2.8))
    for group in groups[-3:]:
        s = next(s for s in group if s["repeat"] == 1)
        folder = root / s["run"]
        config = json.loads((folder/"config.json").read_text())
        begin, end = config["measurement_start_ns"], config["measurement_end_ns"]
        sample, length, times, lengths = begin, 0, [], []
        with (folder/"queue.csv").open() as f:
            for event in csv.DictReader(f):
                t = int(event["timestamp_ns"])
                while sample < min(t, end):
                    times.append((sample-begin)/1e9)
                    lengths.append(length)
                    sample += 100_000_000
                if t >= end:
                    break
                length = int(event["queue_length"])
        ax.plot(times, lengths, linewidth=1, label=f"{s['robots']} equivalents")
    ax.set(xlabel="Measurement time (s)", ylabel="Waiting messages", title="Repeat 1, queue state sampled every 100 ms")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.2)
    save(fig, "appendix_queue_trace")

    rows, per_class = [], []
    for n, g in zip(loads, groups):
        avg = lambda key: st.mean(s[key] for s in g)
        latency = lambda metric: st.mean(s["classes"]["all"][metric] for s in g)
        generated = sum(s["generated"] for s in g)
        row = {"robot_equivalents": n, "actual_ros_robots": g[0]["actual_ros_robots"], "repeats": len(g),
               "offered_rps": avg("offered_rps"), "completed_rps": avg("completed_rps"),
               "p50_ms": latency("end_to_end_p50_ms"), "p95_ms": latency("end_to_end_p95_ms"),
               "p99_ms": latency("end_to_end_p99_ms"), "p95_min_ms": min(s["classes"]["all"]["end_to_end_p95_ms"] for s in g),
               "p95_max_ms": max(s["classes"]["all"]["end_to_end_p95_ms"] for s in g),
               "mean_queue_length": avg("mean_queue_length"), "payload_goodput_mbps": avg("payload_goodput_mbps"),
               "drop_percent": 100*sum(s["queue_drops"] for s in g)/generated,
               "server_cpu_percent": avg("server_cpu_percent"), "gateway_cpu_percent": avg("gateway_cpu_percent"),
               "source_cpu_percent": avg("source_cpu_percent"), "sender_busy_percent": avg("sender_busy_percent"),
               "server_handler_busy_percent": avg("server_handler_busy_percent"),
               "queue_wait_mean_ms": latency("queue_wait_mean_ms"), "http_mean_ms": latency("http_mean_ms"),
               "ros_to_gateway_mean_ms": latency("ros_to_gateway_mean_ms")}
        rows.append(row)
        for kind in KINDS:
            deadlines = [s["deadlines"][kind] for s in g]
            per_class.append({"robot_equivalents": n, "class": kind,
                              "p95_ms": st.mean(s["classes"][kind]["end_to_end_p95_ms"] for s in g),
                              "p99_ms": st.mean(s["classes"][kind]["end_to_end_p99_ms"] for s in g),
                              "deadline_ms": deadlines[0]["deadline_ms"],
                              "generated": sum(d["generated"] for d in deadlines),
                              "deadline_miss_percent": 100*sum(d["misses"] for d in deadlines)/sum(d["generated"] for d in deadlines),
                              "late_successful": sum(d["late_successful"] for d in deadlines),
                              "not_completed": sum(d["not_completed"] for d in deadlines)})
    write_csv(root/"summary_table.csv", rows)
    write_csv(root/"class_deadlines.csv", per_class)
    low, reference, stress = rows[0], max((r for r in rows if r["drop_percent"] == 0), key=lambda r:r["robot_equivalents"]), rows[-1]
    stressed_runs = groups[-1]
    run_rows = []
    for s in summaries:
        run_rows.append({"run": s["run"], "equivalents": s["robots"], "repeat": s["repeat"],
                         "offered_rps": s["offered_rps"], "completed_rps": s["completed_rps"],
                         "p95_ms": s["classes"]["all"]["end_to_end_p95_ms"],
                         "mean_queue_length": s["mean_queue_length"],
                         "drop_percent": 100*s["queue_drops"]/s["generated"],
                         "source_cpu_percent": s["source_cpu_percent"],
                         "gateway_cpu_percent": s["gateway_cpu_percent"],
                         "server_cpu_percent": s["server_cpu_percent"]})
    write_csv(root/"individual_runs.csv", run_rows)
    stress_command = next(r for r in per_class if r["robot_equivalents"] == stress["robot_equivalents"] and r["class"] == "command")
    generated = sum(s["generated"] for s in summaries)
    dropped = sum(s["queue_drops"] for s in summaries)
    ros_missing = sum(s["ros_missing"] for s in summaries)
    errors = sum(s["request_errors"] for s in summaries)
    pending = sum(s["pending_after_drain"] for s in summaries)
    paragraphs = {
        "Implementation and experimental method":
            "A reproducible ROS2-to-REST baseline was implemented using a dedicated ROS2 traffic package, "
            "a bounded FIFO gateway and a separate HTTP/1.1 REST server. The traffic comprised command, status, "
            "odometry and sensor messages, with configurable rates and payload sizes. One or two namespaced ROS2 "
            "robots published on separate class-specific topics; higher offered loads were generated by scaling "
            "the rates of the two robots while preserving the traffic proportions. Thus, the load labels denote "
            "robot-equivalents, not independent HTTP clients. The gateway used one persistent TCP connection and "
            "one outstanding request, with a capacity of 1,000 waiting messages and drop-tail overflow. Seven "
            "nominal loads from 32 to 2,048 messages/s were tested three times each, using a five-second warm-up and "
            "60-second measurement window. Application retries and injected network faults were disabled. "
            "Reported latency values are averages of per-run statistics; repetitions are not pooled.",
        "Instrumentation and validation":
            "Messages were traced using unique IDs, sequence numbers and a common host monotonic clock. "
            "Timestamps separated ROS2-to-gateway delay, FIFO waiting and the HTTP exchange. Queue occupancy, "
            "process CPU consumption, sender/handler busy fractions and deadline outcomes were also logged. "
            f"The formal sweep accounted for {generated:,} generated messages, including {dropped:,} FIFO "
            f"drops, with {ros_missing:,} missing ROS2 messages, {errors:,} HTTP request errors and {pending:,} "
            "messages pending after the drain period. A separate five-minute two-robot test verified topic rates "
            "and persistent communication, with a packet capture covering every logged HTTP exchange. Configurations and source snapshots "
            "were saved so the results can be reproduced.",
        "Baseline scaling results":
            f"At the lowest load, the mean of the run-level p95 latencies was {low['p95_ms']:.2f} ms. "
            f"At {reference['offered_rps']:.0f} messages/s ({reference['robot_equivalents']} robot-equivalents), "
            f"completed throughput was {reference['completed_rps']:.1f} requests/s and mean run-level p95 "
            f"latency was {reference['p95_ms']:.2f} ms. Increasing the offered load to {stress['offered_rps']:.0f} "
            f"messages/s ({stress['robot_equivalents']} equivalents) produced a completed throughput of "
            f"{stress['completed_rps']:.1f} requests/s, a mean run-level p95 latency of {stress['p95_ms']:.2f} ms and a "
            f"time-averaged queue occupancy of {stress['mean_queue_length']:.1f} messages. At this load, "
            f"{stress['drop_percent']:.2f}% of generated messages were discarded at FIFO admission. "
            "The rising delay and queue occupancy, together with throughput falling below offered load, "
            "provide evidence of overload in the measured architecture.",
        "Interpreting the bottleneck":
            f"At the highest load, the gateway sender was occupied for {stress['sender_busy_percent']:.1f}% "
            f"of the measurement interval, while the server POST handler was occupied for "
            f"{stress['server_handler_busy_percent']:.1f}%. Gateway and server CPU consumption averaged "
            f"{stress['gateway_cpu_percent']:.1f}% and {stress['server_cpu_percent']:.1f}% of one logical CPU, "
            f"respectively. Averaging each run's mean delays gave {stress['queue_wait_mean_ms']:.2f} ms of queue "
            f"waiting and {stress['http_mean_ms']:.2f} ms for the HTTP exchange. These measurements implicate the serial "
            "gateway/client service path and its queue; they do not establish that the REST server or a physical "
            "network link exhausted its capacity. Consequently, adding server workers alone cannot be assumed "
            "to resolve this bottleneck.",
        "Deadline performance":
            "Illustrative project deadline targets were fixed before the final sweep: 100 ms for command, "
            "200 ms for status, 100 ms for odometry and 1,000 ms for sensor messages. A deadline failure included "
            "a late response or any message that did not complete, including FIFO overflow. At the highest "
            f"load, command p95 latency reached {stress_command['p95_ms']:.2f} ms and "
            f"{stress_command['deadline_miss_percent']:.2f}% of generated commands missed their target. "
            "These results show the extent to which FIFO fails to protect critical traffic under this stress "
            "condition. The deadline values are experimental design choices, not safety requirements.",
        "Repeat variability":
            f"At the highest load, completed throughput ranged from {min(s['completed_rps'] for s in stressed_runs):.1f} "
            f"to {max(s['completed_rps'] for s in stressed_runs):.1f} requests/s across the three repetitions, "
            f"while p95 latency ranged from {min(s['classes']['all']['end_to_end_p95_ms'] for s in stressed_runs):.2f} "
            f"to {max(s['classes']['all']['end_to_end_p95_ms'] for s in stressed_runs):.2f} ms. CPU time per "
            "message increased across multiple processes later in the sweep, with a marked change during "
            "the first 32-equivalent run. This indicates variation in shared-host execution performance; "
            "the measurements do not establish its cause. All runs were retained. The results support "
            f"a no-overflow reference at {reference['robot_equivalents']} equivalents and an overload stress "
            f"condition at {stress['robot_equivalents']} equivalents, but do not support a single stable "
            "service-capacity estimate.",
        "Limitations and scope":
            "All processes ran on a shared Ubuntu WSL2 host using loopback. The results therefore describe "
            "this implementation and machine, rather than a physical distributed-robot deployment. Periodic "
            "rate-scaled traffic was used, so an M/M/1 model is not assumed. Latency statistics include only "
            "successful messages generated during measurement, including completions during the bounded drain; "
            "deadline-failure ratios include unsuccessful messages. Throughput counts completions inside the "
            "measurement window. Figure points are means of three run-level statistics and error bars show "
            "their minimum and maximum, not confidence intervals. Sparse command samples at low load limit "
            "p99 precision. The earlier single-topic baseline is retained as preliminary data and is not "
            "pooled with this final dataset."
    }
    blurbs = ["# E1 report-ready text", "", "Use the paragraphs below with the figures and definitions supplied here. "
              "They describe the completed E1 dataset only.", ""]
    for title, text in paragraphs.items():
        blurbs += [f"## {title}", "", text, ""]
    (root/"report_blurbs.md").write_text("\n".join(blurbs), encoding="utf-8")
    mixed_load_caption = (
        "The bottom axis is robot-equivalent load N; the top axis is nominal total mixed traffic, "
        "32N messages/s, across all four classes. Each coloured curve reports one class under the "
        "same mixed workload. At N=1, command, status, odometry and sensor rates are 2, 5, 20 and "
        "5 messages/s; all rates scale by N. ")
    repeat_caption = "Points are means of three run-level statistics; error bars show minimum-maximum. "
    main_figures = [
        ("fig1_latency", "Successful-message latency",
         "Per-class p95 source-to-response latency for a FIFO HTTP/1.1 gateway. "
         + mixed_load_caption + "Latency includes successful messages only. "
         + repeat_caption + "Load and latency use logarithmic scales.",
         "Supports the scaling result; read alongside deadline failures and drops."),
        ("fig2_deadlines", "Deadline failures",
         "Per-class deadline-failure ratios, including late and unsuccessful messages. "
         + mixed_load_caption + "Targets are command 100 ms, status 200 ms, odometry 100 ms "
         "and sensor 1,000 ms. " + repeat_caption + "Load uses a logarithmic scale.",
         "Shows whether each class meets its target. Account for different target values when comparing classes."),
        ("fig3_queue", "Queue occupancy",
         "Time-averaged FIFO occupancy versus total mixed-traffic load across all four classes. "
         "The waiting queue holds 1,000 messages and excludes the in-service request. "
         + repeat_caption + "Load and mean occupancy use logarithmic scales.",
         "Shows queue accumulation and the bounded capacity under overload."),
        ("fig4_throughput", "Completed throughput",
         "Completed request throughput versus total mixed-traffic load across all four classes. "
         "The reference line denotes completed throughput equal to offered load. "
         + repeat_caption + "Load uses a logarithmic scale.",
         "Shows where completions fall behind the offered traffic."),
        ("fig5_cpu", "Process CPU consumption",
         "Process CPU consumption versus total mixed-traffic load across all four classes. "
         "CPU use is normalised to one logical CPU, so multithreaded processes can exceed 100%. "
         + repeat_caption,
         "Compares source, gateway and server CPU demand; CPU use differs from occupied wall time."),
        ("fig6_busy_time", "Sender and server activity",
         "Gateway-sender and server-handler occupied fractions versus total mixed-traffic load. "
         "Sender activity covers dequeue to response completion; handler activity covers POST-handler "
         "entry to response write completion. " + repeat_caption,
         "Supports the bottleneck interpretation. These measures are not inferred M/M/1 utilisation parameters."),
    ]
    guide = ["# Which figures to include", "",
             "Every PDF, PNG and SVG now contains one plot. There are six main plots and five appendix plots; "
             "the previous multi-panel versions are archived in `figures/archive_combined/`. "
             "Use vector PDFs in Overleaf or the supplied 300-dpi PNGs elsewhere.", "",
             "Include the six main plots below as space permits, together with the small latency/throughput table. "
             "The completed measurements and error-bar calculations are unchanged.", ""]
    for number, (name, title, caption, support) in enumerate(main_figures, 1):
        guide += [f"## Figure {number} - {title}", "",
                  f"![{title}](figures/{name}.png)", "",
                  f"[PDF](figures/{name}.pdf) | [PNG](figures/{name}.png) | [SVG](figures/{name}.svg)", "",
                  "**Caption:** " + caption, "", "**Use it to support:** " + support, ""]
    appendix_figures = [
        ("appendix_goodput", "Application payload goodput",
         "Delivered application payload goodput versus total mixed-traffic load. The dashed line shows "
         "nominal offered payload rate. " + repeat_caption),
        ("appendix_drops", "FIFO drops",
         "The fraction of generated messages dropped at FIFO admission versus total mixed-traffic load. "
         + repeat_caption + "Unlike the pooled summary-table percentage, each point averages run-level fractions."),
        ("appendix_queue_trace", "Queue traces",
         "Waiting-queue occupancy sampled every 100 ms during repeat 1 of the three highest load conditions. "
         "This is a fixed repeat selection, not a best-run selection."),
        ("appendix_within_run_cpu", "CPU variation within a run",
         "Process CPU consumption in consecutive 10-second bins of the first 32-equivalent repetition. "
         "CPU is normalised to one logical CPU; lines connect bin averages, not instantaneous samples."),
        ("appendix_within_run_queue", "Queue variation within a run",
         "Time-averaged waiting-queue occupancy in consecutive 10-second bins of the first 32-equivalent "
         "repetition. The dashed line marks the 1,000-message capacity."),
    ]
    guide += ["## Optional appendix plots", ""]
    for name, title, caption in appendix_figures:
        guide += [f"### {title}", "",
                  f"[PDF](figures/{name}.pdf) | [PNG](figures/{name}.png) | [SVG](figures/{name}.svg)", "",
                  "**Caption:** " + caption, ""]
    guide += ["`individual_runs.csv` lists all 21 run-level results. `variability_note.md` explains the "
              "large high-load spread and the within-run CPU/queue changes without assuming a cause.", "",
              "The five-minute packet capture supplies separate protocol/overhead evidence. Do not mix "
              "the old `results/baseline_formal/baseline.png` into the completed dataset: its workload "
              "implementation and instrumentation differ."]
    (root/"figure_guide.md").write_text("\n".join(guide)+"\n", encoding="utf-8")
    table = ["# Completed E1 results", "", "[Report-ready paragraphs](report_blurbs.md) · [Figure selection and captions](figure_guide.md)", "",
             "Percentiles below are means of per-run percentiles, not percentiles of a pooled dataset. "
             "Drops are pooled over generated messages at each load.", "",
             "| Load equivalents | Offered/s | Completed/s | p50 ms | p95 ms | p99 ms | Mean queue | Goodput Mbit/s | Drops % | Server CPU % |",
             "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        table.append(f"| {r['robot_equivalents']} | {r['offered_rps']:.1f} | {r['completed_rps']:.1f} | "
                     f"{r['p50_ms']:.2f} | {r['p95_ms']:.2f} | {r['p99_ms']:.2f} | {r['mean_queue_length']:.2f} | "
                     f"{r['payload_goodput_mbps']:.2f} | {r['drop_percent']:.2f} | {r['server_cpu_percent']:.1f} |")
    table += ["", "CPU percentages are relative to one logical CPU. Source rates are scaled across at most "
              "two namespaced robots. Full statistics are in `summary_table.csv` and per-class deadline results "
              "are in `class_deadlines.csv`. All repetitions are listed in `individual_runs.csv`; the large "
              "spread at high loads must be discussed, not hidden by the means.", "", "## Recommended figures", ""]
    for name, title, _, _ in main_figures:
        table += [f"![{title}](figures/{name}.png)", ""]
    (root/"report.md").write_text("\n".join(table), encoding="utf-8")
    print(root/"report.md")


if __name__ == "__main__":
    main()
