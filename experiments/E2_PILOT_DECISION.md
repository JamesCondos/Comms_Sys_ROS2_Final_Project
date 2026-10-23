# E2B pilot decision, 23 October 2026

The first pilot revealed a recording lifecycle error: the ON/OFF source's last
ON interval ends before the configured observation horizon, and the recorder
could stop during the final OFF interval. CPU samples consequently did not
bracket the complete measurement window. The failed attempt and its traceback
remain in `e2_live_pilot`; none of its runs contribute to final statistics.

The corrected source waits to the absolute observation horizon before its quiet
shutdown interval. Independently, the gateway runner also waits until the
measurement end before evaluating final drain completion. A regression test
covers the burst model's silent tail and early wakeups. Eight live-runner tests
passed both on Windows and in the Linux execution environment.

A fresh four-condition pilot in `e2_live_pilot2` used 5 s warm-up, 20 s
measurement and at most 30 s drain. All four application validations passed,
with no missing, dropped, errored or unresolved messages. Strict capture
validation reconciled all 12,901 HTTP exchanges, including warm-up and drain.
All temporary experiment network namespaces were absent after execution.

| Pattern | Actual offered/s | Worker busy fraction | Source scheduling lag p95 (ms) |
|---|---:|---:|---:|
| Staggered periodic | 128.00 | 0.6426 | 0.178 |
| Synchronized periodic | 128.00 | 0.6405 | 1.765 |
| Poisson | 130.45 | 0.6569 | 0.194 |
| ON/OFF Poisson | 130.75 | 0.6494 | 0.235 |

This verifies stable spare capacity and usable instrumentation under the
preselected workload. Retain the proposed four actual sources, 4 ms server
hold, FIFO capacity 1,000 and unshaped link. No arrival pattern, random seed,
traffic rate or service parameter was adjusted to obtain an outcome ranking.
The formal configuration remains 20 s warm-up, 120 s measurement, up to 60 s
drain, five repetitions per pattern, with reproducibly shuffled order.

All pilot measurements are excluded from the formal report's statistics.
