# Baseline experiment results

[Interpretation and main findings](findings.md)

Actual ROS2 Humble publishers and subscriber in Ubuntu 22.04 WSL2, with a separate local HTTP server. One shared bounded FIFO, one sender, one outstanding HTTP request, no application retries or injected network faults.

Each condition has a 5-second warm-up, 60-second measurement window (unless overridden in saved config), and up to 15 seconds to drain. Load order is seeded and shuffled. Publishers use seeded independent periodic phases. All clocks are the same Linux host's monotonic clock.

| Robots | Runs | Offered msg/s | Completed msg/s | p95 ms, mean [min–max] | Mean FIFO length | Goodput Mbit/s | Drops | ROS missing |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 3 | 32.0 | 32.0 | 3.21 [3.08–3.39] | 0.016 | 0.846 | 0 | 0 |
| 2 | 3 | 64.0 | 64.0 | 3.43 [3.27–3.54] | 0.037 | 1.691 | 0 | 0 |
| 4 | 3 | 128.0 | 128.0 | 3.41 [3.12–3.89] | 0.082 | 3.382 | 0 | 0 |
| 8 | 3 | 256.0 | 256.0 | 3.32 [2.97–3.80] | 0.164 | 6.765 | 0 | 0 |
| 16 | 3 | 512.0 | 512.0 | 3.51 [3.27–3.69] | 0.416 | 13.530 | 0 | 0 |
| 32 | 3 | 1024.0 | 1024.0 | 6.45 [5.74–7.15] | 2.125 | 27.059 | 0 | 0 |
| 64 | 3 | 2048.0 | 1250.8 | 1163.36 [1105.03–1210.44] | 997.969 | 33.797 | 143476 | 0 |

## Per-class latency

Mean of each run's source-to-response p95, in milliseconds. Successful messages only.

| Robots | Command | Status | Odometry | Sensor |
|---:|---:|---:|---:|---:|
| 1 | 3.34 | 2.79 | 3.22 | 3.17 |
| 2 | 3.20 | 3.21 | 3.41 | 3.50 |
| 4 | 3.46 | 3.44 | 3.36 | 3.36 |
| 8 | 3.48 | 3.61 | 3.28 | 3.32 |
| 16 | 3.70 | 3.55 | 3.41 | 3.62 |
| 32 | 6.70 | 6.35 | 6.34 | 6.75 |
| 64 | 1158.69 | 1156.06 | 1164.66 | 1168.52 |

## Delay components and delivery

Mean delays across runs, in milliseconds. Delivery includes the drain period; drops are summed across repeats.

| Robots | ROS to gateway | FIFO wait | HTTP exchange | Successful/generated | FIFO drop fraction |
|---:|---:|---:|---:|---:|---:|
| 1 | 0.974 | 0.516 | 0.977 | 5760/5760 | 0.00% |
| 2 | 0.934 | 0.582 | 0.955 | 11520/11520 | 0.00% |
| 4 | 0.835 | 0.647 | 0.879 | 23040/23040 | 0.00% |
| 8 | 0.672 | 0.647 | 0.742 | 46081/46081 | 0.00% |
| 16 | 0.568 | 0.817 | 0.654 | 92160/92160 | 0.00% |
| 32 | 0.494 | 2.079 | 0.659 | 184320/184320 | 0.00% |
| 64 | 0.454 | 798.615 | 0.791 | 225163/368639 | 38.92% |

![Baseline plots](baseline.png)

![Queue occupancy](queue_occupancy.png)

## Definitions and limitations

- Latency: source publication timestamp to complete HTTP response receipt. Queue wait: gateway enqueue to dequeue. HTTP time includes JSON serialization and the exchange. Source-to-gateway delay includes ROS transport and callback scheduling.
- Latency statistics use successfully completed messages generated during the measurement window, including those completed during drain. Throughput counts completions inside the measurement window, so the populations intentionally differ.
- FIFO length is integrated over enqueue/dequeue events, excludes the request in service, and is bounded at 1,000 messages. Overflow drops the arriving message. ROS additionally uses reliable KEEP_LAST depth 1,000.
- Payload goodput counts only configured payload bytes, excluding JSON metadata, HTTP, TCP/IP and ROS overhead. The payload is repeated ASCII bytes; no compression is enabled.
- Each robot contributes 32 messages/s and nominally 0.8456 Mbit/s of application payload. These are synthetic robot identities/topics carried through one source process; they are not separate physical computers.
- This is a local WSL2 architecture baseline, not physical-link capacity or a real robot benchmark. Publisher, gateway and server share host CPU. There is no artificial server processing delay.
- HTTP is cleartext, with no TLS or compression. The server enables TCP_NODELAY. The client uses Python's standard-library HTTPConnection, with a two-second request timeout and no replay on failure.
- The plots report means of per-run statistics with min–max ranges; these are not confidence intervals or pooled percentiles. Three repeats and approximately 120 command samples per robot per run limit p99 precision.
- No M/M/1 fit is claimed. Periodic mixed-size arrivals and measured HTTP service times do not establish its assumptions.
- Missing ROS messages, overflow drops, failed requests and pending messages must be considered alongside successful-message latency.

## Validation

All 21 completed runs passed identity uniqueness, source/gateway/server matching checks, timestamp ordering, HTTP/1.1 verification and exactly one accepted HTTP connection per run.
Total request errors: 0. Pending after drain: 0.
