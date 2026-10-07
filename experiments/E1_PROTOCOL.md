# E1 protocol, fixed before the completion sweep

Question: how does a FIFO, persistent HTTP/1.1 ROS2-to-REST gateway respond to
increasing offered traffic on a clean network?

Independent variable: 1, 2, 4, 8, 16, 32 and 64 robot-equivalent loads. One
namespaced ROS2 robot is used at load 1, and two at every other load. For N >= 2,
each robot's four publishing rates are multiplied by N/2. Payload sizes and class
proportions remain constant. This uses the workbook's permitted equivalent-rate
approach, rather than claiming N independent ROS nodes or N HTTP clients.

Each ROS robot has its own node and namespace, and separate command, status,
odometry and sensor topics. One scheduler process hosts the ROS source nodes. One
gateway subscribes to all topics and uses one bounded FIFO, one sender and one
persistent HTTP/1.1 connection to one server. Only one request is outstanding.

Fixed conditions: queue capacity 1,000 waiting messages, drop-tail overflow,
cleartext HTTP/1.1, server TCP_NODELAY, no compression, no application retries,
no injected faults, clean loopback. Qdisc and ping evidence are saved automatically.
No impairment settings are changed by E1, so there is no shaping state to reset.

Three seeded repetitions of every load, shuffled order, five-second warm-up,
60-second measurement and up to 15-second drain. A separate two-robot stability
test uses a 300-second measurement and a full packet capture. Live `ros2 topic hz`
checks run during that stability test only, not during the formal load sweep.

Deadline targets, chosen before the completion sweep:

| Class | Target | Rationale |
|---|---:|---|
| Command | 100 ms | Illustrative latency-critical control target |
| Status | 200 ms | One nominal heartbeat period |
| Odometry | 100 ms | Two nominal sample periods |
| Sensor | 1,000 ms | Looser bulk-data target |

These are project design targets, not safety guarantees or lecture-prescribed
requirements. They stay fixed as source rates increase. Response completion is
the deadline event. Every generated message in the measurement window is in the
denominator; late successful responses, drops, missing messages and failed or
unfinished requests all count as deadline failures. Successful-only tardiness is
also saved separately so loss is not disguised as latency.

Latency percentiles use successful messages generated inside the measurement
window, including responses during the drain. Throughput and goodput count
completions inside the window. FIFO occupancy is integrated over state changes.

Linux process CPU counters are sampled every 0.2 seconds and interpolated at the
measurement boundaries. 100% means one logical CPU; a multithreaded process can
exceed 100%. Server handler busy fraction covers entry to `do_POST` through the
response write, including body-read waiting. It excludes idle time waiting for
the next request and HTTP header parsing. Gateway sender busy fraction covers
dequeue through response completion, including serialization and HTTP waiting.
These different quantities must not be interpreted as interchangeable utilisation.

The earlier single-topic dataset in `results/baseline_formal` is preliminary.
The namespaced rate-scaled source and new instrumentation alter the workload
implementation. Do not pool the two datasets or attribute their difference to
a particular optimisation. Use only `results/e1_complete` for the final E1 report.

Scope boundary: this completes the clean E1 experiment and its relevant traffic,
logging and stability checks. DELAY/LOSS/BW profiles, queueing-theory validation,
HTTP/2, priority, ARQ and multi-server comparisons remain separate work.
