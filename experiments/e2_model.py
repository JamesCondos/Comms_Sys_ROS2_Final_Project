"""E2A: exact discrete-event M/M/1 model calibration, NOT ROS/HTTP performance.

Raw job/event archives contain named NumPy arrays in simulated seconds. One
worker serves a FIFO waiting queue; capacity excludes its in-service job.
"""
import argparse
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import sys
import time

import numpy as np


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def seed_for(base_seed, rho, repeat, stream):
    text = f"e2a:{base_seed}:{rho:.12g}:{repeat}:{stream}"
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:16], "big")


def validate_config(config):
    assert config["experiment"] == "E2A"
    assert config["model"] == "discrete_event_mm1"
    assert config["mu_per_s"] > 0
    assert config["repeats"] >= 1 and config["queue_capacity"] >= 1
    assert config["sample_interval_s"] > 0
    assert len({case["rho"] for case in config["cases"]}) == len(config["cases"])
    assert all(0 < case["rho"] < 1 and case["warmup_s"] >= 0 and case["duration_s"] > 0
               for case in config["cases"])


def fifo_schedule(arrivals, services, capacity):
    """Exact FCFS recurrence with drop-tail capacity, draining all admitted jobs."""
    arrivals = np.asarray(arrivals, dtype=np.float64)
    services = np.asarray(services, dtype=np.float64)
    if len(arrivals) != len(services) or np.any(np.diff(arrivals) < 0) or np.any(services <= 0):
        raise ValueError("Invalid arrival/service sequence")
    start = np.full(len(arrivals), np.nan)
    departure = np.full(len(arrivals), np.nan)
    admitted = np.zeros(len(arrivals), dtype=bool)
    active = deque()
    last_departure = 0.0
    for i, (arrival, service) in enumerate(zip(arrivals, services)):
        while active and active[0] <= arrival:
            active.popleft()
        if len(active) >= capacity + 1:
            continue
        admitted[i] = True
        start[i] = max(arrival, last_departure)
        departure[i] = start[i] + service
        last_departure = departure[i]
        active.append(last_departure)
    return start, departure, admitted


def event_log(jobs):
    """Departure events precede arrivals at identical times (capacity frees first)."""
    ids = jobs["job_id"]
    admitted = jobs["admitted"]
    timestamps = np.concatenate((jobs["arrival_s"], jobs["departure_s"][admitted]))
    kinds = np.concatenate((np.where(admitted, 1, 2), np.zeros(int(admitted.sum()), dtype=np.int8)))
    event_ids = np.concatenate((ids, ids[admitted]))
    order = np.lexsort((event_ids, kinds, timestamps))
    timestamps, kinds, event_ids = timestamps[order], kinds[order], event_ids[order]
    delta = np.where(kinds == 1, 1, np.where(kinds == 0, -1, 0))
    population = np.cumsum(delta)
    assert np.all(population >= 0) and population[-1] == 0
    return {"timestamp_s": timestamps, "kind": kinds.astype(np.int8), "job_id": event_ids,
            "system_after": population.astype(np.int32),
            "queue_after": np.maximum(population - 1, 0).astype(np.int32),
            "busy_after": (population > 0)}


def clipped(start, finish, begin, end):
    return np.maximum(0.0, np.minimum(finish, end) - np.maximum(start, begin))


def lag_correlation(values, lag):
    if len(values) <= lag + 1:
        return None
    return float(np.corrcoef(values[:-lag], values[lag:])[0, 1])


def exponential_diagnostics(values, rate):
    values = np.asarray(values)
    n = len(values)
    if n < 3:
        raise ValueError("At least three observations required")
    ordered = np.sort(values)
    expected_cdf = -np.expm1(-rate * ordered)
    d = float(max(np.max(np.arange(1, n + 1) / n - expected_cdf),
                  np.max(expected_cdf - np.arange(n) / n)))
    z = (math.sqrt(n) + 0.12 + 0.11 / math.sqrt(n)) * d
    p = min(1.0, max(0.0, 2 * sum((-1) ** (k - 1) * math.exp(-2 * k * k * z * z)
                                 for k in range(1, 101))))
    probabilities = [0.5, 0.9, 0.95, 0.99]
    mean = float(np.mean(values))
    return {"n": n, "configured_rate_per_s": rate, "mean_s": mean,
            "std_s": float(np.std(values, ddof=1)),
            "cv": float(np.std(values, ddof=1) / mean),
            "observed_quantiles_s": {str(q): float(np.quantile(values, q)) for q in probabilities},
            "exponential_quantiles_s": {str(q): -math.log(1 - q) / rate for q in probabilities},
            "ks_d_against_configured_exponential": d, "ks_asymptotic_p": p,
            "ks_5pct_critical_d": 1.36 / math.sqrt(n),
            "serial_correlations": {str(lag): lag_correlation(values, lag) for lag in [1, 5, 10]},
            "approx_95pct_zero_correlation_bound": 1.96 / math.sqrt(n)}


def summarize(jobs, events, config, rho):
    begin, end = config["warmup_s"], config["warmup_s"] + config["duration_s"]
    duration = config["duration_s"]
    arrivals = jobs["arrival_s"]
    admitted = jobs["admitted"]
    start, departure = jobs["service_start_s"], jobs["departure_s"]
    window = (arrivals >= begin) & (arrivals < end)
    cohort = window & admitted
    pre = admitted & (arrivals < begin)
    completions = admitted & (departure >= begin) & (departure < end)
    initial = int(np.sum(admitted & (arrivals < begin) & (departure >= begin)))
    final = int(np.sum(admitted & (arrivals < end) & (departure >= end)))
    event_end = np.r_[events["timestamp_s"][1:], max(end, float(events["timestamp_s"][-1]))]
    widths = clipped(events["timestamp_s"], event_end, begin, end)
    area = float(np.dot(widths, events["system_after"]))
    queue_area = float(np.dot(widths, events["queue_after"]))
    busy_area = float(np.dot(widths, events["busy_after"]))
    residence = departure[cohort] - arrivals[cohort]
    waiting = start[cohort] - arrivals[cohort]
    service = departure[cohort] - start[cohort]
    carry_in = float(np.sum(clipped(arrivals[pre], departure[pre], begin, end)))
    carry_in_q = float(np.sum(clipped(arrivals[pre], start[pre], begin, end)))
    carry_out = float(np.sum(np.maximum(0, departure[cohort] - np.maximum(arrivals[cohort], end))))
    carry_out_q = float(np.sum(np.maximum(0, start[cohort] - np.maximum(arrivals[cohort], end))))
    cohort_system = float(np.sum(residence))
    cohort_queue = float(np.sum(waiting))
    lam = rho * config["mu_per_s"]
    mu = config["mu_per_s"]
    diagnostics = {
        "population": "All offered arrivals in [warmup, warmup+duration); inter-arrival interval ending at each arrival",
        "interarrival": exponential_diagnostics(jobs["interarrival_s"][window], lam),
        "service_draw": exponential_diagnostics(jobs["service_draw_s"][window], mu),
        "same_job_arrival_service_correlation": float(np.corrcoef(jobs["interarrival_s"][window], jobs["service_draw_s"][window])[0, 1]),
        "arrival_service_lag1_correlation": float(np.corrcoef(jobs["interarrival_s"][window][:-1], jobs["service_draw_s"][window][1:])[0, 1]),
        "interpretation": "Diagnostics, not selection criteria: no reruns or tuning on p-values; configured rates are not fitted to samples",
    }
    result = {
        "kind": "discrete_event_model_calibration", "rho_target": rho,
        "lambda_target_per_s": lam, "mu_target_per_s": mu,
        "window_begin_s": begin, "window_end_s": end, "duration_s": duration,
        "offered": int(window.sum()), "admitted": int(cohort.sum()),
        "completed_in_window": int(completions.sum()),
        "completed_admission_cohort_including_drain": int(cohort.sum()),
        "drops": int(np.sum(window & ~admitted)), "drops_all": int(np.sum(~admitted)),
        "pending_after_drain": 0, "initial_population": initial, "final_population": final,
        "conservation_residual": initial + int(cohort.sum()) - int(completions.sum()) - final,
        "offered_per_s": float(window.sum() / duration),
        "admitted_per_s": float(cohort.sum() / duration),
        "completed_per_s": float(completions.sum() / duration),
        "L": area / duration, "Lq": queue_area / duration,
        "busy_fraction": busy_area / duration,
        "W_s": float(np.mean(residence)), "Wq_s": float(np.mean(waiting)),
        "service_mean_s": float(np.mean(service)),
        "p95_s": float(np.quantile(residence, .95)), "p99_s": float(np.quantile(residence, .99)),
        "queue_p95_s": float(np.quantile(waiting, .95)), "queue_p99_s": float(np.quantile(waiting, .99)),
        "max_system_all": int(events["system_after"].max()),
        "max_queue_all": int(events["queue_after"].max()),
        "drain_duration_s": max(0.0, float(departure[admitted].max()) - end),
        "theory": {"rho": rho, "W_s": 1 / (mu - lam), "Wq_s": rho / (mu - lam),
                   "L": rho / (1 - rho), "Lq": rho * rho / (1 - rho), "busy_fraction": rho,
                   "p95_s": -math.log(.05) / (mu - lam), "p99_s": -math.log(.01) / (mu - lam)},
        "little": {"system_area_job_s": area, "queue_area_job_s": queue_area,
                   "cohort_residence_sum_s": cohort_system, "cohort_wait_sum_s": cohort_queue,
                   "system_carry_in_area_job_s": carry_in, "system_carry_out_area_job_s": carry_out,
                   "queue_carry_in_area_job_s": carry_in_q, "queue_carry_out_area_job_s": carry_out_q,
                   "lambda_W": cohort_system / duration, "lambda_Wq": cohort_queue / duration,
                   "system_boundary_correction": (carry_in - carry_out) / duration,
                   "queue_boundary_correction": (carry_in_q - carry_out_q) / duration,
                   "system_identity_residual": (area - cohort_system - carry_in + carry_out) / duration,
                   "queue_identity_residual": (queue_area - cohort_queue - carry_in_q + carry_out_q) / duration,
                   "identity": "L = lambda_admitted * W_admission_cohort + (carry_in - carry_out) / duration; analogous Lq"},
        "distribution_diagnostics": diagnostics,
    }
    return result


def simulate(config, rho, repeat):
    end = config["warmup_s"] + config["duration_s"]
    lam = rho * config["mu_per_s"]
    arrival_seed = seed_for(config["seed"], rho, repeat, "arrival")
    service_seed = seed_for(config["seed"], rho, repeat, "service")
    arrival_rng = np.random.Generator(np.random.PCG64(arrival_seed))
    service_rng = np.random.Generator(np.random.PCG64(service_seed))
    draw_count = max(1024, int(lam * end + 12 * math.sqrt(lam * end) + 1000))
    intervals = arrival_rng.exponential(1 / lam, draw_count)
    arrivals = np.cumsum(intervals)
    while arrivals[-1] < end:
        intervals = np.concatenate((intervals, arrival_rng.exponential(1 / lam, 1024)))
        arrivals = np.cumsum(intervals)
    count = int(np.searchsorted(arrivals, end, side="left"))
    next_interval, next_arrival = float(intervals[count]), float(arrivals[count])
    intervals, arrivals = intervals[:count], arrivals[:count]
    services = service_rng.exponential(1 / config["mu_per_s"], count)
    start, departure, admitted = fifo_schedule(arrivals, services, config["queue_capacity"])
    jobs = {"job_id": np.arange(count, dtype=np.int64), "interarrival_s": intervals,
            "arrival_s": arrivals, "service_draw_s": services, "admitted": admitted,
            "service_start_s": start, "departure_s": departure}
    events = event_log(jobs)
    summary = summarize(jobs, events, config, rho)
    summary.update({"repeat": repeat, "arrival_seed": arrival_seed, "service_seed": service_seed,
                    "all_offered_jobs": count, "first_excluded_interarrival_s": next_interval,
                    "first_excluded_arrival_s": next_arrival})
    return jobs, events, summary


def save_samples(path, events, config):
    begin, end = config["warmup_s"], config["warmup_s"] + config["duration_s"]
    sample_times = np.arange(begin, end, config["sample_interval_s"])
    sample_times = np.r_[sample_times, end]
    indices = np.searchsorted(events["timestamp_s"], sample_times, side="right") - 1
    system = np.where(indices >= 0, events["system_after"][np.maximum(indices, 0)], 0)
    data = np.column_stack((sample_times, system, np.maximum(system - 1, 0), system > 0))
    np.savetxt(path, data, delimiter=",", header="timestamp_s,system,queue,busy", comments="", fmt=["%.17g", "%d", "%d", "%d"])


def run(config_path, output):
    config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    validate_config(config)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "config.json", config)
    metadata = {"kind": "discrete_event_model_calibration", "created_utc": datetime.now(timezone.utc).isoformat(),
                "python": sys.version, "numpy": np.__version__, "platform": platform.platform(),
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "config_sha256": digest(config), "rng": "numpy.Generator(PCG64); independent SHA256-derived 128-bit seeds",
                "time_unit": "simulated seconds; wall-clock execution is not a latency measurement",
                "boundary": "System: arrival/admission to service completion. Queue: admission to service start. One FIFO worker.",
                "capacity": "queue_capacity counts waiting jobs; one additional job may be in service",
                "cohort": "Arrival in half-open measurement window; residence includes complete drain, no timeout",
                "event_schema": {"kind": {"0": "departure", "1": "admitted arrival", "2": "rejected arrival"},
                                 "state": "right-continuous post-event; departures before arrivals at identical times"},
                "storage": "jobs.npz/events.npz compressed NumPy named arrays preserve float64 timestamps exactly; samples.csv for illustration only",
                "statistical_policy": "Fixed seeds and durations. Distribution/theory mismatches retained, never used to select or rerun data."}
    write_json(output / "metadata.json", metadata)
    summaries = []
    started = time.monotonic()
    for case in config["cases"]:
        rho = case["rho"]
        run_config = {**config, **case}
        for repeat in range(1, config["repeats"] + 1):
            name = f"rho{rho:.2f}_r{repeat:02d}"
            destination = output / name
            destination.mkdir()
            print(f"START {name}", flush=True)
            jobs, events, summary = simulate(run_config, rho, repeat)
            np.savez_compressed(destination / "jobs.npz", **jobs)
            np.savez_compressed(destination / "events.npz", **events)
            save_samples(destination / "samples.csv", events, run_config)
            summary["run_id"] = name
            summary["config_sha256"] = digest(config)
            write_json(destination / "summary.json", summary)
            summaries.append(summary)
            write_json(output / "summaries.json", summaries)
            print(f"DONE {name} jobs={summary['all_offered_jobs']} W_ms={summary['W_s']*1000:.6f} "
                  f"L={summary['L']:.6f} drops={summary['drops_all']}", flush=True)
    write_json(output / "execution.json", {"wall_duration_s": time.monotonic() - started, "completed_runs": len(summaries)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    run(args.config, args.output)
