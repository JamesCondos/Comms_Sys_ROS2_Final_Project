# Two-site ROS 2 network study

This study uses four independent robot source/gateway processes on two isolated
site LANs, two pass-through edges, two IP router namespaces, one central proxy
and two separate worker processes. The proxy has one shared FIFO and two service
loops; each loop targets one worker. All hosts share one WSL2 machine and clock.
ROS messages travel locally from each source node to its gateway; HTTPS crosses
the application hops and routed WAN. Site-link ROS relay stubs in the starter
workspace are replaced here by real namespace routing.

## Traffic and queueing study

The primary controlled study uses HTTP/2 on every application hop, independent
open-loop Poisson sources and equal-sized 256-byte useful payloads. Each worker
has independent seeded exponentially distributed processing holds with mean
20 ms. Proxy service includes the worker exchange and software overhead. A
separate live pilot estimates this complete service mean before the formal load
levels are frozen. The five target loads are 0.2, 0.5, 0.75, 0.9 and 1.1 times
that pilot capacity, rounded to 0.01 messages/s, with three independent repeats.
Each run has 10 s warmup, 45 s measurement and up to 30 s drain. Gateway/edge
concurrency is deliberately high enough that the planned overload can queue
centrally; all stages remain bounded at 2,000 waiting jobs. Video is disabled
in this controlled model comparison; it is enabled in the protocol comparison.

Compare measured central waiting/residence and time-integrated queue occupancy
against Erlang-C M/M/2 predictions. Show nominal configured holds and independent
pilot calibration distinctly. Within-run service calibration is diagnostic.
Report observed arrival/service variability; network/software overhead means
an exact exponential service model is an approximation. No stationary mean is
predicted for overload. End-to-end latency includes additional hops and is
not the model's central residence time. Inspect every stage's queue for shifted
backlogs and preserve failed/dropped messages in all source denominators.

## HTTP protocol study with ROS video

All application hops use the selected protocol: HTTP/1.1 or HTTP/2 over TCP/TLS
1.3, or HTTP/3 over QUIC v1/TLS 1.3. Strict ALPN and certificate checks prohibit
fallback. Both TCP and QUIC use Cubic. H1 uses eight persistent connections per
robot while H2/H3 use one connection with eight concurrent streams; the equal
application concurrency budget is explicit, but transport connection counts
and congestion windows differ. Each edge permits 32 outstanding operations.
Both central worker slots remain unchanged with fixed 4 ms processing holds.
No application operation is retried; any connection replacement serves a new
operation, never a retransmitted failed message. TLS setup is before warmup;
no 0-RTT is used. This compares these implementations and settings, not a
universal ranking of protocol standards.

Each robot publishes command/status/odometry/sensor at 2/5/20/5 Hz with
100/100/250/20,000 useful bytes, plus a real sensor_msgs/CompressedImage JPEG
stream at 5 frames/s, 320x180. Synthetic moving detailed frames are cached
before timing; no physical camera or online encoder is measured. JPEG bytes
are base64-encoded in the HTTPS envelope; useful bytes, body bytes and captured
network bytes are different quantities. Source schedules are independent of
responses. ROS reliable KEEP_LAST depth is 10,000.

Five matched network profiles apply to both sites: clean; 30 ms added delay
in each WAN direction; 1% loss in each direction; 2 Mbit/s forward capacity
per site; and all three impairments together. Reverse ACK capacity is unshaped.
Each shaped qdisc holds up to 1,000 packets. Every protocol/profile has three
repeats with 5 s warmup, 20 s measurement and up to 15 s drain. The fixed video
workload intentionally overloads the 2 Mbit/s links. Report its unsuccessful
or undrained operations together with successful latency and useful goodput.

## Integrity and presentation

Run order is reproducibly shuffled within each study; source/service seeds
are matched across protocols/conditions by repeat. Kernel packet-loss draws
and transport dynamics are not bitwise reproducible. All source IDs, body
hashes, per-hop queue events, worker processing and exact ACKs are retained.
Queue area is independently reconciled with clipped per-job residence
intervals, including carry-in and drain boundaries. Captures contain TLS/QUIC
handshakes plus warmup/measurement/drain; capture integrity and negotiated
versions are checked, without claiming decrypted HTTP stream reconstruction.
Owned interfaces have recorded TSO/GSO/GRO state, qdiscs, capture-drop counters
and cleanup evidence. No performance collection runs concurrently with another
experiment or report rendering.

One plotted point is the mean of three run-level statistics; bars are the
observed minimum and maximum, not confidence intervals. Percentiles are never
pooled across runs. Raw data is retained outside Google Drive/Git due to size;
figures, tables, config matrices, validation and provenance are deliverables.

References: https://www.rfc-editor.org/rfc/rfc9113.html ;
https://www.rfc-editor.org/rfc/rfc9114.html ;
https://ocw.mit.edu/courses/6-041-probabilistic-systems-analysis-and-applied-probability-spring-2006/1a538356bf7a1f78408de525dd2d1032_lec21.pdf
