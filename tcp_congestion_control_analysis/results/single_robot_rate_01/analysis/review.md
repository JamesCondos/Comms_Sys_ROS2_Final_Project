# TCP result review

All raw-log and socket-selection checks passed.

| Algorithm | Load | Loss % | Repeat | Generated | Successful incl. drain | Completed/s | p95 (s) | Mean queue wait (s) | Verified WAN sockets |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cubic | 1 | 0 | 1 | 2220 | 1330 | 17.73 | 36.997 | 19.321 | 2 |
| bbr | 1 | 0 | 1 | 2220 | 1328 | 17.73 | 37.107 | 19.325 | 2 |
| reno | 1 | 0 | 1 | 2220 | 1330 | 17.73 | 37.017 | 19.340 | 2 |

Successful cohort counts include drain; completion rates count responses inside the measurement window.
Socket settings confirm that the requested algorithms were applied. Similar application performance does not establish equivalent TCP behavior.
Retransmissions and congestion-window dynamics have not been inferred from capture-drop counters.

## Figures

- [comparison_load1_loss0.png](comparison_load1_loss0.png)
- [comparison_load1_loss0.pdf](comparison_load1_loss0.pdf)
- [tcp_samples_load1_loss0.png](tcp_samples_load1_loss0.png)
- [tcp_samples_load1_loss0.pdf](tcp_samples_load1_loss0.pdf)
