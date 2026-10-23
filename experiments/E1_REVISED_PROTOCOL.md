# Revised E1: actual sources and controlled bottleneck diagnostics

This is an additive experiment under the current twelve-experiment specification.
The historical data in `results/e1_complete` are preserved. They are a separate
aggregate-message-rate sweep with at most two actual ROS2 sources, and must not
be relabelled as independent-client or actual-source-count measurements.

## Questions and controls

1. How does a shared gateway respond to 1, 2, 4 and 8 actual ROS2 source nodes,
   all publishing at unchanged per-source rates?
2. At eight sources, how do separately imposed link, server and gateway limits
   alter throughput, FIFO occupancy, deadlines and the measured latency stages?

Every new condition uses one shared gateway, one bounded FIFO (1,000 waiting
messages, drop-tail), one persistent cleartext HTTP/1.1 TCP connection, one
outstanding request and one central server. These remain synthetic upload
sources, not independent HTTP clients or a demonstrated robot control loop.
Compression, batching, telemetry replacement and application retries are off.
ROS2 reliable KEEP_LAST depth 1,000 and the periodic seeded source scheduler are
unchanged. One source process hosts the configured number of actual ROS2 nodes;
each publishes four distinct topics. The runner verifies the discovered nodes
and publisher counts before starting the schedule.

Base traffic per source: command 2/s at 100 useful bytes, status 5/s at 100 bytes,
odometry 20/s at 250 bytes and sensor 5/s at 20,000 bytes. Thus the source-count
sweep offers 32, 64, 128 and 256 original messages/s. JSON metadata and protocol
headers are not useful application payload bytes.

## Network and diagnostic cases

All new cases share the same isolated virtual Ethernet/network-namespace path
inside the WSL2 Linux host. The gateway/source stay in the original namespace;
the server runs in a dedicated namespace. This is an emulated same-host path,
not a physical network. It is different from the historical loopback path, so
cross-cohort differences are not attributed solely to source count.

| Condition | Sources | Deliberate change |
|---|---:|---|
| reference | 1, 2, 4, 8 | Unshaped virtual link, no artificial service delay |
| gateway_10ms | 8 | 10 ms gateway dispatch hold per dequeued message |
| server_10ms | 8 | 10 ms server processing sleep after request receipt |
| network_2mbit | 8 | 2 Mbit/s TBF on gateway egress towards the server |

The server and gateway sleeps represent explicitly controlled service time,
not CPU saturation and not injected propagation delay. Only one factor changes
relative to the eight-source reference. The TBF burst is 4,000 bytes and its
configured maximum queueing latency is 200 ms; the response direction is
unshaped. There is no deliberately injected random loss or propagation delay.
Actual qdisc drops, overlimits, backlog, RTT and TCP calibration goodput are
recorded. Congestion-related drops remain possible and must not be confused
with configured random loss. Only uniquely named experiment interfaces are
modified; cleanup restores their absence after each run.

## Run design and analysis

The frozen final configuration contains seven cells, five repetitions each,
for 35 runs. Order is reproducibly shuffled. Each run has 10 s warm-up, 60 s
measurement and at most 30 s drain. Smoke/pilot data are kept outside the final
cohort. Infrastructure-invalid runs are retained with a reason; overload itself
does not invalidate a run. Source schedules are independent of HTTP completion.
Warm-up uses the same workload, and the FIFO is not reset at the measurement
boundary. Under overload it can already be full when measurement begins; its
measured occupancy therefore describes sustained backlog, not necessarily
queue growth from an empty initial state.

Application latency ends at complete response receipt, consistent with the
historical endpoint. Successful measurement-cohort messages completed during
drain contribute to latency. Throughput and goodput count responses inside the
measurement window. Deadline denominators retain all generated cohort messages,
including discarded, failed, missing and unresolved work. Fixed targets are
command 100 ms, status 200 ms, odometry 100 ms and sensor 1,000 ms; these are
illustrative experimental targets, not safety guarantees.

Additional timestamps distinguish ROS delivery/callback, waiting FIFO, gateway
dispatch preparation/hold, request start to server receipt, server processing,
and processed-to-response receipt. The forward and return intervals include
software overhead; neither is pure one-way network latency. Means of these six
stages are additive for the same successful cohort. Individual stage p95 values
must not be added to obtain end-to-end p95.

Queue occupancy is time-integrated and excludes work in service. CPU is measured
from process counters and expressed relative to one logical CPU; busy wall-time
fractions are separate metrics. Shared boot/time-namespace clock identifiers are
checked before cross-process timestamp subtraction. Source scheduling lag and
actual versus intended source counts/rates remain visible.

Figures show means of per-run statistics with observed minimum-maximum bars.
They are not confidence intervals, and messages within a run are not independent
repetitions. Report per-class successful counts with p95/p99 to expose limited
tail sample support. Historical plots retain their three-run statistics and use
total mixed-message rates, while new source-count plots use actual ROS2 nodes.

## Validation and completion evidence

Required evidence includes actual ROS graph counts; unchanged per-source rates;
unique IDs and source/gateway/server conservation; stage ordering and additive
latency; common clock domain; HTTP/1.1 and connection reuse; realized network
calibration and counters; packet-capture drop metadata; and normal network/process
cleanup. Reconcile representative capture requests/responses with server records.
All configurations, source snapshots, raw logs and validation outputs are retained.
No result or saturation limit is prescribed by this protocol.
