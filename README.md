# ROS2 REST communication baseline

For the Reno, CUBIC, and BBR socket-selection runner, see
[the TCP congestion-control experiment guide](docs/tcp_congestion_experiment.md).

Experiment E1 is complete for the clean-network scope in the project workbook.
Use the new dataset below for the report. The earlier `baseline_formal` dataset
is retained as preliminary data and must not be pooled with the new results.

- [Report-ready paragraphs](results/e1_complete/report_blurbs.md)
- [Results table and main figures](results/e1_complete/report.md)
- [Which figures to include, with captions](results/e1_complete/figure_guide.md)
- [Completion checklist](results/e1_complete/completion_checklist.md)
- [Fixed experimental protocol and definitions](experiments/E1_PROTOCOL.md)
- [Packet accounting evidence](results/e1_packet_evidence/README.md)

The completed sweep contains 21 runs: seven offered loads, three repetitions each,
with five-second warm-ups and 60-second measurement windows. A separate five-minute
run verifies two ROS2 robot namespaces and all eight topic rates.

There is substantial variation between the high-load runs, including a change in
execution performance during the first 32-equivalent repetition. Every run is
retained, error bars show the complete min-max range, and the report does not
claim a fixed service capacity. The cause of the host-performance change has not
been established. Use 16 equivalents as the measured no-overflow reference and
64 equivalents as the stress setting; recalibrate after implementation or host changes.

## Build and run

The main E1 code is `experiments/baseline.py` (server, gateway and run summaries),
`ros2_ws/src/rest_traffic/rest_traffic/generator.py` (ROS2 traffic), and
`experiments/metrics.py` (CPU and deadline calculations). Detailed log checks
are separate: run `experiments/validate_results.py` after an experiment.
New summaries contain measurements; validation results go in `validation.json`.
The saved E1 datasets retain their original source snapshots and results.

Ubuntu 22.04 with ROS2 Humble, `rclpy`, `std_msgs`, `colcon`, `iproute2` and `ping`
are required. No global packages were installed during this work.

From the repository root in Linux:

```bash
source /opt/ros/humble/setup.bash
cd ros2_ws
colcon build --symlink-install --packages-select rest_traffic
cd ..
bash scripts/run_baseline.sh
```

The sweep takes approximately 25 minutes. Results get a unique new directory;
existing runs are never overwritten. The runner checks that loopback uses `noqueue`
and saves a ping check. E1 does not apply or alter any impairment settings.

A short functional check:

```bash
bash scripts/run_baseline.sh --robots 2 64 --duration 5 --repeats 1
```

A reusable stress experiment:

```bash
bash scripts/run_baseline.sh --config experiments/configs/e1_stress.json
```

The five-minute stability configuration is
`experiments/configs/e1_two_robot_validation.json`. The standalone source package
can also be launched for inspection:

```bash
source /opt/ros/humble/setup.bash
source ros2_ws/install/setup.bash
export ROS_DOMAIN_ID=161 ROS_LOCALHOST_ONLY=1
ros2 run rest_traffic traffic_sources --config experiments/baseline.json --robots 2 --duration 300
```

For the executed copy on this workstation, run from PowerShell:

```powershell
wsl -d Ubuntu-22.04 --cd /home/jcondos/comm_nets_e1_complete -- bash scripts/run_baseline.sh
```

Google Drive's G: mount is not directly translated by WSL. The Linux execution
copy is `/home/jcondos/comm_nets_e1_complete`; Windows staging is under
`C:\Users\james\comm_nets_e1_complete`. Result copies in this project are verified
against the original Linux SHA-256 manifests.

## Regenerate and verify

Python with matplotlib is needed for the figures; this can run on Windows:

```bash
python experiments/test_metrics.py
python experiments/validate_results.py results/e1_complete
python experiments/report_e1.py results/e1_complete
python experiments/variability_e1.py results/e1_complete
python experiments/validate_results.py results/e1_two_robot_validation
python experiments/packet_accounting.py results/e1_packet_evidence/two_robot_full.pcap results/e1_two_robot_validation
```

`report_e1.py` writes only derived reports, tables and PNG/PDF/SVG figures. It
requires three repetitions per load. `analyze.py` is retained for the preliminary
version; use `report_e1.py` for the final E1 dataset.

Each current figure file contains one plot: six main plots and five appendix
plots, including the already standalone queue trace. The figure guide lists
their filenames and captions. Previous combined plots are retained separately
under `results/e1_complete/figures/archive_combined/`.

## What is measured

One ROS source process hosts up to two actual ROS2 nodes, each with its own robot
namespace and four separate topics. Load 1 uses one robot at the base rates; load
N >= 2 uses two robots at N/2 times the base rates. The workbook permits equivalent
rates. These labels do not represent N independent HTTP clients or physical robots.

The gateway has eight subscriptions at most, one FIFO with 1,000 waiting slots,
and one sender with one outstanding HTTP/1.1 request. The separate server returns
the composite message ID and receipt timestamp. HTTP is cleartext, server
TCP_NODELAY is enabled, compression is off, and application retries are disabled.
ROS uses reliable KEEP_LAST depth 1,000. Queue overflow drops the arriving message.

Latency is source publication to complete response receipt. Raw timestamps also
separate ROS2 delivery/callback delay, queue waiting and the HTTP exchange. All
processes share the Linux host monotonic clock. Successful-message percentiles
include measurement-window messages completed during the bounded drain, whereas
throughput/goodput count completions inside the measurement window.

Deadline failure counts all generated messages in the denominator and includes
late completions, drops, failures and missing messages. The illustrative targets
are command 100 ms, status 200 ms, odometry 100 ms and sensor 1,000 ms. They are
fixed project design choices, not robot safety requirements.

CPU use is sampled from Linux process counters every 0.2 seconds and normalised
to one logical CPU (100% = one CPU). Separately measured sender and server-handler
busy fractions describe occupied wall time. They are not interchangeable metrics.
Queue occupancy is time-weighted and excludes the request in service.

Every run saves configs, topic mappings, source/gateway/server CSVs, queue events,
CPU samples and summaries. Source snapshots and configuration hashes establish
provenance. Logs are buffered in memory and saved at run end; an abrupt process
kill can lose the current run. Config hashes reside in each run's metadata rather
than being repeated in every CSV row.

## Files changed or added for completion

- `experiments/baseline.py`, `baseline.json`: namespaced source integration, CPU,
  busy fractions, deadline metrics, snapshots, config hashing and CLEAN checks.
- `experiments/metrics.py`, `test_metrics.py`: metric definitions and boundary tests.
- `experiments/validate_results.py`: cross-log, topic, source-hash and metric checks.
- `experiments/report_e1.py`: final tables, paragraphs, captions and figures.
- `experiments/variability_e1.py`: reproducible within-run variability appendix.
- `experiments/packet_accounting.py`: captured IPv4/TCP byte accounting.
- `experiments/verify_topics.py`: actual `ros2 topic hz` evidence collection.
- `experiments/configs/`: stability, no-overflow reference and stress presets.
- `experiments/E1_PROTOCOL.md`: frozen targets, populations and scope.
- `ros2_ws/src/rest_traffic/`: installable ROS2 traffic package.
- `scripts/run_baseline.sh`: updated entry point loading the built ROS2 package.
- `results/e1_complete`, `e1_two_robot_validation`, `e1_packet_evidence`,
  `e1_smoke_v2`: new raw datasets and derived report artifacts.
- `README.md`: current implementation and reproduction instructions.

## Scope remaining

This completes E1's clean-network experiment, including utilisation and deadlines.
It does not mark the entire foundation/M2 gate complete: DELAY/LOSS/BW impairment
profiles, Git/lecturer approval milestones and later comparison experiments remain
separate work. No claims are made about physical-link capacity, M/M/1 assumptions,
HTTP/2 superiority, priority scheduling or ARQ performance.
