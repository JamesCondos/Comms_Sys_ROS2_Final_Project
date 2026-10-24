# TCP congestion-control trials

These trials use **`partner_network` as the only live ROS/HTTP implementation**.
The launcher does not use colcon, `ros2_ws`, or `experiments/baseline.py`.

```text
tcp_congestion_control_analysis/
├── run_tcp_congestion.sh       Loads installed ROS 2 Humble and starts the sweep
├── run_trials.py               Config generation, shuffled repeats and comparison CSV
├── plot_results.py             Raw-log audit and automatic comparison graphs
├── results/                    All TCP plans, raw runs, reports and graphs
├── configs/tcp_cubic.json      Full two-site topology with controlled WAN conditions
├── configs/tcp_reno.json       Same conditions, Reno WAN senders
├── configs/tcp_bbr.json        Same conditions, BBR WAN senders (kernel support needed)
├── test_trials.py             Trial-control and planning tests
└── tcp_congestion.py          Retained legacy socket helper; not used by this runner
```

## Shared architecture

```text
Robots 1–2 → Edge A → Router A ─┐
                              ├→ Proxy → Workers 1 and 2
Robots 3–4 → Edge B → Router B ─┘
```

Each robot has an independent ROS source, FIFO gateway and HTTP connection.
The edge forwards each request and waits for the central worker's response.
The edge's outgoing TCP connections cross the WAN, so **only the edge socket
algorithm changes** in full-topology comparisons. Robot, proxy and worker
algorithms remain fixed. For an explicitly selected T1/T2 base config, the
runner instead changes robot sockets because those are the WAN senders.
Socket logs record actual endpoints and requested/verified TCP algorithms.

The default profile uses four actual sources, two workers, 20 ms added delay
per direction and a 2 Mbit/s forward link at each site. Traffic, queue limits,
server service time, timeout and connection policy stay fixed across algorithms.
These are starting settings, not a calibrated formal experiment. Edges pass
messages through; they do not batch or filter them. The protocol is HTTP/1.1.

## Prerequisites

Use the runtime prerequisites in [partner_network/README.md](../partner_network/README.md).
From the repository root:

```bash
bash partner_network/run.sh doctor
cat /proc/sys/net/ipv4/tcp_available_congestion_control
```

All TCP sweeps default to **Reno, CUBIC and BBR** (`--tcp reno cubic bbr`).
The algorithm is BBR, not BRR. If any requested algorithm is unavailable, a live sweep stops before
creating output or network resources. There is no silent substitution and no
system-wide TCP setting is changed. The runner does not install/load BBR.
The current kernel reports only `reno cubic`; BBR support must be enabled in
the kernel before a live three-algorithm sweep can run. A dry run can still
preview all three algorithms and explicitly records unavailable ones.

## Preview and smoke test

The commands below run from the repository root. The launcher also works from
other directories. Config paths are relative to your current directory. All TCP
outputs are restricted to `tcp_congestion_control_analysis/results/`.
`--output tcp_smoke_02` and the older `--output results/tcp_smoke_02` syntax both
select a directory inside that section. Omitting `--output` creates a unique
`results/tcp_*` directory inside the TCP section. Absolute output paths must also
be inside that results folder; attempts to escape it are rejected. Always choose
a fresh output directory.

First inspect a complete plan without running the network or requiring root:

```bash
bash tcp_congestion_control_analysis/run_tcp_congestion.sh \
  --dry-run --tcp reno cubic bbr --repeats 1 --output tcp_plan_01
```

Then run a short functional comparison:

```bash
sudo bash tcp_congestion_control_analysis/run_tcp_congestion.sh \
  --tcp reno cubic bbr --warmup 1 --duration 5 --drain 8 --repeats 1 \
  --output results/tcp_three_algorithm_smoke_01
```

Root is required for the harness's temporary namespaces, routes and captures.
The harness owns uniquely named resources and cleans them up after each run.
The default sweep is nine runs: three algorithms, three repetitions, one load/loss
condition, with 10 s warm-up, 60 s measurement and up to 30 s drain per run.

## Increasing congestion

Increase offered traffic while keeping the topology and link settings fixed:

```bash
sudo bash tcp_congestion_control_analysis/run_tcp_congestion.sh \
  --tcp reno cubic bbr --loads 1 2 4 --loss-percent 0 \
  --delay-ms 5 --bandwidth-mbit 1 --duration 60 --repeats 3 \
  --output results/tcp_load_sweep_01
```

`--loads` multiplies every source's four publication rates. It does not create
extra ROS nodes. `--sources 4` (alias `--robots 4`) sets actual clients, supports
1–16, and divides them between sites. It does not use the old baseline's
robot-equivalent scaling. Default client identities and site assignments are
preserved unless `--sources` is supplied.

## Packet loss and recovery

Hold offered load fixed while changing forward loss probability at both sites:

```bash
sudo bash tcp_congestion_control_analysis/run_tcp_congestion.sh \
  --tcp reno cubic bbr --loads 1 --loss-percent 0 0.1 1 \
  --duration 60 --repeats 3 --output results/tcp_loss_sweep_01
```

Or introduce a timed forward-path loss episode and observe recovery afterwards:

```bash
sudo bash tcp_congestion_control_analysis/run_tcp_congestion.sh \
  --tcp reno cubic bbr --duration 60 --repeats 3 \
  --loss-burst 20 25 --burst-loss-percent 100 \
  --output results/tcp_recovery_01
```

Burst times are seconds after measurement begins. Both site forward profiles
are changed in sequence; actual application times are logged. At the end,
each site's static loss/rate/delay profile is restored. Reverse traffic is not
suppressed. The runner requires time after the burst for recovery observation.
The CLI's `--loss-percent` defaults to zero and explicitly sets forward static
loss at both sites, even when loading a custom config. `--delay-ms` sets both
directions; `--bandwidth-mbit` sets the forward capacity at each site.

All algorithm/load/loss/repetition combinations are shuffled with a recorded
seed. Within each repetition, algorithms share the same source seed. There are
no automatic reruns that select favorable results. A failed run stops the sweep
and retains its logs and a failed sweep status.

## Outputs and interpretation

- `plan.json`, `configs/`: planned order and fully resolved per-run settings.
- `environment.json`: prerequisite and algorithm availability checks.
- `trial_runner_snapshot.py`, `trial_provenance.json`: sweep-code provenance.
- `comparison.csv`: separate rows for algorithm/load/loss/repeat, throughput,
  useful-payload goodput, successful-message p95/p99 latency and outcomes.
- `sweep_status.json`: completed-run count and final success/failure.
- `analysis/`: PNG/PDF comparison graphs, `review.md` and raw-log/socket-selection
  checks in `evidence.json`. Comparisons remain separate by load and loss setting.
- `plot_status.json`: automatic plotting success/failure. Plotting failure does
  not invalidate the saved measurement files.
- Each `run_*/`: application/queue logs, per-class deadlines, socket settings,
  independent accounting checks, network state, PCAPs and cleanup evidence.

Example packet-capture paths are `run_001_reno/network/siteA_wan.pcap` and
`siteB_wan.pcap`. Inspect these with Wireshark for retransmissions and throughput
over time. Retain logs of both failed requests and central processing: a timeout
may occur after the server has already processed a message. Application retries
are disabled; normal TCP recovery remains active.

**Retransmission counts and recovery time are not automatically computed by this
runner.** Define their measurement boundaries before formal trials. Capture
kernel-drop counters describe observation loss, not TCP retransmissions.
`comparison.csv` reports aggregate application performance, not a TCP time series.
The queue graph reconstructs waiting-message counts from every robot's event log.
Latency includes successful measurement-cohort messages completed during drain;
throughput counts only responses completed inside the measurement window.

Before interpreting algorithm differences, calibrate the link and inspect queue
and traffic traces. Serial HTTP requests may be application/RTT/server limited,
even when the gateway queue grows. Equal performance does not establish that
algorithms behave identically. The lack of `ethtool` is recorded; without
verified offload disabling, virtual capture records are not physical wire frames.

## Graphs and result review

Graphs are generated automatically after a successful sweep if matplotlib is
installed for the Python used by the launcher. They show offered/completed
throughput, successful-message p95 latency, gateway queue traces and a breakdown
of mean latency. Repeated conditions use means and observed min–max ranges of
run statistics; single runs have no error bars. Latency is undefined for runs
without successes, and missing latency is not interpreted as zero performance.

Regenerate the smoke-test review from the repository root:

```bash
python3 -B tcp_congestion_control_analysis/plot_results.py \
  tcp_congestion_control_analysis/results/tcp_smoke_01
```

Use `sudo` for regeneration only if the run directory is still owned by root.
Raw measurements are read without rewriting them. Saved absolute paths describe
the original execution location and are deliberately preserved after relocation.
A hash-verified copy of the first smoke test is now inside the TCP results folder;
the original sudo-created copy could not be removed with the current permissions.
See `results/tcp_smoke_01_relocation.json` for the copy-verification record.

## Checks and recovery

```bash
python3 -B -m unittest discover -s tcp_congestion_control_analysis -p 'test_*.py' -v
(cd partner_network && python3 -B -m unittest discover -s tests -v)
```

If interrupted, pass the individual run directory, not the sweep root:

```bash
bash partner_network/run.sh status ../tcp_congestion_control_analysis/results/tcp_smoke_01/run_001_reno
sudo bash partner_network/run.sh cleanup ../tcp_congestion_control_analysis/results/tcp_smoke_01/run_001_reno
```

The shared `partner_network/run.sh` resolves these paths from `partner_network`.
The order is shuffled: replace the example name with the actual directory.
