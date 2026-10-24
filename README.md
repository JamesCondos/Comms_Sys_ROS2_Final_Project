# ROS 2 / REST communication networks testbed

This branch combines James's **`complex_experiments/`** HTTP/queueing framework
with Luke's existing **`tcp_congestion_control_analysis/`** trial tools and results.
Both use the routing and network harness in `partner_network/`.

ROS 2 Humble is required; the new experiment runner does not require a colcon
build. `ros2_ws/` remains removed. `complex_network_ws/` is retained because the
new framework imports its JPEG video helpers directly; its ROS-relay launch file
is not the routed REST experiment entry point.

The existing TCP launcher still uses the original HTTP/1.1 partner-network
runtime. The new framework currently fixes congestion control to CUBIC; using it
for Reno/CUBIC/BBR trials requires adapting its transport configuration and
validator. Rebasing makes both implementations available without silently
changing the meaning of previous TCP measurements.

## Active structure

```text
complex_experiments/               New HTTP/1.1, HTTP/2, HTTP/3 and queueing framework
complex_network_ws/                Video helpers required by the new framework
partner_network/                   Shared network harness and original HTTP runtime
  lab/components.py                Sources, gateways, edges, proxy and workers
  lab/network.py                   Routing, network impairments and captures
  lab/runner.py                    Starts, checks and cleans up one experiment
  lab/analyze.py                   Independent accounting and summary metrics
  configs/full_reference.json      Four robots, two sites, two edges, two workers
tcp_congestion_control_analysis/  TCP trial runner, presets and instructions
  results/                        TCP raw results, audits and PNG/PDF graphs
https_protocol_analysis/          Reserved area for protocol-comparison work
results/                          Retained measurements and new run outputs
report/                           Written reports and figures
docs/                             Course material
```

The full reference topology is:

```text
Robots 1–2 + ROS/REST gateways → Edge A → Router A ─┐
                                                  ├→ Proxy → Workers 1 and 2
Robots 3–4 + ROS/REST gateways → Edge B → Router B ─┘
```

Each site uplink can have controlled delay, packet loss and bandwidth. All hosts
are simulated using separate Linux network namespaces on one machine. Edges
currently forward individual messages; they do not batch/filter traffic. The
shared runtime currently implements HTTP/1.1. HTTP/2/3 are future extensions.

## James's new HTTP/queueing framework

See [the protocol](complex_experiments/PROTOCOL.md) and
[pinned dependencies](complex_experiments/requirements.txt). With ROS 2 sourced
and those dependencies installed in your chosen Python environment, inspect
prerequisites using:

```bash
python3 -B -m complex_experiments.runner doctor
python3 -B -m unittest discover -s complex_experiments/tests -v
```

Live runs require sudo and all tools checked by `doctor`, including `ethtool`.
The `run` command accepts `--config`/`--output`; `suite` accepts a generated
`--matrix` and `--output`. Keep new-framework results separate from the retained
TCP smoke data: TLS, request concurrency, central queues and workloads differ.

## Prerequisites and first check

Use Ubuntu 22.04 with ROS 2 Humble (`rclpy`, `std_msgs`), Python 3, `iproute2`,
`iputils-ping` and `tcpdump`. `ethtool` is recommended to verify disabled virtual
interface offloads. See [the network setup guide](partner_network/README.md).

From the repository root:

```bash
bash partner_network/run.sh doctor
sudo bash partner_network/run.sh run \
  --config configs/full_reference.json --warmup 1 --duration 3 --drain 8 \
  --output ../results/network_smoke_01
```

The shared `partner_network/run.sh` changes into `partner_network`, so its config
and output arguments are relative to that folder. Every run needs a fresh output
directory. Root is required for temporary namespaces and virtual interfaces.

## TCP congestion-control trials

Read [the TCP trial guide](tcp_congestion_control_analysis/README.md). To preview
and then run a short comparison:

```bash
bash tcp_congestion_control_analysis/run_tcp_congestion.sh \
  --dry-run --tcp reno cubic bbr --repeats 1 --output tcp_plan_01

sudo bash tcp_congestion_control_analysis/run_tcp_congestion.sh \
  --tcp reno cubic bbr --warmup 1 --duration 5 --drain 8 --repeats 1 \
  --output results/tcp_three_algorithm_smoke_01
```

The TCP launcher keeps all output under `tcp_congestion_control_analysis/results/`,
including when using the older `--output results/name` syntax. It produces
comparison graphs after successful sweeps when matplotlib is installed. It
uses the full topology and changes the **edge's outgoing WAN TCP algorithm**.
Reno, CUBIC and BBR are the defaults. BBR must be registered by the kernel;
unsupported requested algorithms stop the sweep before any network setup.

## Historical experiments

`experiments/`, the older experiment scripts in `scripts/`, `results/`, and
`report/` retain earlier E1/E2 studies and their provenance. The old live runners
are not the active entry points: they depend on removed ROS workspace code and
previous helper locations. Saved raw results and source snapshots have not been
rewritten. E2's formal live archive is stored outside Git; its retained manifest
and checksum remain in `results/`.
