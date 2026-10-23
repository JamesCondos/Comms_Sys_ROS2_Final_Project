# ROS2–REST Communication Networks Project
## Codex project context and experimental specification

**Course:** ELEN90061 Communication Networks, The University of Melbourne  
**Proposed project title:** Deadline- and Freshness-Aware RESTful Communication for Distributed ROS2 Systems  
**Specification version:** 1.0 — consolidated twelve-experiment plan  
**Document status:** Proposed design and implementation guidance; not a claim that any feature or experiment is complete.

---

## 1. Instructions to Codex

Read this document before proposing changes to the repository. Act as an implementation partner for a communication-networks investigation, not as an agent tasked with building every feature immediately.

Before editing code:

1. Inspect the repository, existing documentation, tests, launch files, configurations, and available results.
2. Identify the topology and measurement conventions actually implemented. Do not infer implementation progress from this document.
3. Map existing work to the experiment IDs below. Preserve working components and existing raw datasets.
4. Identify the smallest missing step for the next requested experiment.
5. Explain any proposed change that would make new results incomparable with earlier runs.
6. Implement only the requested milestone and its necessary prerequisites. Do not build all twelve experiments in one pass.

After a change, report the files changed, exact verified commands to run it, tests performed, and remaining limitations. Do not invent a command that does not exist, claim a test passed without running it, or present predicted outcomes as measured results.

### 1.1 Source boundaries

This document consolidates the topology and twelve experiments agreed in the project discussion. The course materials supply the broad educational requirements and theoretical background; the specific topology, policies, hypotheses, workload values, metrics, and comparison designs below are project proposals.

Important distinctions:

- The LDI manual asks for a communication system, alternative designs, quantitative comparisons, design justification, and links to ELEN90061.
- The REST brief provides the client–server M2M setting and asks for application-level ACK/NACK reliability under intentionally impaired communication.
- The supplied lectures cover HTTP, reliable data transfer, IP networking, and queueing.
- Deadline-aware scheduling, information freshness, fairness, adaptive coordination, and the detailed failure scenarios are extensions selected for this project. Do not claim that every extension is explicitly taught in the lectures.
- No experimental results, current repository state, or lecturer approval are established by this specification.
- This specification does not create a new submission deadline or promise that all extensions fit a particular remaining time budget. Retain completed work and prioritise depth and reproducibility.

When writing a report, use the original course sources and relevant primary protocol documentation. Do not cite this planning document as evidence of protocol behaviour or measured performance.

---

## 2. Purpose and research contribution

### 2.1 Main research question

> How can a RESTful communication gateway maintain timely processing of critical ROS2 messages and fresh telemetry as offered load, network capacity, and failure conditions change, without starving other robots or discarding nearly all lower-priority traffic?

### 2.2 Engineering story

1. Establish where a simple ROS2-to-REST architecture reaches its performance limits.
2. Separate gateway, network, and server bottlenecks.
3. Relate measured behaviour to HTTP, reliable data transfer, and queueing theory.
4. Compare targeted changes to protocols, scheduling, reliability, and topology.
5. Combine justified mechanisms into an adaptive gateway/edge policy.
6. Evaluate its benefits, costs, fairness, recovery behaviour, and limits.

The main design contribution is the adaptive deadline- and freshness-aware communication policy. The earlier experiments establish why particular mechanisms are needed and provide fair baselines.

### 2.3 What is outside the core scope

Do not spend substantial effort on SLAM, Nav2, perception, path planning, sophisticated Gazebo worlds, machine learning, or a production-grade dashboard. Use synthetic or replayed ROS2 traffic unless a real robot is already available and adds directly measurable communications value.

Do not add BGP, OSPF, a complex service mesh, or a new database platform merely to make the project look more complex. Static routing and a simple controlled network are sufficient for the proposed topology experiments.

---

## 3. Topology and component roles

### 3.1 Full reference topology

```text
SITE A
Robot 1: ROS2 + REST gateway/client ----\
                                        > Edge A ---- Site router A ----\
Robot 2: ROS2 + REST gateway/client ----/                                 \
                                                                          > Central load balancer
SITE B                                                                   /          |          |
Robot 3: ROS2 + REST gateway/client ----\                                 /      Worker C1   Worker C2
                                        > Edge B ---- Site router B ----/
Robot 4: ROS2 + REST gateway/client ----/

Operator/monitoring REST client --------------------> Central load balancer
```

The two site uplinks can be impaired independently. Diagram arrows show the main upload/request direction; responses travel back on the corresponding connections.

### 3.2 Counts in the full reference configuration

| Component | Count | Role |
|---|---:|---|
| Robot-side REST client applications | 4 | One ROS2–REST gateway per robot; independent source identities and local queues. |
| Operator/monitoring client | 1 | Optional read traffic and, in an explicitly configured experiment, command creation. |
| Edge application servers | 2 | Receive site traffic, maintain local queues/state, and forward selected requests. |
| Central application workers | 2 | Interchangeable instances of the same central REST service. |
| Central reverse proxy/load balancer | 1 | Distributes requests to central workers. |
| Emulated site routers | 2 | Separate site networks and allow independently controlled uplinks. |

“Client” and “server” are roles. Edge A is a server to its robots and a client when it forwards to the central service. The proxy also accepts downstream connections and creates upstream ones. Do not count TCP connections as additional robot applications.

The operator client is disabled by default in controlled benchmarks. When enabled, its request pattern is specified and held fixed unless operator load is the experimental factor.

### 3.3 Named topology variants

Implement named topology configurations rather than repeatedly editing code:

| ID | Configuration | Main use |
|---|---|---|
| T0_SHARED | N ROS2 sources → one shared REST gateway → one central server | Original E1 baseline. |
| T1_DISTRIBUTED | N robots, each with its own gateway → one central server | E9 and distributed-client mechanism studies. |
| T2_CENTRAL_POOL | Independent robot gateways → load balancer → m identical workers | E7. |
| T3_EDGE_PASS | Independent robot gateways → two pass-through edges → central service | E10 topology control. |
| T4_EDGE_AWARE | Same as T3, but with configured edge scheduling/filtering | E10–E12 and full E8. |

The central service in E10 can have one or two workers, but keep its worker count and proxy presence fixed across the direct/pass-through/traffic-aware comparison.

The full reference topology has four robots and two central workers. Scaling experiments may use different counts. Record both configured and actually launched counts.

### 3.4 Historical baseline preservation

Earlier planning used both a shared gateway and per-robot gateways. Treat these as explicit alternatives, not interchangeable descriptions:

- Preserve existing shared-gateway E1 results as T0_SHARED.
- Introduce per-robot gateways as T1_DISTRIBUTED and compare them in E9.
- Never silently rename old shared-gateway measurements as distributed-client results.
- If the repository already implements a different topology, document it before aligning experiments with this plan.

### 3.5 HTTP connections are separate on each hop

For an edge-assisted deployment, record the actual protocol separately for:

1. Robot → edge.
2. Edge → load balancer/central service.
3. Load balancer → central worker.

An HTTP/3 connection on one hop does not establish HTTP/3 use on every hop. Name the tested hop in every protocol experiment, and locate the network impairment on that hop.

Application scheduling/load balancing is not IP routing. Site routers forward packets; the HTTP proxy selects application backends. Keep these explanations separate.

---

## 4. Workload contract

### 4.1 Four traffic classes

| Class | Purpose in the testbed | Example initial rate per robot | Example application payload size |
|---|---|---:|---:|
| command | Small critical event or explicitly defined application operation | 2 Hz | 100 bytes |
| status | Periodic status/heartbeat | 5 Hz | 100 bytes |
| odometry | Frequent state updates for which freshness matters | 20 Hz | 250 bytes |
| sensor | Bulk traffic competing with smaller messages | 5 Hz | 20,000 bytes |

These are starter experimental values, not real-robot requirements or course-mandated values. Keep all rates and sizes configurable. Under one-message-per-request operation, they imply 32 original messages per second per robot; batching, filtering, polling, and retries change the HTTP request rate and must be counted separately.

Define exactly what `payload_bytes` means. Record application payload bytes, serialised request-body bytes, and captured network bytes separately. JSON metadata or encoding may change the transmitted size; do not call a 20,000-byte request body a 20,000-byte useful payload without checking.

### 4.2 Traffic direction and command semantics

For the initial mixed-upload experiments, the `command` class may be a synthetic latency-critical event sent robot → server. Label its semantics explicitly, for example:

```text
message_type = command
semantic_role = uplink_synthetic_critical_event
```

Do not describe this upload-only workload as a demonstrated server-to-robot control loop.

Actual downlink commands are a separate configured scenario. One possible REST design is:

1. Operator creates a command resource at the central service.
2. Edge and robot retrieve pending commands using a defined polling mechanism.
3. Robot reports the processing result against the same command ID.

If this is implemented, hold polling intervals fixed for comparisons, account for polling traffic, and measure polling wait separately. A `GET` request must not silently execute or destructively dequeue a command; acknowledge processing explicitly.

Distinguish one-time mission/configuration operations from continuously refreshed velocity setpoints. Decide when each message expires and whether an old value remains meaningful before enabling retries.

### 4.3 Open-loop offered traffic

Generate messages on a configured schedule independently of response completion. A bounded queue may overflow, but the generator must record the intended generation, actual generation time, admission decision, and loss reason.

Do not accidentally create a closed-loop load generator that waits for each HTTP response before producing the next message. That can reduce the offered rate precisely when the system slows down.

Log source scheduling lag and ROS2 message loss before the REST gateway. Keep ROS2 middleware, QoS, executor, subscription depth, and local transport settings fixed unless deliberately studied.

### 4.4 Unique identity and lifecycle

Use a stable original message identity, such as:

```text
(run_id, robot_id, message_type, sequence_number)
```

Retries retain the original message/operation ID and receive a new `attempt_id`. Add site and worker identifiers where relevant. Do not create a new original message for a retransmission.

Suggested lifecycle events:

```text
generated
ros_gateway_received
queue_admitted / admission_rejected
queue_dequeued
request_started
edge_accepted
upstream_forwarded
central_received
processing_started
processed / rejected_expired / rejected_invalid
response_received
retry_scheduled
replaced_as_stale / dropped_overflow / failed / unresolved_at_end
```

Events not present in a topology are omitted, not fabricated.

### 4.5 Service targets

Define experimental deadlines and minimum service levels in configuration before evaluation. Example starting deadlines could be 100 ms for a critical event and 500 ms for status, but these are design targets requiring justification, not safety guarantees.

Specify the endpoint: a deadline to central processing completion is not a deadline to edge acceptance. Decide separately whether telemetry has a delivery deadline, a freshness target, or both.

Minimum sensor service must be expressed in a measurable way, such as useful bytes per second or a fraction of generated updates per robot. State when total demand makes the requirement infeasible.

---

## 5. Measurement and validity rules

### 5.1 Primary instruments

Use application/Python logging for message lifecycle, queueing, deadlines, retries, duplicates, and operation outcomes. Use packet captures and protocol logs to verify connections, negotiated HTTP versions, request framing, retransmission behaviour, and captured traffic volume.

Wireshark does not observe a message's entire wait inside a ROS2 callback or application queue. Do not substitute HTTP request/response timing for the full application path.

### 5.2 Clocks

- Use monotonic timestamps for durations within a verified shared clock domain. Include a `clock_domain_id` in metadata.
- For processes on the same host/kernel, verify that their clocks and time-namespace configuration are comparable.
- On separate hosts, synchronise clocks and report uncertainty before subtracting timestamps across hosts.
- If one-way timing is not defensible, report same-clock transaction durations and per-component waits instead. Do not label RTT as one-way latency.
- Record UTC timestamps for run identification, not as an unexamined replacement for stable duration measurements.
- Do not mix ROS simulation time with wall-clock networking measurements without an explicit mapping.

### 5.3 Metric definitions

| Metric | Definition / boundary |
|---|---|
| Offered message rate | Original messages generated per measurement second, before gateway filtering. |
| Admission rate | Messages accepted into the specified queue/system per second. |
| HTTP attempt rate | Actual requests started per second, including retries; distinguish polling and operator requests. |
| Application latency | Named terminal timestamp minus generation timestamp, e.g. central processing completion. |
| Queue waiting time | Dequeue minus enqueue for a particular queue; record one value per stage. |
| HTTP transaction duration | Response receipt minus request start on the client clock; separate from application latency. |
| Successful processing ratio | Unique original messages successfully processed / original messages generated in the defined cohort. |
| Timely completion ratio | Unique critical messages correctly processed before deadline / critical messages generated. |
| Payload goodput | Unique useful application payload bytes reaching the specified success boundary per second; exclude retry duplicates. |
| Captured throughput | Bytes seen at a specified capture point per second, with layer and direction documented. |
| Duplicate effect count | Extra semantic applications of an operation that should have occurred once; not simply duplicate requests received. |
| Retry amplification | Total application attempts / original submitted operations, with denominator stated. |
| Queue occupancy | Time-varying number waiting; distinguish from number in the system including service. |
| Time-average occupancy | Integral of occupancy over the observation interval divided by interval length. |
| Worker utilisation | Measured busy time / available worker time; do not substitute CPU percent without labelling it. |
| Telemetry age | Current time minus generation time of freshest valid update available at the specified observer. |

Report mean, median, p95 and p99 per traffic class where sample support is sufficient. A successful-message latency distribution is conditional on success: always report unsuccessful, discarded, expired, and unresolved counts alongside it.

If reporting “jitter,” explicitly define the statistic. Do not use an undocumented mixture of standard deviation, consecutive-delay differences, and percentile spread.

### 5.4 Deadline and freshness accounting

Dropped, expired, permanently failed, and unresolved critical messages must not disappear from the generated-message denominator. They do not count as timely successes.

Telemetry freshness uses the newest generation timestamp received, not merely the last packet to arrive. A late old update must not replace a newer value for age calculations. After a previously observed stream stops, its age continues to increase. Before its first valid update, record “no information yet” rather than an age of zero.

Distinguish planned telemetry suppression/replacement from network loss. Filtering is permitted in selected policies, but its cost must remain visible.

### 5.5 Little's Law boundaries

For E2, begin with a stable FIFO configuration and effectively no drops so population accounting is unambiguous:

```text
L = lambda_admitted * W
```

All three quantities must refer to the same system and the same population. `L` includes in-service work; `Lq` includes waiting work only. Estimate occupancy as a time average, not only by averaging samples taken at arrivals.

If admitted jobs can later be discarded or replaced, include their residence time and departure disposition in any accounting for that population. Do not multiply the offered rate by successful-message delay while excluding rejected or discarded work from occupancy.

Check conservation at every stage:

```text
initial_population + admitted
    = completed + discarded + terminally_failed + final_population
```

Retries create additional attempts/work, not additional original operations. Decide which population is being modelled. Do not apply stable M/M/1 formulas to a persistently growing overload run.

### 5.6 Network and application faults are separate

Network impairment changes delay, loss, bandwidth, or jitter for packets. Application fault injection changes the semantic request/response lifecycle. Log them separately.

HTTP/1.1 and HTTP/2 over TCP, and normal HTTP/3 request streams over QUIC, already use transport recovery. Application ARQ investigates acknowledgement of application work and deliberately injected message faults; it does not “make TCP/QUIC reliable.”

For corruption tests, specify exactly where alteration occurs. A useful controlled abstraction is mutation after transport reception but before application checksum validation. This is not a claim that ordinary link corruption routinely passes through transport/security checks.

### 5.7 Network configuration

- Specify delay per direction and per interface. For example, adding 50 ms in each direction adds roughly 100 ms to RTT before other delays.
- Verify realised RTT, shaped throughput, packet-loss characteristics and queue counters.
- Keep qdisc/buffer limits fixed or explicitly report their changes.
- Apply shaping only to dedicated experiment interfaces/namespaces. Never blindly modify the host's primary route or connectivity.
- Provide cleanup on normal exit, errors, and interruption.
- A Python sleep in an application is processing delay, not by itself a network-delay emulator.
- For burst-loss experiments, record the loss model and realised burst characteristics; matching only the nominal mean is insufficient.

### 5.8 Packet capture accounting

Record capture interface, namespace/host, direction, filter, snap length, capture drops, and relevant offload settings. Avoid double-counting packets captured on both ends of a virtual link or all forwarding interfaces.

State whether “wire bytes” means captured frame length, IP bytes, or another measured quantity. Do not claim physical-layer overhead absent from the capture. Do not use a protocol-hierarchy byte sum as an automatic application-overhead calculation; nested layers overlap.

Transport-analysis flags in a capture are diagnostic evidence, not an unquestionable ground-truth packet-loss counter. Compare them with experiment and emulator logs.

Encrypted HTTP may require protocol-negotiation logs, supported session-key logging, or suitable protocol tracing for application inspection. Do not weaken the main benchmark inconsistently just to produce an attractive screenshot.

### 5.9 Fair comparisons

Record and, where possible, hold constant:

- Original source message schedules, sizes, seeds and payload encoding.
- CPU/memory allocations and host hardware.
- Gateway count, server count, concurrency limits and queue capacities.
- ROS2 middleware/QoS/executor settings.
- TLS/encryption configuration, connection reuse and startup procedure.
- Every HTTP hop's negotiated version and proxy behaviour.
- Compression, batching, caching, telemetry replacement and polling settings.
- Application retry limits and library-internal automatic retries.

Use both aggregate-resource-matched and deployment-style comparisons when appropriate, and label them separately. More workers with more allocated cores is a capacity increase, not free algorithmic improvement.

Avoid a full Cartesian product of every parameter. Run staged comparisons that isolate one factor, then selected interactions motivated by earlier results.

---

## 6. Common run procedure and outputs

### 6.1 Pilot versus final runs

Suggested pilot defaults are 10 seconds of warm-up, 60 seconds of measurement, and a separately logged bounded drain interval. These are configurable starting points, not requirements for every experiment.

Important comparisons should normally begin with at least five independent repetitions, then be extended if variability or tail estimates are unstable. A 2 Hz command source produces only 120 events in a 60-second single-robot run; that is not a basis for a strong p99 claim by itself. Record sample counts and use longer runs or more repetitions when needed.

Treat runs as the independent replication units. Preserve per-run metrics and explain confidence intervals or bootstrap methods; do not treat all correlated messages in a run as independent experimental repetitions.

### 6.2 Run lifecycle

1. Validate the configuration and identify the selected topology and experiment.
2. Save the code commit, dependency versions, machine resources and resolved configuration.
3. Start the components, perform health checks and verify the actual protocol on the tested hop.
4. Apply and verify impairment; start capture/instrumentation as needed.
5. Run the documented warm-up.
6. Execute the measured workload without silently changing its source rate.
7. Stop generation and observe outstanding work for a bounded drain interval.
8. Preserve late completions, expirations and unresolved work; distinguish cohort results from rates measured only during the measurement interval.
9. Stop components and captures cleanly; restore the experiment network.
10. Validate data consistency, write a summary, and mark infrastructure-invalid runs with a reason.

Do not invalidate a run merely because overload caused many application failures. That may be the intended result. Retain infrastructure-invalid runs and exclusion reasons rather than silently deleting them.

### 6.3 Per-run directory

```text
results/
  E03_http_versions/
    run_<unique_id>/
      config.yaml
      metadata.json
      events.jsonl
      messages.csv
      queues.csv
      resources.csv
      network_counters.json
      summary.json
      validation.json
      capture_<point>.pcapng          # when capture is enabled
      protocol_trace/                # optional, implementation-dependent
      notes.md
```

The implementation may use equivalent structured formats; keep a documented schema. Every final figure must be regenerable from retained raw data and a known analysis version.

### 6.4 Minimal metadata

Include experiment/subexperiment ID, topology ID, run ID, independent repetition/seed, code commit, protocol per hop, queue policies per stage, robot and worker counts, process/container resource limits, workload definition, impairment model, application faults, clock domains, measurement/drain windows, capture details, and validity status.

---

## 7. Experiment index and execution order

| ID | Experiment | Primary comparison |
|---|---|---|
| E1 | Baseline performance and bottleneck identification | Offered load and controlled limiting component. |
| E2 | Queueing theory and burstiness | Theory versus measurements; periodic/Poisson/bursty arrivals. |
| E3 | HTTP/1.1 versus HTTP/2 | Sequential H1, pooled H1, concurrent H2. |
| E4 | Scheduling, deadlines and freshness | FIFO, static priority, deadline/freshness-aware policy. |
| E5 | Application reliability and duplicate processing | No retries, Stop-and-Wait, bounded selective retransmission. |
| E6 | HTTP/2 versus HTTP/3 under impairment | Protocol, RTT and independent/burst loss. |
| E7 | Central-server scaling and load balancing | Worker count and, separately, balancing policy. |
| E8 | Adaptive gateway and coordination | Static, fixed-rate, local adaptive, coordinated adaptive policies. |
| E9 | Shared versus per-robot gateways | T0_SHARED versus T1_DISTRIBUTED. |
| E10 | Direct versus edge-assisted communication | Direct, pass-through edge, traffic-aware edge. |
| E11 | Unequal site loads and fairness | Balanced/unbalanced traffic and site capabilities. |
| E12 | Failure, outage and recovery | Central worker, edge, and site-link failures, separately. |

**Recommended execution order:**

```text
E1 → E9 → E2 → E3 → E4 → E5 → E6 → E7 → E10 → E11 → E8 → E12
```

E6 is a conditional extension: an unresolved HTTP/3 tooling issue must not block unrelated experiments. E7 does not require HTTP/3. E8 relies on stable scheduling and the edge topology, not on implementing every protocol. A limited central-worker failure pilot can be performed after E7; the complete resilience study remains E12.

---

## 8. E1 — Baseline performance and bottleneck identification

### Research question

At what offered load does the simplest architecture degrade, and which part of the path limits performance?

### Hypothesis to test, not assume

Increasing offered load eventually produces rising queue occupancy, drops, or tail latency at a limiting component. The limiting component may differ between network, gateway and server configurations.

### Configuration

- T0_SHARED: one shared REST gateway and one central server.
- Persistent HTTP/1.1; explicit, recorded outstanding-request limit.
- FIFO with bounded capacity and visible admission/overflow counters.
- No application retries or fault injection.
- Initially no added network impairment.
- Operator disabled; telemetry replacement/compression/batching disabled unless already part of a separately labelled baseline.

### Variables and controls

Start with 1, 2, 4 and 8 robot traffic sources at unchanged per-source settings. If all loads remain light, increase message rate or payload size in a separate sweep. If the generator itself cannot maintain its schedule, report that limitation.

After identifying candidate stress loads, create selected diagnostic conditions:

1. Network-limited: constrained bandwidth with spare application capacity.
2. Server-limited: restricted worker resources or explicitly labelled controlled service workload.
3. Gateway-limited: restricted dispatch capacity/concurrency, with other resources unchanged.

These are diagnostic subexperiments, not three simultaneously changed conditions.

### Procedure

1. Verify one-message and two-source smoke tests.
2. Verify connection reuse and logging identities.
3. Run a coarse load sweep and locate the region of rising delay or backlog.
4. Repeat selected light, near-capacity and overload settings.
5. Inspect delay by pipeline stage and resource usage.
6. Verify representative packet captures against application request counts, accounting for segmentation and drain time.

### Measurements and outputs

Measure offered/admitted/completed rates, per-class latency percentiles, per-stage waiting, time-average and maximum occupancy, drops, payload goodput and resource utilisation.

Produce:

- Offered load versus p95 application latency.
- Offered load versus mean queue occupancy and completion rate.
- A stage-by-stage latency breakdown at representative loads.
- A table identifying evidence for the candidate bottleneck and uncertainty.

### Completion gate and interpretation

Done means the source workload is verified, runs are reproducible, queue counters reconcile, and a stress-load configuration is documented. A visible saturation knee is not mandatory: if it is not reached, report the tested range rather than fabricating a limit.

Do not infer that a quiet capture proves no network contribution. Use timestamp boundaries, resources and network counters together.

---

## 9. E2 — Queueing theory and traffic burstiness

### Research question

How well do queueing relationships explain measured behaviour, and how does variability affect delay at the same average load?

### Prerequisites

E1 instrumentation, an explicit queue/system boundary, reliable time-average occupancy calculation and conservation checks. Select the gateway arrangement after E9 and keep it fixed within E2.

### E2A: Controlled queue-model validation

Use one FIFO queue and one worker with independent exponential inter-arrival times and independent exponential service times. A deliberately simulated service duration is acceptable, but label it as model calibration rather than natural REST processing performance.

For stable, effectively no-drop conditions, compare:

```text
rho = lambda / mu
L = lambda * W
W_M/M/1 = 1 / (mu - lambda), for lambda < mu
```

Verify realised arrival and service distributions and the worker concurrency. Generating Poisson arrivals alone does not create M/M/1 if service times are not exponential.

Suggested utilisation targets for pilot settings: 0.2, 0.5, 0.8 and 0.9. Use longer observation near saturation. If a finite queue fills, treat that as a different model rather than quietly applying infinite-buffer predictions.

### E2B: Realistic arrival variability

Keep mean arrival rate, message-size distribution, queue policy, service setup and protocol fixed. Compare:

- Periodic publishers with staggered phases.
- Periodic publishers with synchronised phases.
- Poisson arrivals.
- A defined bursty arrival model with the same long-run mean.

The research hypothesis is that similar average load can produce different occupancy and tail-delay behaviour. Do not prescribe which case must win.

### Measurements and outputs

Record lambda, service-time distribution, busy fraction, L, Lq, W, Wq, p95/p99, losses and accounting residuals.

Produce measured-versus-predicted mean-delay plots for calibration, a Little's Law table, and a comparison of realistic arrival patterns.

### Completion gate and interpretation

Done means the theoretical population/boundary is explicit and assumptions are checked. A mismatch is a valid finding: investigate finite buffers, transients, mixed sizes, concurrency or measurement errors rather than adjusting data to fit.

Do not apply steady-state M/M/1 predictions to a permanently growing overload run. Analyse those runs separately using backlog growth, drops and completion rates.

---

## 10. E3 — HTTP/1.1 versus HTTP/2

### Research question

Does HTTP/2 improve mixed-traffic performance beyond the benefit of allowing more concurrent requests?

### Prerequisites

Stable traffic generation and E1 metrics. Use a simple direct topology first so an edge/proxy does not introduce an unmeasured protocol change.

### Variants

| Variant | Connection model |
|---|---|
| H1_SERIAL | One persistent HTTP/1.1 connection; one request outstanding at a time. |
| H1_POOL | A bounded pool of persistent HTTP/1.1 connections. |
| H2_STREAMS | HTTP/2 with concurrent streams and a documented connection count. |

Match the total outstanding-request budget K for H1_POOL and H2_STREAMS. H1_SERIAL is the deliberately simple reference, not the only representative HTTP/1.1 implementation.

### Procedure

1. Confirm the negotiated protocol and actual concurrency, not just the client configuration flag.
2. Use identical critical-message and sensor workloads across variants.
3. Increase sensor load at a fixed critical-message rate.
4. Repeat at selected RTTs; vary link capacity separately or use a small predefined interaction subset.
5. Keep server workload, queue discipline, encryption mode and resource budgets fixed.
6. Record connection count, connection reuse, and client/proxy/worker timing.

### Measurements and outputs

Primary: critical-message p95/p99 latency and timely completion. Secondary: sensor goodput, total captured traffic, resource usage, queue wait and completed requests.

Produce a three-variant latency-versus-sensor-load graph and a concurrency/configuration table. Save representative connection/stream evidence.

### Completion gate and interpretation

Done means all variants demonstrably use their stated protocol/concurrency and the workload is matched.

Do not call client-side sequential dispatch “proof of TCP head-of-line blocking.” Distinguish application scheduling, HTTP/1 response ordering, and TCP byte-stream blocking. HTTP/2 does not remove every cause of delay, and pooled H1 is allowed to perform competitively.

---

## 11. E4 — Scheduling, deadlines and freshness

### Research question

How should a gateway prioritise, retain, or discard messages when not all generated work can be served promptly?

### Prerequisites

A fixed protocol/topology and the load regions from E1. Begin with one scheduler boundary, normally the robot gateway or shared gateway. An edge version can follow later, but do not change the scheduler location within one comparison.

### Variants

1. FIFO: bounded first-come-first-served queue.
2. STATIC_PRIORITY: command > status > odometry > sensor, with documented tie-breaking.
3. DEADLINE_FRESHNESS: urgency ordering for valid critical work, replacement of obsolete waiting telemetry, and a configurable minimum service allocation for lower-priority traffic.

Treat scheduling and replacement as separate configuration switches. This permits later mechanism-isolation tests.

### Procedure

1. Unit-test queue order, equal-priority ties, overflow, expiry and replacement.
2. Hold generated traffic, protocol concurrency and queue capacity budgets fixed.
3. Compare at light, near-capacity and overloaded settings.
4. Define deadlines and minimum sensor service before running.
5. Increase sensor load while keeping the critical stream fixed.
6. Test overload long enough to expose persistent neglect of low-priority classes.

Use non-preemptive scheduling initially. Document that prioritising queued messages cannot recall bytes already sent or interrupt work already in progress. Limit dispatch-ahead/concurrency consistently so a deep transport backlog does not conceal the scheduler's action.

### Measurements and outputs

Measure timely completion using all original critical messages, latency among successes, discarded/expired/unresolved counts, per-class waiting, telemetry age, useful sensor bytes and per-robot service.

Produce a trade-off plot of critical-message timely completion versus retained sensor goodput, plus age and drop/replacement summaries.

### Completion gate and interpretation

Done means both benefits and costs are visible. Dropping nearly all sensors or filtering critical work out of the denominator is not a valid improvement claim.

The policy has no hard real-time guarantee; targets are experimental requirements and may become infeasible under overload.

---

## 12. E5 — Application reliability and duplicate processing

### Research question

Can application operations complete under controlled faults without repeated effects or excessive retries?

### Prerequisites

Stable operation identity, a defined acknowledgement boundary, deterministic fault tests and separate application/network fault configuration.

### Variants

- NO_APP_RETRY.
- STOP_AND_WAIT: one outstanding operation in the defined flow; application ACK/NACK and timeout.
- SELECTIVE_RETRANSMISSION: bounded outstanding window with individual acknowledgement and selective retransmission of missing operations.

If implementing textbook Selective Repeat semantics, document per-flow windows, receiver buffering, sequence-space rules and in-order delivery. Otherwise label the implementation accurately as a bounded selective application-retry scheme. Do not claim that parallel HTTP requests alone implement Selective Repeat.

### Fault cases

| Case | Injection boundary | Expected thing to test |
|---|---|---|
| Request suppressed | Before semantic processing | Client detects missing confirmation and may retry. |
| Payload altered | Before application integrity check | Validator rejects it; no invalid operation is applied. |
| Response suppressed after processing | After operation result is recorded | Retry returns the previous result without applying the operation again. |
| Response delayed | Beyond the configured timeout | Concurrent/late attempts are correctly recognised. |
| Repeated request/ACK | Explicit duplicated application event | Identity/window handling remains correct. |

Use a synthetic operation with an observable effect, such as incrementing a test counter or creating a uniquely identified event. It must not command real physical hardware.

### Processing semantics

Define whether ACK means accepted, stored, or processed. Prefer processed-result acknowledgements for the duplicate-effect test. A generic successful HTTP status is not automatically evidence of completed semantic work.

Retain the original ID and checksum across retries; recompute neither merely to make a corrupted payload pass. Specify the byte representation covered by the checksum. Error detection is not authentication.

If the receiver recorded an operation but its ACK was lost, duplicate handling must avoid a second effect. Claims are restricted to the implemented persistence and crash model. Universal “exactly once” semantics are not established by a finite test.

### Procedure

1. Pass deterministic no-fault, corruption, suppressed-request, suppressed-response, duplicate and timeout tests.
2. Disable or record library-internal automatic retries.
3. Sweep selected application fault rates and RTTs.
4. Sweep the outstanding window separately for the selective scheme.
5. Use fixed maximum attempts/deadlines and log terminal outcomes.
6. Add a load-stress subset to inspect retry amplification near capacity.

### Measurements and outputs

Measure unique processed operations, timely completion, duplicate effects, attempts per original, recovery duration, NACK/timeouts, payload goodput and resource consumption.

Produce useful completion versus fault rate and retry-cost/goodput versus RTT/window size. Keep retransmitted attempts out of unique-message goodput.

### Completion gate and interpretation

Done means semantic outcomes are auditable from IDs and operation records. Network packet loss remains a separate impairment; transport reliability is not turned off by disabling application retries.

Strict delivery deadlines may make unlimited retransmission inappropriate. Do not replay expired operations simply to improve eventual delivery counts.

---

## 13. E6 — HTTP/2 versus HTTP/3 under network impairment

### Research question

How do protocol and packet-loss pattern affect concurrent critical and bulk traffic?

### Prerequisites and contingency

Verify end-to-end use of each protocol on the selected impaired hop. Prefer a direct connection initially; for the larger topology, edge → central front end is a useful explicitly named hop.

Check actual client/server support and installed versions before selecting libraries. If HTTP/3 cannot be made reliable within available project effort, mark E6 deferred with the blocker and preserve the working HTTP/1.1/2 system. Do not fake H3 using ordinary UDP messages.

### Variables

- HTTP/2 versus HTTP/3.
- Selected RTTs, e.g. low/high, defined by realised measured RTT.
- Independent packet loss versus burst loss with comparable average rates.
- Optional capacity subset after the first factors are understood.

Use normal request/response semantics. Keep encryption enabled/comparable across protocol variants and record transport congestion-control and implementation differences that cannot be matched.

### Procedure

1. Confirm no-loss operation and actual concurrent streams.
2. Separate connection-startup tests from warm established-connection tests.
3. Replay the same source traffic schedules across protocol variants.
4. Apply the same defined loss models; log realised mean and burst characteristics.
5. Record per-hop queues and resource use to expose implementation bottlenecks.
6. Save packet/protocol traces for representative independent-loss and burst-loss runs.

Matching seeds does not guarantee identical lost application messages when protocols packetise differently. Report equivalent impairment models and realised characteristics, not falsely identical packet histories.

### Measurements and outputs

Measure critical timely completion, per-class p95/p99, sensor goodput, gaps in fresh telemetry and recovery after a loss burst. Record protocol setup duration separately.

Produce protocol-by-loss-pattern comparison plots and a table of unavoidably different implementation settings.

### Completion gate and interpretation

Done means actual protocol use is verified and important confounders are disclosed. QUIC avoids TCP's cross-stream byte-delivery dependency; it is not immune to packet loss, congestion, shared bottlenecks or application scheduling. Do not assume HTTP/3 wins in every condition.

---

## 14. E7 — Central-server scaling and load balancing

### Research question

When does more central processing capacity help, and when does the limiting resource remain elsewhere?

### Configuration and variables

Use T2_CENTRAL_POOL with identical central API workers. Start with one balancing policy and compare m = 1, 2 and optionally 4 workers.

Then, in a separate comparison, hold worker count fixed and compare supported load-balancing policies, such as round robin and least connections. Do not change both dimensions without labelling an interaction experiment.

### Resource accounting

Run or clearly distinguish:

1. Fixed aggregate compute budget split across m workers.
2. Increased aggregate compute budget as workers are added.

Record actual CPU cores/quotas, memory, process count and service workload. More asynchronous handlers do not necessarily represent independent service capacity.

### Procedure

1. Verify every worker performs equivalent work and records its ID.
2. Keep the load balancer present in all worker-count configurations.
3. Run selected loads in a server-limited condition.
4. Repeat selected points in a network-limited condition.
5. Inspect per-worker request counts, busy time and latency.
6. Separately validate a common-queue, m-worker controlled model if comparing directly with M/M/m predictions.

For HTTP/2, least connections need not equal least active application work. Record the proxy's actual balancing unit and backend connection reuse. Do not assume connection count is a direct load measurement.

### Measurements and outputs

Measure completed throughput, central wait and service times, per-worker utilisation, tail latency and balancing imbalance.

Produce throughput/delay versus worker count for both bottleneck regimes and a resources-versus-performance table.

### Completion gate and interpretation

Done means capacity added and bottleneck evidence are explicit. An arbitrary set of independently queued backends is not automatically the lecture's common-queue M/M/m model.

A central-worker failure pilot is allowed here, but the complete persistence/recovery study belongs to E12.

---

## 15. E8 — Adaptive gateway and coordinated traffic control

### Research question

Can runtime adaptation preserve critical-message timeliness and fresh telemetry while maintaining useful service for lower-priority traffic and other robots?

### Prerequisites

E4 scheduling and replacement tests, E10's edge topology, E11's per-robot/per-site metrics and stable timing. Use one working protocol for the first adaptive study; HTTP/3 is not a prerequisite.

### Policies to compare

1. FIFO reference where useful.
2. STATIC_PRIORITY.
3. FIXED_RATE: a well-tuned but frozen per-class/per-site rate limiter.
4. LOCAL_ADAPTIVE: each edge adapts from its local observations.
5. COORDINATED_ADAPTIVE: edges also use explicitly logged central congestion feedback or rate budgets.

A fixed-rate baseline is essential: otherwise adaptation may look good simply because every comparison sends too much traffic.

### Candidate mechanisms

Use a small interpretable controller, not ML. Possible observations include queue waiting time, occupancy, recent critical outcomes, achieved completions and central worker backlog.

Possible decisions include per-class forwarding-rate limits, per-site service budgets, telemetry replacement and a bounded retry budget. Define observation interval, smoothing, action limits, minimum service allocations and hysteresis/cooldown in configuration.

The controller must not read the future network-impairment schedule. It may use only information available during a real run. Log feedback traffic, decision timestamps, inputs and actions.

### E8A: Dynamic response

Replay deterministic network profiles and changing source demand. An example capacity profile is 10 → 2 → 0.5 → 5 Mbit/s, with sufficiently long stages to observe transient and settled behaviour. This is a proposed test profile, not a real-world measurement.

Also test additional robots arriving or a temporary bulk-traffic burst. Keep original generation schedules common across policies and report gateway suppression explicitly.

Measure reaction time, recovery time, mode oscillation, timely completion, freshness, sensor goodput and queues over time.

### E8B: Mechanism isolation (ablation)

Compare the complete policy with one mechanism removed at a time, for example:

- Without stale telemetry replacement.
- Without dynamic rate adjustment.
- Without per-robot/per-site minimum allocation.
- Without central feedback, leaving local adaptation.

Only run combinations relevant to the final policy; do not add inactive features just to create ablations.

### E8C: Unseen evaluation conditions

Tune thresholds on designated development profiles. Freeze policy code and parameters before final tests on different capacity patterns, loads, robot counts or burst timings.

Report each robot and the worst-served robot, not just the fleet average. When the workload exceeds feasible capacity, record unmet targets and the controller's overload behaviour rather than claiming guaranteed service.

### Measurements and outputs

Produce:

- A time trace of capacity, queue waiting, controller decisions and timely completion.
- A trade-off plot of timely critical processing versus retained useful sensor traffic.
- An ablation table.
- Per-robot fairness/service outcomes.
- An operating-region map showing which tested combinations of robot count and capacity meet the chosen requirements.

### Completion gate and interpretation

Done means policy settings were frozen before final evaluation, all baselines had reasonable tuning, filtering costs are explicit and mechanism contributions can be explained. Improvement on every metric is not required. No improvement is a valid result to analyse.

---

## 16. E9 — Shared versus per-robot gateways

### Research question

Does distributing REST gateways improve capacity or isolate interference between robot sources?

### Configurations

```text
T0_SHARED:
N ROS2 sources → one gateway → one central server

T1_DISTRIBUTED:
N ROS2 sources → N separate gateways → one central server
```

No edge servers or load balancer are introduced in the primary comparison.

### Controls

Keep source schedules, protocol, payload representation, central worker and queue discipline fixed. Match aggregate gateway CPU/memory, total buffer capacity and aggregate outstanding HTTP requests as closely as practical.

For example, four gateways each allowed K requests are not resource-equivalent to one gateway allowed only K requests. Either partition a total budget or explicitly label the larger-budget result.

Connection count and scheduling overhead may differ structurally; record them rather than hiding them. Per-source FIFO ordering must be documented in the shared case.

### Procedure

1. Verify message conservation and clock boundaries for both arrangements.
2. Run selected robot counts and loads spanning the E1 stress region.
3. Repeat with one source producing a sensor burst while the others remain unchanged.
4. Record per-robot outcomes and gateway resource use.

### Measurements and outputs

Measure per-robot p95/p99, waiting, queue occupancy, completion rates, total goodput, drops and CPU/memory.

Produce aggregate-resource-matched capacity curves and a per-robot interference comparison.

### Completion gate and interpretation

Done means original E1 results remain preserved and each run's topology is unmistakable. A speed-up produced by extra CPU or concurrency must not be presented solely as a benefit of decentralisation.

---

## 17. E10 — Direct versus edge-assisted communication

### Research question

Does local edge management justify an additional application hop, and under which network conditions?

### Variants

1. DIRECT: per-robot gateways → central service.
2. EDGE_PASS: the same robot gateways → pass-through edges → central service.
3. EDGE_AWARE: the same paths with configured edge scheduling/filtering.

Use two sites with two robots per site initially. Keep central worker count, proxy configuration and original source traffic fixed.

### Controls

Use equivalent site-to-central WAN budgets and delays across the variants. Do not accidentally give the direct case a slower route or the edge case more central CPU.

Keep packet-level impairment on the corresponding site uplink. Record protocols per hop and all queue boundaries. If aggregation/filtering changes central request rate, that is an intended measured effect, not an unreported workload change.

### Procedure

1. Verify direct and pass-through paths with no filtering.
2. Compare them at light load to measure the cost of the added stage.
3. Constrain site uplinks and increase bulk demand.
4. Enable edge-aware policies while keeping source generation unchanged.
5. Measure local and central outcomes separately.
6. Test selected equal-link and unequal-link configurations after basic controls pass.

### Measurements and outputs

Measure robot→edge acceptance, robot→central processing completion, per-stage queues, site uplink bytes, central request rate, telemetry freshness, command outcomes and useful sensor delivery.

Produce a topology trade-off table and latency/traffic plots across link capacities.

### Completion gate and interpretation

Done means a local ACK is never misrepresented as central completion, and traffic reduction is accompanied by freshness/service measurements. An edge may add delay without helping at light load; preserve that finding.

---

## 18. E11 — Unequal site loads and fairness

### Research question

Can one busy robot or site degrade service for others, and can resource allocation reduce that interference?

### Configuration and variables

Use the full two-site topology with fixed central capacity. Compare selected scenarios separately:

- Equal links and a 50/50 site-load split.
- Equal links and an 80/20 split at the same aggregate source load.
- Unequal link capacity with comparable source demand.
- A temporary sensor burst from one robot.

For the equal-total-load split experiment, decide which classes change. Prefer changing bulk demand while retaining critical rates unless the scenario explicitly changes critical load too.

Compare shared scheduling with per-robot or per-site allocations. Keep the definition of fairness explicit: equal bytes, equal fraction of demand, timely critical service, and minimum telemetry service are different objectives.

### Procedure

1. Establish balanced reference performance.
2. Introduce one asymmetry at a time.
3. Record unaffected-site and unaffected-robot outcomes.
4. Enable the selected allocation policy with a documented total service budget.
5. Check the cost to aggregate goodput and to high-demand clients.

### Measurements and outputs

Report per-robot timely completion, age, useful sensor rate, queue waits, drops and minimum service shortfalls. Include the worst robot and worst site.

If calculating a fairness index, define the input values. For unequal demand, a useful candidate is delivered/generated fraction or delivered/allocated-target fraction, not unexplained raw throughput. Do not report a “perfectly fair” system without also reporting absolute service; equal starvation is not success.

Produce per-robot plots and a summary of cross-site interference under each policy.

### Completion gate and interpretation

Done means fleet averages cannot hide neglected robots. Conclusions must state the chosen fairness objective and the performance cost of enforcing it.

---

## 19. E12 — Failure, outage and recovery

### Research question

Which failures are contained by the design, what work becomes lost or late, and how does recovery affect the system?

### Prerequisites

A stable full topology, deterministic operation IDs, explicit retry/expiry policy, and a documented state/persistence model. Freeze the chosen controller and baseline configurations before resilience comparisons.

### Separate failure cases

| Case | Action | Main boundary |
|---|---|---|
| Central-worker failure | Terminate one worker at a known time | Load-balancer detection, in-flight work and shared operation records. |
| Edge failure | Terminate one edge while the other remains active | Local clients, edge-held queues, reconnect/restart behaviour. |
| Site-uplink outage | Disconnect one site for a defined interval | Buffering, expiry, stale telemetry and recovery burst. |

Do not inject all failures simultaneously in the first study. Include a no-failure reference.

### State contract

Document where each item lives and whether it survives the injected failure:

- Latest robot state.
- Queued original messages.
- Pending mission/operation records.
- Applied-operation/deduplication records.
- Processing results and acknowledgement state.

For synthetic exactly-once-effect tests within the stated model, coordinate the operation effect and its deduplication/result record appropriately. In-memory records local to a killed worker cannot establish cross-worker duplicate suppression after failure.

A shared persistent store may be introduced for this experiment if justified, but record that it creates another dependency/failure point. Do not claim universal exactly-once processing or complete high availability. The single load balancer remains a single failure point unless explicitly addressed and tested.

### Procedure

1. Define the failure time, outage duration, detection mechanism and recovery policy in the scenario.
2. Run a stable pre-failure interval.
3. Inject one failure and log the actual time it occurs.
4. Keep sources generating according to the same schedule.
5. Restore the component/link where specified.
6. Observe recovery until the documented criterion or timeout.
7. Examine in-flight IDs, duplicates, expired operations and backlog disposition.

Do not assume clients can automatically switch to the other edge unless reassignment is implemented. A legitimate result is that the unaffected site continues while the failed site's clients wait for restart.

### Measurements and outputs

Define recovery in advance, for example sustained return to a chosen fraction of pre-failure completion rate or restoration of a specified deadline-success target. Report failure detection, interruption, time to that criterion, backlog clearance, late/failed operations, duplicate effects and stale data replay.

Produce a timeline around failure/restoration, per-site outcomes and a state-loss/retention table.

### Completion gate and interpretation

Done means actual recovery is measured and claims match the tested failure scope. Distinguish fast local acceptance, restored connectivity and completed recovery of central processing. Expired commands should not be replayed simply to empty a queue.

---

## 20. Implementation architecture and interfaces

These are design contracts, not claims that the repository already contains these modules. Adapt naming to an existing coherent codebase.

### 20.1 Suggested component boundaries

| Component | Responsibility |
|---|---|
| Traffic source | Generate reproducible per-robot ROS2 messages independently of responses. |
| Robot gateway | Translate messages, identify them, manage local queueing and send REST requests. |
| Queue/scheduling policy | Admission, ordering, expiry, replacement and service allocation. |
| HTTP transport adapter | Connection reuse, concurrency, protocol verification and request attempts. |
| Application reliability layer | IDs, ACK/NACK interpretation, retry timers and terminal outcomes. |
| Edge service | Local receipt/state, upstream queueing, forwarding and optional adaptive decisions. |
| Central worker | Validate and process requests; record operation outcome and timing. |
| Central feedback component | Expose documented congestion observations/budgets for E8. |
| Network harness | Isolated topology, impairment verification and cleanup. |
| Experiment runner | Lifecycle, configuration, seeds, capture, metadata and result preservation. |
| Analysis package | Validate accounting and regenerate metrics/figures. |

Separate policy logic from ROS2 and HTTP plumbing so queue order, duplicate handling and adaptive decisions can be tested without a live network.

### 20.2 REST resource design

Keep stable resource semantics across protocol variants. Possible routes include robot telemetry/event collections, operation resources, operation results and recent robot state. Record actual API definitions in an API document.

Use explicit request/operation IDs. Do not equate resource storage with forbidden session state: a service can expose resources while requests carry the context necessary to interpret them.

For E5/E12, distinguish accepted from processed responses. State how clients discover a pending operation's final result. Use authentication/TLS consistently where part of the experiment, but do not expose test control endpoints on an untrusted public interface.

### 20.3 Configuration example

The following is an illustrative schema to implement or map onto the repository's existing configuration. It is not a verified runner invocation and does not prescribe software-library choices.

```yaml
experiment:
  id: E01
  subexperiment: clean_load_sweep
  topology: T0_SHARED
  run_id: generated_by_runner
  repetition: 1
  seed: 1001
  warmup_s: 10
  measurement_s: 60
  drain_timeout_s: 30

resources:
  robot_count: 2
  site_count: 1
  gateway_mode: shared
  edge_count: 0
  central_workers: 1
  central_load_balancer: false
  operator_client_enabled: false
  # Specify verified CPU/memory allocations in the resolved configuration.

traffic:
  generation_mode: periodic
  independent_of_http_responses: true
  phase_mode: staggered
  payload_size_definition: useful_application_bytes
  classes:
    command:
      semantic_role: uplink_synthetic_critical_event
      rate_hz: 2
      payload_bytes: 100
      deadline_ms: 100
    status:
      rate_hz: 5
      payload_bytes: 100
      deadline_ms: 500
    odometry:
      rate_hz: 20
      payload_bytes: 250
    sensor:
      rate_hz: 5
      payload_bytes: 20000

transport:
  tested_hop: gateway_to_central
  requested_http_version: "1.1"
  persistent_connections: true
  total_max_in_flight: 1
  library_automatic_retries: false
  tls_enabled: true
  # Log negotiated versions and actual connection counts, not only requests.

queueing:
  policy: fifo
  capacity_messages: 1000
  latest_value_replacement: false
  expiry_policy: record_lateness_only
  # Later experiments may reject expired operations; record the change.

network:
  profile: clean
  added_delay_ms_per_direction: 0
  independent_loss_percent: 0
  bandwidth_mbit_s: null
  # Null means no configured shaping limit, not infinite physical capacity.

application_faults:
  enabled: false

application_reliability:
  policy: no_app_retry

measurement:
  success_boundary: central_processed
  clock_mode: verified_shared_monotonic
  capture_enabled: true
  capture_point: dedicated_gateway_egress
```

Before a real run, validate the host capabilities, clock domain, TLS setup, selected network interfaces and actual source rates. A 1,000-message queue is an illustrative finite capacity, not a recommended universal value.

### 20.4 Suggested repository layout

```text
project/
  PROJECT_CONTEXT.md
  EXPERIMENTS.md
  ros2_ws/src/
    traffic_generator/
    rest_gateway/
  services/
    edge/
    central/
    load_balancer/
  policies/
    scheduling/
    reliability/
    adaptive/
  network/
    topology/
    impairment/
  experiments/
    configs/
    scenarios/
    runner/
  analysis/
  tests/
  docs/
  results/                  # avoid committing large captures by accident
  README.md
```

Use the existing project structure if it already provides equivalent boundaries. Do not move working files solely to match this sketch.

---

## 21. Verification gates and minimum tests

| Gate | Evidence required |
|---|---|
| G0: Repository understood | Actual components, missing dependencies and implemented topology documented. |
| G1: Message path works | Unique messages traced from ROS2 source to central processing in a clean smoke test. |
| G2: Measurements trustworthy | Clock checks, source-rate checks, lifecycle accounting and reproducible raw outputs. |
| G3: Baseline established | E1 dataset and selected stress configurations, with no fabricated saturation claim. |
| G4: Architecture comparison valid | E9 resource/concurrency budgets recorded; topology choice justified. |
| G5: Core mechanisms tested | E2–E5 assumptions, queue policies, protocol versions and reliability tests verified. |
| G6: Expanded topology stable | E7/E10 launch reproducibly; each hop and state boundary identified. |
| G7: Final policy frozen | E11 fairness observations incorporated; E8 settings fixed before held-out evaluation. |
| G8: Evidence packaged | Final raw data, plots, configs, code version, limitations and E12 failure records preserved. |

Minimum automated checks should cover:

- Stable IDs and sequences across retries.
- Correct source rates and detectable source scheduling lag.
- FIFO/priority ordering, tie handling and bounded capacity.
- Expiry and latest-value replacement accounting.
- Time-average queue occupancy on a small hand-checkable trace.
- Duplicate operation without duplicate effect under the tested state model.
- Lost response after processing, delayed ACK, corrupt payload and retry exhaustion.
- Correct stream/window behaviour for the implemented reliability scheme.
- Protocol negotiation evidence and persistent-connection reuse.
- Impairment application and cleanup after interruption.
- Controller hysteresis/limits and no access to future network profiles.
- Conservation of admitted/completed/discarded/outstanding messages.
- Analysis handling of missing samples, zero arrivals, failures and unresolved work.

A test that cannot run in the current environment must be marked unexecuted with its requirements, not reported as passing.

---

## 22. Report and experiment deliverables

For each completed experiment, create a short `experiment_report.md` containing:

1. Research question and pre-run hypothesis.
2. Actual topology and tested connection/path.
3. Independent variable(s), fixed controls and resource budgets.
4. Config IDs, repetitions, run durations and exclusion reasons.
5. Primary metrics with explicit definitions and sample counts.
6. Results with figures/tables generated from raw data.
7. Interpretation linked to the relevant lecture concepts.
8. Confounders, limitations, negative findings and unresolved questions.
9. The design decision motivated by the evidence.
10. Reproduction commands that have actually been verified.

Distinguish measured findings from hypotheses and theoretical explanations. “No clear improvement under these conditions” is an acceptable result.

### 22.1 Suggested main-report narrative

- Problem and communication requirements.
- Baseline architecture, instrumentation and bottleneck diagnosis.
- Queueing/protocol/reliability studies.
- Distributed gateways, edge processing and central scaling.
- Deadline/freshness/fairness-aware adaptive design.
- Controlled evaluation, mechanism isolation and recovery.
- Trade-offs, operating limits, reproducibility and future work.

Do not devote equal space to every parameter sweep. E1 establishes the problem; E4/E5 establish useful mechanisms and semantic correctness; E8 is the central design result. Other studies support and bound that result.

---

## 23. Course-material traceability

These are source-navigation pointers to the supplied course documents, not a claim that the course mandates this exact experiment list.

| Supplied material | Relevant material | Use in this project |
|---|---|---|
| `ELEN90061-LDI-Project_Manual.pdf`, pp. 1–2 | Learn/design/implement/present; alternative architectures; measured comparisons and justification | Overall methodology and emphasis on communications rather than robotics complexity. |
| `ELEN90061-LDI-Project_Manual.pdf`, pp. 4–5 | Deliverable guidelines and report structure | Reproducible code, clear reporting, theory/design/results/discussion. |
| `ELEN90061-LDI-REST_Project.pdf`, pp. 1–2 | RESTful client–server M2M setting; application-layer focus; packet analysis | REST testbed and Python/Wireshark measurement split. |
| `ELEN90061-LDI-REST_Project.pdf`, p. 4 | Multi-client/multi-server examples; application-level ACK/NACK reliability under intentional faults | Expanded topology and E5's controlled reliability abstraction. |
| `Mod_6-L1-Application.pdf`, pp. 13–16 | Persistence, pipelining and HTTP head-of-line discussion | E3 baseline and connection-management analysis. |
| `Mod_6-L1-Application.pdf`, pp. 17–28 | HTTP/2 streams, packet-analysis example, HTTP/3/QUIC | E3 and E6. Check primary protocol documentation before making detailed implementation claims. |
| `Mod_5-L1-Transport.pdf`, pp. 5–7 | Application transport requirements and transport services | Per-class timing, reliability and throughput requirements. |
| `Mod_5-L1-Transport.pdf`, pp. 26–46 and 53–56 | Error recovery, acknowledgements, timeouts, pipelining and Selective Repeat | E5; identify the implemented application abstraction accurately. |
| `Queuing_Theory-1.pdf`, pp. 15–24 | Arrival/service patterns, capacity, disciplines, service channels and stages | E1/E2/E4/E7/E10. |
| `Queuing_Theory-1.pdf`, pp. 28–37 | Little's Law and utilisation | E2 with consistent measurement boundaries. |
| `Queuing_Theory-1.pdf`, pp. 81–90 | M/M/1 analysis | Controlled E2 calibration, not an automatic model of all ROS traffic. |
| `Queuing_Theory-1.pdf`, pp. 99–105 | M/M/m analysis | Controlled parallel-worker comparison in E7. |
| `Mod_4-Network.pdf`, pp. 6–13 | Network-layer operation, forwarding/routing and service properties | Site-network setup and distinction between IP paths and application decisions. |

Information freshness/Age of Information, coordinated adaptation and detailed service-fairness policies are project extensions. Use appropriate primary references if presenting them as established research concepts in the final report. Do not treat earlier informal planning statements as authoritative protocol specifications.

---

## 24. First response expected from Codex

After reading this document and inspecting the repository, respond with:

1. A concise inventory of the actual implementation and topology.
2. Which experiment prerequisites are already satisfied, supported by files/tests/results.
3. Any conflict between the existing implementation and this specification.
4. The next experiment or prerequisite to implement, with the smallest coherent change.
5. The validation plan and exact files expected to change.

Do not start advanced implementation merely because it appears later in this document. Ask for genuinely missing choices only after checking the repository, and preserve a working baseline throughout.

**Success criterion for the project:** a reproducible explanation of which communication designs meet the chosen requirements, under which measured conditions, and at what cost—not a large collection of features or a predetermined winning protocol.
