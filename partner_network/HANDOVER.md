# Partner handover

You own a separate testbed for the more complex topology. James's E1/E2 work uses
one shared gateway; this kit gives each robot its own gateway and connection.
Keep the datasets and conclusions separate unless you design a matched comparison.

## First deliverable

1. Inspect `example_run/` and `VALIDATION.md`, then run `doctor`, the unit tests and the short full-reference smoke command in
   README. Read `summary.json` and require `validation.json` to pass.
2. Check that four robot processes each report one ROS source node and one
   gateway node. Check both worker IDs occur in `worker.jsonl` records.
3. Open both WAN captures. Find a POST, its matching central-worker ACK and the
   reused TCP connection. Confirm that the source ID survives every application hop.
4. Run `central_pool_2.json` and `full_reference.json` with the same workload,
   durations and seed. This is an initial direct-versus-pass-through-edge check.
5. Agree with James which one experimental question you will own before adding
   new scheduling, retry or protocol code.

## Useful division of work

E7 (central pool capacity) and the T2/T3 control for E10 are the most immediate
uses. The per-robot and per-site outputs also support developing E11 fairness
measurements. The site impairment presets and packet captures provide a base
for later reliability and protocol studies; those mechanisms are not yet built.

For E9, implement a matched shared-gateway configuration on this same network
before comparing it with the independent clients. Reusing historical loopback
numbers as the other side would change both topology and network environment.

## Where to edit

| File | Responsibility |
|---|---|
| `configs/*.json` | Workload, topology, worker count, TCP settings and impairments |
| `lab/components.py` | ROS source/gateway, worker, proxy and pass-through edge logic |
| `lab/common.py` | Message validation, TCP sockets, exact FIFO events and logging |
| `lab/network.py` | Owned namespaces, routing, netem, capture and cleanup |
| `lab/runner.py` | Startup, readiness, common start time, dynamic profile events and shutdown |
| `lab/analyze.py` | Independent ID, timestamp, queue and outcome reconciliation |
| `tests/` | Focused regressions for accounting, forwarding and ownership |

Add new mechanisms behind explicit configuration values. Preserve FIFO and
NO_APP_RETRY as controls. Do not rename pass-through edges as traffic-aware
edges, concurrent requests as Selective Repeat, or ordinary TCP as HTTP/2.

## Return these files with each result

Keep the entire run directory: resolved config, start times, code snapshot,
environment/readiness metadata, raw JSONL, captures, summaries and validation.
Send a small table naming the question, factors changed, controls, repetitions,
actual clients/workers and the metric endpoint. Report failed attempts and their
reasons. A functional smoke run is not a repeated performance experiment.

Use at least several independent repetitions with shuffled order for final
comparisons; freeze rates, payloads, holds, limits and durations beforehand.
Treat one run as one repetition. Show per-run statistics and the declared
variation measure, not a pooled percentile disguised as repeated evidence.

## Current limitations

- One physical host shares CPU, memory and kernel across all logical hosts.
- The server hold is controlled wall time, not CPU-bound computation. Worker
  additions add service slots; they do not impose a fixed aggregate CPU budget.
- Edges forward synchronously and retain no persistent store. An upstream
  timeout can leave processed-but-unacknowledged work; no retry resolves it.
- No authentication, TLS or external exposure is configured. Use this isolated
  synthetic laboratory, not a production robot deployment.
- Multi-computer operation needs address/routing deployment and clock handling
  changes. The delivered driver targets a single Linux/WSL2 machine.
