# TCP result review

All raw-log and socket-selection checks passed.

| Algorithm | Load | Loss % | Repeat | Generated | Successful incl. drain | Completed/s | p95 (s) | Mean queue wait (s) | Verified WAN sockets |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cubic | 1 | 0 | 1 | 640 | 640 | 58.20 | 7.075 | 4.267 | 4 |
| reno | 1 | 0 | 1 | 640 | 640 | 58.20 | 7.102 | 4.278 | 4 |

Successful cohort counts include drain; completion rates count responses inside the measurement window.
Socket settings confirm that the requested algorithms were applied. Similar application performance does not establish equivalent TCP behavior.
Retransmissions and congestion-window dynamics have not been inferred from capture-drop counters.

## Figures

- [comparison_load1_loss0.png](comparison_load1_loss0.png)
- [comparison_load1_loss0.pdf](comparison_load1_loss0.pdf)
