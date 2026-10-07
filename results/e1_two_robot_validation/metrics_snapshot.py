"""Boundary-aware metrics shared by the runner and offline verification."""
def clipped_duration_ns(start, finish, begin, end):
    return max(0, min(finish, end) - max(start, begin))


def cpu_percent(samples, field, begin, end):
    """Interpolate cumulative Linux CPU seconds at exact window boundaries.

    100% is one logical CPU, not all CPUs on the host. Sampling granularity and
    Linux clock ticks limit precision; the raw samples remain available.
    """
    def at(t):
        for a, b in zip(samples, samples[1:]):
            ta, tb = int(a["timestamp_ns"]), int(b["timestamp_ns"])
            if ta <= t <= tb:
                fraction = (t - ta) / (tb - ta)
                return float(a[field]) + fraction * (float(b[field]) - float(a[field]))
        raise ValueError("CPU samples do not bracket measurement window")
    return 100 * (at(end) - at(begin)) / ((end - begin) / 1e9)


def deadline_stats(offered, received, deadline_ms):
    """Deadline failure includes loss; successful-only tardiness is also exposed."""
    by_id = {r["id"]: r for r in received}
    on_time = late = not_completed = 0
    for source in offered:
        row = by_id.get(source["id"])
        if row is None or row["outcome"] != "ok":
            not_completed += 1
        elif (int(row["response_ns"]) - int(source["source_ns"])) <= deadline_ms * 1e6:
            on_time += 1
        else:
            late += 1
    total = len(offered)
    return {"deadline_ms": deadline_ms, "generated": total, "on_time": on_time,
            "late_successful": late, "not_completed": not_completed,
            "misses": late + not_completed,
            "miss_ratio": (late + not_completed) / total if total else None,
            "late_fraction_of_successful": late / (late + on_time) if late + on_time else None}
