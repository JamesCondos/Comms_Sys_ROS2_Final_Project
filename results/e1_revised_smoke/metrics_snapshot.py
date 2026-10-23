"""Measurements used by the experiment and result checker."""


def clipped_duration_ns(start, finish, begin, end):
    return max(0, min(finish, end) - max(start, begin))


def cpu_percent(samples, field, begin, end):
    """Estimate CPU use between the boundaries. 100% means one logical CPU."""

    def cpu_at(timestamp):
        for before, after in zip(samples, samples[1:]):
            before_ns = int(before["timestamp_ns"])
            after_ns = int(after["timestamp_ns"])

            if before_ns <= timestamp <= after_ns:
                fraction = (timestamp - before_ns) / (after_ns - before_ns)
                before_cpu = float(before[field])
                after_cpu = float(after[field])

                return before_cpu + fraction * (after_cpu - before_cpu)

        raise ValueError("CPU samples do not bracket measurement window")

    duration_seconds = (end - begin) / 1e9

    return 100 * (cpu_at(end) - cpu_at(begin)) / duration_seconds


def deadline_stats(offered, received, deadline_ms):
    """Count late and missing messages as deadline misses."""

    received_by_id = {row["id"]: row for row in received}
    on_time = late = not_completed = 0

    for source in offered:
        row = received_by_id.get(source["id"])

        if row is None or row["outcome"] != "ok":
            not_completed += 1
            continue

        delay_ns = int(row["response_ns"]) - int(source["source_ns"])

        if delay_ns <= deadline_ms * 1e6:
            on_time += 1
        else:
            late += 1

    total = len(offered)
    misses = late + not_completed
    successful = late + on_time

    return {
        "deadline_ms": deadline_ms,
        "generated": total,
        "on_time": on_time,
        "late_successful": late,
        "not_completed": not_completed,
        "misses": misses,
        "miss_ratio": misses / total if total else None,
        "late_fraction_of_successful": late / successful if successful else None,
    }
