"""Independently reconstruct E2A raw-job schedules, events and finite-window metrics.

No simulator imports. Statistical departures from theory remain valid findings;
accounting, RNG provenance and effectively infinite-buffer assumptions are gates.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import numpy as np


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def close(actual, expected, name, atol=1e-9):
    assert np.isclose(actual, expected, atol=atol, rtol=1e-9), f"{name}: {actual} != {expected}"


def overlap(start, end, lower, upper):
    return np.maximum(0, np.minimum(end, upper) - np.maximum(start, lower))


def verify_diagnostics(values, rate, recorded):
    count = len(values)
    assert recorded["n"] == count
    close(recorded["configured_rate_per_s"], rate, "configured distribution rate")
    close(recorded["mean_s"], np.mean(values), "distribution mean")
    close(recorded["std_s"], np.std(values, ddof=1), "distribution std")
    close(recorded["cv"], np.std(values, ddof=1) / np.mean(values), "distribution CV")
    x = np.sort(values)
    cdf = 1 - np.exp(-rate * x)
    d = max(np.max(np.linspace(1 / count, 1, count) - cdf), np.max(cdf - np.arange(count) / count))
    close(recorded["ks_d_against_configured_exponential"], d, "KS statistic")
    close(recorded["ks_5pct_critical_d"], 1.36 / math.sqrt(count), "KS reference critical value")
    z = (math.sqrt(count) + .12 + .11 / math.sqrt(count)) * d
    p = np.clip(2 * sum((-1) ** (k - 1) * math.exp(-2 * k ** 2 * z ** 2) for k in range(1, 101)), 0, 1)
    close(recorded["ks_asymptotic_p"], p, "KS p diagnostic")
    for q in [.5, .9, .95, .99]:
        close(recorded["observed_quantiles_s"][str(q)], np.quantile(values, q), "distribution quantile")
        close(recorded["exponential_quantiles_s"][str(q)], -math.log(1 - q) / rate, "theoretical quantile")
    for lag in [1, 5, 10]:
        expected = float(np.corrcoef(values[:-lag], values[lag:])[0, 1]) if count > lag + 1 else None
        if expected is None:
            assert recorded["serial_correlations"][str(lag)] is None
        else:
            close(recorded["serial_correlations"][str(lag)], expected, "serial correlation")
    close(recorded["approx_95pct_zero_correlation_bound"], 1.96 / math.sqrt(count), "correlation reference bound")


def verify_run(path, config, case, repeat):
    summary = read_json(path / "summary.json")
    with np.load(path / "jobs.npz", allow_pickle=False) as archive:
        jobs = {key: archive[key] for key in archive.files}
    with np.load(path / "events.npz", allow_pickle=False) as archive:
        events = {key: archive[key] for key in archive.files}
    assert set(jobs) == {"job_id", "interarrival_s", "arrival_s", "service_draw_s", "admitted", "service_start_s", "departure_s"}
    assert set(events) == {"timestamp_s", "kind", "job_id", "system_after", "queue_after", "busy_after"}
    a, s, d = jobs["arrival_s"], jobs["service_start_s"], jobs["departure_s"]
    accepted = jobs["admitted"]
    n = len(a)
    assert all(len(v) == n for v in jobs.values())
    assert np.array_equal(jobs["job_id"], np.arange(n)) and accepted.dtype == bool
    assert n > 2 and np.all(np.isfinite(a)) and np.all(np.diff(a) > 0)
    assert np.all(jobs["interarrival_s"] > 0) and np.all(jobs["service_draw_s"] > 0)
    assert summary["config_sha256"] == digest(config)
    assert summary["rho_target"] == case["rho"] and summary["repeat"] == repeat
    assert summary["run_id"] == path.name and summary["kind"] == "discrete_event_model_calibration"
    begin, end = case["warmup_s"], case["warmup_s"] + case["duration_s"]
    duration = case["duration_s"]
    mu, lam = config["mu_per_s"], config["mu_per_s"] * case["rho"]
    expected_seeds = {}
    for stream in ["arrival", "service"]:
        encoded = f"e2a:{config['seed']}:{case['rho']:.12g}:{repeat}:{stream}".encode()
        expected_seeds[stream] = int.from_bytes(hashlib.sha256(encoded).digest()[:16], "big")
        assert summary[f"{stream}_seed"] == expected_seeds[stream]
    assert expected_seeds["arrival"] != expected_seeds["service"]
    replay_arrival = np.random.Generator(np.random.PCG64(expected_seeds["arrival"])).exponential(1 / lam, n + 1)
    replay_service = np.random.Generator(np.random.PCG64(expected_seeds["service"])).exponential(1 / mu, n)
    assert np.array_equal(replay_arrival[:n], jobs["interarrival_s"]), "Arrival RNG replay differs"
    assert np.array_equal(replay_service, jobs["service_draw_s"]), "Service RNG replay differs"
    assert np.array_equal(np.cumsum(replay_arrival)[:n], a), "Arrival schedule differs"
    close(summary["first_excluded_interarrival_s"], replay_arrival[n], "terminal interval")
    close(summary["first_excluded_arrival_s"], np.cumsum(replay_arrival)[n], "terminal arrival")
    assert a[-1] < end <= summary["first_excluded_arrival_s"]
    finished = []
    first_active = 0
    previous_finish = 0.0
    for i in range(n):
        while first_active < len(finished) and finished[first_active] <= a[i]:
            first_active += 1
        expected_admit = len(finished) - first_active <= config["queue_capacity"]
        assert bool(accepted[i]) == expected_admit, "Finite capacity decision differs"
        if expected_admit:
            expected_start = max(a[i], previous_finish)
            assert s[i] == expected_start, "FIFO or one-worker start violated"
            assert d[i] == s[i] + jobs["service_draw_s"][i], "Service duration differs"
            previous_finish = d[i]
            finished.append(d[i])
        else:
            assert np.isnan(s[i]) and np.isnan(d[i])
    assert np.all(s[accepted] >= a[accepted]) and np.all(d[accepted] > s[accepted])
    assert np.all(s[accepted][1:] >= d[accepted][:-1]), "Concurrent service"
    assert accepted.all(), "Queue filled: this is finite-buffer M/M/1/K, not the no-drop calibration"
    ids = np.r_[np.arange(n), np.arange(n)[accepted]]
    kinds = np.r_[np.where(accepted, 1, 2), np.zeros(accepted.sum(), dtype=np.int8)]
    timestamps = np.r_[a, d[accepted]]
    order = np.lexsort((ids, kinds, timestamps))
    expected_t, expected_k, expected_id = timestamps[order], kinds[order], ids[order]
    assert np.array_equal(events["timestamp_s"], expected_t)
    assert np.array_equal(events["kind"], expected_k) and np.array_equal(events["job_id"], expected_id)
    expected_population = np.cumsum(np.where(expected_k == 1, 1, np.where(expected_k == 0, -1, 0)))
    assert np.array_equal(events["system_after"], expected_population)
    assert np.array_equal(events["queue_after"], np.maximum(expected_population - 1, 0))
    assert np.array_equal(events["busy_after"], expected_population > 0)
    assert expected_population.min() >= 0 and expected_population[-1] == 0
    window = (a >= begin) & (a < end)
    cohort = window & accepted
    pre = (a < begin) & accepted
    in_window = accepted & (d >= begin) & (d < end)
    initial = int(np.sum(accepted & (a < begin) & (d >= begin)))
    final = int(np.sum(accepted & (a < end) & (d >= end)))
    system_area = float(np.sum(overlap(a[accepted], d[accepted], begin, end)))
    queue_area = float(np.sum(overlap(a[accepted], s[accepted], begin, end)))
    busy_area = float(np.sum(overlap(s[accepted], d[accepted], begin, end)))
    widths = overlap(expected_t[:-1], expected_t[1:], begin, end)
    close(system_area, np.dot(widths, expected_population[:-1]), "job/event system area", 1e-7)
    close(queue_area, np.dot(widths, np.maximum(expected_population[:-1] - 1, 0)), "job/event queue area", 1e-7)
    close(busy_area, np.dot(widths, expected_population[:-1] > 0), "job/event busy area", 1e-7)
    residence, wait, service = d[cohort] - a[cohort], s[cohort] - a[cohort], d[cohort] - s[cohort]
    expected = {"window_begin_s": begin, "window_end_s": end, "duration_s": duration,
                "all_offered_jobs": n, "lambda_target_per_s": lam, "mu_target_per_s": mu,
                "offered": int(window.sum()), "admitted": int(cohort.sum()),
                "completed_in_window": int(in_window.sum()),
                "completed_admission_cohort_including_drain": int(cohort.sum()),
                "initial_population": initial, "final_population": final,
                "conservation_residual": initial + cohort.sum() - in_window.sum() - final,
                "drops": int(np.sum(window & ~accepted)), "drops_all": int(np.sum(~accepted)), "pending_after_drain": 0,
                "offered_per_s": window.sum() / duration, "admitted_per_s": cohort.sum() / duration,
                "completed_per_s": in_window.sum() / duration, "L": system_area / duration,
                "Lq": queue_area / duration, "busy_fraction": busy_area / duration,
                "W_s": np.mean(residence), "Wq_s": np.mean(wait), "service_mean_s": np.mean(service),
                "p95_s": np.quantile(residence, .95), "p99_s": np.quantile(residence, .99),
                "queue_p95_s": np.quantile(wait, .95), "queue_p99_s": np.quantile(wait, .99),
                "max_system_all": expected_population.max(), "max_queue_all": np.maximum(expected_population - 1, 0).max(),
                "drain_duration_s": max(0, d[accepted].max() - end)}
    for name, value in expected.items():
        close(summary[name], value, name)
    assert expected["conservation_residual"] == 0
    carry_in = float(np.sum(overlap(a[pre], d[pre], begin, end)))
    carry_in_q = float(np.sum(overlap(a[pre], s[pre], begin, end)))
    carry_out = float(np.sum(overlap(a[cohort], d[cohort], end, float(d.max()))))
    carry_out_q = float(np.sum(overlap(a[cohort], s[cohort], end, float(d.max()))))
    little_expected = {"system_area_job_s": system_area, "queue_area_job_s": queue_area,
                       "cohort_residence_sum_s": np.sum(residence), "cohort_wait_sum_s": np.sum(wait),
                       "system_carry_in_area_job_s": carry_in, "system_carry_out_area_job_s": carry_out,
                       "queue_carry_in_area_job_s": carry_in_q, "queue_carry_out_area_job_s": carry_out_q,
                       "lambda_W": np.sum(residence) / duration, "lambda_Wq": np.sum(wait) / duration,
                       "system_boundary_correction": (carry_in - carry_out) / duration,
                       "queue_boundary_correction": (carry_in_q - carry_out_q) / duration,
                       "system_identity_residual": (system_area - np.sum(residence) - carry_in + carry_out) / duration,
                       "queue_identity_residual": (queue_area - np.sum(wait) - carry_in_q + carry_out_q) / duration}
    for name, value in little_expected.items():
        close(summary["little"][name], value, name, 1e-7)
    assert abs(little_expected["system_identity_residual"]) < 1e-8
    assert abs(little_expected["queue_identity_residual"]) < 1e-8
    close(summary["L"] - summary["Lq"], summary["busy_fraction"], "L-Lq equals busy")
    rho = case["rho"]
    theory = {"rho": rho, "W_s": 1 / (mu - lam), "Wq_s": rho / (mu - lam),
              "L": rho / (1 - rho), "Lq": rho ** 2 / (1 - rho), "busy_fraction": rho,
              "p95_s": -math.log(.05) / (mu - lam), "p99_s": -math.log(.01) / (mu - lam)}
    for name, value in theory.items():
        close(summary["theory"][name], value, f"theory {name}")
    diag = summary["distribution_diagnostics"]
    arrival_values, service_values = jobs["interarrival_s"][window], jobs["service_draw_s"][window]
    verify_diagnostics(arrival_values, lam, diag["interarrival"])
    verify_diagnostics(service_values, mu, diag["service_draw"])
    close(diag["same_job_arrival_service_correlation"], np.corrcoef(arrival_values, service_values)[0, 1], "cross correlation")
    close(diag["arrival_service_lag1_correlation"], np.corrcoef(arrival_values[:-1], service_values[1:])[0, 1], "lag cross correlation")
    samples = np.genfromtxt(path / "samples.csv", delimiter=",", names=True)
    sample_times = np.r_[np.arange(begin, end, config["sample_interval_s"]), end]
    assert np.array_equal(samples["timestamp_s"], sample_times)
    index = np.searchsorted(expected_t, sample_times, side="right") - 1
    populations = np.where(index >= 0, expected_population[np.maximum(index, 0)], 0)
    assert np.array_equal(samples["system"], populations)
    assert np.array_equal(samples["queue"], np.maximum(populations - 1, 0))
    assert np.array_equal(samples["busy"], populations > 0)
    return {"run_id": path.name, "passed": True, "jobs_all": n, "measurement_jobs": int(window.sum()),
            "events": len(expected_t), "drops": int(np.sum(~accepted)),
            "max_queue_all": int(summary["max_queue_all"]),
            "system_identity_residual": little_expected["system_identity_residual"],
            "queue_identity_residual": little_expected["queue_identity_residual"],
            "distribution_rejections_at_unadjusted_5pct": sum(diag[key]["ks_asymptotic_p"] < .05 for key in ["interarrival", "service_draw"]),
            "statistical_note": "P-values are diagnostics across multiple tests, not acceptance gates; no selected reruns"}


def validate(root):
    root = Path(root)
    config, metadata = read_json(root / "config.json"), read_json(root / "metadata.json")
    assert metadata["config_sha256"] == digest(config)
    assert metadata["kind"] == "discrete_event_model_calibration"
    summaries = read_json(root / "summaries.json")
    expected_ids = {f"rho{case['rho']:.2f}_r{repeat:02d}" for case in config["cases"] for repeat in range(1, config["repeats"] + 1)}
    assert {r["run_id"] for r in summaries} == expected_ids and len(summaries) == len(expected_ids)
    assert {p.name for p in root.glob("rho*") if p.is_dir()} == expected_ids
    assert read_json(root / "execution.json")["completed_runs"] == len(expected_ids)
    checks = []
    seeds = []
    for case in config["cases"]:
        for repeat in range(1, config["repeats"] + 1):
            name = f"rho{case['rho']:.2f}_r{repeat:02d}"
            summary = read_json(root / name / "summary.json")
            assert summary == next(row for row in summaries if row["run_id"] == name)
            seeds.extend([summary["arrival_seed"], summary["service_seed"]])
            check = verify_run(root / name, config, case, repeat)
            checks.append(check)
            print(f"PASS {name} jobs={check['jobs_all']} residual={check['system_identity_residual']:.3g}", flush=True)
    assert len(seeds) == len(set(seeds)), "RNG stream seeds reused"
    result = {"all_passed": True, "kind": "discrete_event_model_calibration", "run_count": len(checks),
              "config_sha256": digest(config), "validated_utc": datetime.now(timezone.utc).isoformat(),
              "validator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "all_offered_jobs": sum(c["jobs_all"] for c in checks),
              "measurement_jobs": sum(c["measurement_jobs"] for c in checks), "runs": checks}
    (root / "validation.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results")
    args = parser.parse_args()
    validate(args.results)
