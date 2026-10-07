# Completed E1 results

[Report-ready paragraphs](report_blurbs.md) · [Figure selection and captions](figure_guide.md)

Percentiles below are means of per-run percentiles, not percentiles of a pooled dataset. Drops are pooled over generated messages at each load.

| Load equivalents | Offered/s | Completed/s | p50 ms | p95 ms | p99 ms | Mean queue | Goodput Mbit/s | Drops % | Server CPU % |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 32.0 | 32.0 | 3.56 | 4.54 | 5.45 | 0.03 | 0.85 | 0.00 | 2.4 |
| 2 | 64.0 | 64.0 | 3.63 | 4.99 | 6.05 | 0.07 | 1.69 | 0.00 | 4.6 |
| 4 | 128.0 | 128.0 | 3.30 | 4.99 | 5.87 | 0.15 | 3.38 | 0.00 | 7.4 |
| 8 | 256.0 | 256.0 | 3.70 | 6.37 | 7.95 | 0.44 | 6.76 | 0.00 | 15.1 |
| 16 | 512.0 | 512.1 | 2.58 | 4.42 | 8.54 | 0.65 | 13.53 | 0.00 | 17.7 |
| 32 | 1024.0 | 601.8 | 1361.06 | 2329.82 | 2453.45 | 782.67 | 11.99 | 40.68 | 23.4 |
| 64 | 2047.9 | 547.5 | 4363.20 | 4689.89 | 4811.69 | 999.69 | 7.62 | 73.27 | 14.7 |

CPU percentages are relative to one logical CPU. Source rates are scaled across at most two namespaced robots. Full statistics are in `summary_table.csv` and per-class deadline results are in `class_deadlines.csv`. All repetitions are listed in `individual_runs.csv`; the large spread at high loads must be discussed, not hidden by the means.

## Recommended figures

![Successful-message latency](figures/fig1_latency.png)

![Deadline failures](figures/fig2_deadlines.png)

![Queue occupancy](figures/fig3_queue.png)

![Completed throughput](figures/fig4_throughput.png)

![Process CPU consumption](figures/fig5_cpu.png)

![Sender and server activity](figures/fig6_busy_time.png)
