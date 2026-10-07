You are helping me implement a University of Melbourne ELEN90061 Communication Networks Learn-Design-Implement project.

I want you to act as my technical implementation partner for the entire project. Before making large architectural changes, keep the overall research goals and experimental methodology below in mind.

======================================================================
PROJECT TITLE
======================================================================

Performance and Reliability of RESTful Communication for Distributed
ROS2 Systems under Network Constraints

======================================================================
PROJECT CONTEXT
======================================================================

This is primarily a COMMUNICATION NETWORKS project.

ROS2 and robotics are the application context, but the assessed technical
focus must remain on:

- RESTful machine-to-machine communication
- HTTP/1.1, HTTP/2 and potentially HTTP/3
- latency and throughput
- queueing behaviour
- FIFO vs priority scheduling
- network congestion
- reliable data transfer
- ACK/NACK
- sequence numbers
- retransmissions
- application-layer fault injection
- packet loss / delay / bandwidth constraints
- multi-client communication
- potentially multi-server scaling
- experimental measurement
- Wireshark analysis
- queueing theory
- Little's Law
- M/M/1 and potentially M/M/m systems
- comparing alternative communication architectures

Do NOT turn this into a robotics/autonomy project.

Do NOT spend substantial project time implementing:

- SLAM
- Nav2
- complex robot path planning
- computer vision
- autonomous exploration
- sophisticated Gazebo worlds
- machine learning

ROS2 exists mainly to generate realistic heterogeneous network traffic.

======================================================================
PRIMARY RESEARCH QUESTION
======================================================================

How do HTTP protocol design, queueing strategies and application-layer
reliability affect latency, reliability and network utilisation for
heterogeneous ROS2 traffic under constrained network conditions?

A later extension of this question is:

Can an adaptive ROS2-to-REST gateway preserve low latency for critical
robot traffic while reducing lower-priority traffic during congestion?

======================================================================
CORE SYSTEM ARCHITECTURE
======================================================================

The initial system should look approximately like this:

    ROS2 Robot A ─┐
                  │
    ROS2 Robot B ─┼──> ROS2-to-REST Gateway
                  │
    ROS2 Robot N ─┘
                         |
                         | REST over HTTP
                         |
                  Network impairment layer
                  delay / loss / bandwidth
                         |
                         v
                   REST Server
                         |
                   metrics/logging


Later extensions may produce:

    ROS2 robots
         |
         v
    Adaptive REST Gateway
         |
     HTTP/1.1
     HTTP/2
     HTTP/3
         |
    Network Emulator
         |
         v
     Load Balancer
       /      \
      /        \
 REST Server  REST Server


The software must be modular enough that protocol, queue discipline,
fault model and number of robots can be changed from configuration
without rewriting the system.

======================================================================
ROS2 TRAFFIC CLASSES
======================================================================

Use four main traffic classes throughout the project.

1. COMMAND

Characteristics:
- very small payload
- low rate
- latency critical
- reliability critical
- highest priority

This may be implemented as a synthetic experimental ROS2 topic if using
real /cmd_vel creates unnecessary directionality or application issues.

2. STATUS / HEARTBEAT

Characteristics:
- very small
- periodic
- reliability important
- relatively latency sensitive

3. ODOMETRY

Characteristics:
- small
- frequent
- freshness matters
- moderate priority
- older queued values may become less useful than the newest value

4. SENSOR

Characteristics:
- comparatively large
- bandwidth intensive
- lower priority
- intended to generate contention with small critical messages

The sensor traffic can initially be synthetic bytes rather than real
LiDAR/image data. Repeatability and control are more important than
robot realism.

All rates and payload sizes MUST be configurable.

A possible INITIAL test configuration is:

command:
    rate_hz: 2
    payload_bytes: 100

status:
    rate_hz: 5
    payload_bytes: 100

odometry:
    rate_hz: 20
    payload_bytes: 250

sensor:
    rate_hz: 5
    payload_bytes: 20000

These are experimental parameters, not fixed real-world requirements.

======================================================================
MESSAGE IDENTIFICATION
======================================================================

Every experimental message should be uniquely traceable.

At minimum include:

- experiment_id
- robot_id
- message_type
- sequence_number
- source_timestamp
- payload size or payload
- optional checksum for reliability experiments

Where appropriate, record:

- gateway_enqueue_timestamp
- gateway_dequeue_timestamp
- server_receive_timestamp
- response_timestamp
- queue length
- retry count
- ACK/NACK status
- HTTP status
- server/worker ID
- protocol version

Do not mix clocks unnecessarily.

Prefer monotonic clocks for latency measurement where endpoints share
the same machine.

If separate physical machines are introduced later, explicitly handle
clock synchronisation rather than silently assuming timestamps are
comparable.

======================================================================
REST DESIGN
======================================================================

REST is being used as the machine-to-machine API architecture.

Example resources may include:

POST /robots/{robot_id}/telemetry
POST /robots/{robot_id}/odom
POST /robots/{robot_id}/status
POST /robots/{robot_id}/sensor
GET  /robots/{robot_id}/commands

The exact API can be improved, but it should:

- remain simple
- remain stateless where practical
- carry robot ID explicitly
- use consistent JSON schemas
- support multiple robot clients
- make retransmitted requests safely identifiable
- support idempotency / duplicate suppression for reliability tests

Do not create unnecessary database complexity.

======================================================================
HTTP PROTOCOL WORK
======================================================================

The baseline protocol is:

HTTP/1.1 with persistent connections.

Do NOT create a new TCP connection for every REST request unless an
experiment explicitly studies non-persistent communication.

Later implement:

HTTP/2

and, if feasible:

HTTP/3 / QUIC

Important conceptual rule:

HTTP/2 provides multiplexed application streams, but all streams still
share TCP.

Therefore HTTP/2 can still suffer transport-level head-of-line blocking
when TCP loses data.

HTTP/3 uses QUIC over UDP and avoids cross-stream TCP head-of-line
blocking.

Do not incorrectly claim that HTTP/2 eliminates every form of HOL
blocking.

When comparing protocol versions:

- use the same application payloads
- use the same traffic rates
- use the same REST semantics
- use the same network impairment conditions
- avoid changing server implementation unnecessarily
- confirm the actual negotiated protocol
- verify using Wireshark where possible

======================================================================
PRIMARY METRICS
======================================================================

The project should eventually calculate:

LATENCY
- mean
- median / p50
- p95
- p99

QUEUEING
- gateway queue waiting time
- mean queue length
- maximum queue length
- queue occupancy over time

TRAFFIC
- offered load
- requests per second
- payload goodput
- total wire throughput where available
- bytes per application message
- protocol overhead

RELIABILITY
- application delivery ratio
- retransmissions
- duplicates
- ACK/NACK counts
- recovery time
- timeout count

QUALITY OF SERVICE
- per-traffic-class latency
- deadline miss ratio
- dropped low-priority messages

SCALABILITY
- server utilisation
- messages per second
- queue growth
- performance versus robot count
- performance versus server count

OPTIONAL ADVANCED METRIC
- Age of Information / message freshness

======================================================================
PYTHON LOGGING VS WIRESHARK
======================================================================

Application/Python logging should be the PRIMARY data source for:

- message-level latency
- queueing delay
- p50/p95/p99
- queue length
- application delivery
- retries
- deadline misses
- experiment summaries

Wireshark should be used as network-level evidence for:

- HTTP version validation
- TCP connection reuse
- HTTP/2 streams
- QUIC / UDP
- TCP retransmissions
- packet sizes
- protocol overhead
- connection establishment
- packet-level behaviour

Do NOT try to make Wireshark replace application instrumentation.

Both should be used.

======================================================================
NETWORK IMPAIRMENT
======================================================================

Use a controlled impairment mechanism such as Linux tc/netem,
network namespaces, containers or an equivalent reproducible emulator.

Support configurable:

- delay
- jitter
- packet loss
- bandwidth limit

Example named profiles:

CLEAN
    delay = 0
    loss = 0
    bandwidth = unlimited/high

DELAY_50
    approximately 50 ms added delay

DELAY_100
    approximately 100 ms added delay

LOSS_1
    1% packet loss

LOSS_5
    5% packet loss

BW_1M
    1 Mbit/s bottleneck

MIXED
    constrained bandwidth + delay + loss

The runner should:

1. apply network conditions
2. verify them
3. execute the experiment
4. save the exact settings
5. clean up/reset conditions afterwards

======================================================================
IMPORTANT RELIABILITY DISTINCTION
======================================================================

HTTP/1.1 and HTTP/2 normally use TCP.

TCP already provides reliable byte-stream delivery.

Therefore:

tc/netem packet loss will generally cause TCP retransmission rather
than directly produce lost REST messages.

Do NOT incorrectly describe normal HTTP/TCP as unreliable.

The course project specifically requires us to investigate
application-level reliable communication.

Therefore application ARQ should be tested using a deliberate
APPLICATION-LEVEL fault injector.

Examples:

- drop a complete application message before semantic processing
- corrupt application payload data after transport reception
- deliberately suppress an application ACK
- simulate semantic processing failure

The reliability layer can then use:

- sequence numbers
- application checksum
- ACK
- NACK
- timeout
- retransmission
- duplicate detection

This is an experimental abstraction designed to study reliable data
transfer concepts.

Keep network-level impairment and application-level fault injection
as two conceptually separate mechanisms.

======================================================================
QUEUEING DESIGN
======================================================================

The ROS2-to-REST gateway should eventually contain an explicit queue.

Initial policy:

FIFO

Second policy:

PRIORITY

Suggested priority order:

1. command
2. status
3. odometry
4. sensor

The queue should have finite capacity.

Log:

- arrival time
- service time
- waiting time
- queue length
- drop count
- traffic class

Optional extension:

LATEST-VALUE replacement.

For odometry and sensor messages, stale queued information may be
discarded in favour of the newest value.

This should be configurable independently from priority scheduling.

======================================================================
QUEUEING THEORY
======================================================================

This project must connect implementation to Communication Networks
queueing theory.

Measure:

lambda = observed arrival rate

L = average number of messages in the system

W = average time in the system

Then experimentally test Little's Law:

    L ≈ lambda * W

Do NOT automatically claim real ROS traffic is M/M/1.

Periodic ROS publishers and mixed message sizes may violate assumptions
of Poisson arrivals and exponential service times.

Therefore create an OPTIONAL controlled synthetic mode with Poisson
arrivals if we want to compare more directly against M/M/1 theory.

Later, if multiple server workers are implemented, investigate M/M/m
style scaling while clearly stating assumptions and deviations.

======================================================================
EXPERIMENT 1 — BASELINE SCALING
======================================================================

Research question:

How does a basic ROS2-to-REST HTTP/1.1 architecture behave as offered
traffic load increases?

FIX:

- HTTP/1.1
- persistent connection
- FIFO
- one server
- clean network
- no application ARQ

VARY:

- number of robots OR equivalent offered load

Suggested:

1 robot
2 robots
4 robots
8 robots

Run each important condition at least 3 times.

Suggested run length:
approximately 60 seconds initially.

Measure:

- total request rate
- p50 latency
- p95 latency
- p99 latency
- per-class latency
- queueing delay
- average queue length
- max queue length
- goodput
- server utilisation where possible

Main objective:

Identify the load region where queueing and latency begin increasing
significantly.

This becomes the stress-load region used in later experiments.

Main plots:

offered load vs p95 latency

offered load vs average queue length

======================================================================
EXPERIMENT 2 — QUEUEING THEORY
======================================================================

Research question:

How does measured queue behaviour relate to queueing theory as load
approaches service capacity?

Use the baseline system.

Measure:

lambda
L
W
utilisation

Test:

L ≈ lambda W

Run:

low load
medium load
near-saturation load

Optionally run Poisson synthetic arrivals separately to compare with
idealised M/M/1 behaviour.

Do not force the real ROS workload to match M/M/1.

======================================================================
EXPERIMENT 3 — HTTP/1.1 VS HTTP/2
======================================================================

Research question:

Does HTTP/2 multiplexing improve the latency of small, latency-critical
messages when they coexist with large sensor transfers?

Use simultaneous traffic such as:

large sensor data
+
small command messages

Compare:

HTTP/1.1
HTTP/2

Keep everything else as identical as possible.

Potential variables:

- sensor traffic load
- RTT
- possibly bottleneck bandwidth

Measure especially:

- command p95 latency
- command p99 latency
- command deadline miss ratio
- total goodput
- wire behaviour

Capture representative packet traces.

Do not assume HTTP/2 must always perform better.

Negative or null findings are valid.

======================================================================
EXPERIMENT 4 — FIFO VS PRIORITY
======================================================================

Research question:

Can traffic prioritisation preserve latency-sensitive robot messages
under congestion?

Compare:

FIFO
vs
PRIORITY

Use the stress load identified earlier.

Test under several conditions such as:

CLEAN
DELAY_100
BW_1M

Measure per traffic class:

- queue waiting time
- end-to-end latency
- p95
- p99
- deadline misses
- drops
- goodput

Also quantify the cost to low-priority sensor traffic.

Do not simply conclude "priority is better".

Discuss the trade-off.

======================================================================
EXPERIMENT 5 — APPLICATION-LAYER RELIABLE DATA TRANSFER
======================================================================

Research question:

How does application-level reliability affect delivery, latency and
goodput under intentionally faulty communication?

Implement a deliberate application fault injector.

Initial reliability scheme:

Stop-and-Wait style ARQ

Use:

sequence number
checksum
ACK/NACK
timeout
retransmission
duplicate suppression

Compare:

no application ARQ
vs
application ARQ

Vary:

application fault rate
RTT

Measure:

- successful delivery ratio
- latency
- retransmission count
- recovery time
- goodput

Advanced comparison:

Stop-and-Wait
vs
Selective Repeat

Possible later extension:

Go-Back-N

Do not implement advanced ARQ until the basic scheme is correct.

======================================================================
EXPERIMENT 6 — HTTP/2 VS HTTP/3
======================================================================

ADVANCED / STRETCH BUT HIGH VALUE.

Research question:

How do HTTP/2 over TCP and HTTP/3 over QUIC behave under packet loss and
latency when multiple ROS traffic classes share the connection?

Compare:

HTTP/2
HTTP/3

Potential matrix:

loss:
0%
1%
5%

RTT:
low
high

Measure:

- command p95/p99
- deadline misses
- goodput
- connection setup time
- effect of loss on concurrent streams

Verify HTTP/3/QUIC with Wireshark.

If HTTP/3 tooling becomes unstable, do not destabilise the core project.

A deeper HTTP/1.1/HTTP/2 experiment is preferable to a broken HTTP/3
implementation.

======================================================================
EXPERIMENT 7 — MULTI-SERVER SCALING
======================================================================

ADVANCED.

Research question:

How does increasing the number of REST service channels affect queueing
delay and system throughput?

Implement:

1 server
2 servers
3 servers

Potentially use a lightweight reverse proxy / load balancer.

REST workers should remain stateless where practical.

Record worker ID.

Measure:

- queue length
- utilisation
- response time
- throughput
- time in system

Relate to:

M/M/1
vs
M/M/m

without claiming a perfect theoretical fit.

Optional resilience test:

kill one worker under load.

Measure:

- recovery time
- messages lost
- messages delayed
- whether clients continue operating

======================================================================
EXPERIMENT 8 — ADAPTIVE GATEWAY
======================================================================

This is intended to be the standout design contribution.

Research question:

Can a gateway respond to congestion by protecting critical traffic while
reducing unnecessary bulk traffic?

Implement a SIMPLE, EXPLAINABLE controller.

Do not use machine learning.

Inputs may include:

- current queue occupancy
- recent p95 latency
- recent retry rate
- recent goodput

Example behaviour:

NETWORK HEALTHY:

command      normal
status       normal
odometry     normal
sensor       normal

CONGESTED:

command      unchanged
status       unchanged
odometry     reduced rate
sensor       strongly reduced rate
old sensor/odom messages optionally discarded

Use hysteresis:

if queue > HIGH_THRESHOLD:
    enter CONGESTED

if queue < LOW_THRESHOLD:
    return to NORMAL

Log every mode transition.

Freeze controller thresholds before final evaluation.

Compare:

FIFO
vs
STATIC PRIORITY
vs
ADAPTIVE

Measure:

- command p95/p99
- command deadline misses
- sensor goodput
- dropped sensor messages
- odometry freshness
- queue occupancy
- recovery after congestion

The goal is NOT for adaptive control to dominate every metric.

The expected engineering contribution is a measurable trade-off.

======================================================================
OPTIONAL AGE OF INFORMATION
======================================================================

For rapidly changing telemetry, reliability may not mean delivering
every old sample.

Optional freshness metric:

Age of Information

AoI(t) = current time - generation time of newest received information

This is particularly useful for:

- odometry
- sensor/state updates

It can help evaluate latest-value queueing.

This is an extension and should not replace core lecture-linked metrics.

======================================================================
EXPERIMENT REPRODUCIBILITY
======================================================================

Every serious run should produce a folder like:

results/
    experiment_name/
        run_001/
            config.yaml
            messages.csv
            queue.csv
            summary.json
            capture.pcap
            notes.txt

Every experiment should have:

- unique experiment ID
- exact config saved
- timestamp
- protocol version
- queue policy
- robot count
- network impairment settings
- source code commit hash if possible

Do not overwrite raw experimental data.

======================================================================
STATISTICAL PROCEDURE
======================================================================

For important comparisons:

- run at least 3 repeats
- preferably 5+ if execution is cheap
- do not cherry-pick runs

Report where relevant:

- mean
- median
- p95
- p99
- standard deviation / variability
- confidence intervals when useful

Prefer a smaller well-controlled experiment matrix with repetitions over
hundreds of poorly controlled combinations.

Do NOT blindly cross every variable.

Use staged experiments that isolate one main factor.

======================================================================
EXPERIMENT AUTOMATION
======================================================================

Eventually one command should be able to specify something like:

protocol = HTTP2
robots = 4
loss = 5
delay_ms = 100
bandwidth_mbps = 2
queue = PRIORITY
server_count = 1
duration_s = 60

The runner should then:

1. validate configuration
2. configure networking
3. start server
4. start gateway
5. start ROS2 traffic
6. capture metrics
7. optionally start Wireshark/tshark capture
8. run for fixed duration
9. stop everything cleanly
10. reset networking
11. save outputs
12. calculate summary statistics

Avoid manual experiment procedures whenever possible.

======================================================================
PROPOSED REPOSITORY STRUCTURE
======================================================================

Prefer something similar to:

comm-nets-project/
│
├── ros2_ws/
│   └── src/
│       ├── traffic_generator/
│       └── rest_gateway/
│
├── server/
│
├── network/
│   ├── netem/
│   └── fault_injector/
│
├── experiments/
│   ├── configs/
│   ├── runner/
│   └── analysis/
│
├── results/
│
├── wireshark/
│
├── scripts/
│
├── tests/
│
├── report/
│
└── README.md

Do not force this structure if the existing repository already has a
good equivalent.

======================================================================
IMPLEMENTATION ORDER / MILESTONES
======================================================================

CURRENT THREE-WEEK DEVELOPMENT WINDOW:

5 OCTOBER TO 25 OCTOBER 2026

--------------------------------
PHASE 1 — FOUNDATION
--------------------------------

Milestone 1:
Freeze project scope and architecture.

Milestone 2:
Create deterministic ROS2 traffic generators.

Milestone 3:
Implement basic ROS2-to-REST HTTP/1.1 communication.

Milestone 4:
Implement complete timestamping/logging.

Milestone 5:
Create network impairment scripts.

Milestone 6:
Run baseline scaling experiment.

Milestone 7:
Validate queueing behaviour / Little's Law.

The core system must be stable before advanced work begins.

--------------------------------
PHASE 2 — CORE COMPARISONS
--------------------------------

Milestone 8:
Implement HTTP/2.

Milestone 9:
Run HTTP/1.1 vs HTTP/2 experiment.

Milestone 10:
Implement FIFO and priority queueing.

Milestone 11:
Run FIFO vs priority experiment.

Milestone 12:
Implement application-level fault injection and ARQ.

Milestone 13:
If stable, implement Selective Repeat comparison.

Milestone 14:
Regression-test and freeze the core system.

--------------------------------
PHASE 3 — ADVANCED FEATURES
--------------------------------

Milestone 15:
Attempt HTTP/3 / QUIC.

Milestone 16:
Run HTTP/2 vs HTTP/3 impairment experiment.

Milestone 17:
Implement multi-server architecture.

Milestone 18:
Run server scaling / resilience experiments.

Milestone 19:
Implement adaptive congestion-aware gateway.

Milestone 20:
Evaluate FIFO vs priority vs adaptive.

Milestone 21:
Freeze final code and perform repeated experiment sweep.

======================================================================
PRIORITY ORDER
======================================================================

MUST WORK:

1. ROS2 traffic generators
2. REST gateway
3. HTTP/1.1
4. REST server
5. instrumentation
6. experiment runner
7. network impairments
8. baseline scaling
9. queueing theory measurements
10. HTTP/2
11. FIFO vs priority
12. basic ARQ

HIGH-VALUE ADVANCED:

13. Selective Repeat
14. HTTP/3
15. multi-server architecture
16. adaptive gateway

OPTIONAL ONLY IF EVERYTHING ELSE IS STRONG:

17. Go-Back-N
18. Age of Information
19. Zenoh external comparison
20. sophisticated live dashboard

Never sacrifice core reproducibility to implement optional features.

======================================================================
FIRST EXPERIMENT TO SUPPORT
======================================================================

The FIRST complete experiment should be:

BASELINE REST PERFORMANCE UNDER INCREASING LOAD

Architecture:

ROS2 robots
    ->
ROS2-to-REST gateway
    ->
persistent HTTP/1.1
    ->
single REST server

Network:

clean

Queue:

FIFO

ARQ:

off

Vary:

1, 2, 4, 8 robots
or equivalent increasing offered load

Run:

at least 3 repetitions per load level

Measure:

- requests per second
- p50 latency
- p95 latency
- p99 latency
- queue delay
- mean queue length
- max queue length
- goodput
- per-class latency

Primary objective:

find the approximate saturation / congestion region.

This value is then reused when evaluating priority queues, protocols and
adaptive behaviour.

======================================================================
CODING PRINCIPLES
======================================================================

When writing code for this project:

1. Prefer simple readable Python over unnecessary abstraction.

2. Keep components modular.

3. Put tunable parameters in YAML/config files rather than hardcoding.

4. Add type hints where useful.

5. Add comments for communication/network logic, but do not over-comment
   trivial Python.

6. Use structured logging.

7. Avoid hidden global state.

8. Make clean startup/shutdown possible.

9. Catch errors and save diagnostics.

10. Make experiments reproducible.

11. Do not fabricate experimental results.

12. Never silently change experiment parameters.

13. Do not optimise prematurely.

14. When changing implementation in a way that could invalidate previous
    experiments, tell me explicitly.

15. When implementing something based on a networking assumption, explain
    the assumption to me.

16. When multiple implementation options exist, favour the one that makes
    a fair experimental comparison easiest.

17. If my requested implementation would create a misleading networking
    experiment, warn me before implementing it.

======================================================================
HOW I WANT YOU TO HELP ME
======================================================================

When I ask you to implement a feature:

1. First identify which project milestone it belongs to.

2. Explain briefly how it contributes to the research question.

3. Inspect the existing repository before rewriting working components.

4. Make the minimum coherent change required.

5. Tell me exactly which files you changed.

6. Tell me how to run it.

7. Tell me how to verify that it worked.

8. Where appropriate, provide a small test before moving to a full
   experiment.

9. Do not jump ahead into optional features unless I ask.

10. Preserve compatibility with the experiment runner and logging system.

When debugging:

- distinguish application problems from network problems
- distinguish ROS2 problems from REST problems
- distinguish HTTP problems from TCP/network impairment behaviour
- use logs and packet captures instead of guessing

======================================================================
IMPORTANT ACADEMIC GOAL
======================================================================

The final project should tell a coherent engineering story:

1. Build a simple RESTful ROS2 communication baseline.

2. Measure where and why it degrades.

3. Relate the degradation to Communication Networks theory.

4. Compare alternative communication designs.

5. Implement improvements.

6. Measure whether those improvements actually work.

7. Discuss the trade-offs and limitations.

The strongest final story is approximately:

"ROS2 produces heterogeneous traffic with very different communication
requirements. A naive REST architecture works well at low load but
experiences increased queueing delay and interference as offered load
and network impairment increase. We characterise these effects using
HTTP and queueing theory, investigate application-level reliability,
and then design communication mechanisms such as priority scheduling,
protocol changes, additional service capacity and adaptive rate control
to preserve critical traffic. We quantitatively evaluate the resulting
latency, reliability, throughput and resource-utilisation trade-offs."

Do not allow the codebase to drift away from this story.

======================================================================
CURRENT TASK
======================================================================

Before coding anything, inspect the repository and tell me:

1. what files/directories currently exist,
2. which milestone we are currently at,
3. what is already implemented,
4. what is missing for the next milestone,
5. the smallest sensible next implementation step.

Do not implement advanced features yet unless they already exist in the
repository or I explicitly request them.