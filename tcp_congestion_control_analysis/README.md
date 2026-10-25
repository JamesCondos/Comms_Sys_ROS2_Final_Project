# TCP congestion-control trials

This folder contains the TCP experiment settings, sweep runner, plots and results.
The **only runtime dependency inside this repository** is `https_protocol_analysis`.
Both teammates now use that folder's ROS traffic, HTTPS transports and topology.
See the [root setup instructions](../README.md).

```text
run_tcp_congestion.sh   Loads ROS and selects the shared Python environment
run_trials.py          Builds and shuffles algorithm/load/loss repetitions
configs/               Small overrides of the shared settings
plot_results.py        Checks raw logs and produces application/TCP graphs
plot_transmission_rate.py  Plots the single-robot WAN transmission rate over time
results/               All TCP plans, raw logs, captures and graphs
tests/                 Development checks; not needed to run experiments
```

## Starting

The topology has four actual ROS robot/gateway processes, two pass-through edges,
two site routers, one shared proxy FIFO and two workers. TCP trials change only
**the edges' outgoing WAN sender sockets**. Other TCP connections remain CUBIC.
The default protocol is HTTP/1.1 over TLS 1.3; an `h2` preset also works. HTTP/3
uses QUIC and is deliberately rejected by the kernel TCP trial runner.

The default set is Reno, CUBIC and BBR. The kernel must support every requested
algorithm. On the current host only Reno/CUBIC are registered, so use the explicit
two-algorithm option until BBR is available. Unsupported requests stop before
network setup; nothing is silently substituted or installed.

```bash
bash tcp_congestion_control_analysis/run_tcp_congestion.sh \
  --dry-run --repeats 1 --output shared_plan_01

sudo bash tcp_congestion_control_analysis/run_tcp_congestion.sh \
  --tcp reno cubic --duration 5 --warmup 1 --drain 8 --repeats 1 \
  --output tcp_shared_smoke_01
```

The launcher prefers `https_protocol_analysis/.venv/bin/python`. `TCP_PYTHON`
can explicitly select another configured interpreter. No colcon workspace is used.
All output is restricted to this folder's `results/`: `--output name` and
`--output results/name` both mean `tcp_congestion_control_analysis/results/name`.
Existing directories are never overwritten. The default sweep has nine runs:
three algorithms and three repeats at one load/loss setting.

## First trial: one robot and a rate-over-time graph

```bash
sudo bash tcp_congestion_control_analysis/run_tcp_congestion.sh \
  --sources 1 --tcp reno cubic bbr \
  --duration 60 --warmup 0 --drain 15 --repeats 1 \
  --delay-ms 10 --bandwidth-mbit 2 --output single_robot_rate_01
```

Only robot1 generates traffic. The active application path is robot1 -> edgeA
-> routerA -> proxy -> workers. The other robots and edgeB are not started.
Both workers and the shared routing infrastructure remain available, so this
uses the existing implementation rather than a separate TCP benchmark.

The runner automatically saves `analysis/transmission_rate.png` and
`analysis/transmission_rate.csv`. Blue is Reno, red is CUBIC and green is BBR.
The x axis is seconds since traffic started, including any warm-up. The y axis
is observed TCP payload transmission rate in Mbit/s on edgeA's WAN connection.
This includes HTTPS/TLS data overhead; it is not application payload goodput
or a count of physical Ethernet bytes. Drain traffic is excluded from this graph.

The graph subtracts consecutive Linux TCP_INFO bytes_sent counters and groups
them into one-second intervals. Counter increases spanning a boundary are
shared proportionally across that interval. A socket becomes observable after
its first response, so the first transfer's bytes and very early startup are
not measured. The CSV includes sampled_seconds to expose partial coverage;
missing intervals are gaps, not invented zero-rate measurements. Existing
runs without sent-byte counters must be rerun to obtain this graph.

These are measured curves with no artificial smoothing or imposed sawtooth.
HTTP request/response timing, the offered workload and worker processing may
limit throughput before TCP congestion does. A flat curve alone does not show
that an algorithm is faulty. Use --loads to increase traffic after checking
this baseline, while keeping the robot count at one.

Regenerate the graph from an existing completed single-robot sweep:

```bash
https_protocol_analysis/.venv/bin/python -B tcp_congestion_control_analysis/plot_transmission_rate.py \
  tcp_congestion_control_analysis/results/single_robot_rate_01
```

## Controlled comparisons

`--loads` multiplies the shared total publication rate (default 148 messages/s,
including JPEG video). It does not increase robot count. `--sources`/`--robots`
accept one or four. The default is four; single-robot mode keeps only robot1
active at site A and uses one persistent edge-to-proxy TCP connection. Its base
load is 37 messages/s, keeping the normal per-robot traffic rate. All other traffic, concurrency,
service and queue settings stay fixed between algorithms.

```bash
sudo bash tcp_congestion_control_analysis/run_tcp_congestion.sh \
  --tcp reno cubic --loads 1 2 4 --delay-ms 10 --bandwidth-mbit 2 \
  --duration 60 --repeats 3 --output shared_load_01

sudo bash tcp_congestion_control_analysis/run_tcp_congestion.sh \
  --tcp reno cubic --loss-percent 0 0.1 1 --duration 60 --repeats 3 \
  --output shared_loss_01

sudo bash tcp_congestion_control_analysis/run_tcp_congestion.sh \
  --tcp reno cubic --duration 60 --loss-burst 20 25 --burst-loss-percent 100 \
  --repeats 3 --output shared_recovery_01
```

Static loss affects both forward uplinks; the default is zero. --delay-ms sets
added delay in both directions and --bandwidth-mbit sets forward capacity per
site. Burst times are relative to measurement start. The original forward
profile is restored at the end. Change times are logged, and failures remain
in the data. No application operation is retried. Run order is shuffled; source
seeds match algorithms within each repetition. Do not run other experiments
concurrently. Pilot the load settings before drawing congestion conclusions.

## Results and graphs

Each sweep retains its plan/configs, a comparison CSV and the trial code snapshot.
Each individual run retains source, gateway, queue and worker JSONL logs, actual
TCP socket settings/counters, one-second throughput, summaries, packet captures,
process/network cleanup records and shared-source snapshots.

Graphs are generated automatically when matplotlib is installed:

- Offered and completed message rates.
- Successful-message p95 latency and mean latency breakdown.
- Gateway waiting-queue traces.
- Sampled RTT, congestion windows and cumulative observed retransmission counters.

The application latency cohort includes responses completed during drain; window
throughput does not. Repetition bars are observed min–max ranges, not confidence
intervals. Kernel retransmission counters are sampled, so sockets closed before
sampling may be missed. Full packet-based retransmission classification and a
predefined numerical recovery-time criterion are not automatically supplied.

Regenerate a completed sweep's graphs:

```bash
https_protocol_analysis/.venv/bin/python -B tcp_congestion_control_analysis/plot_results.py \
  tcp_congestion_control_analysis/results/tcp_shared_smoke_01
```

Use sudo for regeneration only if the output directory is root-owned. Plotting
failure is recorded separately and does not invalidate saved measurements.

The retained `tcp_smoke_01` data came from the older partner-network runtime.
Its raw files and original source snapshots are unchanged. Those measurements
must not be pooled with this HTTPS/concurrent runtime's results. The graph reader
continues to support them. The relocation record documents the verified copy.

## Checks and cleanup

```bash
https_protocol_analysis/.venv/bin/python -B -m unittest discover -s https_protocol_analysis -p 'test_*.py' -v
https_protocol_analysis/.venv/bin/python -B -m unittest discover -s tcp_congestion_control_analysis/tests -p 'test_*.py' -v
```

If necessary, run cleanup for one individual run, not the sweep root:

```bash
sudo -E https_protocol_analysis/.venv/bin/python -m https_protocol_analysis.runner cleanup \
  --output tcp_congestion_control_analysis/results/tcp_shared_smoke_01/run_001_reno
```

Replace that example with the actual directory; run order is shuffled.
