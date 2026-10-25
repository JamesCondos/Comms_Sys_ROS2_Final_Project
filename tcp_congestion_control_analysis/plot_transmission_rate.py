"""Plot the measured WAN transmission rate for a single-robot TCP sweep."""

import argparse
import csv
import json
import math
from pathlib import Path

from plot_results import review_sweep, read_log


def transmission_rates(samples, start, end):
    # Divide traffic into one-second intervals, including the last partial second.
    count = math.ceil((end - start) / 1e9)
    sent = [0.0] * count
    coverage = [0.0] * count
    previous = {}

    # Subtract consecutive counters from the same socket, never across connections.
    for row in sorted(samples, key=lambda item: item["time_ns"]):
        if row["event"] != "tcp_sample" or "bytes_sent" not in row:
            continue
        key = row["connection_id"]
        before = previous.get(key)
        previous[key] = row
        if before is None:
            continue
        left, right = before["time_ns"], row["time_ns"]
        difference = row["bytes_sent"] - before["bytes_sent"]
        if right <= left or difference < 0:
            continue

        # Share each sampled byte increase across the time interval it covers.
        first = max(0, (left - start) // 10**9)
        last = min(count - 1, (right - start) // 10**9)
        for index in range(first, last + 1):
            overlap = max(0, min(right, start + (index + 1) * 10**9, end)
                          - max(left, start + index * 10**9))
            sent[index] += difference * overlap / (right - left)
            coverage[index] += overlap / 1e9

    # Convert bytes per interval into megabits per second.
    rows = []
    for index in range(count):
        width = min(1, (end - start) / 1e9 - index)
        rows.append(dict(time_s=index + width / 2,
            transmission_mbit_s=sent[index] * 8 / width / 1e6 if coverage[index] else None,
            sampled_seconds=coverage[index]))
    return rows


def generate_rate_graph(sweep):
    sweep = Path(sweep).resolve()
    review_sweep(sweep)

    # Draw measured data using the same colours as the example: Reno, CUBIC, BBR.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colours = {"reno": "blue", "cubic": "red", "bbr": "green"}
    figure, axes = plt.subplots(figsize=(10, 5))
    saved = []
    plan = json.loads((sweep / "plan.json").read_text())
    for run in plan["runs"]:
        folder = sweep / run["run"]
        config = json.loads((folder / "resolved.json").read_text())
        if len(config["robots"]) != 1 or config["edge_concurrency"] != 1:
            raise ValueError("This graph requires --sources 1 and one edge connection")
        timing = json.loads((folder / "start.json").read_text())
        samples = read_log(folder / "logs" / "edgeA" / "connections.jsonl")
        if not any("bytes_sent" in row for row in samples):
            raise ValueError("This run has no TCP sent-byte counters; run a new trial")
        rows = transmission_rates(samples, timing["start_ns"], timing["measurement_end_ns"])
        saved.extend(dict(algorithm=run["algorithm"], run=run["run"], **row) for row in rows)
        label = f"{run['algorithm'].upper()} (load ×{run['load']:g}, loss {run['loss_percent']:g}%, repeat {run['repeat']})"
        axes.plot([row["time_s"] for row in rows],
                  [row["transmission_mbit_s"] if row["transmission_mbit_s"] is not None else math.nan for row in rows],
                  color=colours[run["algorithm"]], label=label)

    # Save both the graph and its underlying measurements inside the TCP results.
    axes.set(xlabel="Time since traffic started (s)", ylabel="TCP payload transmission rate (Mbit/s)",
             title="One robot: edge A → central proxy", ylim=(0, None))
    axes.set_xlim(left=0)
    axes.grid(alpha=0.25)
    axes.legend(fontsize=8)
    figure.tight_layout()
    output = sweep / "analysis"
    output.mkdir(exist_ok=True)
    figure.savefig(output / "transmission_rate.png", dpi=180)
    plt.close(figure)
    with (output / "transmission_rate.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(saved[0]))
        writer.writeheader()
        writer.writerows(saved)
    return output / "transmission_rate.png"


if __name__ == "__main__":
    # Read an existing completed sweep without starting another experiment.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sweep", type=Path)
    print(generate_rate_graph(parser.parse_args().sweep))
