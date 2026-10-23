# Delivery validation — 23 October 2026

The kit was executed in Ubuntu 22.04 under WSL2 with ROS 2 Humble and Python
3.10.12. These checks establish that the testbed works; they are not repeated
performance measurements for E7–E12.

## Automated and live checks

- **39 unit tests passed**, covering protocol identity, worker ACK semantics,
  FIFO accounting, drain outcomes, configuration, process ownership, PCAP
  framing, routing, impairment commands, offload reporting and cleanup guards.
- **11 short ROS/HTTP runs passed independent accounting checks.** These cover
  T1 direct clients, T2 with four workers, T3 with two pass-through edges, site-A
  delay/rate/loss, Cubic, Reno, outage/recovery and final-build reference checks.
- The runs generated **4,864 measurement-cohort messages**. Of these, 4,862
  received valid central-worker ACKs. The short outage run explicitly recorded
  one timeout and one HTTP error; it delivered 766 of 768 messages. All other
  checks delivered every generated message. Failed messages stay in the
  denominators and are not silently discarded.
- All four robot clients used separate connections and reported one source
  and one gateway ROS node each. Every configured central worker processed
  messages. Socket logs confirmed the requested Cubic or Reno algorithm.
- Queue events reconciled with message intervals, ACK timestamps matched exact
  worker records, both site captures passed framing/count checks with zero
  recorder drops, and processes and owned network resources cleaned up.

See `validation/functional_checks.json` for per-run counts, connection evidence,
worker usage, capture counts and snapshot hashes. Earlier build checks are
identified separately; `full_packaged` used the delivered runtime files.

## Independent network calibration

All **13 checks passed**, with raw command outputs retained in
`validation/network_calibration.json`:

| Check | Observed result |
|---|---|
| Site A, add 20 ms each direction | Mean ping RTT 0.195 → 40.226 ms |
| Site B control during A delay | Mean RTT 0.158 → 0.125 ms |
| Site A, 100% forward loss | A blocked, B reachable; A recovered after clearing |
| Site A, 2 Mbit/s forward capacity | Exact 512 KiB delivered at 1.939 Mbit/s payload rate |
| Live process inside an owned namespace | Cleanup refused and deleted no resources |
| Stop/reap that process, reload journal | Cleanup succeeded |
| Host network state | Addresses, routes and forwarding unchanged |

These single short observations verify configuration effects, not statistical
estimates of networking performance. The bulk check used a dedicated TCP
transfer because the normal per-site useful payload alone need not saturate
the configured link.

## Included example

`example_run/` is the final T3 reference check: one-second warm-up, three-second
measurement, eight-second maximum drain, four actual clients and two workers.
All **384 measurement messages succeeded**; warm-up messages are also retained
in the raw logs. It contains both complete WAN PCAPs and the code snapshot.

```bash
python3 -B -m lab.runner analyze example_run
python3 -B -m unittest discover -s tests -v
```

Analysis of this saved example requires only Python's standard library. Live
execution needs Linux, ROS and the dependencies listed in README. Saved paths,
addresses and process IDs describe the completed original run; they are not
instructions to recreate or clean up a network on your computer.

`ethtool` was absent during these live checks. The final example records
`offloads.status = unavailable`; virtual captured packets must not be treated
as physical wire frames. The available-ethtool readback paths have mocked test
coverage, not live verification on this machine. Application/queue checks and
PCAP framing do not constitute full HTTP stream reconstruction.

The smoke tests do not validate real multi-computer deployment, HTTP/2/3,
retries/deduplication, adaptive policies, persistent recovery or production
security. Those remain separate implementation tasks.
