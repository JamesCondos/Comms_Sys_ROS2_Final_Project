"""Build the E2 report only from completed, independently validated datasets.

No experiment runs or statistical selection occur here. Every plotted point is
the mean of run-level statistics, with minimum-to-maximum bars across runs.
The M/M/1 simulation and empirical ROS/HTTP measurements are kept separate.
"""
import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


PATTERNS = ("periodic_staggered", "periodic_synchronized", "poisson", "bursty_onoff_poisson")
LABELS = {"periodic_staggered": "Staggered", "periodic_synchronized": "Synchronized",
          "poisson": "Poisson", "bursty_onoff_poisson": "Bursty ON/OFF"}
COLORS = ("#0072B2", "#D55E00", "#009E73", "#CC79A7")
INPUTS = {}


def record(path):
    path = Path(path)
    INPUTS[str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest()
    return path


def read_json(path):
    return json.loads(record(path).read_text(encoding="utf-8-sig"))


def read_csv(path):
    with record(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_tex(path, value):
    controls = sorted({ord(c) for c in value if ord(c) < 32 and ord(c) != 10})
    if controls:
        raise ValueError(f"Unexpected control characters in LaTeX: {controls}")
    path.write_text(value, encoding="utf-8")


def write_csv(path, rows):
    if not rows:
        raise ValueError(f"No rows for {path}")
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def tex(value):
    replacements = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
                    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}"}
    return "".join(replacements.get(c, c) for c in str(value))


def num(value, digits=3):
    return f"{value:,.{digits}f}" if value is not None else "--"


def avg(rows, key):
    values = [key(row) if callable(key) else row[key] for row in rows]
    if any(value is None or not math.isfinite(value) for value in values):
        raise ValueError(f"Nonfinite report data: {key}")
    return float(np.mean(values))


def grouped(rows, key):
    result = defaultdict(list)
    for row in rows:
        result[row[key]].append(row)
    return dict(result)


def error_values(groups, keys, value, scale=1):
    samples = np.array([[scale * (value(r) if callable(value) else r[value]) for r in groups[k]] for k in keys])
    means = samples.mean(axis=1)
    return means, np.vstack((means - samples.min(axis=1), samples.max(axis=1) - means))


def axes(ylabel, xlabel=None):
    figure, axis = plt.subplots(figsize=(7.0, 3.65), layout="constrained")
    axis.set_ylabel(ylabel)
    if xlabel:
        axis.set_xlabel(xlabel)
    axis.spines[["top", "right"]].set_visible(False)
    axis.grid(axis="y", alpha=.25, linewidth=.6)
    axis.set_axisbelow(True)
    return figure, axis


def save(figure, directory, name):
    for extension in ("pdf", "png"):
        figure.savefig(directory / f"{name}.{extension}", dpi=180, bbox_inches="tight")
    plt.close(figure)


def table(headers, rows, caption, label, spec=None):
    spec = spec or "l" + "r" * (len(headers) - 1)
    lines = [r"\begin{table}[H]\centering", r"\small", r"\setlength{\tabcolsep}{5pt}",
             r"\begin{tabular}{" + spec + "}", r"\hline", " & ".join(headers) + r" \\", r"\hline"]
    lines += [" & ".join(map(str, row)) + r" \\" for row in rows]
    lines += [r"\hline", r"\end{tabular}", r"\caption{" + caption + "}", r"\label{" + label + "}", r"\end{table}"]
    return "\n".join(lines)


def figure(name, caption, width="0.98"):
    return (r"\begin{figure}[H]\centering" + "\n" + r"\includegraphics[width=" + width + r"\linewidth]{figures/" + name + ".pdf}\n"
            + r"\caption{" + caption + "}\n" + r"\label{fig:" + name + "}\n" + r"\end{figure}" + "\n")


def load_datasets(model_path, live_path):
    model_config = read_json(model_path / "config.json")
    model_validation = read_json(model_path / "validation.json")
    if model_validation.get("all_passed") is not True:
        raise ValueError("Model raw-data validation has not passed")
    model = read_json(model_path / "summaries.json")
    expected_model = {(case["rho"], repeat) for case in model_config["cases"] for repeat in range(1, model_config["repeats"] + 1)}
    if {(r["rho_target"], r["repeat"]) for r in model} != expected_model or len(model) != len(expected_model):
        raise ValueError("Incomplete/duplicate model cells")
    for row in model:
        if read_json(model_path / row["run_id"] / "summary.json") != row:
            raise ValueError("Model aggregate summary differs from run summary")
    live_config = read_json(live_path / "sweep_config.json")
    live_validation = read_json(live_path / "validation.json")
    if live_validation.get("all_passed") is not True:
        raise ValueError("Live raw-data validation has not passed")
    capture_validation = read_json(live_path / "capture_validation.json")
    if capture_validation.get("all_passed") is not True:
        raise ValueError("Strict HTTP packet-capture validation has not passed")
    live_validation = dict(live_validation, capture_validation=capture_validation)
    live, folders = [], {}
    for path in sorted(live_path.glob("*/summary.json")):
        if (path.parent / "invalid.json").exists():
            continue
        row = read_json(path)
        validation = read_json(path.parent / "validation.json")
        if validation.get("passed") is not True:
            raise ValueError(f"Run validation missing/failed: {path.parent}")
        folders[row["run"]] = path.parent
        live.append(row)
    expected_live = {(pattern, repeat) for pattern in PATTERNS for repeat in range(1, live_config["repeats"] + 1)}
    if {(r["arrival_pattern"], r["repeat"]) for r in live} != expected_live or len(live) != len(expected_live):
        raise ValueError("Incomplete/duplicate live cells")
    if capture_validation["runs_checked"] != len(live):
        raise ValueError("Capture validation does not cover every live run")
    if model_config["repeats"] != 5 or live_config["repeats"] != 5:
        raise ValueError("Final E2 report requires the preregistered five repetitions")
    return model_config, model_validation, model, live_config, live_validation, live, folders


def make_plots(output, model_path, model, live, folders):
    destination = output / "figures"
    destination.mkdir(exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.labelsize": 11, "legend.fontsize": 9, "pdf.fonttype": 42})
    mg, lg = grouped(model, "rho_target"), grouped(live, "arrival_pattern")
    rhos = sorted(mg)
    mu = model[0]["mu_target_per_s"]
    dense = np.linspace(min(rhos), max(rhos), 300)
    for metric, name, ylabel, scale, theoretical in (
            ("W_s", "model_mean_delay", "Mean system residence W (simulated ms)", 1000, 1000 / (mu * (1 - dense))),
            ("L", "model_occupancy", "Time-average system population L (jobs)", 1, dense / (1 - dense))):
        fig, ax = axes(ylabel, r"Configured utilization $\rho=\lambda/\mu$")
        mean, errors = error_values(mg, rhos, metric, scale)
        ax.plot(dense, theoretical, color="0.35", label="Steady-state M/M/1 prediction")
        ax.errorbar(rhos, mean, yerr=errors, fmt="o", capsize=4, color=COLORS[0], label="Simulation: mean and run range")
        ax.set_xticks(rhos)
        ax.set_ylim(bottom=0)
        ax.legend()
        save(fig, destination, name)

    example = next(r for r in model if r["rho_target"] == max(rhos) and r["repeat"] == 1)
    with np.load(record(model_path / example["run_id"] / "jobs.npz")) as archive:
        mask = (archive["arrival_s"] >= example["window_begin_s"]) & (archive["arrival_s"] < example["window_end_s"])
        arrival = archive["interarrival_s"][mask] * example["lambda_target_per_s"]
        service = archive["service_draw_s"][mask] * example["mu_target_per_s"]
    fig, ax = axes("Cumulative probability", "Duration divided by configured mean")
    grid = np.linspace(0, 7, 501)
    ax.plot(grid, -np.expm1(-grid), color="0.3", linewidth=2.0, label="Unit-mean exponential prediction")
    for index, (values, label) in enumerate(((arrival, "Inter-arrival samples"), (service, "Service samples"))):
        ax.plot(grid, np.searchsorted(np.sort(values), grid, side="right") / len(values),
                color=COLORS[index], linestyle=("--" if index == 0 else ":"), label=label)
    ax.set_ylim(0, 1.02)
    ax.set_xlim(0, 7)
    ax.legend()
    save(fig, destination, "model_exponential_cdf")

    x = np.arange(len(PATTERNS))
    plots = [
        ("live_rates", "Rate (messages/s)", [("Published", "offered_rps"),
           ("Admitted", lambda r: r["queue_system"]["arrival_rate_rps"]),
           ("Completed", lambda r: r["queue_system"]["completed_rps"])]),
        ("live_wait", "FIFO waiting time (ms)", [("Mean waiting time", lambda r: r["queue_system"]["Wq_ms"]["mean"]),
           ("p95 waiting time", lambda r: r["queue_system"]["Wq_ms"]["p95"]),
           ("p99 waiting time", lambda r: r["queue_system"]["Wq_ms"]["p99"])]),
        ("live_system_delay", "Gateway system residence W (ms)", [("Mean residence", lambda r: r["queue_system"]["W_ms"]["mean"]),
           ("p95 residence", lambda r: r["queue_system"]["W_ms"]["p95"]),
           ("p99 residence", lambda r: r["queue_system"]["W_ms"]["p99"])]),
        ("live_occupancy", "Time-average population (messages)", [("System L", lambda r: r["queue_system"]["mean_L"]),
           ("Waiting queue Lq", lambda r: r["queue_system"]["mean_Lq"])])]
    for name, ylabel, series in plots:
        fig, ax = axes(ylabel)
        for index, (label, value) in enumerate(series):
            mean, errors = error_values(lg, PATTERNS, value)
            offset = (index - (len(series) - 1) / 2) * .18
            ax.errorbar(x + offset, mean, yerr=errors, fmt="o", capsize=3,
                        color=COLORS[index], label=label)
        if name == "live_rates":
            ax.axhline(live[0]["nominal_offered_rps"], color="0.45", ls="--", lw=1,
                       label="Configured expected rate")
        ax.set_xticks(x, [LABELS[p] for p in PATTERNS])
        ax.set_ylim(bottom=0)
        if name == "live_rates":
            ax.set_ylim(0, 1.15 * max(r["offered_rps"] for r in live))
        ax.margins(x=.12)
        ax.legend(loc="best")
        save(fig, destination, name)

    arrival_fig, arrival_ax = axes("Published messages per 100 ms bin", "Time since measurement began (s)")
    queue_fig, queue_ax = axes("Messages waiting in FIFO", "Time since measurement began (s)")
    trace_tables = []
    for index, pattern in enumerate(PATTERNS):
        row = next(r for r in live if r["arrival_pattern"] == pattern and r["repeat"] == 1)
        folder = folders[row["run"]]
        config = read_json(folder / "config.json")
        begin = config["measurement_start_ns"]
        generated = read_csv(folder / "generated.csv")
        edges_ns = begin + np.arange(101, dtype=np.int64) * 100_000_000
        stamps = np.array([int(r["source_ns"]) for r in generated], dtype=np.int64)
        counts, _ = np.histogram(stamps[(stamps >= begin) & (stamps < edges_ns[-1])], edges_ns)
        centers = (np.arange(100) + .5) * .1
        arrival_ax.step(centers, counts, where="mid", color=COLORS[index], alpha=.85, label=LABELS[pattern])
        events = read_csv(folder / "queue.csv")
        times = np.array([int(r["timestamp_ns"]) for r in events], dtype=np.int64)
        states = np.array([int(r["queue_length"]) for r in events], dtype=np.int64)
        start_index = np.searchsorted(times, begin, side="right") - 1
        initial = int(states[start_index]) if start_index >= 0 else 0
        use = (times > begin) & (times < edges_ns[-1])
        qt = np.r_[0., (times[use] - begin) / 1e9, 10.]
        qs = np.r_[initial, states[use], states[np.searchsorted(times, edges_ns[-1], side="left") - 1]]
        queue_ax.step(qt, qs, where="post", color=COLORS[index], alpha=.85, linewidth=.9, label=LABELS[pattern])
        trace_tables += [{"pattern": pattern, "repeat": 1, "bin_start_s": round(i / 10, 1),
                          "bin_end_s": round((i + 1) / 10, 1), "source_count": int(count)}
                         for i, count in enumerate(counts)]
    for fig, ax, name in ((arrival_fig, arrival_ax, "live_arrival_trace"), (queue_fig, queue_ax, "live_queue_trace")):
        ax.set_xlim(0, 10)
        ax.set_ylim(bottom=0)
        ax.legend(loc="upper right", ncol=2)
        save(fig, destination, name)
    write_csv(output / "tables" / "example_arrival_bins.csv", trace_tables)


def build_tables(output, model, live):
    destination = output / "tables"
    destination.mkdir(exist_ok=True)
    model_rows = []
    for row in model:
        d, little = row["distribution_diagnostics"], row["little"]
        model_rows.append({k: row[k] for k in ("run_id", "repeat", "rho_target", "duration_s", "offered_per_s", "admitted_per_s",
            "completed_per_s", "offered", "admitted", "drops", "drops_all", "L", "Lq", "W_s", "Wq_s", "service_mean_s",
            "busy_fraction", "p95_s", "p99_s", "max_queue_all", "conservation_residual")})
        model_rows[-1].update(theory_W_s=row["theory"]["W_s"], theory_L=row["theory"]["L"],
            arrival_cv=d["interarrival"]["cv"], service_cv=d["service_draw"]["cv"],
            arrival_ks_d=d["interarrival"]["ks_d_against_configured_exponential"],
            service_ks_d=d["service_draw"]["ks_d_against_configured_exponential"],
            arrival_ks_p=d["interarrival"]["ks_asymptotic_p"], service_ks_p=d["service_draw"]["ks_asymptotic_p"],
            cross_correlation=d["same_job_arrival_service_correlation"],
            lambda_W=little["lambda_W"], boundary_correction=little["system_boundary_correction"],
            identity_residual=little["system_identity_residual"], queue_identity_residual=little["queue_identity_residual"])
    write_csv(destination / "model_individual_runs.csv", model_rows)
    live_rows, class_rows, source_rows = [], [], []
    for row in live:
        q, jitter = row["queue_system"], row["source_jitter"]
        live_rows.append({k: row[k] for k in ("run", "repeat", "arrival_pattern", "actual_sources", "duration_s", "generated",
            "gateway_received", "successful", "offered_rps", "completed_rps", "queue_drops", "request_errors", "ros_missing",
            "pending_after_drain", "source_cpu_percent", "gateway_cpu_percent", "server_cpu_percent", "server_connections")})
        live_rows[-1].update(admitted_rps=q["arrival_rate_rps"], system_completed_rps=q["completed_rps"],
            mean_L=q["mean_L"], mean_Lq=q["mean_Lq"], busy_fraction=q["busy_fraction"],
            W_mean_ms=q["W_ms"]["mean"], W_p95_ms=q["W_ms"]["p95"], W_p99_ms=q["W_ms"]["p99"],
            Wq_mean_ms=q["Wq_ms"]["mean"], Wq_p95_ms=q["Wq_ms"]["p95"], Wq_p99_ms=q["Wq_ms"]["p99"],
            S_mean_ms=q["S_ms"]["mean"], S_cv=math.sqrt(q["S_ms"]["cv2"]),
            source_schedule_lag_p95_ms=jitter["schedule_to_source_ms"]["p95"],
            gateway_iat_cv=math.sqrt(row["arrival_variability"]["gateway"]["interarrival_ms"]["cv2"]),
            lambda_W=q["little_system"]["uncorrected_lambda_W"], boundary_correction=q["little_system"]["boundary_correction"],
            system_identity_residual_ns=q["little_system"]["identity_residual_ns"],
            event_system_residual_ns=q["event_vs_residence_system_residual_ns"],
            queue_identity_residual_ns=q["little_queue"]["identity_residual_ns"],
            event_queue_residual_ns=q["event_vs_residence_queue_residual_ns"])
        for kind, deadline in row["deadlines"].items():
            class_rows.append({"run": row["run"], "pattern": row["arrival_pattern"], "repeat": row["repeat"], "class": kind,
                               **deadline, "e2e_p95_ms": row["classes"][kind]["end_to_end_p95_ms"],
                               "e2e_p99_ms": row["classes"][kind]["end_to_end_p99_ms"]})
        for rate in row["source_rates"]:
            source_rows.append({"run": row["run"], "pattern": row["arrival_pattern"], "repeat": row["repeat"], **rate})
    write_csv(destination / "live_individual_runs.csv", live_rows)
    write_csv(destination / "live_class_results.csv", class_rows)
    write_csv(destination / "live_source_rates.csv", source_rows)
    aggregates = []
    for part, rows, group_key, metrics in (("E2A", model_rows, "rho_target", ["W_s", "Wq_s", "L", "Lq", "busy_fraction", "p95_s", "p99_s"]),
                                           ("E2B", live_rows, "arrival_pattern", ["offered_rps", "admitted_rps", "mean_L", "mean_Lq", "W_mean_ms", "W_p95_ms", "W_p99_ms", "Wq_mean_ms", "S_mean_ms", "busy_fraction"])):
        for group, values in grouped(rows, group_key).items():
            for metric in metrics:
                samples = [r[metric] for r in values]
                aggregates.append({"part": part, "condition": group, "metric": metric, "runs": len(values),
                                   "mean": np.mean(samples), "min": min(samples), "max": max(samples)})
    write_csv(destination / "aggregate_statistics.csv", aggregates)
    return model_rows, live_rows


def report_text(model_config, model_validation, model, live_config, live_validation, live, model_rows, live_rows):
    mg, lg = grouped(model, "rho_target"), grouped(live, "arrival_pattern")
    rhos = sorted(mg)
    mean = lambda pattern, key: avg(lg[pattern], key)
    qmean = lambda pattern, key: mean(pattern, lambda r: r["queue_system"][key])
    qstat = lambda pattern, key, stat="mean": mean(pattern, lambda r: r["queue_system"][key][stat])
    n_model, n_live = len(model), len(live)
    model_total = sum(r["offered"] for r in model)
    live_total = sum(r["generated"] for r in live)
    model_drops = sum(r["drops_all"] for r in model)
    live_drops = sum(r["queue_drops"] for r in live)
    live_errors = sum(r["request_errors"] + r["ros_missing"] + r["pending_after_drain"] for r in live)
    minimum_pattern = min(PATTERNS, key=lambda p: qstat(p, "W_ms", "p95"))
    maximum_pattern = max(PATTERNS, key=lambda p: qstat(p, "W_ms", "p95"))
    model_errors = [abs(avg(mg[rho], "W_s") / mg[rho][0]["theory"]["W_s"] - 1) * 100 for rho in rhos]
    model_residual = max(abs(r["little"]["system_identity_residual"]) for r in model)
    live_residual = max(abs(r["queue_system"]["event_vs_residence_system_residual_ns"]) for r in live)
    ks_rejections = sum(r["distribution_diagnostics"][field]["ks_asymptotic_p"] < .05 for r in model for field in ("interarrival", "service_draw"))
    all_arrival_cv = [r["distribution_diagnostics"]["interarrival"]["cv"] for r in model]
    all_service_cv = [r["distribution_diagnostics"]["service_draw"]["cv"] for r in model]
    all_cross = [abs(r["distribution_diagnostics"]["same_job_arrival_service_correlation"]) for r in model]
    rate_range = (min(qmean(p, "arrival_rate_rps") for p in PATTERNS), max(qmean(p, "arrival_rate_rps") for p in PATTERNS))
    service_range = (min(qstat(p, "S_ms") for p in PATTERNS), max(qstat(p, "S_ms") for p in PATTERNS))

    text = [r"\section{E2: Queueing Relationships and Arrival Variability}",
        r"\subsection{Question and experimental scope}",
        "E2 asks whether queueing relationships explain the observed delay, and whether changing the timing of arrivals changes delay at the same expected average workload. Two distinct studies answer this question. E2A is an exact discrete-event simulation of a single-server M/M/1 queue. Its times are simulated durations, not measured ROS 2 or HTTP performance. E2B measures the existing ROS 2--HTTP implementation while changing only the source arrival schedule.",
        r"The revised plan lists E9 as a prerequisite for selecting the gateway arrangement. E9 had not been performed when E2 was run. E2 therefore retains E1's T0\_SHARED arrangement as a declared scope choice: multiple ROS source nodes feed one shared FIFO, one HTTP sender and one server worker. The experiment does not compare independent HTTP clients or claim that E9 selected this arrangement.",
        r"\subsection{Queue boundary, populations and reporting conventions}",
        r"A message enters the E2B queueing system when it is admitted to the gateway FIFO. It leaves at response-handling completion: the sender has read the complete HTTP response body, parsed the acknowledgement, and records completion under the queue lock. A separate timestamp records the earlier body-read completion. Let $a_i$, $b_i$ and $d_i$ denote admission, worker dequeue and response-handling completion. Then $W_i=d_i-a_i$, $W_{q,i}=b_i-a_i$ and $S_i=d_i-b_i$, so $W_i=W_{q,i}+S_i$. These boundaries exclude the source-to-gateway ROS delivery stage. Application deadline measurements separately use source timestamp to response-handling completion.",
        r"$L$ and $L_q$ are exact time averages of the piecewise-constant system and waiting-queue populations over the measurement window. The busy fraction is the time average of the single-worker busy indicator; therefore $L=L_q+\text{busy fraction}$. The latency cohort contains messages admitted during the half-open measurement window; those messages are followed through drain. Completion rate instead counts departures occurring within the window. The two populations can differ at the window boundaries.",
        r"Each displayed point is the arithmetic mean of five run-level statistics. Bars span the minimum and maximum of those five statistics; they are not confidence intervals. In particular, a displayed p95 is the mean of five separately calculated run p95 values, not a percentile of pooled messages. No seed or run is selected for agreement with a hypothesis. The illustrative traces always use repeat 1 and the first 10 measurement seconds.",
        r"\subsection{E2A procedure: controlled M/M/1 calibration}",
        r"One FIFO worker serves independently drawn exponential service durations with configured rate $\mu=100$ jobs/s. A separate pseudorandom stream generates exponential inter-arrival durations with rate $\lambda=\rho\mu$. Independent, deterministically derived seeds are used for each stream and repetition. The queue holds up to 10,000 waiting jobs, with one additional job allowed in service. This finite implementation is compared with infinite-buffer theory only after checking that it never fills.",
        table([r"$\rho$", r"$\lambda$ (jobs/s)", "Warmup (s)", "Measure (s)", "Repeats"],
              [[num(c["rho"], 1), num(c["rho"] * model_config["mu_per_s"], 0), num(c["warmup_s"], 0), num(c["duration_s"], 0), 5] for c in model_config["cases"]],
              "E2A settings. All durations in this table are simulated seconds. Every run drains all admitted jobs after arrivals stop.", "tab:model_protocol"),
        r"For $\lambda<\mu$, the steady-state M/M/1 predictions are $W=1/(\mu-\lambda)$, $W_q=\rho/(\mu-\lambda)$, $L=\rho/(1-\rho)$ and $L_q=\rho^2/(1-\rho)$ \cite{mitqueue}. Predictions use configured rates; they are not fitted to the realised sample. Warmup and observation periods increase near saturation to reduce transient and sampling effects. All job timestamps, service draws and population events are retained in lossless numeric archives; sampled traces are not used to compute occupancy.",
        f"Across {n_model} runs, {model_total:,} jobs arrived in the measurement windows. There were {model_drops:,} drops across all simulated jobs, including warmup, and the largest waiting queue contained {max(r['max_queue_all'] for r in model):,} jobs. The independent validator reconstructed the FIFO schedule, population events, occupancy integrals, drain, distribution diagnostics and conservation from raw arrays.",
        r"\subsubsection{Checking the input assumptions}",
        "The exponential distribution check uses the configured arrival and service means. Figure~\\ref{fig:model_exponential_cdf} divides each duration by its configured mean, so both samples can be compared with the same unit-mean exponential CDF. The example is fixed in advance as repeat 1 at the highest utilisation; it is not the run with the closest fit.",
        figure("model_exponential_cdf", "E2A distribution check for repeat 1 at the highest configured utilisation. Empirical CDFs use all measurement-cohort inter-arrival intervals and service draws; the theoretical CDF is $1-e^{-x}$."),
        f"Across all runs, the inter-arrival coefficient of variation ranged from {num(min(all_arrival_cv))} to {num(max(all_arrival_cv))}, and the service coefficient of variation ranged from {num(min(all_service_cv))} to {num(max(all_service_cv))}; an exponential distribution has coefficient of variation 1. The largest absolute same-job arrival/service correlation was {num(max(all_cross), 4)}. The saved diagnostics also include lagged correlations, quantiles and Kolmogorov--Smirnov distances. Of the {2*n_model} configured-distribution tests, {ks_rejections} had an unadjusted asymptotic $p<0.05$. These multiple diagnostic tests are reported without discarding or rerunning data; individual rejections can occur under the specified model.",
        r"\subsubsection{Mean delay and population against theory}",
        "Figure~\\ref{fig:model_mean_delay} compares each run's mean admission-to-completion time with the configured-rate M/M/1 prediction. Figure~\\ref{fig:model_occupancy} independently compares the event-integrated mean system population with its prediction.",
        figure("model_mean_delay", "E2A mean system residence against configured-rate M/M/1 theory. Points are means of five runs; bars show the run range. Durations are simulated milliseconds."),
        f"The absolute relative difference between the mean simulated residence and theory ranged from {num(min(model_errors), 2)}\\% to {num(max(model_errors), 2)}\\% across the four settings. Delay grows as utilisation approaches one because successive arrivals increasingly find unfinished work. The finite observation periods and independent realisations produce variation around the steady-state prediction; the comparison is calibration of the controlled model, not a prediction for the real mixed-payload REST service.",
        figure("model_occupancy", r"E2A system population from exact population-event integration, compared with $L=\rho/(1-\rho)$. The queue includes waiting jobs; the system additionally includes the job in service."),
        table([r"$\rho$", r"$W$ (ms)", r"Theory $W$ (ms)", r"$L$", r"$L_q$", "Busy"],
              [[num(rho,1), num(avg(mg[rho], "W_s")*1000), num(mg[rho][0]["theory"]["W_s"]*1000), num(avg(mg[rho], "L")), num(avg(mg[rho], "Lq")), num(avg(mg[rho], "busy_fraction"))] for rho in rhos],
              "E2A mean run statistics. Busy is a fraction of measurement time, not process CPU utilisation.", "tab:model_means"),
        r"\subsection{Finite-window accounting and Little's relationship}",
        r"Little's steady-state relationship is $L=\lambda W$ for consistent populations and time boundaries \cite{little1961}. A finite observation window also intersects jobs admitted before it or completed after it. Let $A_{\rm in}$ be the in-window residence area from jobs admitted before the window, and $A_{\rm out}$ the post-window residence area of jobs admitted inside it. With $T$ the window duration and $\lambda_a$ the admission-cohort count divided by $T$, the exact identity is",
        r"\[ L=\lambda_a\overline{W}+(A_{\rm in}-A_{\rm out})/T. \]",
        r"The same interval decomposition applies to $L_q$ and $\overline{W_q}$. It is a sample-path accounting check, not an assumption that a finite run is in equilibrium. Exact conservation also requires initial population plus admissions to equal departures plus final population. The raw validators check both identities independently.",
        table(["Study / condition", r"$L$", r"$\lambda_a\overline{W}$", "Boundary term", "Max. residual"],
              [[f"E2A, {rho:.1f}", num(avg(mg[rho],"L"),5), num(avg(mg[rho],lambda r:r["little"]["lambda_W"]),5), num(avg(mg[rho],lambda r:r["little"]["system_boundary_correction"]),5), f"{max(abs(r['little']['system_identity_residual']) for r in mg[rho]):.1e}"] for rho in rhos]
              + [["E2B, " + LABELS[p], num(qmean(p,"mean_L"),5), num(mean(p,lambda r:r["queue_system"]["little_system"]["uncorrected_lambda_W"]),5), num(mean(p,lambda r:r["queue_system"]["little_system"]["boundary_correction"]),5), f"{max(abs(r['queue_system']['little_system']['identity_residual_ns']) / (r['duration_s']*1e9) for r in lg[p]):.1e}"] for p in PATTERNS],
              "Finite-window system accounting. First three numerical columns are means of five runs; the last is the largest absolute identity residual in that condition, expressed in jobs/messages. Rounding displayed columns can obscure small corrections.", "tab:little"),
        f"The maximum E2A system-identity residual was {model_residual:.2e} jobs, at floating-point precision. In E2B, the maximum difference between the independently integrated system events and the summed clipped per-message residence intervals was {live_residual:,} message-nanoseconds. E2B shares an integer-nanosecond timestamp between each state change and its message record, so this check can be exact. Agreement validates the measurement boundary and accounting; it does not establish that E2B has exponential service times.",
        r"\subsection{E2B procedure: vary arrival timing at fixed expected workload}",
        r"Four actual ROS 2 source nodes run in one open-loop publisher process. Each node publishes command, status, odometry and sensor streams at expected rates 2, 5, 20 and 5 messages/s, with synthetic x-filled payload strings of 100, 100, 250 and 20,000 bytes. ROS topics carry \texttt{std\_msgs/String} messages containing the JSON envelope and payload string; the four class names denote traffic profiles, not native ROS message definitions. Thus the expected aggregate arrival rate is 128 messages/s. The payload mixture is fixed in expectation; Poisson realisations have small count and composition variation, which is retained in the per-source and per-class tables. JSON metadata increases HTTP body bytes beyond these payload-string sizes.",
        f"All patterns use the same 1,000-message waiting FIFO, one persistent HTTP/1.1 connection, one server worker, no retries and an unshaped isolated virtual-Ethernet path in WSL. A fixed {num(live_config['server_processing_delay_ms'],1)} ms hold is requested inside each server operation. The realised gateway service $S$ also includes serialization, HTTP transport, response handling and scheduling, so it is neither exactly that hold duration nor assumed exponential. Source scheduling does not wait for HTTP responses.",
        table(["Arrival pattern", "Definition"], [
            ["Staggered periodic", r"Deterministic per-stream periods; phases staggered over the 16 streams."],
            ["Synchronized periodic", r"The same periods, with every stream assigned zero phase."],
            ["Poisson", r"Independent exponential inter-arrival durations per source and class."],
            ["Bursty ON/OFF", r"A shared 1 s cycle: Poisson arrivals at $5\times$ rate for 0.2 s, then no arrivals for 0.8 s."]],
              "E2B arrival definitions. The bursty schedule has deterministic ON/OFF durations and is not a Markov-modulated Poisson process.", "tab:patterns", "p{0.23\\linewidth}p{0.69\\linewidth}"),
        f"Each pattern has five repetitions, with seeded shuffled run order, {num(live_config['warmup_s'],0)} s warmup, {num(live_config['duration_s'],0)} s measurement and up to {num(live_config['drain_s'],0)} s drain. Warmup and measurement contain whole ON/OFF cycles, giving the bursty and ordinary Poisson cases identical integrated expected arrivals over each window. The realised number of Poisson arrivals remains random. The ROS graph, source scheduler timestamps, gateway events, server records, CPU samples and packet captures are retained per run. A separate validator reconstructs schedules, checks actual node/topic counts, verifies FIFO/concurrency and reconciles message identities, timing, populations and outcomes.",
        f"Strict capture checks passed for all {live_validation['capture_validation']['runs_checked']} live runs and matched {live_validation['capture_validation']['total_http_exchanges']:,} complete HTTP exchanges, including warmup and drain. Capture drops totalled {sum(r['capture_drops'] for r in live_validation['capture_validation']['runs']):,}. This cross-check validates captured request/response identities and byte accounting; it does not turn packet capture into a claim about physical-network performance.",
        "Simultaneous scheduled events are ordered by scheduled time, source ID, class name and sequence number; the class tie order is command, odometry, sensor, then status. The source publishes in this deterministic schedule order. ROS callback order is observed separately and is not assumed to reproduce every tie. This convention matters for class-level delays in synchronized batches. The 100 ms arrival bins resolve the ON/OFF cycle; one-second bins alone would integrate a complete burst cycle and conceal its concentration.",
        r"\subsubsection{First verify the offered workload}",
        "Figure~\\ref{fig:live_rates} uses published-message counts, gateway admissions and completed responses within each measurement window, each divided by the window duration. Differences at a boundary need not imply loss; message-level accounting follows the admission cohort through drain.",
        figure("live_rates", "Realised E2B workload and completion rates. The dashed line is the configured expected aggregate rate of 128 messages/s. Bars are the range of five run rates."),
        f"The condition-mean admission rates ranged from {num(rate_range[0],2)} to {num(rate_range[1],2)} messages/s. Across all {n_live} runs, {live_total:,} source-cohort messages were generated, with {live_drops:,} FIFO drops and {live_errors:,} combined request errors, missing ROS messages or unresolved source-cohort messages after drain. Expected average load is held fixed; the plot and saved class counts show the realised load rather than forcing stochastic counts to match.",
        r"\subsubsection{Arrival patterns become different queue histories}",
        "Figure~\\ref{fig:live_arrival_trace} bins source-publication timestamps into 100 ms intervals. Figure~\\ref{fig:live_queue_trace} uses the exact admission/dequeue/completion event log to draw the waiting-queue state. Both use the first 10 measurement seconds of repeat 1 for every pattern. They illustrate timing mechanisms; full-window statistics below support the comparison.",
        figure("live_arrival_trace", "Example source-publication counts in non-overlapping 100 ms bins. Each line is repeat 1 of one arrival pattern; individual source classes are mixed in the aggregate count."),
        figure("live_queue_trace", "Example waiting-queue histories for the same fixed repeat and interval. A step changes only at a logged gateway event; a job in service is excluded from the waiting population."),
        "A server processes one message at a time, so arrivals accumulated during a busy interval must wait. Synchronized periodic sources can produce short simultaneous batches; the ON/OFF case concentrates each second's expected demand into its first fifth. Idle portions of the cycle do not erase the waiting time experienced by messages in the preceding burst. The realised traces, rather than the configured labels alone, establish how these effects appeared in this implementation.",
        r"\subsubsection{Waiting time, tails and occupancy}",
        "Figures~\\ref{fig:live_wait} and~\\ref{fig:live_system_delay} use the gateway-admission cohort and include completions during drain. Their p95 and p99 values describe successful admitted messages; loss is reported alongside them so a favourable tail cannot be obtained by hiding discarded messages.",
        figure("live_wait", "E2B mean, p95 and p99 FIFO waiting time. Waiting runs from admission to worker dequeue. Each point averages the corresponding statistic over five runs; bars show their range."),
        figure("live_system_delay", "E2B residence from gateway admission to response-handling completion. This adds service to waiting; it excludes the source-to-gateway delivery interval."),
        f"The lowest condition-mean gateway p95 was {num(qstat(minimum_pattern,'W_ms','p95'),2)} ms for {LABELS[minimum_pattern].lower()}, and the highest was {num(qstat(maximum_pattern,'W_ms','p95'),2)} ms for {LABELS[maximum_pattern].lower()}. These are observed differences under the fixed shared gateway, not a universal ranking of arrival processes. The realised mean service times ranged from {num(service_range[0],3)} to {num(service_range[1],3)} ms across conditions despite the same requested server hold; differences in scheduling or overhead can contribute alongside arrival variability.",
        figure("live_occupancy", "E2B time-average system and waiting-queue populations from exact event integration. The difference between the two populations equals the single-worker busy fraction."),
        table(["Pattern", r"$W$ (ms)", r"$W_q$ (ms)", r"$S$ (ms)", r"$L_q$", "Busy (\\%)"],
              [[LABELS[p], num(qstat(p,"W_ms"),2), num(qstat(p,"Wq_ms"),2), num(qstat(p,"S_ms"),3), num(qmean(p,"mean_Lq"),3), num(qmean(p,"busy_fraction")*100,2)] for p in PATTERNS],
              "E2B means of five run-level values. The latency decomposition uses one common admission cohort; occupancy and busy fraction use the measurement-time window.", "tab:live_means"),
        r"\subsubsection{Check service, scheduling and class-level consequences}",
        table(["Pattern", r"CV$(S)$", "IAT CV", "Lag p95 (ms)", "CPU (\\%)"],
              [[LABELS[p], num(mean(p,lambda r:math.sqrt(r["queue_system"]["S_ms"]["cv2"])),3), num(mean(p,lambda r:math.sqrt(r["arrival_variability"]["gateway"]["interarrival_ms"]["cv2"])),3), num(mean(p,lambda r:r["source_jitter"]["schedule_to_source_ms"]["p95"]),3), " / ".join(num(mean(p,f"{process}_cpu_percent"),1) for process in ("source","gateway","server"))] for p in PATTERNS],
              "Variability and execution checks, averaged over runs. IAT CV is the gateway inter-arrival coefficient of variation. The three CPU values are source / gateway / server: process CPU time divided by wall time, expressed relative to one logical core. Source lag is scheduled-to-source timestamp delay.", "tab:live_checks", "lrrrr"),
        "The service coefficient of variation and source scheduling lag test how well the implementation follows the intended model. An exponential service distribution would have coefficient of variation near one; a deterministic hold plus real processing overhead does not create M/M/1 merely because one arrival schedule is Poisson. Accordingly, no M/M/1 fitted curve is overlaid on E2B. The service boundary includes the occupied sender while it waits for the HTTP response, so busy fraction is distinct from CPU utilisation.",
        table(["Pattern", "Command", "Status", "Odometry", "Sensor"],
              [[LABELS[p]] + [num(mean(p,lambda r,k=kind:100*r["deadlines"][k]["miss_ratio"]),2) for kind in ("command","status","odometry","sensor")] for p in PATTERNS],
              "Mean run deadline-miss percentage, by class. Denominators include all generated messages of that class in the source-time window; late success, loss and non-completion count as misses. Targets are 100, 200, 100 and 1,000 ms for command, status, odometry and sensor, respectively. These are illustrative project targets, not certified application requirements.", "tab:class_deadlines"),
        f"For bursty ON/OFF arrivals, the mean deadline-miss percentages were {num(mean('bursty_onoff_poisson',lambda r:100*r['deadlines']['command']['miss_ratio']),2)}\\% for command, {num(mean('bursty_onoff_poisson',lambda r:100*r['deadlines']['status']['miss_ratio']),2)}\\% for status, {num(mean('bursty_onoff_poisson',lambda r:100*r['deadlines']['odometry']['miss_ratio']),2)}\\% for odometry and {num(mean('bursty_onoff_poisson',lambda r:100*r['deadlines']['sensor']['miss_ratio']),2)}\\% for sensor. Together with the loss and completion counts, these class-level results distinguish eventual delivery from timely delivery. An average offered rate below the worker's capacity can still produce long waits within a concentrated burst, and classes with different deadline thresholds can therefore experience different practical consequences.",
        r"\subsection{Interpretation, limits and reproducibility}",
        "E2A checks the queue model and the analysis pipeline under explicit exponential assumptions. E2B then isolates arrival timing at a fixed expected mixed-payload workload and fixed service setup. The event-level Little identity provides a common accounting check without asserting that the empirical service is exponential or that every finite window is in steady state. The evidence supports conclusions about this measured shared FIFO implementation and these four specified arrival schedules.",
        f"The sources, gateway and server share one WSL host; the virtual link has no physical propagation path or competing external traffic. Four actual ROS nodes are not four independent computers, and one publishing process shares a scheduler. The fixed server hold is an intentional controlled service cost. Five repetitions show run variation but do not establish a confidence interval, and {num(live_config['duration_s'],0)} s windows do not characterise every rare tail event. Independent HTTP-client scaling, topology selection under E9, different network impairments and other protocol variants remain separate experiments.",
        r"The deliverable includes one plot per PDF, per-run and aggregate CSV tables, this reusable LaTeX section and the standalone report. The raw datasets retain the frozen settings, code snapshots/hashes, seeds, jobs or message records, event logs, validation and captures. \texttt{report\_evidence.json} records SHA-256 hashes of the report's consumed inputs and generator. Rebuild figures from the validated datasets with \texttt{scripts/report\_e2.py}; compile \texttt{main.tex} twice with pdfLaTeX. The individual-run CSVs retain full precision for further analysis.",
        r"\subsection{Recommended figures for the main report}",
        "For a concise narrative, retain the model mean-delay plot, the finite-window Little table, the realised-rate plot, the arrival and queue examples, the waiting-time plot and the occupancy plot. Together they establish the model, verify comparable input workload, show the timing mechanism, quantify its effect and check the accounting. Put the exponential CDF, system-tail plot, detailed service checks and class-level table in supplementary material if space is limited."]

    abstract = (f"A {n_model}-run discrete-event M/M/1 calibration was followed by {n_live} measured ROS 2--HTTP runs with four actual source nodes and four arrival patterns. "
                f"The model mean-delay differences from configured-rate theory were {num(min(model_errors),2)}--{num(max(model_errors),2)}\\%. "
                f"The live study held the expected load at 128 messages/s with one shared FIFO sender and a fixed {num(live_config['server_processing_delay_ms'],1)} ms server hold. "
                f"Condition-mean gateway p95 residence ranged from {num(qstat(minimum_pattern,'W_ms','p95'),2)} to {num(qstat(maximum_pattern,'W_ms','p95'),2)} ms. "
                f"There were {live_drops:,} FIFO drops in the measured source cohorts. Exact finite-window population accounting separates the queueing relationship from assumptions about arrival and service distributions. "
                "Model durations are simulated; the arrival-pattern results are measured on an isolated virtual network on one host.")
    return "\n\n".join(text) + "\n", abstract


def run(model_path, live_path, output):
    datasets = load_datasets(model_path, live_path)
    model_config, model_validation, model, live_config, live_validation, live, folders = datasets
    output.mkdir(parents=True, exist_ok=True)
    (output / "tables").mkdir(exist_ok=True)
    model_rows, live_rows = build_tables(output, model, live)
    make_plots(output, model_path, model, live, folders)
    section, abstract = report_text(model_config, model_validation, model, live_config, live_validation, live, model_rows, live_rows)
    write_tex(output / "e2_section.tex", section)
    main = r"""\documentclass[11pt,a4paper]{article}
\usepackage{graphicx}
\usepackage{float}
\usepackage{amsmath}
\usepackage{url}
\setlength{\textwidth}{6.35in}
\setlength{\oddsidemargin}{0.02in}
\setlength{\evensidemargin}{0.02in}
\setlength{\textheight}{9.25in}
\setlength{\topmargin}{-0.55in}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.55em}
\setlength{\emergencystretch}{2em}
\title{Queueing Relationships and Arrival Variability:\\A Shared ROS~2--REST Gateway}
\author{ELEN90061 Communication Networks}
\date{E2 experimental report}
\begin{document}
\maketitle
\begin{abstract}
""" + abstract + r"""
\end{abstract}
\input{e2_section}
\begin{thebibliography}{9}\raggedright
\bibitem{little1961} J. D. C. Little, ``A Proof for the Queuing Formula: $L=\lambda W$,'' \textit{Operations Research}, vol. 9, no. 3, pp. 383--387, 1961. DOI: 10.1287/opre.9.3.383.
\bibitem{mitqueue} S. B. Gershwin, ``Markov Processes and Queues,'' \textit{2.853/2.854 Introduction to Manufacturing Systems}, Massachusetts Institute of Technology, 2016, lecture notes. Available from MIT OpenCourseWare: \url{https://live.ocw.mit.edu/courses/2-854-introduction-to-manufacturing-systems-fall-2016/5d3e9532c2ff6e156d48e41f9bd9576f_MIT2_854F16_Queueing.pdf}.
\end{thebibliography}
\end{document}
"""
    write_tex(output / "main.tex", main)
    (output / "README.txt").write_text(
        "E2 report package\n\nUpload this directory to Overleaf and choose main.tex as the main document.\n"
        "Compile twice with pdfLaTeX. e2_section.tex can be included in another report; copy the figures directory and bibliography entries too.\n\n"
        "The reusable section requires graphicx, float and amsmath in the receiving document's preamble.\n"
        "Copy bibliography entries little1961 and mitqueue from main.tex, or supply equivalent entries under those citation keys.\n"
        "The standalone main.tex also uses url to typeset the source link in its bibliography.\n\n"
        "E2A is a discrete-event M/M/1 simulation, not measured ROS/HTTP performance. E2B is measured ROS/HTTP arrival variability.\n"
        "Every point is the mean of five run-level statistics; bars show min/max across those runs, not confidence intervals.\n"
        "Every figure contains one plot. tables/ preserves per-run, per-class and per-source values; no robot-equivalent labels are used.\n\n"
        "Rebuild from the workspace root:\n"
        f'python -B scripts/report_e2.py --model "{model_path}" --live "{live_path}" --output "{output}"\n'
        "Do not run plotting during live performance measurement. Input SHA-256 hashes are recorded in report_evidence.json.\n",
        encoding="utf-8")
    write_json(output / "report_evidence.json", {
        "built_utc": datetime.now(timezone.utc).isoformat(), "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "model_root": str(model_path.resolve()), "live_root": str(live_path.resolve()),
        "model_run_count": len(model), "live_run_count": len(live),
        "model_validation_passed": model_validation["all_passed"], "live_validation_passed": live_validation["all_passed"],
        "capture_validation_passed": live_validation["capture_validation"]["all_passed"],
        "captured_http_exchanges": live_validation["capture_validation"]["total_http_exchanges"],
        "aggregation": "Mean of run-level statistics; minimum/maximum run range; no pooled percentiles",
        "trace_policy": "Repeat 1 of each pattern, first 10 measurement seconds, source counts in 100ms bins",
        "model_cdf_policy": "Repeat 1 at largest configured rho; duration normalized by configured mean",
        "input_sha256": INPUTS,
        "figure_files": sorted(p.name for p in (output / "figures").glob("*.pdf")),
        "raw_data_written": False})
    print(json.dumps({"report_directory": str(output), "model_runs": len(model), "live_runs": len(live), "figures": 9}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=Path("results/e2_model_complete"))
    parser.add_argument("--live", type=Path, default=Path("results/e2_live_complete"))
    parser.add_argument("--output", type=Path, default=Path("report/e2"))
    args = parser.parse_args()
    run(args.model, args.live, args.output)
