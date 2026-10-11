# TCP congestion-control selection

The gateway can select Reno, CUBIC, or BBR on each persistent HTTP/1.1 socket.
Linux applies the algorithm; the Python code selects it and reads it back to
verify the setting. The selection does not change the system-wide default.

Check kernel support inside WSL:

```bash
sysctl net.ipv4.tcp_available_congestion_control
```

Build the ROS2 package as described in the main README, then run a short
three-algorithm functional check from the repository root:

```bash
bash scripts/run_tcp_congestion.sh --robots 2 --duration 10 --repeats 1
```

For a longer comparison:

```bash
bash scripts/run_tcp_congestion.sh --robots 16 64 --duration 60 --repeats 5
```

The order is shuffled. Each run directory records the requested algorithm in
`config.json`, the verified per-connection value in `messages.csv`, and both in
`summary.json`. A mismatch or unavailable algorithm stops the experiment rather
than silently testing a different algorithm.

This runner still uses clean WSL loopback. Loopback is useful for checking the
implementation, but it is not a meaningful congestion-control bottleneck. For
the final Reno/CUBIC/BBR comparison, route the HTTP connection through a
controlled finite-bandwidth, delayed path and capture that interface with
tcpdump/Wireshark. Keep bandwidth, RTT, queue size, traffic load, duration, and
all application settings identical between algorithms.
