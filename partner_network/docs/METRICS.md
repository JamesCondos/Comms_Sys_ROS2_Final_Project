# Measurement definitions

`start.json` defines one monotonic start, warm-up end, measurement end and drain
deadline. All components verify a common boot/time-namespace clock identity.
UTC identifies runs; it is not used for latency subtraction. This clock check
must be redesigned before using timestamps from different physical computers.

| Quantity | Definition |
|---|---|
| Actual robot clients | Independent robot processes; each contains one source node and one gateway node |
| Offered rate | Original messages with actual source timestamp inside the measurement window, divided by its length |
| Source-to-response latency | Complete response-body read minus source timestamp, successful source-time cohort including drain |
| Gateway waiting | FIFO dequeue minus admission |
| Gateway system residence | Locked terminal completion minus admission, including response handling |
| Worker waiting | Service start minus worker FIFO admission |
| Worker service | Processed timestamp minus worker service start |
| Completed rate | Successful response reads inside the measurement window divided by its length |
| Payload goodput | Useful payload bits from those successful in-window responses divided by seconds; excludes headers/metadata |
| Deadline misses | All generated class messages minus successful responses meeting the configured target; losses count as misses |
| Mean queue occupancy | Exact event-time integral of waiting jobs divided by window length |
| Mean in service | Event-time average concurrent service population; gateway maximum is one |
| Scheduling lag | Actual source timestamp minus scheduled publication timestamp |

The gateway's response-body timestamp precedes its locked terminal timestamp.
Use the latter for exact queue-population accounting. Per-message residence
clipped to the window must equal the event-state integral, including carry-in
and drain boundaries. A finite-window comparison of L and lambda*W needs those
boundary terms; the kit checks the exact interval identity directly.

`per_robot_class.csv` preserves successful sample counts with mean/p95/p99.
Percentiles describe successes; read the outcome counts beside them. Missing
ROS messages, full local FIFOs, worker503 responses, HTTP failures, timeouts and
drain timeouts are distinct. `central_processed_unacknowledged_all` includes
warm-up and drain; it is a different population from measurement-cohort counts.

Capture files describe the two WAN observation points. Captured bytes include
protocol overhead and retransmissions, unlike useful payload goodput. Kernel
capture drops are packet-recorder loss, not the network's configured loss rate.
The starter checks capture completion, PCAP record framing/counts and recorder
drops; it does not reconstruct every TCP byte stream. Application queue waiting
cannot be read from Wireshark. Inspect the recorded offload status before using
packet sizes or packet counts: virtual TSO/GSO/GRO aggregation can produce
captures with packets larger than MTU. Such records are not physical wire frames.

Raw process CPU/RSS samples are in `cpu.json`; CPU affinity and the absence of
imposed CPU quotas are recorded in `environment.json`. Service holds do not
represent CPU utilisation. Keep resource budgets explicit in worker-count work.
