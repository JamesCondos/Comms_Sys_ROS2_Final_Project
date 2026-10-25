# ROS 2 / HTTPS communication networks project

There are now two active code folders:

- **`https_protocol_analysis/`** contains the shared ROS sources, HTTP transports,
  topology, runner and measurements. Both teammates use this implementation.
- **`tcp_congestion_control_analysis/`** selects Reno/CUBIC/BBR, runs repeatable
  comparisons and produces graphs. All TCP results stay in its `results/` folder.

The old complex workspaces, partner-network runtime, experiment scripts and
unused TCP socket helper have been removed. Historical `results/` and `report/`
artifacts are retained; their embedded source snapshots describe their original
implementation. Do not pool them with measurements from the new runtime.
`run_client.py` is retained as the team's commenting-style example.

## Setup

Use Ubuntu/WSL with ROS 2 Humble, including `rclpy`, `std_msgs` and `sensor_msgs`.
No colcon build is needed. From the repository root:

```bash
sudo apt install python3-venv python3-opencv iproute2 iputils-ping tcpdump ethtool openssl
source /opt/ros/humble/setup.bash
python3 -m venv --system-site-packages https_protocol_analysis/.venv
https_protocol_analysis/.venv/bin/python -m pip install -r https_protocol_analysis/requirements.txt
https_protocol_analysis/.venv/bin/python -m https_protocol_analysis.runner doctor
```

The virtual environment uses system packages so it can import the installed ROS
Python modules. Read [the shared runtime guide](https_protocol_analysis/README.txt)
and [the TCP trial guide](tcp_congestion_control_analysis/README.md).

## First TCP run

```bash
sudo bash tcp_congestion_control_analysis/run_tcp_congestion.sh \
  --tcp reno cubic --duration 5 --warmup 1 --drain 8 --repeats 1 \
  --output tcp_shared_smoke_01
```

Omitting `--tcp` requests Reno, CUBIC and BBR. All requested algorithms must be
registered by the kernel; the current host reports only Reno/CUBIC. The code does
not silently skip BBR or change the system-wide congestion-control setting.

The launcher loads ROS and uses `https_protocol_analysis/.venv/bin/python` when
present. Results and graphs are saved under
`tcp_congestion_control_analysis/results/tcp_shared_smoke_01/`.

## Topology and checks

Four ROS source/gateway processes feed two edges, two routed site uplinks, a
shared proxy FIFO and two workers. All hosts are simulated on one Linux machine.
For the full topology, TCP trials change the edges' outgoing WAN sockets only.
HTTP/3 uses QUIC and is not included in kernel TCP algorithm comparisons.

```bash
https_protocol_analysis/.venv/bin/python -B -m unittest discover -s https_protocol_analysis -p 'test_*.py' -v
https_protocol_analysis/.venv/bin/python -B -m unittest discover -s tcp_congestion_control_analysis/tests -p 'test_*.py' -v
```
