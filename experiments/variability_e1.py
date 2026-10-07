"""Derive a variability appendix from existing E1 logs; no experiment is run."""
import argparse
import bisect
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from metrics import cpu_percent


def queue_bins(path, boundaries):
    """Integrate a piecewise-constant queue, clipping spans at bin boundaries."""
    areas = [0] * (len(boundaries) - 1)
    begin, end = boundaries[0], boundaries[-1]
    previous, length = begin, 0

    def add_span(a, b, value):
        a, b = max(a, begin), min(b, end)
        while a < b:
            index = bisect.bisect_right(boundaries, a) - 1
            stop = min(b, boundaries[index + 1])
            areas[index] += value * (stop - a)
            a = stop

    last_event = None
    with path.open(newline="") as f:
        for event in csv.DictReader(f):
            timestamp = int(event["timestamp_ns"])
            if last_event is not None and timestamp < last_event:
                raise ValueError("Queue events are not chronological")
            last_event = timestamp
            if timestamp <= begin:
                length = int(event["queue_length"])
                continue
            add_span(previous, timestamp, length)
            previous = min(timestamp, end)
            if timestamp >= end:
                break
            length = int(event["queue_length"])
    add_span(previous, end, length)
    return [area / (b - a) for area, a, b in zip(areas, boundaries, boundaries[1:])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    root = args.results
    if not json.loads((root / "validation.json").read_text())["all_passed"]:
        raise ValueError("The formal dataset must pass validation first")
    summaries = [json.loads(p.read_text()) for p in sorted(root.glob("*/summary.json"))]
    selected = [s for s in summaries if s["robots"] == 32 and s["repeat"] == 1]
    if len(selected) != 1:
        raise ValueError("Expected exactly one 32-equivalent, repeat-1 run")
    summary = selected[0]
    folder = root / summary["run"]
    config = json.loads((folder / "config.json").read_text())
    begin, end = config["measurement_start_ns"], config["measurement_end_ns"]
    boundaries = list(range(begin, end, 10_000_000_000)) + [end]
    with (folder / "cpu.csv").open(newline="") as f:
        samples = list(csv.DictReader(f))
    means = queue_bins(folder / "queue.csv", boundaries)
    rows = []
    for a, b, mean_queue in zip(boundaries, boundaries[1:], means):
        row = {"run": summary["run"], "start_s": (a - begin) / 1e9,
               "end_s": (b - begin) / 1e9, "mean_queue_length": mean_queue}
        row.update({name + "_cpu_percent": cpu_percent(samples, name + "_cpu_s", a, b)
                    for name in ("source", "gateway", "server")})
        rows.append(row)
    # Bin integration must recover the already validated whole-window statistics.
    for key in ("mean_queue_length", "source_cpu_percent", "gateway_cpu_percent", "server_cpu_percent"):
        weighted = sum(r[key] * (r["end_s"] - r["start_s"]) for r in rows) / ((end - begin) / 1e9)
        if abs(weighted - summary[key]) > 1e-7:
            raise ValueError(f"Binned {key} does not reproduce the run summary")
    figures = root / "figures"
    figures.mkdir(exist_ok=True)
    stem = figures / "appendix_within_run_variability"
    with stem.with_suffix(".csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    plt.rcParams.update({"font.size": 9, "axes.spines.top": False, "axes.spines.right": False,
                         "savefig.dpi": 300, "pdf.fonttype": 42, "svg.fonttype": "none"})
    cpu_fig, cpu_ax = plt.subplots(figsize=(5.0, 3.6))
    queue_fig, queue_ax = plt.subplots(figsize=(5.0, 3.6))
    centres = [(r["start_s"] + r["end_s"]) / 2 for r in rows]
    for name, color, marker in zip(("source", "gateway", "server"),
                                    ("#009E73", "#0072B2", "#E69F00"), ("^", "o", "s")):
        cpu_ax.plot(centres, [r[name + "_cpu_percent"] for r in rows], marker=marker,
                     markersize=4, linewidth=1.3, color=color, label=name)
    cpu_ax.set(title="Process CPU in each 10-second bin", ylabel="CPU use (% of one logical CPU)", ylim=(0, None))
    cpu_ax.axhline(100, linestyle=":", color="0.6", linewidth=1)
    cpu_ax.legend(fontsize=8)
    queue_ax.bar(centres, means, width=[r["end_s"] - r["start_s"] for r in rows],
                color="#0072B2", alpha=0.8, edgecolor="white")
    queue_ax.axhline(config["queue_capacity"], linestyle="--", color="0.4", linewidth=1,
                    label=f"Capacity: {config['queue_capacity']:,}")
    queue_ax.set(title="Mean queue in each 10-second bin", ylabel="Time-averaged waiting messages", ylim=(0, 1100))
    queue_ax.legend(fontsize=8)
    for ax in (cpu_ax, queue_ax):
        ax.set(xlabel="Measurement time (s)", xlim=(0, (end - begin) / 1e9))
        ax.set_xticks([(t - begin) / 1e9 for t in boundaries])
        ax.grid(alpha=0.2, axis="y")
    for fig, name in ((cpu_fig, "appendix_within_run_cpu"),
                      (queue_fig, "appendix_within_run_queue")):
        fig.tight_layout()
        for suffix in ("png", "pdf", "svg"):
            fig.savefig((figures / name).with_suffix("." + suffix), bbox_inches="tight")
        plt.close(fig)

    stress = sorted((s for s in summaries if s["robots"] == 64), key=lambda s: s["repeat"])
    if [s["repeat"] for s in stress] != [1, 2, 3]:
        raise ValueError("Expected all three 64-equivalent repetitions")
    note = ["# E1 variability appendix", "",
            "This appendix is an offline analysis of the existing formal dataset. No new performance runs were made.", "",
            f"The within-run figures use `{summary['run']}`: 32 robot-equivalents, repeat 1. This fixed selection "
            "makes the change during that run visible; it is not presented as representative of every repetition.", "",
            "![Within-run process CPU](figures/appendix_within_run_cpu.png)", "",
            "**CPU caption:** Process CPU consumption in consecutive 10-second measurement bins of the "
            "32-equivalent, repeat-1 run. CPU is normalised to one logical CPU "
            "and can exceed 100% for a multithreaded process. Cumulative CPU counters are interpolated at bin "
            "boundaries. Lines connect bin averages, not instantaneous CPU samples.", "",
            "![Within-run waiting queue](figures/appendix_within_run_queue.png)", "",
            "**Queue caption:** Time-averaged waiting-queue occupancy in consecutive 10-second measurement bins "
            "of the same 32-equivalent, repeat-1 run. Queue-state intervals are clipped and integrated at bin "
            "boundaries. The dashed line marks the 1,000-message waiting-queue capacity.", "",
            "| Interval (s) | Source CPU % | Gateway CPU % | Server CPU % | Mean waiting queue |",
            "|---:|---:|---:|---:|---:|"]
    for r in rows:
        note.append(f"| {r['start_s']:.0f}-{r['end_s']:.0f} | {r['source_cpu_percent']:.1f} | "
                    f"{r['gateway_cpu_percent']:.1f} | {r['server_cpu_percent']:.1f} | {r['mean_queue_length']:.2f} |")
    note += ["", "**Report-ready observation:** In the 32-equivalent, repeat-1 run, process CPU consumption and "
             "queue occupancy changed during the measurement window despite a constant configured offered load. "
             "The later part of the run consumed more source and gateway CPU time and accumulated a large waiting "
             "queue. This demonstrates that a single whole-run average hides a change in behaviour. These logs "
             "do not establish the cause; they cannot distinguish host conditions, scheduling, runtime effects "
             "or other implementation effects.", "", "## Spread across all 64-equivalent repetitions", "",
             "| Repeat | Offered/s | Completed/s | p95 latency (ms) | FIFO drops % | Mean waiting queue |",
             "|---:|---:|---:|---:|---:|---:|"]
    for s in stress:
        note.append(f"| {s['repeat']} | {s['offered_rps']:.2f} | {s['completed_rps']:.2f} | "
                    f"{s['classes']['all']['end_to_end_p95_ms']:.2f} | {100*s['queue_drops']/s['generated']:.2f} | "
                    f"{s['mean_queue_length']:.2f} |")
    note += ["", "All three stress repetitions are included. Repeat numbers identify seeded repetitions; the "
             "sweep order was shuffled, so they are not chronological. p95 latency includes successful messages "
             "generated in the measurement window, including bounded-drain completions. Throughput counts "
             "completions inside the window, and the FIFO-drop denominator is generated messages in that window. "
             "All three runs experienced overflow, but their performance differs substantially. Report the "
             "range alongside the mean and avoid presenting the mean as a stable service-capacity estimate.", "",
             "## Reproduce and verify", "", "From the project root:", "", "```bash",
             "python experiments/variability_e1.py results/e1_complete", "```", "",
             "Inputs are the selected run's `config.json`, `cpu.csv` and `queue.csv`, the formal run summaries, "
             "and `validation.json`. The script checks that its time-weighted bin averages recover the validated "
             "whole-window CPU and queue statistics within 1e-7. It changes only this note and its own appendix "
             "CSV/PNG/PDF/SVG outputs.", "",
             f"Selected run configuration SHA-256: `{config['config_sha256']}`.", "",
             "[Bin values (CSV)](figures/appendix_within_run_variability.csv) | "
             "[CPU PDF](figures/appendix_within_run_cpu.pdf) | "
             "[CPU SVG](figures/appendix_within_run_cpu.svg) | "
             "[Queue PDF](figures/appendix_within_run_queue.pdf) | "
             "[Queue SVG](figures/appendix_within_run_queue.svg)", ""]
    (root / "variability_note.md").write_text("\n".join(note), encoding="utf-8")
    print(f"Wrote {stem.with_suffix('.csv')}, separate CPU/queue figures, and "
          f"{root / 'variability_note.md'}; all bin/summary checks passed.")


if __name__ == "__main__":
    main()
