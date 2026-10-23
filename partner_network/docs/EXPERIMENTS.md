# Experiments supported by this starter

These are proposed studies, not completed results. Start with one factor and
retain all original messages, failures and per-robot denominators.

## E7: central service capacity

Compare `server_limited_1.json`, `_2.json` and `_4.json`. The proxy stays present
and keeps request-level round robin; only worker count changes. The 20 ms hold
is deliberately controlled service time. Describe this as additional service
capacity, not a measured CPU-scaling result. Separate workers have separate
queues, so an M/M/m common-queue formula is not automatically applicable.

Run a no-capacity-limit set, then a separately labelled site-capacity-limited
set. Measure per-worker processing, throughput, queue wait, per-class p95/p99,
drops and per-robot service. Explain when extra workers cannot overcome a link
limit or a robot's single-outstanding-request constraint. For a CPU-scaling
study, add a defined CPU workload and record/hold total CPU quotas explicitly.

## E10 control: direct versus pass-through edge

Compare `central_pool_2.json` with `full_reference.json`: same two workers,
proxy, traffic, routing and impairments. The extra application edge is the
factor. Test clean links first, then the same site-A impairment in both variants.
This supplies the pass-through control. A later traffic-aware edge requires an
implemented policy and its own correctness tests; its benefit cannot be assumed.

## E11 development: unequal site conditions

Compare the full reference with `site_a_capacity.json` or `site_a_delay.json`,
one at a time. Show each robot and class, then the site aggregates. Jain's
completion-rate index is recorded as a descriptive metric; equal starvation is
not successful service, so also report useful goodput, losses and timely counts.

## Packet loss and temporary outage

`site_a_loss.json` applies packet loss, with normal TCP recovery still enabled.
`site_a_outage.json` changes profiles at measurement-relative times. Logs retain
the scheduled and actual change times. No application retry or persistence is
implemented: a timeout is not proof that the central worker did nothing.
Use worker IDs and processing records to identify processed-but-unacknowledged
messages before designing E5 retry/deduplication or E12 recovery mechanisms.

## Cubic versus Reno

`tcp_cubic.json` and `tcp_reno.json` change only the robot sender socket's TCP
congestion algorithm. Both use T2, so these sockets cross the site uplinks.
Proxy-to-worker TCP remains Cubic. The requested and actual algorithms are
logged per socket. Available choices depend on the kernel; the kit fails
clearly for an unavailable algorithm instead of silently changing it. See the
[Linux TCP control documentation](https://docs.kernel.org/networking/ip-sysctl.html).

Use captures to inspect handshake/reuse, retransmission and flight-size trends.
These small, serial HTTP requests can be application- or RTT-limited: equal
performance does not establish that congestion algorithms are equivalent.
For a transport-focused study, define longer bulk transfers and a controlled
bandwidth-delay setting, then hold all of those changes fixed across algorithms.
Do not infer the configured algorithm from a Wireshark trace alone; retain the
socket log. No claim about BBR is made unless it is available and actually run.

## Extensions to keep separate

HTTP/2 or HTTP/3 needs real protocol-capable clients and servers and per-hop
negotiation evidence. Priority scheduling requires a defined queue and fairness
contract. Retries require stable IDs, bounded attempts, explicit ACK semantics
and duplicate-effect protection. Adaptation requires frozen controllers and
separate development/evaluation profiles. Each is a new implementation and
experimental comparison, not a label to apply to this baseline.
