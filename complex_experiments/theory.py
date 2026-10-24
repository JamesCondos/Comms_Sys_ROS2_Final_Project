"""Queueing predictions and independent, finite-window accounting.

Times are seconds unless a field explicitly ends in ``_ns``.  M/M/c predictions
assume a single pooled FCFS queue, Poisson arrivals, iid exponential service,
identical workers, infinite capacity, and stationarity.  They are diagnostics,
not a replacement for measurements through a multistage network.
"""
from __future__ import annotations

import math
import statistics
from typing import Iterable, Mapping, Sequence


def mmc(arrival_rate: float, service_rate: float, servers: int = 2) -> dict:
    """Erlang-C means; service_rate is the rate of ONE worker.

    Unstable loads return ``None`` for stationary quantities, never a finite
    prediction at or beyond saturation.  Throughput is a capacity bound there.
    """
    if not isinstance(servers, int) or isinstance(servers, bool) or servers < 1:
        raise ValueError("servers must be a positive integer")
    if not math.isfinite(arrival_rate) or arrival_rate < 0:
        raise ValueError("arrival_rate must be finite and nonnegative")
    if not math.isfinite(service_rate) or service_rate <= 0:
        raise ValueError("service_rate must be finite and positive")
    capacity = servers * service_rate
    rho = arrival_rate / capacity
    result = dict(arrival_rate=arrival_rate, service_rate_per_worker=service_rate,
                  servers=servers, capacity=capacity, rho=rho,
                  stable=rho < 1, throughput_bound=min(arrival_rate, capacity),
                  fluid_backlog_growth_per_s=max(0.0, arrival_rate - capacity))
    if rho >= 1:
        result.update(p_wait=None, Wq_s=None, W_s=None, Lq=None, L=None)
        return result
    # Erlang B recursion avoids factorials and overflow; transform B to C.
    offered = arrival_rate / service_rate
    blocking = 1.0
    for n in range(1, servers + 1):
        blocking = offered * blocking / (n + offered * blocking)
    p_wait = blocking / (1 - rho + rho * blocking)
    wq = p_wait / (capacity - arrival_rate)
    w = wq + 1 / service_rate
    result.update(p_wait=p_wait, Wq_s=wq, W_s=w,
                  Lq=arrival_rate * wq, L=arrival_rate * w)
    return result


def coefficient_of_variation(values: Iterable[float]) -> float | None:
    """Sample CV: exponential samples approach 1, not proof of independence."""
    sample = [float(value) for value in values]
    if any(not math.isfinite(value) or value < 0 for value in sample):
        raise ValueError("samples must be finite and nonnegative")
    if len(sample) < 2 or statistics.mean(sample) == 0:
        return None
    return statistics.stdev(sample) / statistics.mean(sample)


def interval_area_ns(intervals: Iterable[tuple[int, int | None]],
                     start_ns: int, end_ns: int) -> int:
    """Integral of occupancy from per-job [entry, exit) intervals.

    None means still present at the END of the observation.  Carry-in, carry-out,
    and jobs crossing the whole window are included, preventing the common
    finite-window error of comparing L only with completed-job residence times.
    """
    if end_ns <= start_ns:
        raise ValueError("observation window must have positive length")
    area = 0
    for entered, departed in intervals:
        if departed is not None and departed < entered:
            raise ValueError("departure precedes entry")
        exit_at = end_ns if departed is None else departed
        area += max(0, min(exit_at, end_ns) - max(entered, start_ns))
    return area


def event_area_ns(events: Sequence[Mapping], start_ns: int, end_ns: int,
                  value_key: str = "queue_len", time_key: str = "time_ns") -> int:
    """Independent right-continuous occupancy integral from state-change log.

    Events contain the state AFTER their event.  The input must include an
    initial state at or before window start. Equal timestamps retain log order.
    """
    if end_ns <= start_ns or not events:
        raise ValueError("positive window and initial occupancy event required")
    last_time = None
    state = None
    cursor = start_ns
    area = 0
    for event in events:
        when, new_state = int(event[time_key]), int(event[value_key])
        if last_time is not None and when < last_time:
            raise ValueError("occupancy events are not ordered")
        if new_state < 0:
            raise ValueError("negative occupancy")
        last_time = when
        if when <= start_ns:
            state = new_state
            continue
        if state is None:
            raise ValueError("missing state at window start")
        if when >= end_ns:
            break
        area += state * (when - cursor)
        cursor, state = when, new_state
    if state is None:
        raise ValueError("missing state at window start")
    return area + state * (end_ns - cursor)


def audit_little(intervals: Iterable[tuple[int, int | None]],
                 events: Sequence[Mapping], start_ns: int, end_ns: int,
                 value_key: str = "queue_len", time_key: str = "time_ns") -> dict:
    """Exact sample-path identity, valid even during overload or packet loss.

    This audits L*T = sum clipped residence using independent logs. It does NOT
    claim that completed-cohort lambda*W exactly equals finite-window L.
    """
    residence_area = interval_area_ns(intervals, start_ns, end_ns)
    state_area = event_area_ns(events, start_ns, end_ns, value_key, time_key)
    return dict(interval_area_ns=residence_area, event_area_ns=state_area,
                residual_ns=state_area - residence_area,
                passed=state_area == residence_area,
                mean_occupancy=state_area / (end_ns - start_ns))


def rate_and_cv_ns(times_ns: Iterable[int]) -> dict:
    """Arrival diagnostics; rate uses first-to-last span, CV uses spacings."""
    times = list(times_ns)
    if times != sorted(times):
        raise ValueError("arrival times must be ordered")
    spacings = [(b - a) / 1e9 for a, b in zip(times, times[1:])]
    span = (times[-1] - times[0]) / 1e9 if len(times) >= 2 else 0
    return dict(count=len(times), spacing_rate_per_s=(len(times)-1)/span if span else None,
                spacing_cv=coefficient_of_variation(spacings))
